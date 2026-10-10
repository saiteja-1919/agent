"""Sarvam speech recognition over the realtime WebSocket.

Written by hand because Pipecat's older Sarvam adapter validated audio as
``audio/wav`` and produced no transcripts for raw PCM (P1 context).

What this module is careful about:

* ``TranscriptionFrame`` needs a ``timestamp``; a frame built without it ended a
  real call after the parent's first sentence, so frames are built through a
  defensive helper;
* every incoming message is handled in its own ``try`` - one bad message must
  never end the receive loop;
* the socket reconnects by itself with back-off, and audio dropped while it is
  down is logged at most once per second, with a count.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import time
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

try:  # Pipecat helper; the fallback keeps the adapter usable if it moves
    from pipecat.utils.time import time_now_iso8601
except Exception:  # pragma: no cover - defensive
    from datetime import datetime, timezone

    def time_now_iso8601() -> str:
        return datetime.now(timezone.utc).isoformat()

STT_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"

#: websockets>=14 renamed the header argument; try both.
_HEADER_KWARGS = ("additional_headers", "extra_headers")

Connector = Callable[..., Any]


def build_frame(frame_class: type, **kwargs: Any) -> Frame:
    """Build a frame passing only the arguments this Pipecat version accepts."""
    try:
        params = inspect.signature(frame_class.__init__).parameters
    except (TypeError, ValueError):
        return frame_class(**kwargs)
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return frame_class(**kwargs)
    allowed = {key: value for key, value in kwargs.items() if key in params}
    return frame_class(**allowed)


def build_transcription(text: str, *, user_id: str = "parent", language: Optional[str] = None) -> Frame:
    """A final transcript frame, always with the required timestamp.

    ``language`` is deliberately not passed: a bad language value would make
    the frame fail to build, and a failed frame means a silent call (P1).
    """
    return build_frame(
        TranscriptionFrame,
        text=text,
        user_id=user_id,
        timestamp=time_now_iso8601(),
    )


def connect_kwargs(api_key: str, *, ping_interval: float) -> dict:
    """Headers differ between websockets versions; find the one that is accepted."""
    headers = {"api-subscription-key": api_key}
    websockets = _import_websockets()
    signature = getattr(getattr(websockets, "connect", None), "__doc__", "") or ""
    for name in _HEADER_KWARGS:
        if name in signature:
            return {name: headers, "ping_interval": ping_interval}
    return {"additional_headers": headers, "ping_interval": ping_interval}


def _import_websockets():
    import websockets

    return websockets


class SarvamRealtimeSTT(FrameProcessor):
    """Raw 8 kHz linear16 PCM in, final Telugu transcripts out."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "saaras:v3-realtime",
        language_code: str = "te-IN",
        sample_rate: int = 8000,
        stream_type: str = "fast",
        endpointing: str = "vad",
        mode: str = "codemix",
        ping_interval: float = 10.0,
        reconnect_max_secs: float = 5.0,
        connector: Optional[Connector] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        self.model = model
        self.language_code = language_code
        self.sample_rate = sample_rate
        self.stream_type = stream_type
        self.endpointing = endpointing
        self.mode = mode
        self.ping_interval = ping_interval
        self.reconnect_max_secs = reconnect_max_secs
        self._connector = connector
        self._sleep = sleep

        self.connection: Any = None
        self.connected = False
        self.dropped_frames = 0
        self.finals = 0
        self.reconnects = 0
        self._last_drop_log = 0.0
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    # --------------------------------------------------------------- pipeline
    async def process_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.start()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self.stop()
        elif isinstance(frame, InputAudioRawFrame):
            await self._send_audio(bytes(getattr(frame, "audio", b"") or b""))
        await self.push_frame(frame, direction)

    # ----------------------------------------------------------------- socket
    def url(self) -> str:
        return (
            f"{STT_URL}?model={self.model}&language_code={self.language_code}"
            f"&stream_type={self.stream_type}&endpointing={self.endpointing}"
            f"&encoding=linear16&sample_rate={self.sample_rate}&mode={self.mode}"
        )

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._close()

    async def _close(self) -> None:
        connection, self.connection = self.connection, None
        self.connected = False
        if connection is None:
            return
        try:
            await connection.close()
        except Exception as exc:  # pragma: no cover - shutdown must stay quiet
            logger.debug("STT socket close failed: {}", exc)

    async def _run(self) -> None:
        delay = 0.5
        while not self._stopping:
            try:
                await self._connect()
                delay = 0.5
                await self._receive_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("STT socket problem: {}", exc)
            if self._stopping:
                break
            self.reconnects += 1
            await self._close()
            try:
                await self._sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, self.reconnect_max_secs)

    async def _connect(self) -> None:
        connector = self._connector
        if connector is None:
            websockets = _import_websockets()
            connector = websockets.connect
        kwargs = connect_kwargs(self.api_key, ping_interval=self.ping_interval)
        self.connection = await connector(self.url(), **kwargs)
        self.connected = True
        logger.info("STT connected ({} Hz, {})", self.sample_rate, self.mode)

    async def _receive_loop(self) -> None:
        async for message in self.connection:
            try:
                self._handle_message(message)
            except Exception as exc:
                # One bad message must never end speech recognition (P1).
                logger.debug("STT ignored a message: {}", exc)

    def _handle_message(self, message: Any) -> Optional[Frame]:
        if isinstance(message, (bytes, bytearray)):
            return None
        try:
            payload = json.loads(message)
        except (ValueError, TypeError):
            logger.debug("STT sent something that is not JSON: {!r}", str(message)[:120])
            return None
        if not isinstance(payload, dict):
            return None
        event = str(payload.get("event") or payload.get("type") or "")
        if event == "transcript.final":
            text = str(payload.get("text") or "").strip()
            if text:
                self.finals += 1
                self._push_now(build_transcription(text))
            return None
        if event in {"transcript.partial", "transcript.interim"}:
            return None
        if event == "error":
            message_text = str(payload.get("message") or payload.get("error") or payload)
            logger.warning("STT reported an error: {}", message_text)
            self._push_error(message_text)
            return None
        logger.debug("STT event: {}", event or payload)
        return None

    def _push_now(self, frame: Frame) -> None:
        """Push a frame from the receive loop onto the pipeline."""
        task = self.create_task(self.push_frame(frame)) if hasattr(self, "create_task") else None
        if task is None:  # pragma: no cover - defensive
            asyncio.get_event_loop().create_task(self.push_frame(frame))

    def _push_error(self, message: str) -> None:
        """Report a non-fatal error without waiting for it (push_error is async)."""
        if not hasattr(self, "push_error"):
            return
        try:
            self.create_task(self.push_error(error_msg=message))
        except Exception:  # pragma: no cover - defensive
            pass

    # ------------------------------------------------------------------- audio
    async def _send_audio(self, audio: bytes) -> None:
        if not audio:
            return
        connection = self.connection
        if connection is None or not self.connected:
            self._log_drop()
            return
        payload = json.dumps({"event": "audio_input", "audio": base64.b64encode(audio).decode("ascii")})
        try:
            await connection.send(payload)
        except Exception as exc:
            self.connected = False
            self._log_drop()
            logger.debug("STT send failed ({}); waiting for the reconnect", exc)

    def _log_drop(self) -> None:
        """Log dropped audio at most once per second, with a running count (P1)."""
        self.dropped_frames += 1
        now = time.monotonic()
        if now - self._last_drop_log >= 1.0:
            self._last_drop_log = now
            logger.warning(
                "STT is not connected: {} audio frames dropped so far (the socket reconnects by itself)",
                self.dropped_frames,
            )

    # ------------------------------------------------------------------ status
    def stats(self) -> dict:
        return {
            "connected": self.connected,
            "finals": self.finals,
            "dropped_frames": self.dropped_frames,
            "reconnects": self.reconnects,
        }
