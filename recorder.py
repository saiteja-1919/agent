"""Recording a call: what was said, and where the time went.

Three recorders sit at three different places in the pipeline (P3). Each one only
handles its own frames, so nothing is counted twice:

* ``InputRecorder``   - right after speech recognition: user speech events, final
  transcripts, interruptions;
* ``LLMRecorder``     - right after the model: assistant text, end of a reply,
  interruptions (it repeats the user speech events, which are idempotent);
* ``AudioRecorder``   - right after the voice: the first audio of each reply, and
  the moment the voice stops speaking.

Nothing here may ever break a live call: every handler is wrapped, and the files
are written atomically with the latency file FIRST and the transcript LAST (P4).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

#: Marks that make the first seconds of a call readable from the files.
MARKS = (
    "stream_parsed",
    "pipeline_built",
    "client_connected",
    "opener_queued",
    "user_started_speaking",
    "user_stopped_speaking",
    "user_final",
    "llm_first_token",
    "llm_response_end",
    "first_tts_audio",
    "opener_ended",
    "hangup_requested",
    "call_ended",
)


def _iso(epoch: Optional[float]) -> Optional[str]:
    if not epoch:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


class CallRecorder:
    """Shared state for one call: turns, marks and timings."""

    def __init__(
        self,
        call_ref: str,
        *,
        to: str = "",
        from_number: str = "",
        student_name: str = "",
        parent_name: str = "",
        school_name: str = "",
        created_at: Optional[float] = None,
    ) -> None:
        self.call_ref = call_ref
        self.to = to
        self.from_number = from_number
        self.student_name = student_name
        self.parent_name = parent_name
        self.school_name = school_name
        self.created_at = created_at if created_at is not None else time.time()
        self.status = "running"
        self.marks: Dict[str, float] = {}
        self.mark_order: List[str] = []
        self.turns: List[Dict[str, Any]] = []
        self.greeting: List[str] = []
        self.response_latency_secs: Optional[float] = None
        self.last_user_final_at: Optional[float] = None
        self.audio_replies: int = 0
        self.interrupted_assistant_turns: int = 0

    # ------------------------------------------------------------------ marks
    def mark(self, name: str, at: Optional[float] = None) -> Optional[float]:
        """Record the first time something happened. Repeats are ignored."""
        if name in self.marks:
            return self.marks[name]
        moment = at if at is not None else time.time()
        self.marks[name] = moment
        self.mark_order.append(name)
        return moment

    def mark_time(self, name: str) -> Optional[float]:
        return self.marks.get(name)

    def since_start(self, name: str) -> Optional[float]:
        moment = self.marks.get(name)
        if moment is None:
            return None
        return round(moment - self.created_at, 3)

    # ------------------------------------------------------------------ turns
    def _last_turn(self) -> Optional[Dict[str, Any]]:
        return self.turns[-1] if self.turns else None

    def _open_turn(self, role: str) -> Optional[Dict[str, Any]]:
        last = self._last_turn()
        if last and last["role"] == role and not last.get("closed"):
            return last
        return None

    def _new_turn(self, role: str, *, at: Optional[float] = None, kind: str = "speech") -> Dict[str, Any]:
        moment = at if at is not None else time.time()
        turn = {
            "index": len(self.turns) + 1,
            "role": role,
            "text": "",
            "kind": kind,
            "started_at": moment,
            "started_at_iso": _iso(moment),
            "ended_at": None,
            "ended_at_iso": None,
            "interrupted": False,
            "no_reply": False,
            "first_audio_delay_secs": None,
        }
        self.turns.append(turn)
        return turn

    def note_greeting(self, lines: List[str], at: Optional[float] = None) -> None:
        """The opener is spoken outside the model, so it must be recorded here
        (P19: the model also has to be told what was already said)."""
        self.greeting = [str(line) for line in lines if str(line).strip()]
        moment = at if at is not None else time.time()
        turn = self._new_turn("assistant", at=moment, kind="opener")
        turn["text"] = " ".join(self.greeting)
        turn["closed"] = True
        turn["ended_at"] = moment
        turn["ended_at_iso"] = _iso(moment)
        self.mark("opener_queued", moment)

    def note_user_text(self, text: str, at: Optional[float] = None) -> None:
        """Several final transcripts of one turn arrive in pieces; join them."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        moment = at if at is not None else time.time()
        turn = self._open_turn("user")
        if turn is None:
            turn = self._new_turn("user", at=moment)
            turn["text"] = cleaned
        else:
            turn["text"] = f"{turn['text']} {cleaned}".strip()
        self.last_user_final_at = moment
        self.mark("user_final", moment)

    def note_assistant_text(self, text: str, at: Optional[float] = None) -> None:
        cleaned = (text or "")
        if not cleaned:
            return
        moment = at if at is not None else time.time()
        self.mark("llm_first_token", moment)
        turn = self._open_turn("assistant")
        if turn is None:
            turn = self._new_turn("assistant", at=moment)
            turn["text"] = cleaned
        else:
            turn["text"] += cleaned

    def note_response_end(self, at: Optional[float] = None) -> None:
        self.mark("llm_response_end", at)
        turn = self._open_turn("assistant")
        if turn is not None:
            turn["closed"] = True
            turn["ended_at"] = at if at is not None else time.time()
            turn["ended_at_iso"] = _iso(turn["ended_at"])

    def note_interruption(self, at: Optional[float] = None) -> None:
        """An interrupted reply becomes its own finished turn (P5)."""
        moment = at if at is not None else time.time()
        for turn in reversed(self.turns):
            if turn["role"] == "assistant" and not turn.get("closed"):
                turn["interrupted"] = True
                turn["closed"] = True
                turn["ended_at"] = moment
                turn["ended_at_iso"] = _iso(moment)
                self.interrupted_assistant_turns += 1
                return

    def note_user_speech_started(self, at: Optional[float] = None) -> None:
        self.mark("user_started_speaking", at)

    def note_user_speech_stopped(self, at: Optional[float] = None) -> None:
        self.mark("user_stopped_speaking", at)

    def note_first_audio(self, at: Optional[float] = None, gap_seconds: float = 0.0) -> None:
        """First audio of a reply: this is where the response latency ends."""
        moment = at if at is not None else time.time()
        self.audio_replies += 1
        self.mark("first_tts_audio" if self.audio_replies == 1 else f"tts_audio_{self.audio_replies}", moment)
        if gap_seconds:
            moment = max(self.created_at, moment - gap_seconds)
        anchor = self.last_user_final_at
        source = "user_final"
        if anchor is None:
            anchor = self.marks.get("opener_queued") or self.marks.get("client_connected")
            source = "opener_queued" if "opener_queued" in self.marks else "client_connected"
        if anchor is None:
            return
        delay = round(max(0.0, moment - anchor), 3)
        turn = self._open_turn("assistant") or self._last_turn()
        if turn is not None and turn.get("first_audio_delay_secs") is None:
            turn["first_audio_delay_secs"] = delay
            turn["first_audio_measured_from"] = source
        if self.response_latency_secs is None and source == "user_final":
            self.response_latency_secs = delay

    def note_agent_hangup_requested(self, at: Optional[float] = None) -> None:
        self.mark("hangup_requested", at)

    def note_call_ended(self, at: Optional[float] = None) -> None:
        self.mark("call_ended", at)
        for turn in self.turns:
            if turn["role"] == "assistant" and not turn.get("closed"):
                turn["closed"] = True
                turn["ended_at"] = turn["ended_at"] or (at if at is not None else time.time())
                turn["ended_at_iso"] = _iso(turn["ended_at"])

    # ------------------------------------------------------------------- files
    def build_latency(self) -> Dict[str, Any]:
        return {
            "call_ref": self.call_ref,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "saved_at": _iso(time.time()),
            "response_latency_secs": self.response_latency_secs,
            "marks": {
                name: {
                    "epoch": round(self.marks[name], 3),
                    "at": _iso(self.marks[name]),
                    "since_start_secs": self.since_start(name),
                    "since_stream_parsed_secs": self._since("stream_parsed", name),
                    "since_previous_mark_secs": self._since_previous(name),
                }
                for name in self.mark_order
            },
            "replies": self.audio_replies,
            "interrupted_replies": self.interrupted_assistant_turns,
            "from_to_first_audio_secs": self._first_audio_delay(),
        }

    def build_transcript(self) -> Dict[str, Any]:
        last = self._last_turn()
        if last is not None and last["role"] == "user" and not last.get("closed"):
            last["no_reply"] = True
        for index, turn in enumerate(self.turns):
            if (
                turn["role"] == "assistant"
                and not turn["text"].strip()
                and index > 0
                and self.turns[index - 1]["role"] == "user"
            ):
                turn["no_reply"] = True
        return {
            "call_ref": self.call_ref,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "saved_at": _iso(time.time()),
            "duration_secs": round(time.time() - self.created_at, 3),
            "to": self.to,
            "from": self.from_number,
            "student_name": self.student_name,
            "parent_name": self.parent_name,
            "school_name": self.school_name,
            "greeting": self.greeting,
            "turns": [
                {
                    "index": turn["index"],
                    "role": turn["role"],
                    "kind": turn["kind"],
                    "text": turn["text"],
                    "at": turn["started_at_iso"],
                    "interrupted": bool(turn["interrupted"]),
                    "no_reply": bool(turn["no_reply"]),
                    "first_audio_delay_secs": turn["first_audio_delay_secs"],
                }
                for turn in self.turns
            ],
            "flags": {
                "no_reply": any(bool(turn["no_reply"]) for turn in self.turns),
                "interrupted": any(bool(turn["interrupted"]) for turn in self.turns),
                "user_turns": sum(1 for turn in self.turns if turn["role"] == "user"),
                "assistant_turns": sum(1 for turn in self.turns if turn["role"] == "assistant"),
            },
        }

    def _since(self, start_name: str, name: str) -> Optional[float]:
        start = self.marks.get(start_name)
        moment = self.marks.get(name)
        if start is None or moment is None:
            return None
        return round(moment - start, 3)

    def _since_previous(self, name: str) -> Optional[float]:
        try:
            position = self.mark_order.index(name)
        except ValueError:
            return None
        if position == 0:
            return round(self.marks[name] - self.created_at, 3)
        return round(self.marks[name] - self.marks[self.mark_order[position - 1]], 3)

    def _first_audio_delay(self) -> Optional[float]:
        for turn in self.turns:
            if turn.get("first_audio_delay_secs") is not None:
                return turn["first_audio_delay_secs"]
        return None

    def stem(self) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.created_at))
        return f"call_{stamp}_{self.call_ref}"

    def save(self, directory, *, status: Optional[str] = None) -> Dict[str, Optional[str]]:
        """Latency first, transcript last, both atomic. Never raises."""
        if status:
            self.status = status
        from pathlib import Path

        folder = Path(directory)
        result: Dict[str, Optional[str]] = {"latency": None, "transcript": None}
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("could not create the transcripts folder {}: {}", folder, exc)
            return result
        stem = self.stem()
        latency_path = folder / f"{stem}_latency.json"
        transcript_path = folder / f"{stem}.json"
        try:
            _write_json(latency_path, self.build_latency())
            result["latency"] = str(latency_path)
        except Exception as exc:
            logger.error("could not write the latency file: {}", exc)
        try:
            _write_json(transcript_path, self.build_transcript())
            result["transcript"] = str(transcript_path)
        except Exception as exc:
            logger.error("could not write the transcript file: {}", exc)
        return result


