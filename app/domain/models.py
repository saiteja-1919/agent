"""Domain entities for Veda.

Plain, validated data objects with no database or provider knowledge. The
persistence layer maps these to tables, the conversation controller reasons
about them, and the tests construct them directly.

Money is a :class:`~decimal.Decimal`, never a float: a fee amount must never
pick up binary rounding error on its way to a parent's ear.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from .enums import (
    ActionNeeded,
    DialResult,
    ImportRowStatus,
    Intent,
    LatencyStage,
    Outcome,
    ReasonCode,
    ResponseSource,
    TaskStatus,
)


def utcnow() -> datetime:
    """Timezone-aware UTC now. Every durable timestamp comes from here."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- references
@dataclass(frozen=True)
class ProviderCallRef:
    """The provider's identifiers for one outbound call."""

    provider: str
    call_sid: str
    provider_call_id: Optional[str] = None   # Exotel "CallSid" in callbacks
    stream_sid: Optional[str] = None


# ------------------------------------------------------------------------ school
@dataclass(frozen=True)
class SchoolConfig:
    """Everything school-specific comes from here - never from a spreadsheet.

    Appendix A pitfall 12: a hard-coded school name leaked into real calls, so
    the school name is required, validated at startup, and refused if it still
    looks like a placeholder.
    """

    school_id: int = 1
    name: str = ""                      # as typed by the office (English letters)
    name_telugu: str = ""               # as spoken (Telugu script)
    timezone: str = "Asia/Kolkata"
    languages: Tuple[str, ...] = ("te-IN", "en-IN")
    max_attempts_per_task: int = 1
    caller_id: Optional[str] = None
    template_version: str = "v1"

    @property
    def spoken_name(self) -> str:
        """The spelling a voice should read for the school."""
        return self.name_telugu or self.name


# ------------------------------------------------------------- verified call facts
@dataclass(frozen=True)
class VerifiedFacts:
    """Exactly what Veda is allowed to treat as true on this call.

    This object is the *only* source of student, fee and date facts. It is built
    from database rows (never from a provider payload or model output) and is
    what the prompt renderer receives, so an unverified number can never reach
    the voice.

    ``already_spoken`` records the scripted lines that have already been played,
    because the binding clarification requires the model never to guess whether
    the opener happened.
    """

    school_name: str                     # Telugu script
    student_name: str                    # Telugu script
    parent_name: str = ""                # Telugu script, may be missing
    amount_words: str = ""               # English words, e.g. "five thousand rupees"
    amount: Optional[Decimal] = None
    due_date_words: str = ""             # English words, no digits
    today_words: str = ""                # English words, no digits
    student_notes: str = ""              # cleaned, capped, untrusted but non-instructive
    already_spoken: Tuple[str, ...] = ()

    #: Set by the controller, not the model: the identity gate lives in Python.
    identity_confirmed: bool = False
    payment_date_questions_asked: int = 0

    def with_identity_confirmed(self) -> "VerifiedFacts":
        return replace(self, identity_confirmed=True)

    def with_payment_date_question(self) -> "VerifiedFacts":
        return replace(
            self, payment_date_questions_asked=self.payment_date_questions_asked + 1
        )

    def has_fee_facts(self) -> bool:
        """False when the amount is missing or unusable.

        section 16.1 step two: "Do not state an amount if it is missing,
        invalid, or inconsistent."
        """
        return bool(self.amount_words) and (self.amount is None or self.amount > 0)


# ------------------------------------------------------------------ reminder task
@dataclass
class ReminderTask:
    """One fee reminder for one student. The unit that must never be double-dialled."""

    id: Optional[int] = None
    student_id: Optional[int] = None
    school_id: int = 1
    reminder_type: str = "fee_pending"
    business_key: str = ""               # e.g. "2026-10_fee:S-1001" - unique
    status: TaskStatus = TaskStatus.NEW
    import_batch_id: Optional[int] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    blocked_reason: Optional[str] = None


# -------------------------------------------------------------------------- student
@dataclass
class StudentRecord:
    """The minimum data needed to make and complete one reminder call."""

    id: Optional[int] = None
    school_id: int = 1
    student_name: str = ""
    parent_name: str = ""
    parent_phone: str = ""
    amount: Optional[Decimal] = None
    currency: str = "INR"
    due_date: Optional[date] = None
    notes: str = ""
    source_document: str = ""
    source_row: Optional[int] = None
    import_batch_id: Optional[int] = None
    do_not_call: bool = False
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


