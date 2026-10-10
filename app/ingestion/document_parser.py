"""Read a fee workbook (.xlsx, .xls or .csv) safely and map its columns.

Three rules shape this module.

1. **Content, not extension.** Appendix A and section 17.4 both warn against
   trusting a file extension. The format is decided from the leading bytes, and
   a file whose bytes disagree with its name is refused rather than guessed at.
2. **The workbook is untrusted input.** A spreadsheet cell is written by an
   office clerk (or by whatever produced the export), so sizes are bounded,
   archives are checked for a decompression bomb, formulas and error cells are
   never turned into data, and text is cleaned before it can reach a prompt.
3. **The real sheet wins.** The brief says to inspect the actual workbook before
   finalising the importer, so header matching is deliberately flexible: it
   finds the header row, accepts a wide set of synonyms for each column, and
   reports the columns it could not place instead of silently ignoring them.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from speech_text import clean_name, clean_notes, clean_text

#: Formats the importer understands.
FORMAT_XLSX = "xlsx"
FORMAT_XLS = "xls"
FORMAT_CSV = "csv"

#: Leading bytes of the containers we accept.
_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: How many leading rows may contain the header.
MAX_HEADER_SCAN_ROWS = 15

#: A header row must place at least this many known columns.
MIN_HEADER_MATCHES = 2

#: How much bigger the uncompressed archive may be than the file itself.
MAX_COMPRESSION_RATIO = 200

#: Cell values that are spreadsheet errors, never data.
_ERROR_CELL_RE = re.compile(r"^#[A-Z0-9/]+[!?]?$", re.IGNORECASE)

#: Canonical column names.
STUDENT_NAME = "student_name"
PARENT_NAME = "parent_name"
PARENT_PHONE = "parent_phone"
AMOUNT_DUE = "amount_due"
DUE_DATE = "due_date"
NOTES = "notes"

#: Columns that must be present for a workbook to be usable.
REQUIRED_COLUMNS: Tuple[str, ...] = (STUDENT_NAME, PARENT_PHONE, AMOUNT_DUE, DUE_DATE)

#: Header synonyms. The key is the normalised header text (lower case, letters
#: and digits only), so "Parent Phone No." and "parent_phone" both land here.
HEADER_SYNONYMS: Dict[str, str] = {}


def _register(column: str, words: Iterable[str]) -> None:
    for word in words:
        HEADER_SYNONYMS[_normalise_header(word)] = column


def _normalise_header(text: object) -> str:
    """``"Parent Phone No."`` -> ``"parentphoneno"``."""
    raw = "" if text is None else str(text)
    return re.sub(r"[^a-z0-9]", "", raw.lower())


_register(
    STUDENT_NAME,
    (
        "student_name", "studentname", "student", "name", "child_name", "childname",
        "pupil_name", "pupilname", "name_of_student", "student_full_name", "student1",
        "విద్యార్థి", "విద్యార్థి పేరు",
    ),
)
_register(
    PARENT_NAME,
    (
        "parent_name", "parentname", "parent", "father_name", "fathername", "mother_name",
        "mothername", "guardian", "guardian_name", "guardianname", "parent_guardian",
    ),
)
_register(
    PARENT_PHONE,
    (
        "parent_phone", "parentphone", "phone", "phone_number", "phonenumber", "mobile",
        "mobile_no", "mobileno", "mobile_number", "mobilenumber", "contact", "contact_no",
        "contactno", "contact_number", "parent_mobile", "parentmobile", "whatsapp",
        "whatsapp_number", "cell", "cell_no", "phoneno", "parentphone number",
        "పోన్", "ఫోన్", "మొబైల్",
    ),
)
_register(
    AMOUNT_DUE,
    (
        "amount_due", "amountdue", "amount", "fee", "fees", "fee_amount", "feeamount",
        "pending_fee", "pendingfee", "pending", "due_amount", "dueamount", "balance",
        "balance_due", "outstanding", "total_due", "feepending", "fee_pending",
        "ఫీజు", "బాకీ",
    ),
)
_register(
    DUE_DATE,
    (
        "due_date", "duedate", "due", "due_on", "dueon", "last_date", "lastdate",
        "payment_date", "paymentdate", "date", "due_date_of_fee", "duedateof the fee",
        "due_by", "fee_due_date",
    ),
)
_register(
    NOTES,
    (
        "notes", "note", "remarks", "remark", "comment", "comments", "extra",
        "additional_info", "info", "details",
    ),
)


class DocumentError(Exception):
    """The file cannot be used. The message is written for the operator."""


@dataclass(frozen=True)
class ParsedDocument:
    """A workbook reduced to headers plus text rows, with provenance."""

    format: str
    sheet_name: str
    header_row_index: int
    headers: Tuple[str, ...]
    rows: Tuple[Tuple[str, ...], ...] = ()
    sheet_names: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    #: Cell coordinates (row, column) whose value was a formula or an error.
    formula_or_error_cells: Tuple[Tuple[int, int], ...] = ()


# ------------------------------------------------------------------- format sniff
def sniff_format(path: Path) -> str:
    """Decide the format from the file's bytes; raise when it is unusable."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError as exc:
        raise DocumentError(f"cannot read {path.name}: {exc}") from exc

    if head.startswith(_ZIP_MAGIC):
        return FORMAT_XLSX
    if head.startswith(_OLE2_MAGIC):
        return FORMAT_XLS
    # A text file: assume CSV unless it contains NUL bytes (i.e. binary).
    try:
        with open(path, "rb") as handle:
            sample = handle.read(4096)
    except OSError as exc:  # pragma: no cover - the first open already succeeded
        raise DocumentError(f"cannot read {path.name}: {exc}") from exc
    if b"\x00" in sample:
        raise DocumentError(
            f"{path.name} is not an Excel or CSV file. Use .xlsx, .xls or .csv "
            "(a .csv exported from Excel is fine)."
        )
    return FORMAT_CSV