def _write_json(path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class _RecorderBase(FrameProcessor):
    """Pass-through processor that records frames and never breaks the call."""

    role = "base"

    def __init__(self, recorder: CallRecorder, **kwargs) -> None:
        super().__init__(**kwargs)
        self.recorder = recorder

    async def process_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        await super().process_frame(frame, direction)
        try:
            self.handle(frame)
        except Exception as exc:  # recording errors must never stop a call (P3)
            logger.debug("{} recorder ignored a frame: {}", self.role, exc)
        await self.push_frame(frame, direction)

    def handle(self, frame: Frame) -> None:  # pragma: no cover - overridden
        return None


class InputRecorder(_RecorderBase):
    """User speech: start, stop, final transcripts and interruptions."""

    role = "input"

    def handle(self, frame: Frame) -> None:
        if isinstance(frame, TranscriptionFrame):
            self.recorder.note_user_text(getattr(frame, "text", "") or "")
        elif isinstance(frame, UserStartedSpeakingFrame):
            self.recorder.note_user_speech_started()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.recorder.note_user_speech_stopped()
        elif isinstance(frame, InterruptionFrame):
            self.recorder.note_interruption()


class LLMRecorder(_RecorderBase):
    """Assistant text, the end of a reply, and repeated user speech events."""

    role = "llm"

    def handle(self, frame: Frame) -> None:
        if isinstance(frame, LLMTextFrame):
            self.recorder.note_assistant_text(getattr(frame, "text", "") or "")
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.recorder.note_response_end()
        elif isinstance(frame, InterruptionFrame):
            self.recorder.note_interruption()
        elif isinstance(frame, UserStartedSpeakingFrame):
            self.recorder.note_user_speech_started()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.recorder.note_user_speech_stopped()


class AudioRecorder(_RecorderBase):
    """First audio of each reply, and the moment the voice stops.

    ``on_tts_stopped`` lets the pipeline know when the opening lines have been
    spoken, so the opener gate can open.
    """

    role = "audio"

    def __init__(
        self,
        recorder: CallRecorder,
        *,
        on_tts_stopped: Optional[Callable[[], None]] = None,
        frame_gap_secs: float = 0.0,
        **kwargs,
    ) -> None:
        super().__init__(recorder, **kwargs)
        self._on_tts_stopped = on_tts_stopped
        self._frame_gap_secs = frame_gap_secs
        self._saw_audio = False

    def handle(self, frame: Frame) -> None:
        if isinstance(frame, TTSAudioRawFrame):
            if not self._saw_audio:
                self._saw_audio = True
                length = len(getattr(frame, "audio", b"") or b"")
                rate = int(getattr(frame, "sample_rate", 8000) or 8000)
                gap = (length / 2) / rate if length and rate else self._frame_gap_secs
                self.recorder.note_first_audio(gap_seconds=gap)
        elif isinstance(frame, TTSStoppedFrame):
            # The next audio frame starts a NEW reply, so its latency is measured
            # from the parent's next final transcript.
            self._saw_audio = False
            if self._on_tts_stopped is not None:
                try:
                    self._on_tts_stopped()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("tts stop callback failed: {}", exc)


def write_recording(recorder: CallRecorder, directory, status: str = "finished") -> Dict[str, Optional[str]]:
    """Convenience wrapper used by the server at the end of a call."""
    return recorder.save(directory, status=status)
