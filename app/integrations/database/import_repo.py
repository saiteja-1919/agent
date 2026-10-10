"""Persist import batches, their row verdicts, and apply the valid rows.

Applying an import is the only step that creates data a call can be made from, so
it is written to be safely repeatable:

* the batch's file checksum is unique, so a repeated upload is recognised and no
  second batch (or second task) appears;
* every student is upserted by identity, and every task is keyed by a deterministic
  business key, so running the same import twice changes nothing;
* a student whose number is on the do-not-call list gets a task that is
  immediately ``BLOCKED`` - the reminder exists for the office to see, but it can
  never be claimed or dialled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ImportBatchStatus, ImportRowStatus, TaskStatus
from app.domain.models import ImportReport, ImportRowResult, StudentRecord, utcnow
from app.ingestion import deduplication

from . import audit_repo, repositories
from .tables import ImportBatch, ImportRowResult as ImportRowResultRow, Student


@dataclass(frozen=True)
class ApplySummary:
    """What an import actually did, in numbers the office can check."""

    batch_id: Optional[int] = None
    students_created: int = 0
    students_updated: int = 0
    tasks_created: int = 0
    tasks_existing: int = 0
    tasks_blocked: int = 0
    rows_applied: int = 0
    rows_rejected: int = 0
    rows_duplicate: int = 0
    repeat_upload: bool = False

    def as_dict(self) -> Dict[str, int | bool | None]:
        return {
            "batch_id": self.batch_id,
            "students_created": self.students_created,
            "students_updated": self.students_updated,
            "tasks_created": self.tasks_created,
            "tasks_existing": self.tasks_existing,
            "tasks_blocked": self.tasks_blocked,
            "rows_applied": self.rows_applied,
            "rows_rejected": self.rows_rejected,
            "rows_duplicate": self.rows_duplicate,
            "repeat_upload": self.repeat_upload,
        }


async def find_batch_by_checksum(session: AsyncSession, checksum: str) -> Optional[ImportBatch]:
    statement = select(ImportBatch).where(ImportBatch.file_checksum == checksum)
    return (await session.execute(statement)).scalar_one_or_none()


async def store_report(
    session: AsyncSession, report: ImportReport
) -> tuple[ImportBatch, bool]:
    """Record a parsed batch. Returns ``(batch, created)``; a repeat upload is
    recognised by the checksum so the same file can never be imported twice."""
    if report.content_checksum:
        existing = await find_batch_by_checksum(session, report.content_checksum)
        if existing is not None:
            return existing, False

    batch = ImportBatch(
        source_path=report.source_path[:400],
        source_format=report.source_format[:16],
        sheet_name=(report.sheet_name or "")[:120],
        file_checksum=report.content_checksum or "",
        header_map=json.dumps(report.header_map, ensure_ascii=False, default=str),
        unknown_columns=json.dumps(list(report.unknown_columns), ensure_ascii=False),
        missing_columns=json.dumps(list(report.missing_columns), ensure_ascii=False),
        row_counts=json.dumps(report.counts, ensure_ascii=False),
        status=ImportBatchStatus.PARSED.value,
        created_at=utcnow(),
    )
    session.add(batch)
    await session.flush()
    return batch, True


async def save_row_results(
    session: AsyncSession, batch_id: int, results: Sequence[ImportRowResult]
) -> int:
    """Store each row's verdict so the office can review the whole file."""
    written = 0
    for result in results:
        session.add(
            ImportRowResultRow(
                batch_id=batch_id,
                row_number=result.row_number,
                status=result.status.value,
                student_name=result.student_name[:100],
                parent_name=result.parent_name[:100],
                parent_phone_masked=result.parent_phone_masked[:20],
                amount=result.amount,
                due_date=result.due_date,
                errors=json.dumps(list(result.errors), ensure_ascii=False),
                warnings=json.dumps(list(result.warnings), ensure_ascii=False),
                duplicate_of=result.duplicate_of,
                created_at=utcnow(),
            )
        )
        written += 1
    await session.flush()
    return written