def check_size(path: Path, *, max_bytes: int) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DocumentError(f"cannot read {path.name}: {exc}") from exc
    if size == 0:
        raise DocumentError(f"{path.name} is empty")
    if size > max_bytes:
        raise DocumentError(
            f"{path.name} is {size // 1024} KB, which is larger than the "
            f"{max_bytes // 1024} KB limit. Split the file into smaller sheets."
        )


def _check_zip_bomb(path: Path) -> None:
    """Refuse an archive whose contents are absurdly larger than the file."""
    try:
        with zipfile.ZipFile(path) as archive:
            uncompressed = sum(info.file_size for info in archive.infolist())
            compressed = max(1, path.stat().st_size)
            if uncompressed > compressed * MAX_COMPRESSION_RATIO:
                raise DocumentError(
                    f"{path.name} looks like a decompression bomb "
                    f"({uncompressed // 1024} KB of data in {compressed // 1024} KB). "
                    "Refusing to open it."
                )
    except zipfile.BadZipFile as exc:
        raise DocumentError(f"{path.name} is a corrupt .xlsx file: {exc}") from exc


def file_checksum(path: Path) -> str:
    """SHA-256 of the file bytes: identifies a repeated upload."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -------------------------------------------------------------------------- xlsx
def _read_xlsx(
    path: Path, *, max_rows: int
) -> Tuple[List[Tuple[str, ...]], str, Tuple[str, ...], List[str], List[Tuple[int, int]]]:
    """Read an .xlsx twice: once for cached values, once to find formulas.

    ``data_only=True`` gives the cached result of a formula, which would look
    like ordinary data. A fee cell that is a formula is a risk (it can be edited
    to point anywhere), so a second read collects the formula coordinates and
    the row is refused rather than trusted.
    """
    from openpyxl import load_workbook

    warnings: List[str] = []
    rows: List[Tuple[str, ...]] = []
    bad_cells: List[Tuple[int, int]] = []

    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        sheet_name = sheet.title
        sheet_names = tuple(workbook.sheetnames)
        for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            if row_index > max_rows:
                warnings.append(
                    f"only the first {max_rows} rows were read; the rest was ignored"
                )
                break
            rows.append(tuple(_cell_to_text(value) for value in row))
    finally:
        workbook.close()

    formulas = load_workbook(filename=str(path), read_only=True, data_only=False)
    try:
        sheet = formulas.active
        for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            if row_index > max_rows:
                break
            for column_index, value in enumerate(row, start=1):
                if isinstance(value, str) and value.startswith("="):
                    bad_cells.append((row_index, column_index))
    finally:
        formulas.close()

    return rows, sheet_name, sheet_names, warnings, bad_cells


def _cell_to_text(value: object) -> str:
    """A cell as plain text. Dates keep their ISO form; nothing is invented."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


