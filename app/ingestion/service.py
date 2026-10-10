"""The import service: inspect a workbook, preview it, then apply it.

The operator-facing flow is always the same two steps:

1. **Inspect** (dry run). Parse, validate every row, and print a report. No
   database write, no task, and - above all - no call. This is the default for
   the CLI, and it is the dry-run preview the brief requires before any dialling.
2. **Apply.** Only after the operator has read the preview. Applying records the
   batch, the per-row verdicts, the students and the reminder tasks.

Applying is idempotent, and a file whose checksum has already been imported is
reported as a repeat upload and never applied twice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Stage1Settings
from app.domain.enums import ImportRowStatus
from app.domain.models import ImportReport, ImportRowResult
from app.integrations.database import import_repo
from app.integrations.database.import_repo import ApplySummary, ValidRow

from . import deduplication
from .document_parser import (
    STUDENT_NAME,
    PARENT_NAME,
    PARENT_PHONE,
    AMOUNT_DUE,
    DUE_DATE,
    NOTES,
    DocumentError,
    HeaderMap,
    ParsedDocument,
    parse_document,
)
from .validation import RowVerdict, validate_document


@dataclass
class InspectedImport:
    """Everything known about a file before anything is written."""

    report: ImportReport
    document: ParsedDocument
    header_map: HeaderMap
    raw_headers: Dict[str, str] = field(default_factory=dict)
    verdicts: Tuple[RowVerdict, ...] = ()
    repeat_upload: bool = False
    existing_batch_id: Optional[int] = None

    @property
    def valid_rows(self) -> Tuple[ValidRow, ...]:
        """The rows that may be called, with their real phone numbers."""
        return tuple(
            ValidRow(
                row_number=verdict.row_number,
                student_name=verdict.student_name,
                parent_name=verdict.parent_name,
                parent_phone=verdict.parent_phone,
                amount=verdict.amount,
                due_date=verdict.due_date,
                notes=verdict.notes,
            )
            for verdict in self.verdicts
            if verdict.status is ImportRowStatus.VALID
        )

    @property
    def is_importable(self) -> bool:
        return bool(self.valid_rows) and not self.repeat_upload


def inspect_file(
    path: Path,
    *,
    settings: Stage1Settings,
    today=None,
) -> InspectedImport:
    """Parse and validate a workbook. Reads the file, writes nothing, calls nobody."""
    path = Path(path)
    document, header_map, raw_headers = parse_document(
        path,
        max_bytes=settings.max_import_file_bytes,
        max_rows=settings.max_import_rows,
    )
    verdicts = validate_document(
        document, header_map, source_document=str(path), today=today
    )
    results: Tuple[ImportRowResult, ...] = tuple(verdict.to_result() for verdict in verdicts)

    report = ImportReport(
        source_path=str(path),
        source_format=document.format,
        sheet_name=document.sheet_name,
        header_map={name: raw_headers.get(name, "") for name in header_map.columns},
        unknown_columns=header_map.unknown_columns,
        missing_columns=header_map.missing_columns,
        rows=results,
        duplicate_file=False,
        content_checksum=deduplication.file_checksum(path),
    )
    return InspectedImport(
        report=report,
        document=document,
        header_map=header_map,
        raw_headers=raw_headers,
        verdicts=verdicts,
    )


async def import_file(
    session: AsyncSession,
    path: Path,
    *,
    settings: Stage1Settings,
    school_id: int,
    dry_run: bool = True,
    actor: str = "operator",
    today=None,
) -> Tuple[InspectedImport, Optional[ApplySummary]]:
    """Inspect a workbook and, unless ``dry_run``, apply it.

    ``dry_run`` is the default on purpose: the safe behaviour must be the one you
    get by not thinking about it.
    """
    inspected = inspect_file(path, settings=settings, today=today)

    existing = await import_repo.find_batch_by_checksum(session, inspected.report.content_checksum)
    if existing is not None:
        inspected.repeat_upload = True
        inspected.existing_batch_id = existing.id
        return inspected, None

    if dry_run:
        # A dry run must not leave a batch behind. Anything already written in
        # this transaction stays, so the caller keeps its own audit entry.
        return inspected, None

    batch, created = await import_repo.store_report(session, inspected.report)
    await import_repo.save_row_results(
        session, batch.id, tuple(verdict.to_result() for verdict in inspected.verdicts)
    )
    summary = await import_repo.apply_validated_rows(
        session,
        rows=inspected.valid_rows,
        school_id=school_id,
        batch_id=batch.id,
        source_document=str(path),
        period_fallback=None,
        actor=actor,
    )
    summary = ApplySummary(**{**summary.as_dict(), "repeat_upload": not created})
    return inspected, summary


# ------------------------------------------------------------------------ report
def render_report_text(inspected: InspectedImport, *, max_rows: int = 25) -> str:
    """A plain-text preview for the operator.

    Never prints a full phone number: the point of the preview is to show which
    rows will be called and which were refused, not to move personal data into a
    terminal scrollback.
    """
    report = inspected.report
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append("IMPORT PREVIEW (nothing has been called)")
    lines.append("=" * 72)
    lines.append(f"File       : {report.source_path}")
    lines.append(f"Format     : {report.source_format}   Sheet: {report.sheet_name or '(none)'}")
    if inspected.document.sheet_names:
        lines.append(f"All sheets : {', '.join(inspected.document.sheet_names)}")
    lines.append(f"Checksum   : {report.content_checksum[:16]}...")
    lines.append("")
    lines.append("Columns recognised:")
    for canonical, header in report.header_map.items():
        lines.append(f"  {canonical:<14} <- {header!r}")
    if report.unknown_columns:
        lines.append("Columns ignored:")
        lines.append(f"  {', '.join(report.unknown_columns)}")
    if report.missing_columns:
        lines.append(f"MISSING REQUIRED COLUMNS: {', '.join(report.missing_columns)}")
    if inspected.document.warnings:
        lines.append("")
        lines.append("File warnings:")
        for warning in inspected.document.warnings:
            lines.append(f"  - {warning}")
    if inspected.repeat_upload:
        lines.append("")
        lines.append(
            f"THIS FILE HAS ALREADY BEEN IMPORTED (batch {inspected.existing_batch_id}). "
            "It will not be imported again."
        )

    counts = report.counts
    lines.append("")
    lines.append(
        f"Rows: {counts['valid']} ready to call, {counts['rejected']} refused, "
        f"{counts['duplicate']} duplicate, {counts['skipped']} blank"
    )

    problem_rows = [
        verdict
        for verdict in inspected.verdicts
        if verdict.status in (ImportRowStatus.REJECTED, ImportRowStatus.DUPLICATE)
    ]
    if problem_rows:
        lines.append("")
        lines.append("Rows that will NOT be called:")
        for verdict in problem_rows[:max_rows]:
            label = "duplicate" if verdict.status is ImportRowStatus.DUPLICATE else "refused"
            detail = "; ".join(verdict.errors) or f"duplicate of row {verdict.duplicate_of}"
            lines.append(f"  row {verdict.row_number:>4} [{label}] {detail}")
        if len(problem_rows) > max_rows:
            lines.append(f"  ... and {len(problem_rows) - max_rows} more")

    warned = [
        verdict
        for verdict in inspected.verdicts
        if verdict.status is ImportRowStatus.VALID and verdict.warnings
    ]
    if warned:
        lines.append("")
        lines.append("Check these rows:")
        for verdict in warned[:max_rows]:
            lines.append(
                f"  row {verdict.row_number:>4} {verdict.parent_phone_masked}: "
                + "; ".join(verdict.warnings)
            )

    if inspected.valid_rows:
        lines.append("")
        lines.append("Ready to call (numbers shown masked):")
        for result in report.valid_rows[:max_rows]:
            lines.append(
                f"  row {result.row_number:>4}  {result.student_name:<22} "
                f"{result.parent_phone_masked:<14} {result.amount}  {result.due_date}"
            )
        if len(report.valid_rows) > max_rows:
            lines.append(f"  ... and {len(report.valid_rows) - max_rows} more")
    else:
        lines.append("")
        lines.append("No row in this file is safe to call. Nothing will be dialled.")
    lines.append("")
    return "\n".join(lines)


def render_row_results(rows: Sequence[object], *, status: str = "") -> str:
    """A compact review of stored row verdicts (for the CLI review command)."""
    lines: List[str] = []
    for row in rows:
        try:
            errors = json.loads(getattr(row, "errors", "[]") or "[]")
            warnings = json.loads(getattr(row, "warnings", "[]") or "[]")
        except (TypeError, ValueError):  # pragma: no cover - defensive
            errors, warnings = [], []
        lines.append(
            f"row {getattr(row, 'row_number', '?'):>4} [{getattr(row, 'status', '?')}] "
            f"{getattr(row, 'student_name', '')} "
            f"{getattr(row, 'parent_phone_masked', '')}"
        )
        for error in errors:
            lines.append(f"      why: {error}")
        for warning in warnings:
            lines.append(f"      note: {warning}")
    return "\n".join(lines)


__all__ = [
    "DocumentError",
    "InspectedImport",
    "import_file",
    "inspect_file",
    "render_report_text",
    "render_row_results",
    "STUDENT_NAME",
    "PARENT_NAME",
    "PARENT_PHONE",
    "AMOUNT_DUE",
    "DUE_DATE",
    "NOTES",
]