# --------------------------------------------------------------------- call attempt
@dataclass
class CallAttempt:
    """One outbound attempt. At most one may exist per task (DB-enforced)."""

    id: Optional[int] = None
    task_id: Optional[int] = None
    student_id: Optional[int] = None
    provider: str = "exotel"
    provider_call_sid: Optional[str] = None
    stream_sid: Optional[str] = None
    status: str = "created"
    dial_result: DialResult = DialResult.NOT_DIALED
    started_at: datetime = field(default_factory=utcnow)
    dialed_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    pipeline_session_id: Optional[str] = None
    prompt_version: str = "v1"
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


# ---------------------------------------------------------------------- outcome
@dataclass(frozen=True)
class PaymentCommitment:
    """A date the parent promised, recorded only after reliable extraction."""

    promised_date: Optional[date] = None
    raw_text: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class CallbackRequest:
    """A request to be called later. Recorded, never acted on by the model."""

    callback_time_text: str = ""
    callback_date: Optional[date] = None
    from_third_party: bool = False


@dataclass(frozen=True)
class CallOutcomeRecord:
    """The final, immutable result of an attempt (sections 7.9 and 18)."""

    attempt_id: Optional[int] = None
    task_id: Optional[int] = None
    student_id: Optional[int] = None
    outcome: Optional[Outcome] = None
    reason_code: Optional[ReasonCode] = None
    outcome_detail: str = ""             # short, non-sensitive, free text

    promised_date: Optional[date] = None
    promised_date_text: str = ""
    callback_time_text: str = ""
    principal_request: bool = False
    already_paid_claim: bool = False
    do_not_call: bool = False
    parent_declined: bool = False
    financial_reason: bool = False
    illness_or_emergency: bool = False
    third_party_answered: bool = False

    payment_date_questions_asked: int = 0
    reply_count: int = 0
    prompt_version: str = "v1"
    recorded_at: datetime = field(default_factory=utcnow)

    @property
    def action_needed(self) -> ActionNeeded:
        """What the office should do next - drives the staff export."""
        if self.outcome is Outcome.DO_NOT_CALL:
            return ActionNeeded.REMOVE_FROM_CALLING
        if self.outcome is Outcome.UNCERTAIN_NEEDS_REVIEW:
            return ActionNeeded.REVIEW_DIAL_RESULT
        if self.outcome is Outcome.WRONG_NUMBER:
            return ActionNeeded.CHECK_WRONG_NUMBER
        if self.principal_request:
            return ActionNeeded.CALL_PRINCIPAL_INFORM
        if self.already_paid_claim:
            return ActionNeeded.CHECK_PAYMENT_RECORD
        return ActionNeeded.NONE


# ------------------------------------------------------------------------ latency
@dataclass
class LatencyTrace:
    """Monotonic-clock latency trace for one conversational turn (section 15).

    Monotonic values are used for the *differences* so a system clock change
    cannot produce a negative latency; UTC wall-clock is stored alongside for
    durable records.
    """

    attempt_id: Optional[int] = None
    turn_index: int = 0
    monotonic: Dict[str, float] = field(default_factory=dict)
    wall_clock: Dict[str, datetime] = field(default_factory=dict)

    def mark(self, stage: LatencyStage, *, monotonic: Optional[float] = None) -> None:
        """Record the moment a stage happened (idempotent: first mark wins)."""
        key = stage.value
        if key in self.monotonic:
            return
        self.monotonic[key] = time.monotonic() if monotonic is None else monotonic
        self.wall_clock[key] = utcnow()

    @property
    def has(self) -> bool:
        return bool(self.monotonic)

    @property
    def t0_present(self) -> bool:
        return LatencyStage.T0_LAST_PARENT_AUDIO.value in self.monotonic

    def delta_ms(self, start: LatencyStage, end: LatencyStage) -> Optional[int]:
        """Milliseconds between two marks, or ``None`` when either is missing.

        ``None`` is deliberate: section 15 forbids inventing a measurement for a
        stage that did not occur (a cache hit has no LLM token time).
        """
        first = self.monotonic.get(start.value)
        second = self.monotonic.get(end.value)
        if first is None or second is None:
            return None
        return int(round((second - first) * 1000))

    def derived(self) -> Dict[str, Optional[int]]:
        """The named latencies the brief asks for, with honest ``None`` gaps."""
        from .enums import LatencyStage as L

        turn_detection = self.delta_ms(L.T0_LAST_PARENT_AUDIO, L.T1_END_OF_TURN)
        stt_final = self.delta_ms(L.T1_END_OF_TURN, L.T2_FINAL_TRANSCRIPT)
        llm_first = self.delta_ms(L.T2_FINAL_TRANSCRIPT, L.T3_FIRST_LLM_TOKEN)
        tts_start = self.delta_ms(L.T3_FIRST_LLM_TOKEN, L.T4_FIRST_TTS_AUDIO)
        transport = self.delta_ms(L.T4_FIRST_TTS_AUDIO, L.T5_FIRST_OUTGOING_AUDIO)
        cache = self.delta_ms(L.CACHE_LOOKUP_START, L.CACHE_LOOKUP_END)

        # End-to-end uses whichever start is available, and never fabricates one.
        response_start: Optional[int] = None
        if self.t0_present:
            for end_stage in (L.T5_FIRST_OUTGOING_AUDIO, L.T4_FIRST_TTS_AUDIO):
                value = self.delta_ms(L.T0_LAST_PARENT_AUDIO, end_stage)
                if value is not None:
                    response_start = value
                    break
        return {
            "turn_detection_ms": turn_detection,
            "stt_finalization_ms": stt_final,
            "llm_first_token_ms": llm_first,
            "tts_startup_ms": tts_start,
            "outgoing_transport_ms": transport,
            "cache_lookup_ms": cache,
            "response_start_ms": response_start,
        }