# --------------------------------------------------------------------------- xls
def _read_xls(path: Path, *, max_rows: int) -> Tuple[List[Tuple[str, ...]], str, List[str], List[Tuple[int, int]]]:
    import xlrd

    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as exc:  # xlrd raises several unrelated types
        raise DocumentError(f"cannot open {path.name} as an .xls file: {exc}") from exc

    sheet = book.sheet_by_index(0)
    rows: List[Tuple[str, ...]] = []
    bad_cells: List[Tuple[int, int]] = []
    warnings: List[str] = []

    for row_index in range(sheet.nrows):
        if row_index + 1 > max_rows:
            warnings.append(f"only the first {max_rows} rows were read; the rest was ignored")
            break
        values: List[str] = []
        for column_index in range(sheet.ncols):
            cell = sheet.cell(row_index, column_index)
            if cell.ctype == xlrd.XL_CELL_ERROR:
                bad_cells.append((row_index + 1, column_index + 1))
                values.append("")
                continue
            if cell.ctype == xlrd.XL_CELL_DATE:
                try:
                    parts = xlrd.xldate_as_tuple(cell.value, book.datemode)
                    values.append(f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}")
                except Exception:  # pragma: no cover - malformed date serial
                    values.append("")
                continue
            values.append(_cell_to_text(cell.value))
        rows.append(tuple(values))

    sheet_name = sheet.name
    book.release_resources()
    return rows, sheet_name, warnings, bad_cells


# --------------------------------------------------------------------------- csv
def _read_csv(path: Path, *, max_rows: int) -> Tuple[List[Tuple[str, ...]], List[str]]:
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        raise DocumentError(f"cannot read the text in {path.name}; try saving it as UTF-8")

    warnings: List[str] = []
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
        warnings.append("the delimiter could not be detected; assuming a comma")

    rows: List[Tuple[str, ...]] = []
    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    for row_index, row in enumerate(reader, start=1):
        if row_index > max_rows:
            warnings.append(f"only the first {max_rows} rows were read; the rest was ignored")
            break
        rows.append(tuple(_cell_to_text(value) for value in row))
    return rows, warnings


# ------------------------------------------------------------------ header mapping
@dataclass(frozen=True)
class HeaderMap:
    """Which spreadsheet column feeds which canonical field."""

    columns: Dict[str, int]
    unknown_columns: Tuple[str, ...]
    missing_columns: Tuple[str, ...]

    def column_for(self, name: str) -> Optional[int]:
        return self.columns.get(name)

    @property
    def is_usable(self) -> bool:
        return not self.missing_columns


def map_headers(header_row: Sequence[str]) -> Tuple[HeaderMap, Dict[str, str]]:
    """Map ``header_row`` to canonical columns.

    Returns the map plus the raw header text per canonical column, so a report
    can show the operator exactly which column was taken as what.
    """
    columns: Dict[str, int] = {}
    raw_names: Dict[str, str] = {}
    unknown: List[str] = []

    for index, header in enumerate(header_row):
        key = _normalise_header(header)
        if not key:
            continue
        canonical = HEADER_SYNONYMS.get(key)
        if canonical is None:
            unknown.append(str(header))
            continue
        if canonical in columns:
            # A repeated header: keep the first and tell the operator.
            unknown.append(f"{header} (duplicate of {raw_names[canonical]})")
            continue
        columns[canonical] = index
        raw_names[canonical] = str(header)

    missing = tuple(name for name in REQUIRED_COLUMNS if name not in columns)
    return HeaderMap(columns=columns, unknown_columns=tuple(unknown), missing_columns=missing), raw_names


