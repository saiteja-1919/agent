"""Builders for test workbooks.

All sample data here is invented and non-sensitive: it exists to exercise the
importer, never to look like a real parent. Addresses use the reserved-looking
``98765xxxxx`` range and names are obvious placeholders.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

DEFAULT_HEADERS = (
    "student_name",
    "parent_name",
    "parent_phone",
    "amount_due",
    "due_date",
    "notes",
)

#: One clean, callable row.
CLEAN_ROWS: List[Sequence[object]] = [
    ["Alekhya Rao", "Ramesh Rao", "9876500001", "5000", "2026-11-20", "sibling in class 4"],
    ["Bhargav Kumar", "Sita Kumar", "9876500002", "7500", "2026-11-25", ""],
    ["Chaitanya Das", "Mohan Das", "9876500003", "12000", "2026-12-05", "paid half"],
]


def write_xlsx(
    path: Path,
    rows: Iterable[Sequence[object]],
    *,
    headers: Optional[Sequence[str]] = None,
    sheet_name: str = "Fee Due",
    preamble: Sequence[Sequence[object]] = (),
    formula_cell: Optional[tuple] = None,
) -> Path:
    """Write a real .xlsx file, optionally with title rows above the header."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name

    for row in preamble:
        sheet.append(list(row))
    sheet.append(list(headers if headers is not None else DEFAULT_HEADERS))
    for row in rows:
        sheet.append(list(row))
    if formula_cell is not None:
        row_index, column_index, formula = formula_cell
        sheet.cell(row=row_index, column=column_index, value=formula)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(path))
    workbook.close()
    return path


def write_xlsx_multi_sheet(path: Path, *, first_rows=None) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    first = workbook.active
    first.title = "Fee Due"
    first.append(list(DEFAULT_HEADERS))
    for row in first_rows or CLEAN_ROWS:
        first.append(list(row))
    second = workbook.create_sheet("Old dues")
    second.append(list(DEFAULT_HEADERS))
    second.append(["Ignored Child", "Someone", "9876500099", "100", "2026-11-01", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(path))
    workbook.close()
    return path


def write_xls(
    path: Path,
    rows: Iterable[Sequence[object]],
    *,
    headers: Optional[Sequence[str]] = None,
) -> Path:
    """Write a real .xls (BIFF) file using xlwt."""
    import xlwt

    book = xlwt.Workbook()
    sheet = book.add_sheet("Sheet1")
    for column, header in enumerate(headers if headers is not None else DEFAULT_HEADERS):
        sheet.write(0, column, header)
    for row_index, row in enumerate(rows, start=1):
        for column, value in enumerate(row):
            sheet.write(row_index, column, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(str(path))
    return path


def write_csv(
    path: Path,
    rows: Iterable[Sequence[object]],
    *,
    headers: Optional[Sequence[str]] = None,
    delimiter: str = ",",
    encoding: str = "utf-8",
) -> Path:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerow(list(headers if headers is not None else DEFAULT_HEADERS))
        for row in rows:
            writer.writerow(list(row))
    return path


def write_binary(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path
