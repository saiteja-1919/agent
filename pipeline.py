"""Assembling one call's pipeline (layers 3 to 9).

The order is fixed and must not be rearranged::

    transport.input -> opener gate -> STT -> recorder("input")
        -> user aggregator (VAD + turn stop timeout) -> LLM
        -> text filter (digits, ending marker) -> recorder("llm")
        -> TTS -> recorder("audio") -> transport.output -> assistant aggregator

Three extras sit in the flow, each with a safe fallback:

* the **opener gate** drops the parent's audio while the two opening lines play,
  so an early "hello" cannot cut the opener off (problem P9). It opens by itself
  after OPENER_GATE_MAX_SECS even if the voice never finishes;
* the **text filter** turns digits into English words even if the model forgets,
  and strips the ending marker (open issues 4 and 5);
* the **hangup signal** queues an EndFrame after the final goodbye, once the
  audio has drained.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from config import Settings
from recorder import AudioRecorder, CallRecorder, InputRecorder, LLMRecorder
from sarvam_llm import build_prompt_values, create_llm_service
from sarvam_stt import SarvamRealtimeSTT
from sarvam_tts import SarvamTTS
from speech_text import DigitWordBuffer

SAMPLE_RATE = 8000
#: 3200 bytes = 200 ms of 8 kHz 16-bit mono, and a multiple of 320.
OUTPUT_PACKET_BYTES = 3200
#: Small natural pause between the two opening lines.
OPENER_GAP_SECS = 0.35


# --------------------------------------------------------------------- pieces
class OpenerGate(FrameProcessor):
    """Keeps the opening lines safe: the parent's audio is not heard yet."""

    def __init__(
        self,
        *,
        max_secs: float = 12.0,
        enabled: bool = True,
        on_release: Optional[Callable[[str], None]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.max_secs = max_secs
        self.enabled = enabled
        self.on_release = on_release
        self.is_open = not enabled
        self.muted_frames = 0
        self.release_reason: Optional[str] = None
        self._waiting_utterances = 0
        self._timeout_secs = max_secs
        self._timer: Optional[asyncio.Task] = None

    def arm(self, expect_utterances: int = 2, timeout_secs: Optional[float] = None) -> None:
        """Close the gate and plan to open it after the opening lines.

        ``timeout_secs`` is used when the opening audio is played from a
        pre-rendered file, where there is no voice callback to wait for.
        """
        if not self.enabled:
            self.is_open = True
            return
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self.is_open = False
        self._waiting_utterances = max(1, int(expect_utterances))
        self._timeout_secs = float(timeout_secs) if timeout_secs else self.max_secs
        try:
            self._timer = asyncio.get_event_loop().create_task(self._timeout())
        except RuntimeError:  # no running loop (offline use)
            self._timer = None

    async def _timeout(self) -> None:
        try:
            await asyncio.sleep(self._timeout_secs)
        except asyncio.CancelledError:
            return
        self.release("timeout")

    def release(self, reason: str = "spoken") -> None:
        if self.is_open:
            return
        self.is_open = True
        self.release_reason = reason
        timer, self._timer = self._timer, None
        if timer is not None and not timer.done():
            timer.cancel()
        if self.on_release is not None:
            try:
                self.on_release(reason)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("opener gate callback failed: {}", exc)

    def note_tts_stopped(self) -> None:
        """One opening line has been spoken."""
        if self.is_open:
            return
        self._waiting_utterances -= 1
        if self._waiting_utterances <= 0:
            self.release("spoken")

    async def process_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, (EndFrame, CancelFrame)):
            self.release("call_ended")
        if not self.is_open and isinstance(frame, InputAudioRawFrame):
            self.muted_frames += 1
            return  # deliberately not forwarded: the opener keeps playing
        await self.push_frame(frame, direction)


class TextFilter(FrameProcessor):
    """Digits become words; the ending marker never reaches the voice."""

    def __init__(
        self,
        *,
        digit_filter: bool = True,
        marker: str = "",
        on_hangup: Optional[Callable[[], None]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.marker = (marker or "").strip()
        self.buffer = DigitWordBuffer(enabled=digit_filter)
        self.on_hangup = on_hangup
        self.marker_seen = False
        self.filtered_digits = 0
        self._hold = ""
        self._hangup_sent = False

    def _without_marker(self, text: str) -> str:
        if not self.marker:
            return text
        combined = self._hold + text
        self._hold = ""
        index = combined.find(self.marker)
        if index >= 0:
            self.marker_seen = True
            combined = combined[:index]
            if self.marker_seen and not self._hangup_sent and self.on_hangup is not None:
                self._hangup_sent = True
                try:
                    self.on_hangup()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("hangup callback failed: {}", exc)
        else:
            for size in range(min(len(self.marker) - 1, len(combined)), 0, -1):
                if self.marker.startswith(combined[-size:]):
                    self._hold = combined[-size:]
                    combined = combined[:-size]
                    break
        return combined

    def flush_text(self) -> str:
        text = self._hold + self.buffer.flush()
        self._hold = ""
        return text

    async def process_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            original = getattr(frame, "text", "") or ""
            cleaned = self._without_marker(original)
            spoken = self.buffer.feed(cleaned)
            if spoken != original:
                self.filtered_digits += 1
            if not spoken:
                return  # hold the text until the rest of the number arrives
            try:
                frame.text = spoken
            except Exception:  # pragma: no cover - defensive
                frame = LLMTextFrame(text=spoken)
        elif isinstance(frame, LLMFullResponseEndFrame):
            tail = self.flush_text()
            if tail:
                await self.push_frame(LLMTextFrame(text=tail), direction)
        await self.push_frame(frame, direction)


class HangupSignal:
    """Asks the pipeline to end once the goodbye has really been spoken."""

    def __init__(
        self,
        recorder: CallRecorder,
        *,
        grace_secs: float = 0.8,
        enabled: bool = True,
    ) -> None:
        self.recorder = recorder
        self.grace_secs = grace_secs
        self.enabled = enabled
        self.worker: Optional[PipelineWorker] = None
        self.requested = False
        self.ended = False
        self._task: Optional[asyncio.Task] = None

    def bind(self, worker: PipelineWorker) -> None:
        self.worker = worker

    def request(self) -> None:
        if not self.enabled or self.requested:
            return
        self.requested = True
        self.recorder.note_agent_hangup_requested()
        logger.info("Veda finished the call; ending in {} s", self.grace_secs)
        try:
            self._task = asyncio.get_event_loop().create_task(self._end())
        except RuntimeError:  # pragma: no cover - no loop (offline use)
            self._task = None

    async def _end(self) -> None:
        try:
            await asyncio.sleep(max(0.0, self.grace_secs))
            worker = self.worker
            if worker is None:
                return
            self.ended = True
            await worker.queue_frame(EndFrame())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failed hang-up must never crash the call
            logger.warning("could not end the call from inside the pipeline: {}", exc)

    def cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()


# ---------------------------------------------------------------------- build
@dataclass
class CallAssembly:
    """Everything the server needs to run one call."""

    recorder: CallRecorder
    pipeline: Any
    worker: PipelineWorker
    gate: OpenerGate
    text_filter: TextFilter
    hangup: HangupSignal
    stt: SarvamRealtimeSTT
    tts: SarvamTTS
    context: LLMContext
    aggregators: LLMContextAggregatorPair
    opener_lines: List[str] = field(default_factory=list)

    def opener_frames(self) -> List[TTSSpeakFrame]:
        return [TTSSpeakFrame(text=line) for line in self.opener_lines]


def vad_analyzer(settings: Settings) -> SileroVADAnalyzer:
    return SileroVADAnalyzer(
        sample_rate=SAMPLE_RATE,
        params=VADParams(
            start_secs=settings.vad_start_secs,
            stop_secs=settings.vad_stop_secs,
            min_volume=settings.vad_min_volume,
        ),
    )


def build_pipeline_params(settings: Settings) -> PipelineParams:
    return PipelineParams(
        audio_in_sample_rate=SAMPLE_RATE,
        audio_out_sample_rate=SAMPLE_RATE,
        enable_heartbeats=True,
        heartbeats_period_secs=settings.pipecat_heartbeat_secs,
        enable_metrics=True,
    )


def build_call(
    *,
    details: dict,
    settings: Settings,
    transport: Any,
    recorder: CallRecorder,
) -> CallAssembly:
    """Build the pipeline for one call from the claimed details."""
    marker = (settings.hangup_marker or "").strip() if settings.agent_hangup else ""
    prompt = details.get("system_prompt") or _prompt(details, settings)

    gate = OpenerGate(
        max_secs=settings.opener_gate_max_secs,
        enabled=settings.opener_gate,
        on_release=lambda reason: recorder.mark("opener_ended", None) or recorder.mark("gate_open", None),
    )
    stt = SarvamRealtimeSTT(
        api_key=settings.sarvam_api_key,
        model=settings.sarvam_stt_realtime_model,
        language_code=settings.sarvam_stt_language,
        sample_rate=SAMPLE_RATE,
    )
    input_recorder = InputRecorder(recorder)
    hangup = HangupSignal(recorder, grace_secs=settings.hangup_grace_secs, enabled=settings.agent_hangup)
    text_filter = TextFilter(
        digit_filter=settings.digit_filter,
        marker=marker,
        on_hangup=hangup.request,
    )
    llm_recorder = LLMRecorder(recorder)
    tts = SarvamTTS(
        api_key=settings.sarvam_api_key,
        model=settings.sarvam_tts_model,
        language_code=settings.sarvam_stt_language,
        speaker=settings.sarvam_tts_voice,
        sample_rate=SAMPLE_RATE,
        pace=settings.sarvam_tts_pace,
        temperature=settings.sarvam_tts_temperature,
        max_chunk_length=150,
        on_utterance_end=gate.note_tts_stopped,
    )
    audio_recorder = AudioRecorder(recorder, on_tts_stopped=gate.note_tts_stopped)
    llm = create_llm_service(settings, prompt)

    context = LLMContext()
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad_analyzer(settings),
            user_turn_stop_timeout=settings.user_turn_stop_timeout_secs,
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            gate,
            stt,
            input_recorder,
            aggregators.user(),
            llm,
            text_filter,
            llm_recorder,
            tts,
            audio_recorder,
            transport.output(),
            aggregators.assistant(),
        ]
    )

    worker_kwargs: dict = {
        "params": build_pipeline_params(settings),
        "cancel_on_idle_timeout": True,
    }
    if settings.idle_timeout_secs:
        worker_kwargs["idle_timeout_secs"] = float(settings.idle_timeout_secs)
    worker = PipelineWorker(pipeline, **worker_kwargs)
    hangup.bind(worker)

    recorder.note_greeting(list(details.get("greeting_lines") or []))
    recorder.mark("pipeline_built")
    gate.arm(expect_utterances=2)

    return CallAssembly(
        recorder=recorder,
        pipeline=pipeline,
        worker=worker,
        gate=gate,
        text_filter=text_filter,
        hangup=hangup,
        stt=stt,
        tts=tts,
        context=context,
        aggregators=aggregators,
        opener_lines=[str(line) for line in (details.get("greeting_lines") or [])],
    )


def _prompt(details: dict, settings: Settings) -> str:
    from sarvam_llm import build_system_prompt

    return build_system_prompt(details, settings)


def exotel_transport_params(settings: Settings):
    """Pipecat's Exotel transport settings: 8 kHz in and out, 200 ms packets."""
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

    return FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SAMPLE_RATE,
        audio_out_sample_rate=SAMPLE_RATE,
        add_wav_header=False,
        fixed_audio_packet_size=OUTPUT_PACKET_BYTES,
        audio_out_auto_silence=False,
    )


