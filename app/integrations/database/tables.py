"""PostgreSQL is the durable source of truth. These are its tables.

Two constraints carry safety rather than convenience, and both are enforced by
the database, not only by Python (section 0.2):

* ``uq_attempt_per_task`` - a unique index on ``call_attempts.task_id``. A second
  attempt row for the same reminder task is structurally impossible, so no race
  between two workers, a restart, or a bug in the controller can produce the
  duplicate call the whole project is designed to avoid. ``NOT_DIALED`` is the
  documented exception: a request that provably never created a call may be
  replaced, which is why the row is *removed* rather than duplicated.
* the outcome triggers in :mod:`app.integrations.database.schema` - an outcome is
  written once and can never be overwritten.

There is deliberately **no transcript table and no recording table**. Stage 1
stores outcomes and metrics only (STORE_TRANSCRIPTS=false), and the absence of
the table is the strongest form of that decision.

A third table exists that the brief does not ask for by name: ``do_not_call``.
Section 23.8 requires that a parent's "do not call me again" suppresses future
tasks for that number, and that has to survive the call that recorded it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.enums import (
    AttemptStatus,
    ImportBatchStatus,
    ImportRowStatus,
    Outcome,
    ReasonCode,
    TaskStatus,
)

#: Explicit names make a migration diff readable and a constraint droppable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


def _enum_check(column: str, enum_cls, *, name: str) -> CheckConstraint:
    """A CHECK constraint built from a Python enum, so the two cannot drift."""
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return CheckConstraint(f"{column} IN ({values})", name=name)


def utc_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False)


# ------------------------------------------------------------------------- school
class School(Base):
    __tablename__ = "schools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    name_telugu: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    languages: Mapped[str] = mapped_column(String(64), nullable=False, default="te-IN,en-IN")
    template_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    #: A reference to the provider caller identity. Never the credential itself.
    caller_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()

    __table_args__ = (
        UniqueConstraint("name", name="uq_schools_name"),
    )


class DoNotCall(Base):
    """A permanent "do not call this number again" list (section 23.8).

    Keyed by phone number rather than student, because a parent asking not to be
    called must not be reached again through a sibling's row either.
    """

    __tablename__ = "do_not_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    source_attempt_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = utc_column()

    __table_args__ = (UniqueConstraint("phone", name="uq_do_not_call_phone"),)


# ------------------------------------------------------------------ batches/rows
class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    source_format: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    sheet_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    #: SHA-256 of the file bytes: uploading the same workbook twice is detected.
    file_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    header_map: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    unknown_columns: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    missing_columns: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    row_counts: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ImportBatchStatus.PARSED.value)
    created_at: Mapped[datetime] = utc_column()

    __table_args__ = (
        UniqueConstraint("file_checksum", name="uq_import_batches_file_checksum"),
        _enum_check("status", ImportBatchStatus, name="import_batch_status"),
    )


class ImportRowResult(Base):
    """The validation verdict for one spreadsheet row, kept for review/export."""

    __tablename__ = "import_row_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    student_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    parent_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    #: Masked at rest: a rejection report does not need the real number.
    parent_phone_masked: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    errors: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    warnings: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    duplicate_of: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = utc_column()

    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_import_row_batch_row"),
        _enum_check("status", ImportRowStatus, name="import_row_status"),
        Index("ix_import_row_results_batch_status", "batch_id", "status"),
    )


# ---------------------------------------------------------------------- students
class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id"), nullable=False)
    student_name: Mapped[str] = mapped_column(String(100), nullable=False)
    parent_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    parent_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_document: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    source_row: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    import_batch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("import_batches.id"), nullable=True
    )
    do_not_call: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()

    __table_args__ = (
        # The identity of a fee record: one student, one parent number, one school.
        UniqueConstraint(
            "school_id", "student_name", "parent_phone", name="uq_student_identity"
        ),
        CheckConstraint("amount IS NULL OR amount > 0", name="amount_positive"),
        Index("ix_students_parent_phone", "parent_phone"),
        Index("ix_students_school_id", "school_id"),
    )


# ----------------------------------------------------------------- reminder task
class ReminderTask(Base):
    __tablename__ = "reminder_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id"), nullable=False)
    reminder_type: Mapped[str] = mapped_column(String(32), nullable=False, default="fee_pending")
    #: The business key of the reminder (student + period). Re-running an import
    #: for the same month cannot create a second task for the same child.
    business_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=TaskStatus.NEW.value)
    import_batch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("import_batches.id"), nullable=True
    )
    blocked_reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()

    __table_args__ = (
        UniqueConstraint("business_key", name="uq_reminder_tasks_business_key"),
        _enum_check("status", TaskStatus, name="reminder_task_status"),
        Index("ix_reminder_tasks_status", "status"),
        Index("ix_reminder_tasks_student_id", "student_id"),
    )


# ----------------------------------------------------------------- call attempt
class CallAttempt(Base):
    __tablename__ = "call_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: UNIQUE is the no-duplicate-call guarantee. See the module docstring.
    task_id: Mapped[int] = mapped_column(ForeignKey("reminder_tasks.id"), nullable=False)
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("students.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False, default="exotel")
    provider_call_sid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    stream_sid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AttemptStatus.CREATED.value)
    dial_result: Mapped[str] = mapped_column(String(32), nullable=False, default="not_dialed")
    started_at: Mapped[datetime] = utc_column()
    dialed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pipeline_session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()

    __table_args__ = (
        UniqueConstraint("task_id", name="uq_attempt_per_task"),
        _enum_check("status", AttemptStatus, name="call_attempt_status"),
        CheckConstraint(
            "dial_result IN ('not_dialed', 'dialed', 'uncertain_needs_review')",
            name="call_attempt_dial_result",
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0", name="duration_non_negative"
        ),
        # One provider call can only ever belong to one attempt.
        UniqueConstraint("provider_call_sid", name="uq_call_attempts_provider_call_sid"),
        Index("ix_call_attempts_status", "status"),
    )


# ---------------------------------------------------------------------- outcome
class CallOutcome(Base):
    """The final result of an attempt - written once, never changed."""

    __tablename__ = "call_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: UNIQUE makes "set exactly once" structural, before the trigger even fires.
    attempt_id: Mapped[int] = mapped_column(ForeignKey("call_attempts.id"), nullable=False)
    task_id: Mapped[int] = mapped_column(ForeignKey("reminder_tasks.id"), nullable=False)
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("students.id"), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome_detail: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    # Structured results (section 7.9)
    promised_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    promised_date_text: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    callback_time_text: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    principal_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    already_paid_claim: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    do_not_call: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parent_declined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    financial_reason: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    illness_or_emergency: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    third_party_answered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    payment_date_questions_asked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    recorded_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()

    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_call_outcomes_attempt_id"),
        _enum_check("outcome", Outcome, name="call_outcome_outcome"),
        _enum_check("reason_code", ReasonCode, name="call_outcome_reason_code"),
        Index("ix_call_outcomes_outcome", "outcome"),
        Index("ix_call_outcomes_promised_date", "promised_date"),
        Index("ix_call_outcomes_recorded_at", "recorded_at"),
    )


# ---------------------------------------------------------------------- metrics
class CallMetric(Base):
    __tablename__ = "call_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("call_attempts.id"), nullable=False)
    task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("reminder_tasks.id"), nullable=True)
    call_duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    llm_completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tts_characters: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stt_seconds: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    audio_cache_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audio_cache_misses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tts_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_validation_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    database_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancellations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pipeline_startup_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Per-turn latency samples: {"turn": {"stage": value}}.
    latency_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    recorded_at: Mapped[datetime] = utc_column()

    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_call_metrics_attempt_id"),
        Index("ix_call_metrics_recorded_at", "recorded_at"),
    )


# ---------------------------------------------------------------- provider events
class ProviderEventRow(Base):
    """Deduplication ledger: a repeated webhook is recognised, not reprocessed."""

    __tablename__ = "provider_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False, default="exotel")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_call_sid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[Optional[int]] = mapped_column(ForeignKey("call_attempts.id"), nullable=True)
    #: A digest, not the payload: enough to tell events apart, nothing to leak.
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    received_at: Mapped[datetime] = utc_column()
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("event_key", name="uq_provider_events_event_key"),
        Index("ix_provider_events_attempt_id", "attempt_id"),
        Index("ix_provider_events_received_at", "received_at"),
    )


# --------------------------------------------------------------- response cache
class ResponseTemplate(Base):
    """Approved text. Section 13.7 limits *audio* caching, not templates."""

    __tablename__ = "response_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="te-IN")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    placeholders: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    #: True only for the phrases allowed to be pre-rendered (opener, closings,
    #: silence check-in). Everything else must be generated live.
    cacheable_audio: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()

    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_response_template_key_version"),
        Index("ix_response_templates_key", "key"),
    )


class AudioAsset(Base):
    """Metadata for a cached audio file. The bytes live on disk, not in the DB."""

    __tablename__ = "audio_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(200), nullable=False)
    template_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="te-IN")
    voice: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    audio_format: Mapped[str] = mapped_column(String(16), nullable=False, default="pcm")
    sample_rate: Mapped[int] = mapped_column(Integer, nullable=False, default=8000)
    storage_path: Mapped[str] = mapped_column(String(400), nullable=False)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_checksum: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    template_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = utc_column()
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_audio_assets_cache_key"),
        Index("ix_audio_assets_template_key", "template_key"),
    )


# ------------------------------------------------------------------- audit trail
class AuditLog(Base):
    """Who did what to the system: imports, dialing, cancellations, config."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = utc_column()
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="operator")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (Index("ix_audit_log_at", "at"),)


#: Every table, in dependency order. Used by the migration and by the tests.
ALL_TABLES = (
    School,
    DoNotCall,
    ImportBatch,
    ImportRowResult,
    Student,
    ReminderTask,
    CallAttempt,
    CallOutcome,
    CallMetric,
    ProviderEventRow,
    ResponseTemplate,
    AudioAsset,
    AuditLog,
)
