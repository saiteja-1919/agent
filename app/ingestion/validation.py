"""Row-by-row validation: reject or quarantine, never dial a bad row.

Section 4 of the clarifications is explicit: before dialling, reject or
quarantine rows with missing required fields, duplicate or ambiguous identities,
invalid phone numbers, invalid or negative amounts, unparseable or ambiguous
dates, formula or error cells, or unsafe values. Every row gets a verdict and a
reason, and a rejected row is never called.

Two judgement calls are documented rather than hidden:

* **Day-first dates.** ``05/11/2026`` could be 5 November or 11 May. Indian
  school sheets are day-first, so the day is assumed first and the row carries a
  warning telling the operator to confirm. Refusing every such row would refuse
  most real sheets, and refusing *nothing* would risk dialling about the wrong
  date - hence an accepted row with a visible warning. A day greater than 12
  (``20/11/2026``) is unambiguous and needs no warning.
* **Ambiguous identity.** Two rows for the same child and number are duplicates,
  so the second is not called twice (Appendix A pitfall 13 in spirit). Two rows
  for the same child with *different* numbers are ambiguous: both are rejected
  and reported, because choosing one would be a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from speech_text import clean_name, clean_notes, clean_text, parse_amount

from app.domain.models import ImportRowResult
from app.domain.enums import ImportRowStatus
from app.domain.phone import normalise_phone
from app.domain.policies import mask_phone

from .document_parser import (
    AMOUNT_DUE,
    DUE_DATE,
    NOTES,
    PARENT_NAME,
    PARENT_PHONE,
    STUDENT_NAME,
    HeaderMap,
    ParsedDocument,
)

#: Fields whose content is capped by speech_text's cleaners.
MAX_NAME_CHARS = 80
MAX_NOTES_CHARS = 400

#: A due date must be within this many years of today to be believable.
MAX_YEARS_AHEAD = 5

#: Excel stores dates as days since this epoch (1900 date system, with the
#: well-known 1900 leap-year bug that Excel keeps for compatibility).
_EXCEL_EPOCH = datetime(1899, 12, 30)

#: Only numbers in this range are treated as Excel date serials, so a plain
#: integer fee or phone number is never mistaken for one.
_EXCEL_SERIAL_MIN = 20000
_EXCEL_SERIAL_MAX = 60000

_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_SLASH_RE = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$")
# The separator before the year may be a space, a dot, a comma or a dash:
# "20-Nov-2026", "20 Nov 2026" and "20 November, 2026" are all in use.
_DAY_MONTH_NAME_RE = re.compile(
    r"^(\d{1,2})\s*(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})[.,\s\-]*(\d{2,4})?$"
)
_MONTH_DAY_NAME_RE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*(?:st|nd|rd|th)?,?\s*(\d{2,4})?$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


@dataclass(frozen=True)
class RowVerdict:
    """The validated content of one row, or the reasons it was refused."""

    row_number: int
    status: ImportRowStatus
    student_name: str = ""
    parent_name: str = ""
    #: The canonical E.164 number, present only when validation succeeded.
    parent_phone: str = ""
    #: Masked version of whatever was typed, so a refused row can still be found
    #: in the sheet without ever storing the real number in a report.
    parent_phone_masked: str = ""
    amount: Optional[Decimal] = None
    due_date: Optional[date] = None
    notes: str = ""
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    duplicate_of: Optional[int] = None

    def to_result(self) -> ImportRowResult:
        from app.domain.policies import mask_phone

        return ImportRowResult(
            row_number=self.row_number,
            status=self.status,
            student_name=self.student_name,
            parent_name=self.parent_name,
            parent_phone_masked=(
                mask_phone(self.parent_phone) if self.parent_phone else self.parent_phone_masked
            ),
            amount=self.amount,
            due_date=self.due_date,
            notes=self.notes,
            errors=self.errors,
            warnings=self.warnings,
            duplicate_of=self.duplicate_of,
        )


# --------------------------------------------------------------------- dates
def parse_due_date(value: str, *, today: Optional[date] = None) -> Tuple[Optional[date], Optional[str], List[str]]:
    """Parse a spreadsheet date. Returns ``(date, error, warnings)``."""
    today = today or date.today()
    text = (value or "").strip()
    if not text:
        return None, "due date is missing", []

    warnings: List[str] = []

    if _ISO_RE.match(text):
        year, month, day = (int(part) for part in _ISO_RE.match(text).groups())  # type: ignore[union-attr]
        return _build(year, month, day, today, warnings)

    match = _SLASH_RE.match(text)
    if match:
        first, second, year_text = match.groups()
        first_i, second_i = int(first), int(second)
        year = _expand_year(year_text, today)
        if first_i > 12 and second_i <= 12:
            day, month = first_i, second_i
        elif second_i > 12 and first_i <= 12:
            day, month = second_i, first_i
            warnings.append("date read as month/day because the month is greater than 12")
        else:
            # Both parts could be a month: assume the Indian day-first order.
            day, month = first_i, second_i
            warnings.append(
                f"'{text}' is ambiguous (day/month); read as day {day} month {month}. "
                "Please confirm this row."
            )
        if year is None:
            return None, f"the year in '{text}' is not believable", warnings
        return _build(year, month, day, today, warnings)

    match = _DAY_MONTH_NAME_RE.match(text)
    if match:
        day_text, month_text, year_text = match.groups()
        month = _MONTHS.get(month_text.lower())
        if month is None:
            return None, f"'{month_text}' is not a month name", warnings
        year = _expand_year(year_text, today)
        if year is None:
            return None, f"the year in '{text}' is not believable", warnings
        return _build(year, month, int(day_text), today, warnings)

    match = _MONTH_DAY_NAME_RE.match(text)
    if match:
        month_text, day_text, year_text = match.groups()
        month = _MONTHS.get(month_text.lower())
        if month is None:
            return None, f"'{month_text}' is not a month name", warnings
        year = _expand_year(year_text, today)
        if year is None:
            return None, f"the year in '{text}' is not believable", warnings
        return _build(year, month, int(day_text), today, warnings)

    if re.fullmatch(r"\d+(\.0+)?", text):
        serial = float(text)
        if _EXCEL_SERIAL_MIN <= serial <= _EXCEL_SERIAL_MAX and float(serial).is_integer():
            candidate = _EXCEL_EPOCH + timedelta(days=int(serial))
            warnings.append("date was stored as an Excel number, not a date")
            return _build(candidate.year, candidate.month, candidate.day, today, warnings, year_given=True)

    return None, (
        f"could not read the date '{text}'. Use YYYY-MM-DD, for example 2026-10-20"
    ), warnings


def _expand_year(year_text: Optional[str], today: date) -> Optional[int]:
    if not year_text:
        return today.year
    year = int(year_text)
    if year < 100:
        year += 2000 if year < 70 else 1900
    return year


def _build(
    year: int, month: int, day: int, today: date, warnings: List[str], *, year_given: bool = True
) -> Tuple[Optional[date], Optional[str], List[str]]:
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None, f"{day:02d}/{month:02d}/{year} is not a real date", warnings
    if candidate.year < 2020 or candidate.year > today.year + MAX_YEARS_AHEAD:
        return None, f"the due date {candidate.isoformat()} is outside the believable range", warnings
    if not year_given and candidate < today:
        candidate = candidate.replace(year=candidate.year + 1)
    if candidate < today - timedelta(days=3650):
        warnings.append("the due date is more than ten years ago; please check this row")
    return candidate, None, warnings


# --------------------------------------------------------------------- amounts
def parse_amount_cell(value: str) -> Tuple[Optional[Decimal], Optional[str]]:
    """Read a fee amount, reusing the tested amount parser.

    An empty cell gets a plain "amount is missing" rather than the parser's
    "'' is not a number", because this message is read by the office.
    """
    text = (value or "").strip()
    if not text:
        return None, "amount is missing"
    try:
        return parse_amount(text), None
    except ValueError as exc:
        return None, str(exc)


# ------------------------------------------------------------------------ rows
def _cell(row: Sequence[str], column: Optional[int]) -> str:
    if column is None or column < 0 or column >= len(row):
        return ""
    return row[column] or ""


def validate_row(
    row: Sequence[str],
    *,
    row_number: int,
    header_map: HeaderMap,
    formula_or_error_cells: Sequence[Tuple[int, int]] = (),
    today: Optional[date] = None,
) -> RowVerdict:
    """Validate one data row. Never raises, always returns a verdict."""
    errors: List[str] = []
    warnings: List[str] = []
    today = today or date.today()

    if not any((cell or "").strip() for cell in row):
        return RowVerdict(row_number=row_number, status=ImportRowStatus.SKIPPED)

    # A formula or an error cell means the value is not trustworthy data.
    bad_columns = [column for (row_idx, column) in formula_or_error_cells if row_idx == row_number]
    if bad_columns:
        errors.append(
            "the row contains a formula or a spreadsheet error in column(s) "
            + ", ".join(str(column) for column in sorted(set(bad_columns)))
            + "; replace it with a plain value"
        )

    raw_student = _cell(row, header_map.column_for(STUDENT_NAME))
    raw_parent = _cell(row, header_map.column_for(PARENT_NAME))
    raw_phone = _cell(row, header_map.column_for(PARENT_PHONE))
    raw_amount = _cell(row, header_map.column_for(AMOUNT_DUE))
    raw_due = _cell(row, header_map.column_for(DUE_DATE))
    raw_notes = _cell(row, header_map.column_for(NOTES))

    if _CONTROL_RE.search("".join(row)):
        warnings.append("invisible control characters were removed from this row")

    student_name = clean_name(raw_student, MAX_NAME_CHARS)
    if not student_name:
        errors.append("student name is missing")
    elif len(raw_student.strip()) > MAX_NAME_CHARS:
        errors.append(f"student name is longer than {MAX_NAME_CHARS} characters")

    parent_name = clean_name(raw_parent, MAX_NAME_CHARS)
    if not parent_name:
        warnings.append("parent name is missing; the call will not use a parent name")

    phone, phone_error = normalise_phone(raw_phone)
    if phone_error:
        errors.append(phone_error)

    amount, amount_error = parse_amount_cell(raw_amount)
    if amount_error:
        errors.append(amount_error)

    due_date: Optional[date] = None
    date_error: Optional[str] = None
    if raw_due.strip():
        due_date, date_error, date_warnings = parse_due_date(raw_due, today=today)
        warnings.extend(date_warnings)
    else:
        date_error = "due date is missing"
    if date_error:
        errors.append(date_error)

    notes = clean_notes(raw_notes)
    if "{{" in raw_notes or "}}" in raw_notes:
        warnings.append("placeholder markers were removed from the notes column")

    status = ImportRowStatus.REJECTED if errors else ImportRowStatus.VALID
    return RowVerdict(
        row_number=row_number,
        status=status,
        student_name=student_name,
        parent_name=parent_name,
        parent_phone=phone or "",
        parent_phone_masked=mask_phone(raw_phone),
        amount=amount,
        due_date=due_date,
        notes=notes,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def validate_document(
    document: ParsedDocument,
    header_map: HeaderMap,
    *,
    source_document: str = "",
    today: Optional[date] = None,
) -> Tuple[RowVerdict, ...]:
    """Validate every data row, including duplicate and ambiguity detection.

    Identity rules, in order:

    * the same student and number in two rows: the second is a duplicate;
    * the same student with two different numbers: both rows are rejected as
      ambiguous, because picking either one would be a guess;
    * the same number used for two different students: both are flagged, because
      a sibling group needs to be called about deliberately, not by accident.
    """
    today = today or date.today()
    verdicts: List[RowVerdict] = []
    seen_identity: Dict[Tuple[str, str], int] = {}
    student_numbers: Dict[str, set] = {}
    number_students: Dict[str, set] = {}

    first_data_row = document.header_row_index + 2  # 1-based spreadsheet row

    for offset, row in enumerate(document.rows):
        row_number = first_data_row + offset
        verdict = validate_row(
            row,
            row_number=row_number,
            header_map=header_map,
            formula_or_error_cells=document.formula_or_error_cells,
            today=today,
        )

        if verdict.status is ImportRowStatus.SKIPPED:
            verdicts.append(verdict)
            continue

        identity = (verdict.student_name.strip().lower(), verdict.parent_phone)
        if verdict.status is ImportRowStatus.VALID:
            if identity in seen_identity:
                verdicts.append(
                    RowVerdict(
                        row_number=row_number,
                        status=ImportRowStatus.DUPLICATE,
                        student_name=verdict.student_name,
                        parent_name=verdict.parent_name,
                        parent_phone=verdict.parent_phone,
                        parent_phone_masked=verdict.parent_phone_masked,
                        amount=verdict.amount,
                        due_date=verdict.due_date,
                        notes=verdict.notes,
                        warnings=verdict.warnings,
                        duplicate_of=seen_identity[identity],
                    )
                )
                continue
            seen_identity[identity] = row_number
            student_numbers.setdefault(verdict.student_name.strip().lower(), set()).add(
                verdict.parent_phone
            )
            number_students.setdefault(verdict.parent_phone, set()).add(
                verdict.student_name.strip().lower()
            )

        verdicts.append(verdict)

    # Second pass: ambiguity that only becomes visible once every row is read.
    ambiguous_students = {
        name: numbers for name, numbers in student_numbers.items() if len(numbers) > 1
    }
    shared_numbers = {
        number: names for number, names in number_students.items() if len(names) > 1
    }

    if not ambiguous_students and not shared_numbers:
        return tuple(verdicts)

    final: List[RowVerdict] = []
    for verdict in verdicts:
        if verdict.status is not ImportRowStatus.VALID:
            final.append(verdict)
            continue
        name = verdict.student_name.strip().lower()
        extra: List[str] = []
        if name in ambiguous_students:
            extra.append(
                f"two different numbers are recorded for '{verdict.student_name}' "
                f"({len(ambiguous_students[name])} numbers); please correct the sheet"
            )
        if verdict.parent_phone in shared_numbers and len(shared_numbers[verdict.parent_phone]) > 1:
            extra.append(
                "this number is used for more than one student; confirm that this is correct"
            )
        if extra:
            final.append(
                RowVerdict(
                    row_number=verdict.row_number,
                    status=ImportRowStatus.REJECTED,
                    student_name=verdict.student_name,
                    parent_name=verdict.parent_name,
                    parent_phone=verdict.parent_phone,
                    parent_phone_masked=verdict.parent_phone_masked,
                    amount=verdict.amount,
                    due_date=verdict.due_date,
                    notes=verdict.notes,
                    errors=tuple(verdict.errors) + tuple(extra),
                    warnings=verdict.warnings,
                )
            )
        else:
            final.append(verdict)
    return tuple(final)