@dataclass
class CallMetrics:
    """Performance and usage counters for one attempt (section 15)."""

    attempt_id: Optional[int] = None
    task_id: Optional[int] = None
    call_duration_seconds: Optional[int] = None
    turn_count: int = 0
    reply_count: int = 0
    llm_prompt_tokens: Optional[int] = None
    llm_completion_tokens: Optional[int] = None
    tts_characters: Optional[int] = None
    stt_seconds: Optional[float] = None
    audio_cache_hits: int = 0
    audio_cache_misses: int = 0
    tts_calls: int = 0
    cache_validation_failures: int = 0
    provider_failures: int = 0
    database_failures: int = 0
    cancellations: int = 0
    pipeline_startup_failures: int = 0
    latency: List[LatencyTrace] = field(default_factory=list)
    recorded_at: datetime = field(default_factory=utcnow)

    def bump(self, name: str, amount: int = 1) -> None:
        """Increment a counter; unknown names raise so typos cannot hide."""
        if not hasattr(self, name) or not isinstance(getattr(self, name), int):
            raise AttributeError(f"{name!r} is not a counter on CallMetrics")
        setattr(self, name, getattr(self, name) + amount)


# ------------------------------------------------------------------------ import
@dataclass(frozen=True)
class ImportRowResult:
    """The validation verdict for one spreadsheet row."""

    row_number: int
    status: ImportRowStatus
    student_name: str = ""
    parent_name: str = ""
    parent_phone_masked: str = ""
    amount: Optional[Decimal] = None
    due_date: Optional[date] = None
    notes: str = ""
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    duplicate_of: Optional[int] = None

    @property
    def is_valid(self) -> bool:
        return self.status is ImportRowStatus.VALID

    @property
    def is_dialable(self) -> bool:
        return self.status is ImportRowStatus.VALID


@dataclass(frozen=True)
class ImportReport:
    """Outcome of parsing and validating one uploaded file (dry-run output)."""

    source_path: str = ""
    source_format: str = ""
    sheet_name: str = ""
    header_map: Dict[str, str] = field(default_factory=dict)
    unknown_columns: Tuple[str, ...] = ()
    missing_columns: Tuple[str, ...] = ()
    rows: Tuple[ImportRowResult, ...] = ()
    duplicate_file: bool = False
    content_checksum: str = ""

    @property
    def valid_rows(self) -> Tuple[ImportRowResult, ...]:
        return tuple(row for row in self.rows if row.is_valid)

    @property
    def rejected_rows(self) -> Tuple[ImportRowResult, ...]:
        return tuple(row for row in self.rows if row.status is ImportRowStatus.REJECTED)

    @property
    def duplicate_rows(self) -> Tuple[ImportRowResult, ...]:
        return tuple(row for row in self.rows if row.status is ImportRowStatus.DUPLICATE)

    @property
    def counts(self) -> Dict[str, int]:
        counts = {status.value: 0 for status in ImportRowStatus}
        for row in self.rows:
            counts[row.status.value] += 1
        return counts

    def is_importable(self) -> bool:
        """A file is importable when at least one row is valid, and it is not a
        byte-identical re-upload of a file already imported."""
        return bool(self.valid_rows) and not self.duplicate_file