async def row_results(
    session: AsyncSession,
    batch_id: int,
    *,
    statuses: Optional[Sequence[ImportRowStatus]] = None,
    limit: int = 500,
) -> List[ImportRowResultRow]:
    statement = (
        select(ImportRowResultRow)
        .where(ImportRowResultRow.batch_id == batch_id)
        .order_by(ImportRowResultRow.row_number)
        .limit(limit)
    )
    if statuses:
        statement = statement.where(
            ImportRowResultRow.status.in_([status.value for status in statuses])
        )
    return list((await session.execute(statement)).scalars().all())


async def mark_batch_status(
    session: AsyncSession, batch_id: int, status: ImportBatchStatus
) -> None:
    await session.execute(
        update(ImportBatch).where(ImportBatch.id == batch_id).values(status=status.value)
    )


async def list_batches(session: AsyncSession, *, limit: int = 20) -> List[ImportBatch]:
    statement = select(ImportBatch).order_by(ImportBatch.id.desc()).limit(limit)
    return list((await session.execute(statement)).scalars().all())


@dataclass(frozen=True)
class ValidRow:
    """A row that passed validation, with the real (unmasked) phone number.

    Kept separate from :class:`ImportRowResult` because that object is what gets
    written to the review table, and the review table deliberately stores a masked
    number.
    """

    row_number: int
    student_name: str
    parent_name: str
    parent_phone: str
    amount: object
    due_date: object
    notes: str


async def apply_validated_rows(
    session: AsyncSession,
    *,
    rows: Sequence[ValidRow],
    school_id: int,
    batch_id: Optional[int] = None,
    source_document: str = "",
    period_fallback: Optional[str] = None,
    actor: str = "operator",
) -> ApplySummary:
    """Create the students and tasks for the validated rows of one import."""
    created_students = updated_students = created_tasks = existing_tasks = blocked = applied = 0

    for row in rows:
        student, created = await repositories.upsert_student(
            session,
            StudentRecord(
                school_id=school_id,
                student_name=row.student_name,
                parent_name=row.parent_name,
                parent_phone=row.parent_phone,
                amount=row.amount,  # type: ignore[arg-type]
                due_date=row.due_date,  # type: ignore[arg-type]
                notes=row.notes,
                source_document=source_document[:400],
                source_row=row.row_number,
                import_batch_id=batch_id,
            ),
        )
        if created:
            created_students += 1
        else:
            updated_students += 1

        period = deduplication.period_key(row.due_date, fallback=period_fallback)  # type: ignore[arg-type]
        key = deduplication.business_key(
            school_id=school_id,
            student_name=row.student_name,
            parent_phone=row.parent_phone,
            period=period,
        )
        task, task_created = await repositories.create_task(
            session,
            student_id=student.id,
            school_id=school_id,
            business_key=key,
            import_batch_id=batch_id,
        )
        if task_created:
            created_tasks += 1
        else:
            existing_tasks += 1

        # A parent who asked not to be called must never get a dialable task.
        if await repositories.is_do_not_call(session, row.parent_phone):
            await repositories.block_task(
                session, task.id, "the parent asked not to be called again"
            )
            await session.execute(
                update(Student).where(Student.id == student.id).values(do_not_call=True)
            )
            blocked += 1
        applied += 1

    if batch_id is not None:
        await mark_batch_status(session, batch_id, ImportBatchStatus.APPLIED)

    await audit_repo.record(
        session,
        action="import.applied",
        actor=actor,
        entity="import_batch",
        entity_id=batch_id,
        details={
            "rows_applied": applied,
            "students_created": created_students,
            "students_updated": updated_students,
            "tasks_created": created_tasks,
            "tasks_existing": existing_tasks,
            "tasks_blocked": blocked,
            "source": source_document,
        },
    )

    return ApplySummary(
        batch_id=batch_id,
        students_created=created_students,
        students_updated=updated_students,
        tasks_created=created_tasks,
        tasks_existing=existing_tasks,
        tasks_blocked=blocked,
        rows_applied=applied,
    )
