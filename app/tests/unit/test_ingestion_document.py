"""The parser decides the format from the bytes and never trusts a cell."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.ingestion.document_parser import (
    AMOUNT_DUE,
    DUE_DATE,
    PARENT_NAME,
    PARENT_PHONE,
    STUDENT_NAME,
    DocumentError,
    file_checksum,
    locate_header_row,
    map_headers,
    parse_document,
    sniff_format,
)
from app.tests.fixtures import workbooks


class ParserTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, name: str) -> Path:
        return self.root / name


class TestFormatSniffing(ParserTestCase):
    def test_xlsx_detected_from_bytes(self):
        path = workbooks.write_xlsx(self.path("fees.dat"), workbooks.CLEAN_ROWS)
        self.assertEqual(sniff_format(path), "xlsx")

    def test_xls_detected_from_bytes(self):
        path = workbooks.write_xls(self.path("fees.xls"), workbooks.CLEAN_ROWS)
        self.assertEqual(sniff_format(path), "xls")

    def test_csv_detected_from_text(self):
        path = workbooks.write_csv(self.path("fees.csv"), workbooks.CLEAN_ROWS)
        self.assertEqual(sniff_format(path), "csv")

    def test_binary_junk_is_refused(self):
        path = workbooks.write_binary(self.path("fees.xlsx"), b"\x00\x01\x02binary rubbish")
        with self.assertRaises(DocumentError) as caught:
            sniff_format(path)
        self.assertIn("not an Excel or CSV file", str(caught.exception))

    def test_missing_file_is_refused(self):
        with self.assertRaises(DocumentError):
            parse_document(self.path("nope.xlsx"))

    def test_empty_file_is_refused(self):
        path = workbooks.write_binary(self.path("empty.csv"), b"")
        with self.assertRaises(DocumentError) as caught:
            parse_document(path)
        self.assertIn("empty", str(caught.exception))

    def test_oversized_file_is_refused(self):
        path = workbooks.write_csv(self.path("big.csv"), workbooks.CLEAN_ROWS)
        with self.assertRaises(DocumentError) as caught:
            parse_document(path, max_bytes=10)
        self.assertIn("larger than", str(caught.exception))

    def test_extension_mismatch_is_reported(self):
        """The contents win, and the operator is told about the disagreement."""
        real = workbooks.write_csv(self.path("real.csv"), workbooks.CLEAN_ROWS)
        misleading = self.path("real.xlsx")
        misleading.write_bytes(real.read_bytes())
        document, _, _ = parse_document(misleading)
        self.assertEqual(document.format, "csv")
        self.assertTrue(any("the name says" in warning for warning in document.warnings))


class TestHeaderMapping(unittest.TestCase):
    def test_synonyms_are_recognised(self):
        header_map, raw = map_headers(
            ["Student Name", "Parent Name", "Mobile No.", "Amount", "Due Date", "Remarks"]
        )
        self.assertTrue(header_map.is_usable)
        self.assertEqual(header_map.column_for(STUDENT_NAME), 0)
        self.assertEqual(header_map.column_for(PARENT_PHONE), 2)
        self.assertEqual(header_map.column_for(AMOUNT_DUE), 3)
        self.assertEqual(header_map.column_for(DUE_DATE), 4)
        self.assertEqual(header_map.column_for(PARENT_NAME), 1)
        self.assertEqual(raw[STUDENT_NAME], "Student Name")

    def test_similar_names_do_not_collide(self):
        header_map, _ = map_headers(
            ["Child Name", "Guardian Name", "Parent Mobile", "Pending Fee", "Last Date"]
        )
        self.assertEqual(header_map.column_for(STUDENT_NAME), 0)
        self.assertEqual(header_map.column_for(PARENT_NAME), 1)
        self.assertEqual(header_map.column_for(PARENT_PHONE), 2)
        self.assertEqual(header_map.column_for(AMOUNT_DUE), 3)
        self.assertEqual(header_map.column_for(DUE_DATE), 4)

    def test_unknown_columns_are_reported_not_swallowed(self):
        header_map, _ = map_headers(
            ["student_name", "parent_phone", "amount_due", "due_date", "Class Section"]
        )
        self.assertEqual(header_map.unknown_columns, ("Class Section",))

    def test_missing_required_column_is_reported(self):
        header_map, _ = map_headers(["student_name", "parent_phone", "due_date"])
        self.assertEqual(header_map.missing_columns, ("amount_due",))
        self.assertFalse(header_map.is_usable)

    def test_duplicate_headers_keep_the_first(self):
        header_map, raw = map_headers(["student_name", "student_name", "parent_phone", "amount_due", "due_date"])
        self.assertEqual(header_map.column_for(STUDENT_NAME), 0)
        self.assertTrue(any("duplicate" in column for column in header_map.unknown_columns))

    def test_header_row_is_found_under_a_title(self):
        rows = [
            ["Sri Vidya High School - Fee Dues", "", "", "", "", ""],
            ["Term 2", "", "", "", "", ""],
            ["", "", "", "", "", ""],
            ["student_name", "parent_name", "parent_phone", "amount_due", "due_date", "notes"],
            ["Alekhya", "Ramesh", "9876500001", "5000", "2026-11-20", ""],
        ]
        self.assertEqual(locate_header_row(rows), 3)

    def test_header_row_not_found(self):
        rows = [["a", "b"], ["1", "2"]]
        self.assertEqual(locate_header_row(rows), -1)


class TestXlsxReading(ParserTestCase):
    def test_clean_workbook_parses(self):
        path = workbooks.write_xlsx(self.path("fees.xlsx"), workbooks.CLEAN_ROWS)
        document, header_map, _ = parse_document(path)
        self.assertEqual(document.format, "xlsx")
        self.assertEqual(document.sheet_name, "Fee Due")
        self.assertEqual(len(document.rows), 3)
        self.assertTrue(header_map.is_usable)
        self.assertEqual(document.rows[0][0], "Alekhya Rao")

    def test_title_rows_above_the_header_are_tolerated(self):
        path = workbooks.write_xlsx(
            self.path("titled.xlsx"),
            workbooks.CLEAN_ROWS[:1],
            preamble=[["Sri Vidya High School"], ["Fee dues for Term 2"], []],
        )
        document, header_map, _ = parse_document(path)
        self.assertEqual(document.header_row_index, 3)
        self.assertEqual(len(document.rows), 1)

    def test_missing_required_columns_refused_with_a_clear_message(self):
        path = workbooks.write_xlsx(
            self.path("thin.xlsx"),
            [["Alekhya", "9876500001"]],
            headers=["student_name", "parent_phone"],
        )
        with self.assertRaises(DocumentError) as caught:
            parse_document(path)
        self.assertIn("missing required column", str(caught.exception))

    def test_no_header_row_refused(self):
        path = workbooks.write_xlsx(
            self.path("noheader.xlsx"), [["a", "b"], ["c", "d"]], headers=["a", "b"]
        )
        with self.assertRaises(DocumentError) as caught:
            parse_document(path)
        self.assertIn("could not find the column headings", str(caught.exception))

    def test_formula_cells_are_flagged(self):
        """A fee cell that is a formula must never be treated as a value."""
        path = workbooks.write_xlsx(
            self.path("formula.xlsx"),
            [["Alekhya", "Ramesh", "9876500001", "5000", "2026-11-20", ""]],
            formula_cell=(4, 4, "=5000+2500"),
        )
        document, _, _ = parse_document(path)
        self.assertIn((4, 4), document.formula_or_error_cells)

    def test_error_cells_are_flagged(self):
        path = workbooks.write_xlsx(
            self.path("error.xlsx"), [["Alekhya", "Ramesh", "9876500001", "#REF!", "2026-11-20", ""]]
        )
        document, _, _ = parse_document(path)
        self.assertTrue(document.formula_or_error_cells)

    def test_several_sheets_are_reported_and_only_the_first_is_read(self):
        path = workbooks.write_xlsx_multi_sheet(self.path("multi.xlsx"))
        document, _, _ = parse_document(path)
        self.assertEqual(document.sheet_names, ("Fee Due", "Old dues"))
        self.assertTrue(any("sheets" in warning for warning in document.warnings))
        self.assertEqual(len(document.rows), 3)

    def test_extra_sheets_can_be_refused_outright(self):
        path = workbooks.write_xlsx_multi_sheet(self.path("multi2.xlsx"))
        with self.assertRaises(DocumentError):
            parse_document(path, allow_extra_sheets=False)

    def test_row_limit_is_enforced(self):
        rows = [[f"Child {index}", "Parent", f"98765{index:05d}", "100", "2026-11-20", ""] for index in range(1, 30)]
        path = workbooks.write_xlsx(self.path("big.xlsx"), rows)
        document, _, _ = parse_document(path, max_rows=10)
        self.assertEqual(len(document.rows), 10 - 1)
        self.assertTrue(any("only the first" in warning for warning in document.warnings))

    def test_numbers_stored_as_floats_are_not_mangled(self):
        path = workbooks.write_xlsx(
            self.path("floats.xlsx"), [["Alekhya", "Ramesh", 9876500001, 5000, "2026-11-20", ""]]
        )
        document, _, _ = parse_document(path)
        self.assertEqual(document.rows[0][2], "9876500001")
        self.assertEqual(document.rows[0][3], "5000")


class TestXlsReading(ParserTestCase):
    def test_xls_workbook_parses(self):
        path = workbooks.write_xls(self.path("fees.xls"), workbooks.CLEAN_ROWS)
        document, header_map, _ = parse_document(path)
        self.assertEqual(document.format, "xls")
        self.assertEqual(len(document.rows), 3)
        self.assertTrue(header_map.is_usable)
        self.assertEqual(document.rows[1][0], "Bhargav Kumar")

    def test_corrupt_xls_is_refused(self):
        path = workbooks.write_binary(
            self.path("corrupt.xls"), b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
        )
        with self.assertRaises(DocumentError):
            parse_document(path)


class TestCsvReading(ParserTestCase):
    def test_comma_csv(self):
        path = workbooks.write_csv(self.path("fees.csv"), workbooks.CLEAN_ROWS)
        document, _, _ = parse_document(path)
        self.assertEqual(document.format, "csv")
        self.assertEqual(len(document.rows), 3)

    def test_semicolon_csv(self):
        path = workbooks.write_csv(self.path("semi.csv"), workbooks.CLEAN_ROWS, delimiter=";")
        document, _, _ = parse_document(path)
        self.assertEqual(len(document.rows), 3)
        self.assertEqual(document.rows[0][0], "Alekhya Rao")

    def test_tab_csv(self):
        path = workbooks.write_csv(self.path("tab.csv"), workbooks.CLEAN_ROWS, delimiter="\t")
        document, _, _ = parse_document(path)
        self.assertEqual(len(document.rows), 3)

    def test_utf8_with_bom_and_telugu(self):
        rows = [["సాయి తేజ", "రమేష్", "9876500004", "5000", "2026-11-20", "గమనిక"]]
        path = workbooks.write_csv(self.path("telugu.csv"), rows, encoding="utf-8-sig")
        document, _, _ = parse_document(path)
        self.assertEqual(document.rows[0][0], "సాయి తేజ")
        self.assertEqual(document.rows[0][5], "గమనిక")

    def test_cp1252_fallback(self):
        path = workbooks.write_csv(self.path("cp.csv"), workbooks.CLEAN_ROWS, encoding="cp1252")
        document, _, _ = parse_document(path)
        self.assertEqual(len(document.rows), 3)


class TestFileGuards(ParserTestCase):
    def test_checksum_is_stable_and_content_based(self):
        first = workbooks.write_csv(self.path("a.csv"), workbooks.CLEAN_ROWS)
        second = workbooks.write_csv(self.path("b.csv"), workbooks.CLEAN_ROWS)
        self.assertEqual(file_checksum(first), file_checksum(second))

    def test_checksum_changes_with_content(self):
        first = workbooks.write_csv(self.path("c.csv"), workbooks.CLEAN_ROWS)
        second = workbooks.write_csv(self.path("d.csv"), workbooks.CLEAN_ROWS[:1])
        self.assertNotEqual(file_checksum(first), file_checksum(second))

    def test_decompression_bomb_is_refused(self):
        """A tiny .xlsx whose contents inflate enormously must be refused."""
        import zipfile

        path = self.path("bomb.xlsx")
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/worksheets/sheet1.xml", b"\x00" * (4 * 1024 * 1024))
        with self.assertRaises(DocumentError) as caught:
            parse_document(path)
        self.assertIn("decompression bomb", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
