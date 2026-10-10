"""States, statuses and reason codes for Veda.

The brief lists one long state list and then explicitly forbids implementing it
as one enum (section 10 [ADDED] structure rule). Three genuinely different
things are modelled separately here, and every state from the brief's list
belongs to exactly one of them:

* :class:`CallLifecycleState` - where the *call* is (telephony/provider view).
* :class:`ConversationPhase` - where the *conversation* is (Veda's view).
* :class:`Outcome` + a reason code - what *happened*, set exactly once.

Keeping them apart is what makes "the call ended" independent of "the
conversation finished politely", and what lets a final outcome be immutable
without freezing the call row.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Iterable, Optional


class _StrEnum(str, Enum):
    """A string enum that prints as its value (nicer logs and SQL values)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


# --------------------------------------------------------------------- call state
class CallLifecycleState(_StrEnum):
    """Telephony-side lifecycle. Owned by the call controller."""

    CREATED = "created"
    DIALING = "dialing"
    RINGING = "ringing"
    ANSWERED = "answered"
    IN_PROGRESS = "in_progress"
    ENDING = "ending"
    ENDED = "ended"


#: A call in one of these states will never change again.
TERMINAL_CALL_STATES: FrozenSet[CallLifecycleState] = frozenset({CallLifecycleState.ENDED})

#: States in which the call is still occupying a provider slot.
ACTIVE_CALL_STATES: FrozenSet[CallLifecycleState] = frozenset(
    {
        CallLifecycleState.CREATED,
        CallLifecycleState.DIALING,
        CallLifecycleState.RINGING,
        CallLifecycleState.ANSWERED,
        CallLifecycleState.IN_PROGRESS,
        CallLifecycleState.ENDING,
    }
)

#: Ordering used to reject out-of-order provider events (a late "ringing" must
#: not drag an answered call backwards).
LIFECYCLE_RANK: Dict[CallLifecycleState, int] = {
    CallLifecycleState.CREATED: 0,
    CallLifecycleState.DIALING: 1,
    CallLifecycleState.RINGING: 2,
    CallLifecycleState.ANSWERED: 3,
    CallLifecycleState.IN_PROGRESS: 4,
    CallLifecycleState.ENDING: 5,
    CallLifecycleState.ENDED: 6,
}


# ---------------------------------------------------------------- conversation state
class ConversationPhase(_StrEnum):
    """Conversation-side phase. Owned by the conversation controller."""

    NOT_STARTED = "not_started"
    PLAYING_GREETING = "playing_greeting"
    WAITING_FOR_IDENTITY_RESPONSE = "waiting_for_identity_response"
    PLAYING_FEE_REMINDER = "playing_fee_reminder"
    LISTENING = "listening"
    FINALIZING_TRANSCRIPT = "finalizing_transcript"
    CLASSIFYING_INTENT = "classifying_intent"
    CHECKING_RESPONSE_CACHE = "checking_response_cache"
    GENERATING_RESPONSE = "generating_response"
    SYNTHESIZING_RESPONSE = "synthesizing_response"
    PLAYING_RESPONSE = "playing_response"
    WAITING_FOR_PARENT_TURN = "waiting_for_parent_turn"
    CLOSING = "closing"
    DONE = "done"


#: Phases during which Veda is speaking. Parent speech is buffered but does not
#: interrupt these (section 10.2: the opener is non-interruptible).
SPEAKING_PHASES: FrozenSet[ConversationPhase] = frozenset(
    {
        ConversationPhase.PLAYING_GREETING,
        ConversationPhase.PLAYING_FEE_REMINDER,
        ConversationPhase.SYNTHESIZING_RESPONSE,
        ConversationPhase.PLAYING_RESPONSE,
        ConversationPhase.CLOSING,
    }
)

#: The phases that make up the scripted, non-interruptible opening.
OPENING_PHASES: FrozenSet[ConversationPhase] = frozenset(
    {
        ConversationPhase.PLAYING_GREETING,
        ConversationPhase.WAITING_FOR_IDENTITY_RESPONSE,
    }
)