def locate_header_row(rows: Sequence[Sequence[str]]) -> int:
    """Find the header row, which may not be the first row.

    A real school export often has a title, a term and a blank line above the
    headings. The row that places the most known columns wins, provided it
    places at least :data:`MIN_HEADER_MATCHES` and names the student or the phone.
    """
    best_index = -1
    best_score = 0
    for index, row in enumerate(rows[:MAX_HEADER_SCAN_ROWS]):
        candidate, _ = map_headers(row)
        score = len(candidate.columns)
        has_identity = STUDENT_NAME in candidate.columns or PARENT_PHONE in candidate.columns
        if score >= MIN_HEADER_MATCHES and has_identity and score > best_score:
            best_index, best_score = index, score
    return best_index


# ---------------------------------------------------------------------- entry point
def parse_document(
    path: Path,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    max_rows: int = 5000,
    allow_extra_sheets: bool = True,
) -> Tuple[ParsedDocument, HeaderMap, Dict[str, str]]:
    """Parse a workbook into header-mapped text rows.

    Returns ``(document, header_map, raw_header_names)``. Raises
    :class:`DocumentError` with an operator-readable message when the file cannot
    be used at all (wrong format, too large, no header row, missing columns).
    """
    path = Path(path)
    if not path.exists():
        raise DocumentError(f"{path} does not exist")
    check_size(path, max_bytes=max_bytes)

    detected = sniff_format(path)
    suffix = path.suffix.lower().lstrip(".")
    warnings_extra: List[str] = []
    if suffix and suffix not in {detected, "xlsx", "xls", "csv"}:
        warnings_extra.append(
            f"the file extension is .{suffix} but the contents are {detected}; "
            f"using the contents"
        )
    if suffix in {"xlsx", "xls", "csv"} and suffix != detected:
        warnings_extra.append(
            f"WARNING: the name says .{suffix} but the contents are {detected}. "
            "Please check you selected the right file."
        )

    formula_or_error: List[Tuple[int, int]] = []
    sheet_names: Tuple[str, ...] = ()
    if detected == FORMAT_XLSX:
        _check_zip_bomb(path)
        rows, sheet_name, sheet_names, warnings, formula_or_error = _read_xlsx(
            path, max_rows=max_rows
        )
    elif detected == FORMAT_XLS:
        rows, sheet_name, warnings, formula_or_error = _read_xls(path, max_rows=max_rows)
        sheet_names = (sheet_name,)
    else:
        rows, warnings = _read_csv(path, max_rows=max_rows)
        sheet_name = path.name
        sheet_names = (sheet_name,)

    if len(sheet_names) > 1:
        if not allow_extra_sheets:
            raise DocumentError(
                f"{path.name} has several sheets ({', '.join(sheet_names)}); only the "
                "first is read"
            )
        warnings.append(
            f"{path.name} has {len(sheet_names)} sheets; only '{sheet_name}' was read"
        )

    warnings = warnings_extra + warnings
    if not rows:
        raise DocumentError(f"{path.name} has no rows")

    header_index = locate_header_row(rows)
    if header_index < 0:
        raise DocumentError(
            f"could not find the column headings in {path.name}. The first row should "
            "name the columns, for example: student_name, parent_name, parent_phone, "
            "amount_due, due_date, notes."
        )

    header_map, raw_names = map_headers(rows[header_index])
    if header_map.missing_columns:
        raise DocumentError(
            f"{path.name} is missing required column(s): "
            f"{', '.join(sorted(header_map.missing_columns))}. "
            f"Found: {', '.join(sorted(raw_names.get(key, '?') for key in header_map.columns))}"
        )

    # Error strings that survived as text (openpyxl turns #REF! into a string).
    for row_index, row in enumerate(rows, start=1):
        for column_index, value in enumerate(row, start=1):
            if value and _ERROR_CELL_RE.match(value):
                formula_or_error.append((row_index, column_index))

    document = ParsedDocument(
        format=detected,
        sheet_name=sheet_name,
        header_row_index=header_index,
        headers=tuple(rows[header_index]),
        rows=tuple(rows[header_index + 1 :]),
        sheet_names=sheet_names,
        warnings=tuple(warnings),
        formula_or_error_cells=tuple(formula_or_error),
    )
    return document, header_map, raw_names
