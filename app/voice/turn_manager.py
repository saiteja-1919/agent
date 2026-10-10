"""Turn manager: decides when a turn ends and when Veda may be cut off.

Everything time-dependent in a conversation is decided here, in one small state
machine that never reads a clock itself. Every method takes ``now`` explicitly,
so a test can walk a whole call through in microseconds and assert the exact
moment a turn was deemed finished.

Two rules from section 10.2 drive it:

* **The opening is not interruptible.** If the parent talks over the
  introduction, Veda keeps speaking and the parent's words are buffered; they
  become the answer to the identity question the moment the opener finishes.
  Veda never restarts the opening.
* **Everything after the opener may be barged in on.** The parent talking over
  a reply means Veda stops, discards its own remaining audio, and the buffered
  words become the next turn immediately.

Endpointing is a deliberate wait: a turn is complete only after the parent has
stopped for ``endpoint_silence_secs``, which is what separates "they finished"
from "they paused to think". A ceiling on turn length stops one rambling answer
from holding the call open forever, and a listening window turns silence into a
recorded no-response instead of an infinite wait.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TurnPhase(str, Enum):
    """Where the conversation is, from the audio pipeline's point of view."""

    LISTENING = "listening"
    PARENT_SPEAKING = "parent_speaking"
    VEDA_SPEAKING = "veda_speaking"
    ENDED = "ended"


class Signal(str, Enum):
    """What the caller should do now. ``NONE`` means carry on."""

    NONE = "none"
    TURN_COMPLETE = "turn_complete"
    SILENCE_TIMEOUT = "silence_timeout"
    MAX_TURN_REACHED = "max_turn_reached"
    INTERRUPT_VEDA = "interrupt_veda"
    OPENER_PROTECTED = "opener_protected"


@dataclass(frozen=True)
class TurnEvent:
    """One verdict from the state machine."""

    signal: Signal = Signal.NONE
    text: str = ""
    phase: TurnPhase = TurnPhase.LISTENING
    waited_secs: float = 0.0

    @property
    def is_turn(self) -> bool:
        """True when the controller should be given this to answer."""
        return self.signal in (Signal.TURN_COMPLETE, Signal.MAX_TURN_REACHED)


