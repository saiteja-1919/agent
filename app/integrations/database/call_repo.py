"""Repositories for the call lifecycle: attempts, outcomes, metrics, events.

Three properties are enforced here rather than hoped for:

* **One attempt per task.** :func:`create_attempt` relies on the
  ``uq_attempt_per_task`` unique index and turns the integrity error into
  :class:`DuplicateAttemptError`. Python never gets to decide.
* **Outcomes are written once.** :func:`record_outcome` inserts and, if a row is
  already there, returns it unchanged. It never issues an UPDATE, and the
  database trigger would reject one anyway.
* **An uncertain dial is never forgotten.** :func:`set_dial_result` refuses to
  overwrite an uncertain or successful result with ``NOT_DIALED``, so a late or
  duplicated callback cannot turn "we may have called this parent" into "we
  definitely did not", which is the one mistake that leads to a duplicate call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    LIFECYCLE_RANK,
    ActionNeeded,
    AttemptStatus,
    CallLifecycleState,
    DialResult,
    Outcome,
    ProviderEventKind,
    ReasonCode,
    validate_outcome_reason,
)
from app.domain.events import ProviderEvent
from app.domain.models import CallAttempt as CallAttemptModel
from app.domain.models import CallMetrics, CallOutcomeRecord, LatencyTrace, as_utc, utcnow

from .tables import CallAttempt, CallMetric, CallOutcome, ProviderEventRow, ReminderTask, Student


class DuplicateAttemptError(RuntimeError):
    """A second call attempt for the same reminder task was refused."""


class OutcomeAlreadyRecorded(RuntimeError):
    """An outcome already exists and cannot be replaced."""


# ------------------------------------------------------------------- attempols
async def create_attempt(
    session: AsyncSession,
    *,
    task_id: int,
    student_id: Optional[int] = None,
    provider: str = "exotel",
    prompt_version: str = "v1",
    pipeline_session_id: Optional[str] = None,
) -> CallAttempt:
    """Create the single attempt row for a task.

    Raises :class:`DuplicateAttemptError` when one already exists. Callers must
    treat that as "do not dial"; there is no code path that dials anyway.
    """
    attempt = CallAttempt(
        task_id=task_id,
        student_id=student_id,
        provider=provider,
        status=AttemptStatus.CREATED.value,
        dial_result=DialResult.NOT_DIALED.value,
        prompt_version=prompt_version,
        pipeline_session_id=pipeline_session_id,
        started_at=utcnow(),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    try:
        # A savepoint, not a full rollback: an integrity error here must not
        # discard work the caller has already done in this transaction (the
        # claimed task status, for example).
        async with session.begin_nested():
            session.add(attempt)
            await session.flush()
    except IntegrityError as exc:
        raise DuplicateAttemptError(
            f"reminder task {task_id} already has a call attempt; refusing to create another"
        ) from exc
    return attempt


async def get_attempt(session: AsyncSession, attempt_id: int) -> Optional[CallAttempt]:
    return (
        await session.execute(select(CallAttempt).where(CallAttempt.id == attempt_id))
    ).scalar_one_or_none()


async def get_attempt_for_task(session: AsyncSession, task_id: int) -> Optional[CallAttempt]:
    return (
        await session.execute(select(CallAttempt).where(CallAttempt.task_id == task_id))
    ).scalar_one_or_none()


async def get_attempt_by_provider_sid(
    session: AsyncSession, provider_call_sid: str
) -> Optional[CallAttempt]:
    """Resolve a provider callback to an internal attempt.

    A provider call id that matches nothing must never be acted on (section 17.2).
    """
    if not provider_call_sid:
        return None
    statement = select(CallAttempt).where(CallAttempt.provider_call_sid == provider_call_sid)
    return (await session.execute(statement)).scalar_one_or_none()


async def delete_attempt_if_undialed(session: AsyncSession, task_id: int) -> bool:
    """Remove a proven ``NOT_DIALED`` attempt so a corrected retry is possible.

    This is the one documented exception to "no second attempt". It is only
    allowed when the dial request provably never created a call, and it is never
    allowed once an outcome exists.
    """
    attempt = await get_attempt_for_task(session, task_id)
    if attempt is None:
        return False
    if attempt.dial_result != DialResult.NOT_DIALED.value:
        return False
    existing_outcome = (
        await session.execute(select(CallOutcome.id).where(CallOutcome.attempt_id == attempt.id))
    ).scalar_one_or_none()
    if existing_outcome is not None:
        return False
    await session.delete(attempt)
    await session.flush()
    return True


async def set_dial_result(
    session: AsyncSession,
    attempt_id: int,
    *,
    dial_result: DialResult,
    provider_call_sid: Optional[str] = None,
    failure_reason: str = "",
    pipeline_session_id: Optional[str] = None,
) -> bool:
    """Record how the dial request turned out, without ever downgrading it."""
    attempt = await get_attempt(session, attempt_id)
    if attempt is None:
        return False

    current = DialResult(attempt.dial_result)
    if current is not DialResult.NOT_DIALED and dial_result is DialResult.NOT_DIALED:
        # A late event must not erase the fact that a call may have happened.
        return False
    if current is DialResult.UNCERTAIN_NEEDS_REVIEW and dial_result is DialResult.DIALED:
        # Learning the call did happen is an upgrade, and is allowed.
        pass

    values: Dict[str, object] = {
        "dial_result": dial_result.value,
        "updated_at": utcnow(),
    }
    if provider_call_sid:
        values["provider_call_sid"] = provider_call_sid
    if pipeline_session_id:
        values["pipeline_session_id"] = pipeline_session_id
    if dial_result is DialResult.DIALED:
        values["dialed_at"] = attempt.dialed_at or utcnow()
        values["status"] = AttemptStatus.DIALING.value
    if dial_result is DialResult.UNCERTAIN_NEEDS_REVIEW:
        values["status"] = AttemptStatus.UNCERTAIN.value
    if failure_reason:
        # Kept out of logs; stored only as a short internal category.
        values["pipeline_session_id"] = attempt.pipeline_session_id

    await session.execute(update(CallAttempt).where(CallAttempt.id == attempt_id).values(**values))
    return True


async def bind_stream_sid(session: AsyncSession, attempt_id: int, stream_sid: str) -> bool:
    statement = (
        update(CallAttempt)
        .where(CallAttempt.id == attempt_id)
        .values(stream_sid=stream_sid, updated_at=utcnow())
        .returning(CallAttempt.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def record_lifecycle_state(
    session: AsyncSession, attempt_id: int, state: CallLifecycleState
) -> bool:
    """Move an attempt forward. Out-of-order and post-terminal events are ignored."""
    attempt = await get_attempt(session, attempt_id)
    if attempt is None:
        return False
    try:
        current_state = CallLifecycleState(attempt.status)
    except ValueError:
        # 'uncertain' is not a lifecycle state; treat it as the last known one.
        current_state = CallLifecycleState.DIALING

    if current_state is CallLifecycleState.ENDED:
        return False
    if LIFECYCLE_RANK[state] <= LIFECYCLE_RANK[current_state]:
        return False

    values: Dict[str, object] = {"status": state.value, "updated_at": utcnow()}
    if state is CallLifecycleState.ANSWERED and attempt.answered_at is None:
        values["answered_at"] = utcnow()
    if state is CallLifecycleState.ENDED:
        values["ended_at"] = utcnow()
        started = as_utc(attempt.started_at)
        if started is not None:
            reference = as_utc(attempt.answered_at) or started
            values["duration_seconds"] = max(0, int((utcnow() - reference).total_seconds()))

    await session.execute(update(CallAttempt).where(CallAttempt.id == attempt_id).values(**values))
    return True


async def set_attempt_duration(session: AsyncSession, attempt_id: int, seconds: int) -> None:
    await session.execute(
        update(CallAttempt)
        .where(CallAttempt.id == attempt_id)
        .values(duration_seconds=max(0, seconds), updated_at=utcnow())
    )


# --------------------------------------------------------------------- outcomes
async def record_outcome(
    session: AsyncSession, record: CallOutcomeRecord
) -> Tuple[CallOutcome, bool]:
    """Write the final outcome exactly once.

    Returns ``(row, created)``. ``created=False`` means an outcome was already
    recorded and the existing row is returned untouched - finalization is
    therefore safe to call twice (section 19.3), and a duplicate provider event
    cannot rewrite history.
    """
    if record.attempt_id is None:
        raise ValueError("an outcome needs an attempt_id")
    if record.task_id is None:
        raise ValueError("an outcome needs a task_id")
    if record.outcome is None:
        raise ValueError("an outcome needs an outcome value")
    validate_outcome_reason(record.outcome, record.reason_code)

    existing = (
        await session.execute(select(CallOutcome).where(CallOutcome.attempt_id == record.attempt_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    row = CallOutcome(
        attempt_id=record.attempt_id,
        task_id=record.task_id,
        student_id=record.student_id,
        outcome=record.outcome.value,
        reason_code=record.reason_code.value if record.reason_code else "",
        outcome_detail=record.outcome_detail[:200],
        promised_date=record.promised_date,
        promised_date_text=record.promised_date_text[:120],
        callback_time_text=record.callback_time_text[:120],
        principal_request=record.principal_request,
        already_paid_claim=record.already_paid_claim,
        do_not_call=record.do_not_call,
        parent_declined=record.parent_declined,
        financial_reason=record.financial_reason,
        illness_or_emergency=record.illness_or_emergency,
        third_party_answered=record.third_party_answered,
        payment_date_questions_asked=record.payment_date_questions_asked,
        reply_count=record.reply_count,
        prompt_version=record.prompt_version,
        recorded_at=record.recorded_at,
        updated_at=utcnow(),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # Two finalizers raced; the first write wins and is returned untouched.
        existing = (
            await session.execute(
                select(CallOutcome).where(CallOutcome.attempt_id == record.attempt_id)
            )
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover - defensive
            raise
        return existing, False
    return row, True


async def get_outcome(session: AsyncSession, attempt_id: int) -> Optional[CallOutcome]:
    return (
        await session.execute(select(CallOutcome).where(CallOutcome.attempt_id == attempt_id))
    ).scalar_one_or_none()


async def get_outcome_for_task(session: AsyncSession, task_id: int) -> Optional[CallOutcome]:
    return (
        await session.execute(select(CallOutcome).where(CallOutcome.task_id == task_id))
    ).scalar_one_or_none()


# ---------------------------------------------------------------------- metrics
async def save_metrics(session: AsyncSession, metrics: CallMetrics) -> CallMetric:
    """Insert or update the metrics row for an attempt.

    Metrics are measurements, not decisions, so updating them is safe where
    updating an outcome is not. Repeated saves are idempotent by attempt.
    """
    if metrics.attempt_id is None:
        raise ValueError("metrics need an attempt_id")
    existing = (
        await session.execute(select(CallMetric).where(CallMetric.attempt_id == metrics.attempt_id))
    ).scalar_one_or_none()

    import json

    payload = json.dumps([trace.derived() | {"turn_index": trace.turn_index} for trace in metrics.latency])

    values = dict(
        call_duration_seconds=metrics.call_duration_seconds,
        turn_count=metrics.turn_count,
        reply_count=metrics.reply_count,
        llm_prompt_tokens=metrics.llm_prompt_tokens,
        llm_completion_tokens=metrics.llm_completion_tokens,
        tts_characters=metrics.tts_characters,
        stt_seconds=metrics.stt_seconds,
        audio_cache_hits=metrics.audio_cache_hits,
        audio_cache_misses=metrics.audio_cache_misses,
        tts_calls=metrics.tts_calls,
        cache_validation_failures=metrics.cache_validation_failures,
        provider_failures=metrics.provider_failures,
        database_failures=metrics.database_failures,
        cancellations=metrics.cancellations,
        pipeline_startup_failures=metrics.pipeline_startup_failures,
        latency_json=payload,
    )
    if existing is None:
        row = CallMetric(
            attempt_id=metrics.attempt_id, task_id=metrics.task_id, recorded_at=utcnow(), **values
        )
        session.add(row)
        await session.flush()
        return row

    await session.execute(update(CallMetric).where(CallMetric.id == existing.id).values(**values))
    return existing


async def get_metrics(session: AsyncSession, attempt_id: int) -> Optional[CallMetric]:
    return (
        await session.execute(select(CallMetric).where(CallMetric.attempt_id == attempt_id))
    ).scalar_one_or_none()


# --------------------------------------------------------------- provider events
async def record_provider_event(
    session: AsyncSession,
    event: ProviderEvent,
    *,
    attempt_id: Optional[int] = None,
    payload_digest: str = "",
) -> bool:
    """Store an event's identity. Returns False when it is a duplicate.

    The digest, not the payload, is stored: enough to tell two events apart,
    nothing that could leak personal data into the database.
    """
    row = ProviderEventRow(
        event_key=event.dedupe_key(),
        provider=event.provider,
        kind=event.kind.value if isinstance(event.kind, ProviderEventKind) else str(event.kind),
        provider_call_sid=event.provider_call_sid,
        attempt_id=attempt_id,
        payload_digest=payload_digest,
        received_at=event.received_at,
    )
    try:
        # Savepoint: recognising a duplicate must not roll back the rest of the
        # transaction (the attempt lookup, the lifecycle change, the outcome).
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return False
    return True


async def mark_event_processed(session: AsyncSession, event_key: str) -> None:
    await session.execute(
        update(ProviderEventRow)
        .where(ProviderEventRow.event_key == event_key)
        .values(processed_at=utcnow())
    )


async def seen_event(session: AsyncSession, event_key: str) -> bool:
    statement = select(ProviderEventRow.id).where(ProviderEventRow.event_key == event_key)
    return (await session.execute(statement)).scalar_one_or_none() is not None


# ------------------------------------------------------------- recovery queries
async def stale_attempts(session: AsyncSession, *, older_than_minutes: int = 30) -> List[CallAttempt]:
    """Attempts that were left non-terminal - a process died mid-call.

    Section 19.2: these must be reconciled, never assumed to be "no call
    happened", and never redialled automatically.
    """
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    statement = (
        select(CallAttempt)
        .where(
            CallAttempt.status.notin_([AttemptStatus.ENDED.value, AttemptStatus.UNCERTAIN.value]),
            CallAttempt.started_at < cutoff,
        )
        .order_by(CallAttempt.id)
    )
    return list((await session.execute(statement)).scalars().all())


async def open_attempt_count(session: AsyncSession) -> int:
    statement = select(func.count(CallAttempt.id)).where(
        CallAttempt.status.in_(
            [
                AttemptStatus.CREATED.value,
                AttemptStatus.DIALING.value,
                AttemptStatus.RINGING.value,
                AttemptStatus.ANSWERED.value,
                AttemptStatus.IN_PROGRESS.value,
                AttemptStatus.ENDING.value,
            ]
        )
    )
    return int((await session.execute(statement)).scalar() or 0)


# --------------------------------------------------------------------- reporting
@dataclass(frozen=True)
class ExportRow:
    """One line of the office-facing staff export.

    The phone number is masked: the office recognises the row from the student
    name and the last four digits, and the export is a file that leaves the
    machine (section 17.7 / 17.8).
    """

    attempt_id: int
    task_id: int
    student_name: str
    parent_name: str
    parent_phone_masked: str
    outcome: str
    reason_code: str
    promised_date: Optional[object]
    callback_time_text: str
    principal_request: bool
    already_paid_claim: bool
    do_not_call: bool
    action_needed: str
    recorded_at: Optional[datetime]


async def export_rows(
    session: AsyncSession,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 5000,
) -> List[ExportRow]:
    """The staff export: promised dates, callbacks and principal requests."""
    statement = (
        select(
            CallOutcome.attempt_id,
            CallOutcome.task_id,
            Student.student_name,
            Student.parent_name,
            Student.parent_phone,
            CallOutcome.outcome,
            CallOutcome.reason_code,
            CallOutcome.promised_date,
            CallOutcome.callback_time_text,
            CallOutcome.principal_request,
            CallOutcome.already_paid_claim,
            CallOutcome.do_not_call,
            CallOutcome.recorded_at,
        )
        .join(CallAttempt, CallAttempt.id == CallOutcome.attempt_id)
        .join(Student, Student.id == CallAttempt.student_id, isouter=True)
        .order_by(CallOutcome.recorded_at.desc())
        .limit(limit)
    )
    if since is not None:
        statement = statement.where(CallOutcome.recorded_at >= since)
    if until is not None:
        statement = statement.where(CallOutcome.recorded_at < until)

    from app.domain.policies import mask_phone

    rows = (await session.execute(statement)).all()
    results: List[ExportRow] = []
    for row in rows:
        record = CallOutcomeRecord(
            outcome=Outcome(row[5]),
            reason_code=ReasonCode(row[6]) if row[6] else None,
            promised_date=row[7],
            callback_time_text=row[8] or "",
            principal_request=bool(row[9]),
            already_paid_claim=bool(row[10]),
            do_not_call=bool(row[11]),
        )
        results.append(
            ExportRow(
                attempt_id=row[0],
                task_id=row[1],
                student_name=row[2] or "",
                parent_name=row[3] or "",
                parent_phone_masked=mask_phone(row[4]),
                outcome=row[5],
                reason_code=row[6],
                promised_date=row[7],
                callback_time_text=row[8] or "",
                principal_request=bool(row[9]),
                already_paid_claim=bool(row[10]),
                do_not_call=bool(row[11]),
                action_needed=record.action_needed.value,
                recorded_at=row[12],
            )
        )
    return results


async def outcome_counts(session: AsyncSession) -> Dict[str, int]:
    statement = select(CallOutcome.outcome, func.count(CallOutcome.id)).group_by(CallOutcome.outcome)
    rows = (await session.execute(statement)).all()
    return {row[0]: row[1] for row in rows}


async def reason_counts(session: AsyncSession) -> Dict[str, int]:
    statement = select(CallOutcome.reason_code, func.count(CallOutcome.id)).group_by(
        CallOutcome.reason_code
    )
    rows = (await session.execute(statement)).all()
    return {row[0]: row[1] for row in rows}


async def latency_samples(session: AsyncSession) -> List[Dict[str, Optional[int]]]:
    """Every stored per-turn latency measurement, for percentile reporting."""
    import json

    rows = (await session.execute(select(CallMetric.latency_json))).scalars().all()
    samples: List[Dict[str, Optional[int]]] = []
    for payload in rows:
        try:
            parsed = json.loads(payload or "[]")
        except ValueError:
            continue
        if isinstance(parsed, list):
            for entry in parsed:
                if isinstance(entry, dict):
                    samples.append(entry)
    return samples
