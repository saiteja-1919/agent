"""Typed events that drive the call controller and the conversation controller.

Two families live here, deliberately separated:

* :class:`ProviderEvent` - something the telephony provider told us (a dial
  result, a status callback, a hangup). These arrive out of order and more than
  once, so each carries a stable ``dedupe_key``.
* :class:`ConversationEvent` - something that happened inside the audio pipeline
  (the opener finished, a parent turn was finalized, the parent interrupted).
  These never come from outside the process.

Neither family carries a raw provider payload: only the fields Veda actually
uses are kept, so personal data cannot leak into a log line by accident
(section 17.8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple

from .models import utcnow
from .enums import DialResult, ProviderEventKind


# --------------------------------------------------------------------- provider
@dataclass(frozen=True)
class ProviderEvent:
    """Base class for a normalized provider event."""

    kind: ProviderEventKind
    provider: str = "exotel"
    provider_call_sid: Optional[str] = None
    provider_event_id: str = ""
    received_at: datetime = field(default_factory=utcnow)

    def dedupe_key(self) -> str:
        """Stable identity for duplicate-event suppression.

        The provider event id is preferred; otherwise a provider-specific
        composite is used so a resent webhook is recognised but two genuinely
        different events are not merged.
        """
        if self.provider_event_id:
            return f"{self.provider}:{self.kind.value}:{self.provider_event_id}"
        return f"{self.provider}:{self.kind.value}:{self.provider_call_sid or 'unknown'}"

    def sanitized(self) -> Dict[str, object]:
        """A log-safe view: no tokens, no numbers, no payload contents."""
        return {
            "kind": self.kind.value,
            "provider": self.provider,
            "has_call_sid": bool(self.provider_call_sid),
            "received_at": self.received_at.isoformat(),
        }


@dataclass(frozen=True)
class DialResultEvent(ProviderEvent):
    """The answer to 'did the dial request create a call?'

    ``UNCERTAIN_NEEDS_REVIEW`` is the important case: the request timed out or
    the connection broke after the request may have been accepted, so Veda must
    never retry it automatically (sections 7.4 and 19.1).
    """

    kind: ProviderEventKind = ProviderEventKind.DIAL_RESULT
    dial_result: DialResult = DialResult.NOT_DIALED
    failure_reason: str = ""
    http_status: Optional[int] = None

    @property
    def may_retry(self) -> bool:
        return self.dial_result.may_redial


@dataclass(frozen=True)
class CallStatusEvent(ProviderEvent):
    """A provider call-status change (ringing, answered, completed, failed)."""

    #: Declared here so a status event can be built without repeating itself;
    #: callers pass the real kind (RINGING, ANSWERED, COMPLETED, FAILED).
    kind: ProviderEventKind = ProviderEventKind.RINGING
    status_text: str = ""
    duration_seconds: Optional[int] = None
    failure_reason: str = ""


@dataclass(frozen=True)
class HangupEvent(ProviderEvent):
    """The call ended - provider-side hangup or the parent hanging up."""

    kind: ProviderEventKind = ProviderEventKind.HANGUP
    hangup_cause: str = ""
    by_parent: bool = False


@dataclass(frozen=True)
class StreamLifecycleEvent(ProviderEvent):
    """The media WebSocket opened or closed for this call."""

    kind: ProviderEventKind = ProviderEventKind.STREAM_STARTED
    stream_sid: Optional[str] = None
    sample_rate: Optional[int] = None


# ----------------------------------------------------------------- conversation
@dataclass(frozen=True)
class ConversationEvent:
    """Base class for pipeline-internal events."""

    at: datetime = field(default_factory=utcnow)
    turn_index: int = 0

    def sanitized(self) -> Dict[str, object]:
        name = type(self).__name__
        return {"event": name, "turn_index": self.turn_index, "at": self.at.isoformat()}


@dataclass(frozen=True)
class OpenerLinePlayed(ConversationEvent):
    """One scripted opening line finished playing.

    ``line_index`` is 0 for the introduction and 1 for the identity question,
    which is what lets the controller know the identity question has definitely
    been heard before it interprets a reply (section 10.1).
    """

    line_index: int = 0
    text_checksum: str = ""
    from_audio_cache: bool = True


@dataclass(frozen=True)
class ParentTurnFinalized(ConversationEvent):
    """A parent turn ended and its final transcript is available.

    ``text`` is live transcript content. It is never written to disk, logs or
    the database (STORE_TRANSCRIPTS=false); it exists only for this turn.
    """

    text: str = ""
    monotonic_marks: Dict[str, float] = field(default_factory=dict)
    is_identity_answer: bool = False


@dataclass(frozen=True)
class InterruptionDetected(ConversationEvent):
    """The parent started speaking while Veda was talking."""

    during_opening: bool = False
    buffered_audio_ms: int = 0


@dataclass(frozen=True)
class SilenceTimeout(ConversationEvent):
    """No usable parent speech inside the allowed window."""

    prompts_so_far: int = 0


@dataclass(frozen=True)
class PlaybackFinished(ConversationEvent):
    """Veda finished speaking a reply; it is safe to listen again."""

    reply_checksum: str = ""
    was_closing: bool = False


INTERNAL_EVENT_TYPES: Tuple[type, ...] = (
    OpenerLinePlayed,
    ParentTurnFinalized,
    InterruptionDetected,
    SilenceTimeout,
    PlaybackFinished,
)