# ------------------------------------------------------------------------ outcome
class Outcome(_StrEnum):
    """Final outcome of a call attempt (section 18 and 18.1)."""

    COMPLETED = "completed"
    NO_RESPONSE = "no_response"
    VOICEMAIL = "voicemail"
    CALLBACK_REQUESTED = "callback_requested"
    OFF_TOPIC_LIMIT = "off_topic_limit"
    PARENT_DISCONNECTED = "parent_disconnected"
    PROVIDER_ERROR = "provider_error"
    WRONG_NUMBER = "wrong_number"
    NOT_PARENT = "not_parent"
    DO_NOT_CALL = "do_not_call"
    UNCERTAIN_NEEDS_REVIEW = "uncertain_needs_review"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Every outcome in this enum is final; the property documents that."""
        return True

    @property
    def is_business_success(self) -> bool:
        """Did the parent hear and respond to the reminder?

        Deliberately *not* the same as "call answered": a wrong number is a
        completed call but not a successful fee discussion (section 18).
        """
        return self in _BUSINESS_SUCCESS_OUTCOMES


_BUSINESS_SUCCESS_OUTCOMES: FrozenSet[Outcome] = frozenset(
    {Outcome.COMPLETED, Outcome.CALLBACK_REQUESTED}
)


class ReasonCode(_StrEnum):
    """Machine-readable reason attached to an outcome.

    One enum for all outcomes keeps the database column simple; the legal
    combinations are declared in :data:`OUTCOME_REASON_CODES` and enforced.
    """

    # Completed
    DATE_PROMISED = "date_promised"
    NO_DATE_GIVEN = "no_date_given"
    ALREADY_PAID = "already_paid"
    PRINCIPAL_REQUESTED = "principal_requested"
    FINANCIAL_REASON = "financial_reason"
    PARENT_DECLINED = "parent_declined"
    ILLNESS_OR_EMERGENCY = "illness_or_emergency"

    # No Response
    RING_TIMEOUT = "ring_timeout"
    PARENT_SILENT = "parent_silent"

    # Voicemail
    ANSWER_MACHINE = "answer_machine"

    # Wrong Number / Not Parent / Do Not Call
    WRONG_PERSON = "wrong_person"
    THIRD_PARTY_ANSWERED = "third_party_answered"
    PARENT_REQUESTED_NO_MORE_CALLS = "parent_requested_no_more_calls"

    # Callback Requested
    BUSY = "busy"
    CALLBACK_TIME_GIVEN = "callback_time_given"

    # Provider Error
    DIAL_FAILED = "dial_failed"
    PROVIDER_TIMEOUT = "provider_timeout"
    STREAM_FAILED = "stream_failed"
    AUDIO_TRANSPORT_FAILED = "audio_transport_failed"

    # Infrastructure
    PIPELINE_STARTUP_FAILED = "pipeline_startup_failed"
    MAX_DURATION_REACHED = "max_duration_reached"
    OPERATOR_CANCELLED = "operator_cancelled"
    APPLICATION_SHUTDOWN = "application_shutdown"

    # Review
    DIAL_RESULT_UNKNOWN = "dial_result_unknown"
    RECONCILIATION_FAILED = "reconciliation_failed"


#: Which reason codes are legal for each outcome. Anything else is a bug.
OUTCOME_REASON_CODES: Dict[Outcome, FrozenSet[ReasonCode]] = {
    Outcome.COMPLETED: frozenset(
        {
            ReasonCode.DATE_PROMISED,
            ReasonCode.NO_DATE_GIVEN,
            ReasonCode.ALREADY_PAID,
            ReasonCode.PRINCIPAL_REQUESTED,
            ReasonCode.FINANCIAL_REASON,
            ReasonCode.PARENT_DECLINED,
            ReasonCode.ILLNESS_OR_EMERGENCY,
        }
    ),
    Outcome.NO_RESPONSE: frozenset({ReasonCode.RING_TIMEOUT, ReasonCode.PARENT_SILENT}),
    Outcome.VOICEMAIL: frozenset({ReasonCode.ANSWER_MACHINE}),
    Outcome.CALLBACK_REQUESTED: frozenset(
        {ReasonCode.BUSY, ReasonCode.CALLBACK_TIME_GIVEN, ReasonCode.NO_DATE_GIVEN}
    ),
    Outcome.OFF_TOPIC_LIMIT: frozenset({ReasonCode.NO_DATE_GIVEN}),
    Outcome.PARENT_DISCONNECTED: frozenset({ReasonCode.PARENT_SILENT}),
    Outcome.PROVIDER_ERROR: frozenset(
        {
            ReasonCode.DIAL_FAILED,
            ReasonCode.PROVIDER_TIMEOUT,
            ReasonCode.STREAM_FAILED,
            ReasonCode.AUDIO_TRANSPORT_FAILED,
        }
    ),
    Outcome.WRONG_NUMBER: frozenset({ReasonCode.WRONG_PERSON}),
    Outcome.NOT_PARENT: frozenset({ReasonCode.THIRD_PARTY_ANSWERED}),
    Outcome.DO_NOT_CALL: frozenset({ReasonCode.PARENT_REQUESTED_NO_MORE_CALLS}),
    Outcome.UNCERTAIN_NEEDS_REVIEW: frozenset(
        {ReasonCode.DIAL_RESULT_UNKNOWN, ReasonCode.RECONCILIATION_FAILED}
    ),
    Outcome.CANCELLED: frozenset(
        {
            ReasonCode.OPERATOR_CANCELLED,
            ReasonCode.APPLICATION_SHUTDOWN,
            ReasonCode.PIPELINE_STARTUP_FAILED,
            ReasonCode.MAX_DURATION_REACHED,
        }
    ),
}

#: Shortest legal reason code per outcome, used when the caller does not supply
#: one and the outcome has exactly one sensible default.
DEFAULT_REASON_FOR_OUTCOME: Dict[Outcome, ReasonCode] = {
    Outcome.NO_RESPONSE: ReasonCode.PARENT_SILENT,
    Outcome.VOICEMAIL: ReasonCode.ANSWER_MACHINE,
    Outcome.WRONG_NUMBER: ReasonCode.WRONG_PERSON,
    Outcome.NOT_PARENT: ReasonCode.THIRD_PARTY_ANSWERED,
    Outcome.DO_NOT_CALL: ReasonCode.PARENT_REQUESTED_NO_MORE_CALLS,
    Outcome.UNCERTAIN_NEEDS_REVIEW: ReasonCode.DIAL_RESULT_UNKNOWN,
    Outcome.CANCELLED: ReasonCode.OPERATOR_CANCELLED,
    Outcome.PARENT_DISCONNECTED: ReasonCode.PARENT_SILENT,
}


class ActionNeeded(_StrEnum):
    """What the office must do next, derived from the outcome (staff export)."""

    NONE = "none"
    CALL_PRINCIPAL_INFORM = "call_principal_inform"
    CHECK_PAYMENT_RECORD = "check_payment_record"
    REMOVE_FROM_CALLING = "remove_from_calling"
    REVIEW_DIAL_RESULT = "review_dial_result"
    CHECK_WRONG_NUMBER = "check_wrong_number"


# ----------------------------------------------------------------- attempts/tasks
class TaskStatus(_StrEnum):
    """Reminder-task status. The database enforces the one-attempt rule."""

    NEW = "new"
    READY = "ready"
    CLAIMED = "claimed"
    DIALING = "dialing"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class AttemptStatus(_StrEnum):
    """A single outbound attempt on a task. At most one row per task exists."""

    CREATED = "created"
    DIALING = "dialing"
    RINGING = "ringing"
    ANSWERED = "answered"
    IN_PROGRESS = "in_progress"
    ENDING = "ending"
    ENDED = "ended"
    UNCERTAIN = "uncertain"

    @classmethod
    def from_lifecycle(cls, state: CallLifecycleState) -> "AttemptStatus":
        return cls(state.value)


class DialResult(_StrEnum):
    """How the dialing request itself turned out.

    The distinction between ``NOT_DIALED`` and ``UNCERTAIN_NEEDS_REVIEW`` is a
    safety rule (sections 7.4 and 19.1): only a request that provably never
    created a call may ever be dialled again.
    """

    NOT_DIALED = "not_dialed"
    DIALED = "dialed"
    UNCERTAIN_NEEDS_REVIEW = "uncertain_needs_review"

    @property
    def may_redial(self) -> bool:
        return self is DialResult.NOT_DIALED


class ImportRowStatus(_StrEnum):
    VALID = "valid"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"


class ImportBatchStatus(_StrEnum):
    PARSED = "parsed"
    APPLIED = "applied"
    DISCARDED = "discarded"


# ------------------------------------------------------------------------- intents
class Intent(_StrEnum):
    """Conversation intents the deterministic layer reasons about.

    Only a minority are answered deterministically (section 12.4); the rest are
    handled by the model within the section 16 prompt.
    """

    IDENTITY_CONFIRMED = "identity_confirmed"
    IDENTITY_DENIED = "identity_denied"
    THIRD_PARTY = "third_party"
    WRONG_NUMBER = "wrong_number"
    GREETING_ONLY = "greeting_only"
    WHO_IS_CALLING = "who_is_calling"
    PAYMENT_DATE_GIVEN = "payment_date_given"
    VAGUE_ANSWER = "vague_answer"
    ALREADY_PAID = "already_paid"
    PREVIOUS_REMINDER = "previous_reminder"
    FEE_QUESTION = "fee_question"
    PRINCIPAL_REQUEST = "principal_request"
    FINANCIAL_REASON = "financial_reason"
    ILLNESS_OR_EMERGENCY = "illness_or_emergency"
    BUSY_OR_CALLBACK = "busy_or_callback"
    IRRITATED = "irritated"
    AI_QUESTION = "ai_question"
    ASKED_TO_STOP = "asked_to_stop"
    DO_NOT_CALL = "do_not_call"
    SILENCE = "silence"
    OFF_TOPIC = "off_topic"
    UNKNOWN = "unknown"


#: Intents that must never be answered by the language model.
DETERMINISTIC_ONLY_INTENTS: FrozenSet[Intent] = frozenset(
    {
        Intent.SILENCE,
        Intent.ASKED_TO_STOP,
        Intent.DO_NOT_CALL,
        Intent.WRONG_NUMBER,
        Intent.IDENTITY_CONFIRMED,
        Intent.IDENTITY_DENIED,
        Intent.GREETING_ONLY,
    }
)


class ResponseSource(_StrEnum):
    """Where a spoken reply came from - recorded for latency attribution."""

    SCRIPTED = "scripted"
    AUDIO_CACHE = "audio_cache"
    TEMPLATE = "template"
    LLM = "llm"
    FALLBACK = "fallback"
    CHECK_IN = "check_in"


class CacheKind(_StrEnum):
    TEMPLATE = "template"
    AUDIO = "audio"


class ProviderEventKind(_StrEnum):
    """Normalised provider events (Exotel callbacks and media-stream events)."""

    DIAL_RESULT = "dial_result"
    RINGING = "ringing"
    ANSWERED = "answered"
    COMPLETED = "completed"
    FAILED = "failed"
    HANGUP = "hangup"
    STREAM_STARTED = "stream_started"
    STREAM_ENDED = "stream_ended"
    DIAL_RESULT_UNKNOWN = "dial_result_unknown"


class LatencyStage(_StrEnum):
    """Measured latency stages (section 15).

    ``t0``..``t5`` are the brief's names, kept for traceability with the docs.
    """

    T0_LAST_PARENT_AUDIO = "t0_last_parent_audio"
    T1_END_OF_TURN = "t1_end_of_turn"
    T2_FINAL_TRANSCRIPT = "t2_final_transcript"
    T3_FIRST_LLM_TOKEN = "t3_first_llm_token"
    T4_FIRST_TTS_AUDIO = "t4_first_tts_audio"
    T5_FIRST_OUTGOING_AUDIO = "t5_first_outgoing_audio"
    CACHE_LOOKUP_START = "cache_lookup_start"
    CACHE_LOOKUP_END = "cache_lookup_end"
    TTS_START = "tts_start"


def legal_reason_codes(outcome: Outcome) -> FrozenSet[ReasonCode]:
    """Reason codes allowed for ``outcome`` (empty set if the outcome is unknown)."""
    return OUTCOME_REASON_CODES.get(outcome, frozenset())


def validate_outcome_reason(outcome: Outcome, reason: Optional[ReasonCode]) -> None:
    """Raise ``ValueError`` if the pair is not a legal combination.

    Called by the outcome service and asserted by the tests, so a new outcome
    cannot quietly be saved with a meaningless reason.
    """
    if not isinstance(outcome, Outcome):
        raise ValueError(f"{outcome!r} is not an Outcome")
    allowed = legal_reason_codes(outcome)
    if not allowed:
        raise ValueError(f"outcome {outcome.value!r} has no declared reason codes")
    if reason is None:
        if outcome in DEFAULT_REASON_FOR_OUTCOME:
            return
        raise ValueError(
            f"outcome {outcome.value!r} requires a reason code from "
            f"{sorted(code.value for code in allowed)}"
        )
    if reason not in allowed:
        raise ValueError(
            f"reason {reason.value!r} is not valid for outcome {outcome.value!r}; "
            f"allowed: {sorted(code.value for code in allowed)}"
        )


def outcomes_from_values(values: Iterable[str]) -> FrozenSet[Outcome]:
    """Map stored strings back to outcomes, raising on anything unknown."""
    result = set()
    for value in values:
        try:
            result.add(Outcome(value))
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"unknown outcome stored in the database: {value!r}") from exc
    return frozenset(result)
