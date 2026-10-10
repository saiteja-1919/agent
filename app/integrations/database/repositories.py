"""Repositories for school, student, reminder-task and do-not-call data.

Every function takes an :class:`~sqlalchemy.ext.asyncio.AsyncSession` and does
no transaction management of its own: the caller's ``Database.session()`` block
is the transaction boundary. That keeps "one provider request never happens
inside an open transaction" (section 20) easy to keep true.

Atomic claiming is the important part. Section 10 requires database-safe
claiming, so :func:`claim_next_task` performs a compare-and-swap: it picks a
candidate, then updates *only if the status is still claimable*, and treats an
empty result as "somebody else won". On PostgreSQL the candidate read also takes
``FOR UPDATE SKIP LOCKED`` to avoid two workers queueing on the same row, but the
correctness comes from the conditional update, which SQLite implements too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.enums import TaskStatus
from app.domain.models import StudentRecord, utcnow
from app.domain.policies import mask_phone

from .tables import CallAttempt, CallOutcome, DoNotCall, ImportBatch, School, Student, ReminderTask

#: Statuses a task may be claimed from.
CLAIMABLE_STATUSES = (TaskStatus.NEW.value, TaskStatus.READY.value)


def _dialect_name(session: AsyncSession) -> str:
    return session.get_bind().dialect.name


# ------------------------------------------------------------------------ schools
async def ensure_school(
    session: AsyncSession,
    *,
    name: str,
    name_telugu: str = "",
    timezone_name: str = "Asia/Kolkata",
    caller_id: Optional[str] = None,
    template_version: str = "v1",
) -> School:
    """Return the school row, creating it once. Idempotent by name."""
    existing = (
        await session.execute(select(School).where(School.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    school = School(
        name=name,
        name_telugu=name_telugu,
        timezone=timezone_name,
        caller_id=caller_id,
        template_version=template_version,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    try:
        async with session.begin_nested():
            session.add(school)
            await session.flush()
    except IntegrityError:
        # Another worker created it between the SELECT and the INSERT. Only the
        # failed INSERT is undone (savepoint), and the doomed object is dropped
        # from the session so the caller's commit cannot try it again.
        session.expunge(school)
        existing = (
            await session.execute(select(School).where(School.name == name))
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover - defensive
            raise
        return existing
    return school


async def get_school(session: AsyncSession, school_id: int) -> Optional[School]:
    return (await session.execute(select(School).where(School.id == school_id))).scalar_one_or_none()


# ----------------------------------------------------------------------- students
async def find_student(
    session: AsyncSession, *, school_id: int, student_name: str, parent_phone: str
) -> Optional[Student]:
    """Look a student up by the same identity the unique constraint uses."""
    statement = select(Student).where(
        Student.school_id == school_id,
        Student.student_name == student_name,
        Student.parent_phone == parent_phone,
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def upsert_student(
    session: AsyncSession, record: StudentRecord
) -> Tuple[Student, bool]:
    """Insert a student, or refresh the fee facts of an existing one.

    Returns ``(student, created)``. ``created=False`` means the row already
    existed and the import row is a duplicate - the caller reports it as such
    rather than dialling the same child twice for the same fee.
    """
    existing = await find_student(
        session,
        school_id=record.school_id,
        student_name=record.student_name,
        parent_phone=record.parent_phone,
    )
    if existing is not None:
        # Keep the latest verified fee facts, never the stale ones.
        if record.amount is not None:
            existing.amount = record.amount
        if record.due_date is not None:
            existing.due_date = record.due_date
        if record.notes:
            existing.notes = record.notes
        existing.parent_name = record.parent_name or existing.parent_name
        existing.updated_at = utcnow()
        await session.flush()
        return existing, False

    student = Student(
        school_id=record.school_id,
        student_name=record.student_name,
        parent_name=record.parent_name,
        parent_phone=record.parent_phone,
        amount=record.amount,
        currency=record.currency,
        due_date=record.due_date,
        notes=record.notes,
        source_document=record.source_document,
        source_row=record.source_row,
        import_batch_id=record.import_batch_id,
        do_not_call=record.do_not_call,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(student)
    await session.flush()
    return student, True


async def get_student(session: AsyncSession, student_id: int) -> Optional[Student]:
    return (await session.execute(select(Student).where(Student.id == student_id))).scalar_one_or_none()


async def student_phone_for_task(session: AsyncSession, task_id: int) -> Optional[str]:
    """The number to dial for a task. Only ever read from the database."""
    statement = select(Student.parent_phone).join(ReminderTask, ReminderTask.student_id == Student.id).where(
        ReminderTask.id == task_id
    )
    return (await session.execute(statement)).scalar_one_or_none()


# ------------------------------------------------------------------ reminder task
async def create_task(
    session: AsyncSession,
    *,
    student_id: int,
    school_id: int,
    business_key: str,
    import_batch_id: Optional[int] = None,
    reminder_type: str = "fee_pending",
    status: TaskStatus = TaskStatus.NEW,
) -> Tuple[ReminderTask, bool]:
    """Insert a task, or return the existing one for the same business key.

    The uniqueness of ``business_key`` is what makes re-importing the same month
    safe: the second import finds the task instead of creating a second one.
    """
    existing = (
        await session.execute(
            select(ReminderTask).where(ReminderTask.business_key == business_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    task = ReminderTask(
        student_id=student_id,
        school_id=school_id,
        reminder_type=reminder_type,
        business_key=business_key,
        status=status.value if isinstance(status, TaskStatus) else str(status),
        import_batch_id=import_batch_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    try:
        async with session.begin_nested():
            session.add(task)
            await session.flush()
    except IntegrityError:
        session.expunge(task)
        existing = (
            await session.execute(
                select(ReminderTask).where(ReminderTask.business_key == business_key)
            )
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover - defensive
            raise
        return existing, False
    return task, True


async def get_task(session: AsyncSession, task_id: int) -> Optional[ReminderTask]:
    return (await session.execute(select(ReminderTask).where(ReminderTask.id == task_id))).scalar_one_or_none()


async def set_task_status(
    session: AsyncSession,
    task_id: int,
    status: TaskStatus,
    *,
    blocked_reason: Optional[str] = None,
) -> bool:
    """Move a task to ``status``. Returns False when the row was already there."""
    statement = (
        update(ReminderTask)
        .where(ReminderTask.id == task_id)
        .values(
            status=status.value,
            blocked_reason=blocked_reason,
            updated_at=utcnow(),
        )
        .returning(ReminderTask.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def block_task(session: AsyncSession, task_id: int, reason: str) -> bool:
    return await set_task_status(session, task_id, TaskStatus.BLOCKED, blocked_reason=reason)


async def set_task_status_if(
    session: AsyncSession, task_id: int, from_statuses: Sequence[TaskStatus], to_status: TaskStatus
) -> bool:
    """Conditional status change: the compare-and-swap primitive."""
    statement = (
        update(ReminderTask)
        .where(
            ReminderTask.id == task_id,
            ReminderTask.status.in_([status.value for status in from_statuses]),
        )
        .values(status=to_status.value, updated_at=utcnow())
        .returning(ReminderTask.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


@dataclass(frozen=True)
class ClaimedTask:
    """Everything the call controller needs to dial, read in one go."""

    task_id: int
    student_id: int
    school_id: int
    student_name: str
    parent_name: str
    parent_phone: str
    amount: Optional[Decimal]
    currency: str
    due_date: Optional[object]
    notes: str
    school_name: str


async def claim_next_task(
    session: AsyncSession,
    *,
    school_id: Optional[int] = None,
    include_new: bool = True,
    exclude_student_ids: Sequence[int] = (),
) -> Optional[ClaimedTask]:
    """Claim one reminder task atomically, or return ``None``.

    A task is claimable when it has no call attempt yet, its student is not on
    the do-not-call list, and its status is NEW or READY. ``NOT_DIALED`` attempts
    are not treated as "already tried" here: the attempt repository removes such
    a row after a proven failure to dial, which is what makes a corrected retry
    possible without ever duplicating a real call.
    """
    claimable = [status.value for status in (TaskStatus.NEW, TaskStatus.READY) if
                 include_new or status is not TaskStatus.NEW]

    statement = (
        select(ReminderTask.id)
        .join(Student, Student.id == ReminderTask.student_id)
        .where(
            ReminderTask.status.in_(claimable),
            Student.do_not_call.is_(False),
            ~select(CallAttempt.id)
            .where(CallAttempt.task_id == ReminderTask.id)
            .exists(),
        )
        .order_by(ReminderTask.id)
        .limit(1)
    )
    if school_id is not None:
        statement = statement.where(ReminderTask.school_id == school_id)
    if exclude_student_ids:
        statement = statement.where(ReminderTask.student_id.notin_(list(exclude_student_ids)))
    if _dialect_name(session) == "postgresql":
        # Reduce contention; correctness still comes from the CAS below.
        statement = statement.with_for_update(skip_locked=True)

    candidate_id = (await session.execute(statement)).scalar_one_or_none()
    if candidate_id is None:
        return None

    claimed = await set_task_status_if(
        session, candidate_id, [TaskStatus.NEW, TaskStatus.READY], TaskStatus.CLAIMED
    )
    if not claimed:
        # Lost the race; the caller may try again.
        return None

    return await load_claimed(session, candidate_id)


async def load_claimed(session: AsyncSession, task_id: int) -> Optional[ClaimedTask]:
    statement = (
        select(
            ReminderTask.id,
            ReminderTask.student_id,
            ReminderTask.school_id,
            Student.student_name,
            Student.parent_name,
            Student.parent_phone,
            Student.amount,
            Student.currency,
            Student.due_date,
            Student.notes,
            School.name,
        )
        .join(Student, Student.id == ReminderTask.student_id)
        .join(School, School.id == ReminderTask.school_id)
        .where(ReminderTask.id == task_id)
    )
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None
    return ClaimedTask(
        task_id=row[0],
        student_id=row[1],
        school_id=row[2],
        student_name=row[3],
        parent_name=row[4],
        parent_phone=row[5],
        amount=row[6],
        currency=row[7],
        due_date=row[8],
        notes=row[9],
        school_name=row[10],
    )


@dataclass(frozen=True)
class TaskPreviewRow:
    """A dry-run preview row. The phone number is masked (section 17.8)."""

    task_id: int
    student_name: str
    parent_phone_masked: str
    amount: Optional[Decimal]
    due_date: Optional[object]
    status: str
    has_attempt: bool
    blocked_reason: Optional[str]


async def preview_tasks(
    session: AsyncSession, *, limit: int = 50, statuses: Optional[Sequence[TaskStatus]] = None
) -> List[TaskPreviewRow]:
    """What a batch *would* dial. Never dials anything."""
    statuses = statuses or (TaskStatus.NEW, TaskStatus.READY)
    attempt = aliased(CallAttempt)
    statement = (
        select(
            ReminderTask.id,
            Student.student_name,
            Student.parent_phone,
            Student.amount,
            Student.due_date,
            ReminderTask.status,
            attempt.id,
            ReminderTask.blocked_reason,
        )
        .join(Student, Student.id == ReminderTask.student_id)
        .outerjoin(attempt, attempt.task_id == ReminderTask.id)
        .where(ReminderTask.status.in_([status.value for status in statuses]))
        .order_by(ReminderTask.id)
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    return [
        TaskPreviewRow(
            task_id=row[0],
            student_name=row[1],
            parent_phone_masked=mask_phone(row[2]),
            amount=row[3],
            due_date=row[4],
            status=row[5],
            has_attempt=row[6] is not None,
            blocked_reason=row[7],
        )
        for row in rows
    ]


async def task_counts(session: AsyncSession) -> dict:
    statement = select(ReminderTask.status, func.count(ReminderTask.id)).group_by(ReminderTask.status)
    rows = (await session.execute(statement)).all()
    return {row[0]: row[1] for row in rows}


# ------------------------------------------------------------------- do-not-call
async def add_do_not_call(
    session: AsyncSession, *, phone: str, reason: str = "", source_attempt_id: Optional[int] = None
) -> bool:
    """Record a permanent do-not-call request. Idempotent per number."""
    existing = (
        await session.execute(select(DoNotCall).where(DoNotCall.phone == phone))
    ).scalar_one_or_none()
    if existing is not None:
        return False
    session.add(
        DoNotCall(
            phone=phone,
            reason=reason[:200],
            source_attempt_id=source_attempt_id,
            created_at=utcnow(),
        )
    )
    # Mark every existing student row for that number too, so a later import
    # cannot quietly re-enable calling through a sibling's row.
    await session.execute(update(Student).where(Student.parent_phone == phone).values(do_not_call=True))
    await session.flush()
    return True


async def is_do_not_call(session: AsyncSession, phone: str) -> bool:
    statement = select(DoNotCall.id).where(DoNotCall.phone == phone)
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def list_do_not_call(session: AsyncSession, limit: int = 200) -> List[DoNotCall]:
    statement = select(DoNotCall).order_by(DoNotCall.created_at.desc()).limit(limit)
    return list((await session.execute(statement)).scalars().all())


async def remove_do_not_call(session: AsyncSession, phone: str) -> bool:
    """Deliberate operator action only; never automatic."""
    result = await session.execute(delete(DoNotCall).where(DoNotCall.phone == phone))
    return bool(result.rowcount)
