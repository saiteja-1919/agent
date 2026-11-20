"""Sarvam Bulbul v3 text to speech over the streaming WebSocket.

A hand-written adapter (the agreed layer 7). Facts used here:

* ``wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true``;
* first a ``config`` message, then ``text`` messages, then ``flush``;
* answers arrive as ``{"type":"audio","data":{"audio":"<base64>"}}``,
  ``{"type":"event","data":{"event_type":"final"}}`` and ``{"type":"error"}``;
* ``min_buffer_size`` must be at least 30;
* Bulbul v3 always preprocesses the text, ignores pitch/loudness, and reads
  digits as Telugu words - which is why numbers must arrive as English words;
* 8 kHz ``linear16`` matches the phone line, so nothing has to be resampled.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any, Callable, List, Optional

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    StartFrame,
    TextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

TTS_URL = "wss://api.sarvam.ai/text-to-speech/ws"

#: websockets>=14 renamed the header argument; try both.
_HEADER_KWARGS = ("additional_headers", "extra_headers")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+")

Connector = Callable[..., Any]


def split_for_speech(text: str, max_len: int = 150) -> List[str]:
    """Cut a sentence into pieces Sarvam can speak, never mid-word.

    Punctuation and short sentences are the only pause controls Bulbul v3 has,
    so commas are kept inside a piece whenever possible.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    pieces: List[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(cleaned) or [cleaned]:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_len:
            pieces.append(sentence)
            continue
        current = ""
        for part in re.split(r"(?<=[,;:])\s+", sentence):
            candidate = f"{current} {part}".strip()
            if len(candidate) <= max_len:
                current = candidate
                continue
            if current:
                pieces.append(current)
            while len(part) > max_len:
                cut = part.rfind(" ", 0, max_len) or max_len
                pieces.append(part[:cut].strip())
                part = part[cut:].strip()
            current = part
        if current:
            pieces.append(current)
    return [piece for piece in pieces if piece]


class SarvamTTS(FrameProcessor):
    """Streaming Telugu voice. One socket for the whole call."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "bulbul:v3",
        language_code: str = "te-IN",
        speaker: str = "shubh",
        sample_rate: int = 8000,
        pace: float = 1.15,
        temperature: float = 0.6,
        min_buffer_size: int = 30,
        max_chunk_length: int = 150,
        audio_codec: str = "linear16",
        audio_bitrate: str = "128k",
        connect_timeout_secs: float = 10.0,
        flush_timeout_secs: float = 10.0,
        connector: Optional[Connector] = None,
        on_utterance_end: Optional[Callable[[], None]] = None,
        on_audio: Optional[Callable[[bytes], None]] = None,
        emit_frames: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        self.model = model
        self.language_code = language_code
        self.speaker = speaker
        self.sample_rate = sample_rate
        self.pace = pace
        self.temperature = temperature
        self.min_buffer_size = max(30, int(min_buffer_size))
        self.max_chunk_length = max(40, int(max_chunk_length))
        self.audio_codec = audio_codec
        self.audio_bitrate = audio_bitrate
        self.connect_timeout_secs = connect_timeout_secs
        self.flush_timeout_secs = flush_timeout_secs
        self._connector = connector
        self._on_utterance_end = on_utterance_end
        self._on_audio = on_audio
        self.emit_frames = emit_frames

        self.connection: Any = None
        self.connected = False
        self.config_sent = False
        self.utterances = 0
        self.failures = 0
        self.audio_bytes = 0
        self._pending_text = ""
        self._speaking = False
        self._flushed = False
        self._stopping = False
        self._task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._collector: Optional[bytearray] = None
        self._final_event: Optional[asyncio.Event] = None
        self._flush_timer: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------- pipeline
    async def process_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self._start_receiver()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self.stop()
        elif isinstance(frame, TTSSpeakFrame):
            await self._speak_whole(getattr(frame, "text", "") or "")
        elif isinstance(frame, (TextFrame, LLMTextFrame)):
            await self._feed_text(getattr(frame, "text", "") or "")
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._end_utterance()
        elif isinstance(frame, InterruptionFrame):
            self._reset_after_interruption()
        await self.push_frame(frame, direction)

    # -------------------------------------------------------------- connection
    def url(self) -> str:
        return f"{TTS_URL}?model={self.model}&send_completion_event=true"

    def config_message(self) -> dict:
        return {
            "type": "config",
            "data": {
                "language_code": self.language_code,
                "speaker": self.speaker,
                "speech_sample_rate": str(self.sample_rate),
                "output_audio_codec": self.audio_codec,
                "output_audio_bitrate": self.audio_bitrate,
                "pace": self.pace,
                "temperature": self.temperature,
                "enable_preprocessing": True,
                "min_buffer_size": self.min_buffer_size,
                "max_chunk_length": self.max_chunk_length,
            },
        }

    def minimal_config_message(self) -> dict:
        """Fallback config, in case Sarvam rejects an optional field."""
        return {
            "type": "config",
            "data": {
                "language_code": self.language_code,
                "speaker": self.speaker,
                "speech_sample_rate": str(self.sample_rate),
                "output_audio_codec": self.audio_codec,
                "min_buffer_size": self.min_buffer_size,
                "max_chunk_length": self.max_chunk_length,
            },
        }

    def _connector_callable(self) -> Connector:
        if self._connector is not None:
            return self._connector
        import websockets

        signature = getattr(getattr(websockets, "connect", None), "__doc__", "") or ""
        headers = {"api-subscription-key": self.api_key}
        if "additional_headers" in signature:
            return lambda url, **kw: websockets.connect(url, additional_headers=headers, **kw)
        if "extra_headers" in signature:
            return lambda url, **kw: websockets.connect(url, extra_headers=headers, **kw)
        return lambda url, **kw: websockets.connect(url, additional_headers=headers, **kw)

    def _start_receiver(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._flush_timer, self._receive_task, self._task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None
        self._receive_task = None
        connection, self.connection = self.connection, None
        self.connected = False
        self.config_sent = False
        if connection is not None:
            try:
                await connection.close()
            except Exception as exc:  # pragma: no cover - shutdown stays quiet
                logger.debug("TTS socket close failed: {}", exc)

    async def _run(self) -> None:
        try:
            await self._connect()
            if self._receive_task is None or self._receive_task.done():
                self._receive_task = asyncio.get_event_loop().create_task(self._receive_loop())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.connected = False
            self.failures += 1
            logger.warning("TTS could not connect ({}); the call continues", exc)

    async def _connect(self) -> None:
        connector = self._connector_callable()
        self.connection = await asyncio.wait_for(
            connector(self.url(), ping_interval=10.0), timeout=self.connect_timeout_secs
        )
        self.connected = True
        await self._send_config()
        logger.info("TTS connected ({}, {}, pace {})", self.model, self.speaker, self.pace)

    async def _send_config(self) -> None:
        sent = await self._send(self.config_message())
        self.config_sent = sent
        self._config_fallback_used = False

    def stats(self) -> dict:
        return {
            "connected": self.connected,
            "utterances": self.utterances,
            "audio_bytes": self.audio_bytes,
            "failures": self.failures,
        }

    # ------------------------------------------------------------------- text
    async def _speak_whole(self, text: str) -> None:
        """A scripted line (the opener): speak it exactly, no model involved."""
        lines = split_for_speech(text, self.max_chunk_length)
        if not lines:
            return
        await self._ensure_connected()
        await self._begin_utterance()
        for line in lines:
            await self._send_text(line)
        await self._flush()

    async def _feed_text(self, text: str) -> None:
        if not text:
            return
        await self._ensure_connected()
        self._pending_text += text
        if not self._speaking:
            await self._begin_utterance()
        ready = self._take_ready_text()
        for piece in ready:
            await self._send_text(piece)

    def _take_ready_text(self) -> List[str]:
        """Send complete sentences (or long enough pieces) as soon as possible."""
        pieces: List[str] = []
        while True:
            match = re.search(r"[.!?।]\s", self._pending_text)
            if match:
                pieces.append(self._pending_text[: match.end()].strip())
                self._pending_text = self._pending_text[match.end():]
                continue
            if len(self._pending_text) >= self.max_chunk_length:
                cut = self._pending_text.rfind(" ", 0, self.max_chunk_length)
                cut = cut if cut > 0 else self.max_chunk_length
                pieces.append(self._pending_text[:cut].strip())
                self._pending_text = self._pending_text[cut:]
                continue
            break
        return [piece for piece in pieces if piece]

    async def _end_utterance(self) -> None:
        remainder = self._pending_text.strip()
        self._pending_text = ""
        if remainder:
            await self._send_text(remainder)
        if self._speaking:
            await self._flush()

    async def _begin_utterance(self) -> None:
        self.utterances += 1
        self._speaking = True
        self._flushed = False
        if self.emit_frames:
            try:
                await self.push_frame(TTSStartedFrame())
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("could not announce speech start: {}", exc)

    async def _flush(self) -> None:
        self._flushed = True
        if not await self._send({"type": "flush"}):
            await self._finish_utterance()
            return
        self._start_flush_watchdog()

    def _start_flush_watchdog(self) -> None:
        """If Sarvam never sends its final event, end the utterance anyway."""
        task = self._flush_timer
        if task is not None and not task.done():
            return
        try:
            self._flush_timer = asyncio.get_event_loop().create_task(self._flush_watchdog())
        except RuntimeError:  # pragma: no cover - no running loop
            self._flush_timer = None

    async def _flush_watchdog(self) -> None:
        try:
            await asyncio.sleep(self.flush_timeout_secs)
        except asyncio.CancelledError:
            return
        if self._speaking:
            logger.debug("TTS never reported the final event; ending the utterance anyway")
            await self._finish_utterance()

    async def _finish_utterance(self) -> None:
        timer, self._flush_timer = self._flush_timer, None
        if timer is not None and not timer.done():
            timer.cancel()
        if self._final_event is not None and not self._final_event.is_set():
            self._final_event.set()
        if not self._speaking:
            return
        self._speaking = False
        self._flushed = False
        if self.emit_frames:
            try:
                await self.push_frame(TTSStoppedFrame())
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("could not announce speech end: {}", exc)
        if self._on_utterance_end is not None:
            try:
                self._on_utterance_end()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("utterance end callback failed: {}", exc)

    def _reset_after_interruption(self) -> None:
        self._pending_text = ""
        if self._speaking:
            asyncio.get_event_loop().create_task(self._finish_utterance())

    # ------------------------------------------------------------------ socket
    async def _ensure_connected(self) -> bool:
        if self.connected and self.connection is not None:
            return True
        try:
            await self._connect()
            return True
        except Exception as exc:
            self.failures += 1
            logger.warning("TTS is not connected ({}); this reply will be silent", exc)
            self._push_error(f"Sarvam TTS is not connected: {exc}")
            return False

    async def _send(self, payload: dict) -> bool:
        connection = self.connection
        if connection is None:
            return False
        try:
            await connection.send(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:
            self.connected = False
            logger.warning("TTS send failed: {}", exc)
            self.failures += 1
            self._push_error(f"Sarvam TTS send failed: {exc}")
            return False

    async def _send_text(self, text: str) -> None:
        if not text:
            return
        if not await self._send({"type": "text", "data": {"text": text}}):
            await self._finish_utterance()

    async def _receive_loop(self) -> None:
        try:
            async for message in self.connection:
                try:
                    await self._handle_message(message)
                except Exception as exc:
                    logger.debug("TTS ignored a message: {}", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.connected = False
            logger.debug("TTS receive loop ended: {}", exc)
            await self._finish_utterance()

    async def _handle_message(self, message: Any) -> None:
        if isinstance(message, (bytes, bytearray)):
            await self._push_audio(bytes(message))
            return
        try:
            payload = json.loads(message)
        except (ValueError, TypeError):
            logger.debug("TTS sent something that is not JSON: {!r}", str(message)[:120])
            return
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("type") or "")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if kind == "audio":
            encoded = data.get("audio") or ""
            if not encoded:
                return
            try:
                audio = base64.b64decode(encoded)
            except Exception as exc:
                logger.debug("TTS audio could not be decoded: {}", exc)
                return
            await self._push_audio(audio)
        elif kind == "event":
            event_type = str(data.get("event_type") or "")
            if event_type == "final":
                await self._finish_utterance()
        elif kind == "error":
            message_text = str(data.get("message") or payload)
            logger.warning("TTS reported an error: {}", message_text)
            if not self.config_sent or "config" in message_text.lower():
                await self._retry_with_minimal_config()
            self._push_error(f"Sarvam TTS error: {message_text}")
            await self._finish_utterance()

    async def _retry_with_minimal_config(self) -> None:
        """If Sarvam refuses an optional config field, go on with the basics."""
        if getattr(self, "_config_fallback_used", False):
            return
        self._config_fallback_used = True
        logger.warning("retrying the TTS configuration with only the required fields")
        if await self._send(self.minimal_config_message()):
            self.config_sent = True

    async def _push_audio(self, audio: bytes) -> None:
        if not audio:
            return
        self.audio_bytes += len(audio)
        if self._collector is not None:
            self._collector.extend(audio)
        if self._on_audio is not None:
            try:
                self._on_audio(audio)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("audio callback failed: {}", exc)
        if not self.emit_frames:
            return
        frame = self._audio_frame(audio)
        try:
            await self.push_frame(frame)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("could not push TTS audio: {}", exc)

    def _audio_frame(self, audio: bytes) -> Frame:
        try:
            return TTSAudioRawFrame(audio=audio, sample_rate=self.sample_rate, num_channels=1)
        except TypeError:  # older signature
            return TTSAudioRawFrame(audio, self.sample_rate, 1)

    def _push_error(self, message: str) -> None:
        """Report a non-fatal error without waiting for it (push_error is async)."""
        if not hasattr(self, "push_error"):
            return
        try:
            self.create_task(self.push_error(error_msg=message))
        except Exception:  # pragma: no cover - defensive
            pass

    # -------------------------------------------------------- offline synthesis
    async def synthesize(self, text: str, *, timeout: float = 25.0) -> bytes:
        """Speak one piece of text and return the raw 8 kHz PCM.

        Used only to pre-render the opener before a call (open issue 1); it is
        never used inside a live call, and it reads the socket directly, so it
        must not run while the normal receive loop is running.
        """
        self._collector = bytearray()
        self._final_event = asyncio.Event()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(1.0, timeout)
        try:
            if not await self._ensure_connected():
                return b""
            await self._begin_utterance()
            for piece in split_for_speech(text, self.max_chunk_length):
                if not await self._send({"type": "text", "data": {"text": piece}}):
                    return bytes(self._collector)
            await self._flush()
            while not self._final_event.is_set():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.warning("pre-rendering timed out after {} s", timeout)
                    break
                try:
                    message = await asyncio.wait_for(self.connection.recv(), timeout=remaining)
                except (StopAsyncIteration, asyncio.TimeoutError):
                    break
                except Exception as exc:
                    logger.debug("pre-rendering stopped reading: {}", exc)
                    break
                await self._handle_message(message)

            return bytes(self._collector)
        finally:
            self._collector = None
            self._final_event = None
            await self._finish_utterance()