async def create_call_transport(runner_args: Any, settings: Settings):
    """Let Pipecat parse the telephony handshake and build the transport.

    Pipecat detects the provider from the first stream message (Exotel or
    Twilio) and picks the matching entry of the dict below, so the same server
    code runs both providers. Afterwards ``runner_args.call_data`` holds
    ``call_id``, ``from`` and ``to``, which is what the pending details are
    matched against.
    """
    from pipecat.runner.utils import create_transport

    params = exotel_transport_params(settings)
    return await create_transport(
        runner_args,
        {"exotel": lambda: params, "twilio": lambda: params},
    )


# --------------------------------------------------- pre-rendered opener (off)
def opener_cache_key(lines: List[str], settings: Settings) -> str:
    """A name for the cached opener audio: same words and voice, same file."""
    payload = "|".join(
        [
            "\n".join(str(line) for line in lines),
            settings.sarvam_tts_model,
            settings.sarvam_tts_voice,
            settings.sarvam_stt_language,
            str(settings.sarvam_tts_pace),
            str(settings.sarvam_tts_temperature),
            str(SAMPLE_RATE),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


async def prerender_opener(
    lines: List[str],
    settings: Settings,
    *,
    cache_dir: Optional[Path] = None,
    timeout: float = 30.0,
) -> Optional[Path]:
    """Render the opening lines with Sarvam before the phone is dialled.

    Returns the cached PCM file, or ``None`` when anything fails - in which case
    the call falls back to the normal live opener.
    """
    if not settings.opener_prerender or not lines:
        return None
    folder = Path(cache_dir) if cache_dir else settings.opener_cache_dir
    key = opener_cache_key(lines, settings)
    pcm_path = folder / f"{key}.pcm"
    if pcm_path.exists() and pcm_path.stat().st_size > 0:
        return pcm_path
    from sarvam_tts import SarvamTTS

    tts = SarvamTTS(
        api_key=settings.sarvam_api_key,
        model=settings.sarvam_tts_model,
        language_code=settings.sarvam_stt_language,
        speaker=settings.sarvam_tts_voice,
        sample_rate=SAMPLE_RATE,
        pace=settings.sarvam_tts_pace,
        temperature=settings.sarvam_tts_temperature,
        emit_frames=False,
    )
    try:
        audio = bytearray()
        for index, line in enumerate(lines):
            if index:  # the small natural pause between the two lines
                audio.extend(b"\x00" * int(OPENER_GAP_SECS * SAMPLE_RATE * 2))
            audio.extend(await tts.synthesize(line, timeout=timeout))
        if not audio:
            logger.warning("pre-rendering produced no audio; the live opener will be used")
            return None
        folder.mkdir(parents=True, exist_ok=True)
        tmp = pcm_path.with_suffix(".pcm.part")
        with open(tmp, "wb") as handle:
            handle.write(bytes(audio))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, pcm_path)
        meta = {
            "key": key,
            "lines": list(lines),
            "voice": settings.sarvam_tts_voice,
            "model": settings.sarvam_tts_model,
            "sample_rate": SAMPLE_RATE,
            "duration_secs": round(len(audio) / (SAMPLE_RATE * 2), 3),
            "words": " ".join(lines),
        }
        with open(folder / f"{key}.json", "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
        logger.info("opener pre-rendered: {} s of audio cached", meta["duration_secs"])
        return pcm_path
    except Exception as exc:
        logger.warning("could not pre-render the opener ({}); the live opener will be used", exc)
        return None
    finally:
        await tts.stop()


def load_prerendered_opener(
    lines: List[str], settings: Settings, *, cache_dir: Optional[Path] = None
) -> Optional[bytes]:
    if not settings.opener_prerender or not lines:
        return None
    folder = Path(cache_dir) if cache_dir else settings.opener_cache_dir
    path = folder / f"{opener_cache_key(lines, settings)}.pcm"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def prerendered_audio_frames(pcm: bytes) -> List[OutputAudioRawFrame]:
    """Cut 8 kHz PCM into 200 ms frames that the Exotel serializer can send."""
    frames: List[OutputAudioRawFrame] = []
    for start in range(0, len(pcm), OUTPUT_PACKET_BYTES):
        chunk = pcm[start : start + OUTPUT_PACKET_BYTES]
        if not chunk:
            continue
        frames.append(OutputAudioRawFrame(audio=chunk, sample_rate=SAMPLE_RATE, num_channels=1))
    return frames