# --------------------------------------------------------------- turn-time context
@dataclass(frozen=True)
class TurnContext:
    """Everything the controller needs to decide one reply.

    Built per turn and discarded. ``last_parent_text`` is live transcript text:
    it is never persisted (STORE_TRANSCRIPTS=false) and never logged.
    """

    facts: VerifiedFacts
    phase: str = "listening"
    last_parent_text: str = ""
    previous_reply_opening: str = ""
    acknowledgement_words_used: Tuple[str, ...] = ()
    clarification_asked_for_date: bool = False
    off_topic_turns: int = 0
    silence_prompts: int = 0


@dataclass(frozen=True)
class ReplyDecision:
    """A reply the controller has decided to say, plus why."""

    text: str = ""
    source: ResponseSource = ResponseSource.LLM
    intent: Intent = Intent.UNKNOWN
    is_question: bool = False
    asks_payment_date: bool = False
    end_call_after: bool = False
    cache_audio_key: Optional[str] = None
    outcome: Optional[Outcome] = None
    reason_code: Optional[ReasonCode] = None


@dataclass(frozen=True)
class ConversationTurn:
    """One finalized parent turn plus the controller's decision."""

    index: int
    parent_text: str = ""
    intent: Intent = Intent.UNKNOWN
    intent_confidence: float = 0.0
    decision: Optional[ReplyDecision] = None
    latency: Optional[LatencyTrace] = None


@dataclass
class ConversationState:
    """Mutable per-call conversation memory. One instance per call, never shared.

    Section 20: "Do not share mutable per-call state between calls."
    """

    attempt_id: Optional[int] = None
    task_id: Optional[int] = None
    facts: VerifiedFacts = field(default_factory=lambda: VerifiedFacts("", ""))
    turns: List[ConversationTurn] = field(default_factory=list)
    reply_openings: List[str] = field(default_factory=list)
    acknowledgement_words_used: List[str] = field(default_factory=list)
    payment_date_questions_asked: int = 0
    clarification_for_date_asked: bool = False
    off_topic_turns: int = 0
    silence_prompts: int = 0
    identity_confirmed: bool = False
    promised: PaymentCommitment = field(default_factory=PaymentCommitment)
    callback: CallbackRequest = field(default_factory=CallbackRequest)
    principal_request: bool = False
    already_paid_claim: bool = False
    do_not_call: bool = False
    parent_declined: bool = False
    financial_reason: bool = False
    illness_or_emergency: bool = False
    third_party_answered: bool = False
    cached_audio_keys_used: List[str] = field(default_factory=list)
    prompt_version: str = "v1"

    @property
    def reply_count(self) -> int:
        return len(self.reply_openings)

    def remember_reply(self, text: str, acknowledgement: Sequence[str] = ()) -> None:
        opening = _first_word(text)
        if opening:
            self.reply_openings.append(opening)
        for word in acknowledgement:
            if word and word not in self.acknowledgement_words_used:
                self.acknowledgement_words_used.append(word)

    @property
    def previous_opening(self) -> str:
        return self.reply_openings[-1] if self.reply_openings else ""

    def to_outcome_record(self) -> CallOutcomeRecord:
        """Snapshot the structured results for persistence (section 7.9)."""
        return CallOutcomeRecord(
            attempt_id=self.attempt_id,
            task_id=self.task_id,
            promised_date=self.promised.promised_date,
            promised_date_text=self.promised.raw_text,
            callback_time_text=self.callback.callback_time_text,
            principal_request=self.principal_request,
            already_paid_claim=self.already_paid_claim,
            do_not_call=self.do_not_call,
            parent_declined=self.parent_declined,
            financial_reason=self.financial_reason,
            illness_or_emergency=self.illness_or_emergency,
            third_party_answered=self.third_party_answered,
            payment_date_questions_asked=self.payment_date_questions_asked,
            reply_count=self.reply_count,
            prompt_version=self.prompt_version,
        )


_WORD_SPLIT_EXTRAS = ".,!?;:\u0964\u3002\u0c7c\u0c7d"


def _first_word(text: str) -> str:
    """First spoken word, punctuation stripped, for the repetition rule."""
    stripped = (text or "").strip()
    for char in _WORD_SPLIT_EXTRAS:
        stripped = stripped.replace(char, " ")
    parts = [part for part in stripped.split() if part]
    return parts[0].lower() if parts else ""


def first_word(text: str) -> str:
    """Public helper used by the controller and its tests."""
    return _first_word(text)
