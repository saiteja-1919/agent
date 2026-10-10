"""Turning an Exotel callback into a typed event, without inventing semantics.

The honesty rule for this module: **the exact callback payload has not been
verified against the live account.** The brief forbids pretending otherwise, so
this parser is deliberately tolerant and explicit:

* it reads field names case-insensitively from a flat mapping, because a form
  post and a JSON body reach here the same way;
* every status string it recognises is listed in :data:`STATUS_TO_EVENT`, and an
  unrecognised status becomes ``None`` rather than a guess - the caller records
  the callback (so a repeat is still detected) and moves on;
* it validates the fields it needs and reports what was missing, so a malformed
  callback is rejected with a reason instead of half-processed;
* the content digest is computed over a small canonical subset, never stored as
  the raw payload, so no personal data lands in the database (section 17.8).

Status polling (``GET .../Calls/{sid}.json``) remains the primary, verified path;
these callbacks are an accelerator on top of it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.domain.enums import ProviderEventKind
from app.domain.events import CallStatusEvent, DialResultEvent, HangupEvent, ProviderEvent
from app.domain.models import utcnow

#: Exotel's documented call-status words, mapped onto what Veda acts on.
#: Anything absent from this table is unrecognised and does not change state.
STATUS_TO_EVENT: Dict[str, ProviderEventKind] = {
    "ringing": ProviderEventKind.RINGING,
    "in-progress": ProviderEventKind.ANSWERED,
    "in_progress": ProviderEventKind.ANSWERED,
    "answered": ProviderEventKind.ANSWERED,
    "completed": ProviderEventKind.COMPLETED,
    "no-answer": ProviderEventKind.FAILED,
    "no_answer": ProviderEventKind.FAILED,
    "busy": ProviderEventKind.FAILED,
    "failed": ProviderEventKind.FAILED,
    "canceled": ProviderEventKind.FAILED,
    "cancelled": ProviderEventKind.FAILED,
    "cancel": ProviderEventKind.FAILED,
}

#: Words that mean the call ended for a reason the parent caused.
_PARENT_CAUSE_HINTS = ("caller", "customer", "parent", "hangup by")
#: Words that mean nobody answered.
_NO_ANSWER_HINTS = ("no-answer", "no_answer", "busy", "unanswered")

_CALL_ID_KEYS = ("CallSid", "CallSID", "callsid", "call_sid", "sid")
_STATUS_KEYS = ("Status", "status", "CallStatus", "call_status", "EventType", "event_type")
_DETAILS_KEYS = ("Details", "details", "Detail", "detail")
_DURATION_KEYS = ("ConversationDuration", "Duration", "duration", "CallDuration")

#: Statuses that mean the call never reached the parent.
_FAILED_STATUSES = {"no-answer", "no_answer", "busy", "failed", "canceled", "cancelled", "cancel"}


@dataclass(frozen=True)
class CallbackProblem:
    """Why a callback cannot be used."""

    field: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.field}: {self.reason}"


def normalise_keys(payload: Mapping[str, Any]) -> Dict[str, str]:
    """Flatten any mapping into lower-case string keys with string values."""
    normalised: Dict[str, str] = {}
    for key, value in (payload or {}).items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            try:
                normalised[str(key).strip().lower()] = json.dumps(value, sort_keys=True)
            except (TypeError, ValueError):  # pragma: no cover - defensive
                continue
        else:
            normalised[str(key).strip().lower()] = str(value).strip()
    return normalised


def pick(payload: Mapping[str, Any], keys: Tuple[str, ...]) -> str:
    normalised = normalise_keys(payload)
    for key in keys:
        value = normalised.get(key.strip().lower())
        if value:
            return value
    return ""


def validate_callback(payload: Mapping[str, Any]) -> List[CallbackProblem]:
    """Check the fields Veda needs. An empty list means the callback is usable."""
    problems: List[CallbackProblem] = []
    if not isinstance(payload, Mapping) or not payload:
        return [CallbackProblem("payload", "the callback body is empty")]
    if not pick(payload, _CALL_ID_KEYS):
        problems.append(CallbackProblem("CallSid", "the provider call id is missing"))
    status = pick(payload, _STATUS_KEYS)
    if not status:
        problems.append(CallbackProblem("Status", "the call status is missing"))
    return problems


def payload_digest(payload: Mapping[str, Any], extra: str = "") -> str:
    """A digest of the identifying fields only - never the whole payload."""
    parts = [
        pick(payload, _CALL_ID_KEYS),
        pick(payload, _STATUS_KEYS),
        pick(payload, _DURATION_KEYS),
        pick(payload, _DETAILS_KEYS),
        extra,
    ]
    canonical = "|".join(part or "" for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def event_key(payload: Mapping[str, Any], *, extra: str = "") -> str:
    """The deduplication key for a callback.

    Two different callbacks for the same call and status collapse into one event,
    which is what makes a provider that retries a webhook harmless.
    """
    return f"exotel:{pick(payload, _CALL_ID_KEYS)}:{pick(payload, _STATUS_KEYS)}:{extra}"


def parse_status_callback(
    payload: Mapping[str, Any],
    *,
    received_at: Optional[datetime] = None,
) -> Optional[ProviderEvent]:
    """Turn a callback into a typed event, or ``None`` when it is not understood.

    ``None`` is a real answer here: an unrecognised status must not be guessed at,
    and the caller still records the callback's identity so a duplicate is caught.
    """
    if validate_callback(payload):
        return None

    call_sid = pick(payload, _CALL_ID_KEYS)
    status = pick(payload, _STATUS_KEYS).lower()
    kind = STATUS_TO_EVENT.get(status)
    if kind is None:
        return None

    duration_text = pick(payload, _DURATION_KEYS)
    try:
        duration = int(float(duration_text)) if duration_text else None
    except (TypeError, ValueError):
        duration = None

    details = pick(payload, _DETAILS_KEYS)
    at = received_at or utcnow()

    if kind is ProviderEventKind.FAILED:
        return CallStatusEvent(
            kind=ProviderEventKind.FAILED,
            provider_call_sid=call_sid,
            provider_event_id=event_key(payload),
            status_text=status,
            duration_seconds=duration,
            failure_reason=details[:120],
            received_at=at,
        )

    if kind is ProviderEventKind.COMPLETED:
        return CallStatusEvent(
            kind=ProviderEventKind.COMPLETED,
            provider_call_sid=call_sid,
            provider_event_id=event_key(payload),
            status_text=status,
            duration_seconds=duration,
            received_at=at,
        )

    return CallStatusEvent(
        kind=kind,
        provider_call_sid=call_sid,
        provider_event_id=event_key(payload),
        status_text=status,
        duration_seconds=duration,
        received_at=at,
    )


def status_implies_no_answer(payload: Mapping[str, Any]) -> bool:
    """True when the status means the parent never picked up."""
    status = pick(payload, _STATUS_KEYS).lower()
    if status in _FAILED_STATUSES:
        return True
    details = pick(payload, _DETAILS_KEYS).lower()
    return any(hint in details for hint in _NO_ANSWER_HINTS)


def status_implies_parent_hung_up(payload: Mapping[str, Any]) -> bool:
    """True when the provider's detail text says the parent ended the call.

    Used only to choose between two similar outcomes; the wording is checked
    against the live payload before it is relied on.
    """
    details = pick(payload, _DETAILS_KEYS).lower()
    return any(hint in details for hint in _PARENT_CAUSE_HINTS)