class TurnManager:
    """Owns endpointing, barge-in and the listening window for one call."""

    def __init__(
        self,
        *,
        endpoint_silence_secs: float = 1.1,
        max_turn_secs: float = 25.0,
        min_speech_secs: float = 0.25,
        listening_window_secs: float = 12.0,
        interruptible: bool = False,
    ) -> None:
        if endpoint_silence_secs <= 0:
            raise ValueError("endpoint_silence_secs must be positive")
        if max_turn_secs <= endpoint_silence_secs:
            raise ValueError("max_turn_secs must exceed endpoint_silence_secs")
        self.endpoint_silence_secs = endpoint_silence_secs
        self.max_turn_secs = max_turn_secs
        self.min_speech_secs = min_speech_secs
        self.listening_window_secs = listening_window_secs

        self.interruptible = interruptible
        self.phase: TurnPhase = TurnPhase.LISTENING
        self.parent_text: str = ""
        self.speech_started_at: Optional[float] = None
        self.last_speech_at: Optional[float] = None
        self.turn_started_at: Optional[float] = None
        self.listening_since: Optional[float] = None
        self.veda_started_at: Optional[float] = None
        self.barge_ins: int = 0
        self.protected_interruptions: int = 0
        self._ended = False

    # ------------------------------------------------------------------ helpers
    @property
    def ended(self) -> bool:
        return self._ended

    def allow_interruptions(self) -> None:
        """Called once the scripted opening has finished playing."""
        self.interruptible = True

    def begin_listening(self, now: float) -> None:
        """Start (or restart) the window in which the parent may answer."""
        if self._ended:
            return
        self.phase = TurnPhase.LISTENING
        self.listening_since = now
        self.parent_text = ""
        self.speech_started_at = None
        self.last_speech_at = None
        self.turn_started_at = None

    def end(self) -> None:
        self.phase = TurnPhase.ENDED
        self._ended = True

    def _reset_turn_clocks(self) -> None:
        self.speech_started_at = None
        self.last_speech_at = None
        self.turn_started_at = None

    def _turn_complete(self, now: float, *, truncated: bool) -> TurnEvent:
        waited = (now - self.turn_started_at) if self.turn_started_at is not None else 0.0
        text = self.parent_text.strip()
        span = waited if self.last_speech_at is None else (self.last_speech_at - self.turn_started_at)
        if not text or (not truncated and span < self.min_speech_secs):
            # Nothing recognisable, or too short to be an answer: a cough is a
            # silence, not a reply, and must not spend one of Veda's questions.
            self.phase = TurnPhase.LISTENING
            self.listening_since = now
            self._reset_turn_clocks()
            return TurnEvent(Signal.SILENCE_TIMEOUT, "", self.phase, waited)
        self.phase = TurnPhase.LISTENING
        self.listening_since = now
        self._reset_turn_clocks()
        signal = Signal.MAX_TURN_REACHED if truncated else Signal.TURN_COMPLETE
        return TurnEvent(signal, text, self.phase, waited)

    # ------------------------------------------------------------------ events
    def on_parent_speech_start(self, now: float) -> TurnEvent:
        """The parent started talking."""
        if self._ended:
            return TurnEvent(Signal.NONE, "", TurnPhase.ENDED)
        if self.phase is TurnPhase.VEDA_SPEAKING:
            if not self.interruptible:
                # Section 10.2: the opener plays out; the words are buffered.
                self.protected_interruptions += 1
                return TurnEvent(Signal.OPENER_PROTECTED, "", self.phase)
            self.barge_ins += 1
            self.phase = TurnPhase.PARENT_SPEAKING
            self.speech_started_at = now
            self.last_speech_at = now
            self.turn_started_at = now
            return TurnEvent(Signal.INTERRUPT_VEDA, self.parent_text, self.phase)
        if self.phase is TurnPhase.LISTENING:
            self.phase = TurnPhase.PARENT_SPEAKING
            self.speech_started_at = now
            self.last_speech_at = now
            self.turn_started_at = now
        else:
            self.last_speech_at = now
        return TurnEvent(Signal.NONE, "", self.phase)

    def on_parent_partial(self, text: str, now: float) -> TurnEvent:
        """An interim transcript. Kept so a turn never ends on empty text."""
        if self._ended or self.phase is not TurnPhase.PARENT_SPEAKING:
            return TurnEvent(Signal.NONE, "", self.phase)
        self.parent_text = text or ""
        self.last_speech_at = now
        return TurnEvent(Signal.NONE, self.parent_text, self.phase)

    def on_parent_speech_end(self, now: float) -> TurnEvent:
        """The voice activity detector says the parent stopped."""
        if self.phase is not TurnPhase.PARENT_SPEAKING:
            return TurnEvent(Signal.NONE, "", self.phase)
        self.last_speech_at = now
        return TurnEvent(Signal.NONE, self.parent_text, self.phase)

    def on_stt_final(self, text: str, now: float) -> TurnEvent:
        """STT finalized the utterance: the text is now trustworthy.

        It does **not** end the turn by itself. Ending here would cut off a
        parent who pauses mid-sentence, so completion is decided by
        :meth:`tick` once the endpoint silence has actually elapsed.
        """
        if self.phase is not TurnPhase.PARENT_SPEAKING:
            return TurnEvent(Signal.NONE, "", self.phase)
        if text:
            self.parent_text = text
        self.last_speech_at = now
        return TurnEvent(Signal.NONE, self.parent_text, self.phase)

    def on_veda_started_speaking(self, now: float) -> None:
        if self._ended:
            return
        self.phase = TurnPhase.VEDA_SPEAKING
        self.veda_started_at = now
        self._reset_turn_clocks()

    def on_veda_stopped_speaking(self, now: float) -> None:
        if self._ended:
            return
        self.veda_started_at = None
        if self.phase is TurnPhase.VEDA_SPEAKING:
            self.begin_listening(now)
        # If we are already in PARENT_SPEAKING, Veda was barged in on: the
        # buffered words belong to the live turn and must survive this call.

    def tick(self, now: float) -> TurnEvent:
        """Periodic deadline check. Safe to call as often as the loop allows."""
        if self._ended:
            return TurnEvent(Signal.NONE, "", TurnPhase.ENDED)

        if self.phase is TurnPhase.PARENT_SPEAKING:
            started = self.turn_started_at if self.turn_started_at is not None else now
            if now - started >= self.max_turn_secs:
                return self._turn_complete(now, truncated=True)
            last = self.last_speech_at if self.last_speech_at is not None else started
            if now - last >= self.endpoint_silence_secs:
                return self._turn_complete(now, truncated=False)
            return TurnEvent(Signal.NONE, self.parent_text, self.phase)

        if self.phase is TurnPhase.LISTENING and self.listening_since is not None:
            if now - self.listening_since >= self.listening_window_secs:
                waited = now - self.listening_since
                self.phase = TurnPhase.LISTENING
                self._reset_turn_clocks()
                return TurnEvent(Signal.SILENCE_TIMEOUT, "", self.phase, waited)

        return TurnEvent(Signal.NONE, "", self.phase)

    def current(self, now: float) -> TurnEvent:
        """Read the current state without advancing any deadline."""
        return TurnEvent(Signal.NONE, self.parent_text, self.phase)
