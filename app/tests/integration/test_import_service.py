"""The importer: preview first, apply only when asked, never call anyone."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from app.config.settings import Stage1Settings
from app.domain.enums import TaskStatus
from app.ingestion import service
from app.ingestion.document_parser import DocumentError
from app.integrations.database import audit_repo, import_repo, repositories
from app.integrations.database.engine import Database, database_from_url
from app.integrations.database.migrate import create_schema_for_tests
from app.integrations.database.tables import ImportBatch, ReminderTask, Student
from app.tests.fixtures import workbooks


def settings() -> Stage1Settings:
    return Stage1Settings.from_env({})


class ImportServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Building the schema is fast but not instant, and the default 0.1s
        # slow-callback warning would flood the test output.
        asyncio.get_running_loop().slow_callback_duration = 10.0
        self.db: Database = database_from_url("sqlite+aiosqlite:///:memory:")
        await create_schema_for_tests(self.db)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        async with self.db.session() as session:
            school = await repositories.ensure_school(session, name="Sri Vidya High School")
            self.school_id = school.id

    async def asyncTearDown(self) -> None:
        await self.db.dispose()
        self._tmp.cleanup()

    async def counts(self) -> dict:
        async with self.db.session() as session:
            return {
                "batches": (await session.execute(select(func.count(ImportBatch.id)))).scalar(),
                "students": (await session.execute(select(func.count(Student.id)))).scalar(),
                "tasks": (await session.execute(select(func.count(ReminderTask.id)))).scalar(),
            }


class TestPreview(ImportServiceTestCase):
    async def test_inspect_reads_the_workbook_without_touching_the_database(self):
        path = workbooks.write_xlsx(self.root / "fees.xlsx", workbooks.CLEAN_ROWS)
        inspected = service.inspect_file(path, settings=settings())
        self.assertEqual(inspected.report.counts["valid"], 3)
        self.assertEqual(len(inspected.valid_rows), 3)
        self.assertEqual(await self.counts(), {"batches": 0, "students": 0, "tasks": 0})

    async def test_dry_run_import_writes_nothing(self):
        path = workbooks.write_xlsx(self.root / "fees.xlsx", workbooks.CLEAN_ROWS)
        async with self.db.session() as session:
            inspected, summary = await service.import_file(
                session,
                path,
                settings=settings(),
                school_id=self.school_id,
                dry_run=True,
            )
            self.assertIsNone(summary)
            self.assertEqual(inspected.report.counts["valid"], 3)
        self.assertEqual(await self.counts(), {"batches": 0, "students": 0, "tasks": 0})

    async def test_preview_never_prints_a_full_phone_number(self):
        path = workbooks.write_xlsx(self.root / "fees.xlsx", workbooks.CLEAN_ROWS)
        inspected = service.inspect_file(path, settings=settings())
        text = service.render_report_text(inspected)
        self.assertIn("nothing has been called", text)
        self.assertNotIn("9876500001", text)
        self.assertIn("0001", text)

    async def test_preview_explains_why_rows_were_refused(self):
        rows = [
            workbooks.CLEAN_ROWS[0],
            ["Bad Row", "Parent", "12345", "5000", "2026-11-20", ""],
        ]
        path = workbooks.write_xlsx(self.root / "mixed.xlsx", rows)
        inspected = service.inspect_file(path, settings=settings())
        text = service.render_report_text(inspected)
        self.assertIn("1 ready to call, 1 refused", text)
        self.assertIn("ten-digit", text)

    async def test_unusable_file_raises_an_operator_readable_error(self):
        path = workbooks.write_binary(self.root / "junk.xlsx", b"not a workbook at all")
        with self.assertRaises(DocumentError):
            service.inspect_file(path, settings=settings())


class TestApply(ImportServiceTestCase):
    async def test_apply_creates_students_and_tasks(self):
        path = workbooks.write_xlsx(self.root / "fees.xlsx", workbooks.CLEAN_ROWS)
        async with self.db.session() as session:
            inspected, summary = await service.import_file(
                session,
                path,
                settings=settings(),
                school_id=self.school_id,
                dry_run=False,
            )
        self.assertIsNotNone(summary)
        self.assertEqual(summary.students_created, 3)
        self.assertEqual(summary.tasks_created, 3)
        self.assertEqual(summary.rows_applied, 3)
        self.assertFalse(summary.repeat_upload)
        self.assertEqual(await self.counts(), {"batches": 1, "students": 3, "tasks": 3})

    async def test_applied_student_has_the_verified_fee_facts(self):
        path = workbooks.write_xlsx(self.root / "fees.xlsx", workbooks.CLEAN_ROWS[:1])
        async with self.db.session() as session:
            await service.import_file(
                session, path, settings=settings(), school_id=self.school_id, dry_run=False
            )
        async with self.db.session() as session:
            student = (await session.execute(select(Student))).scalar_one()
            self.assertEqual(student.student_name, "Alekhya Rao")
            self.assertEqual(student.amount, Decimal("5000.00"))
            self.assertEqual(student.parent_phone, "+919876500001")
            self.assertEqual(student.currency, "INR")

    async def test_repeat_upload_is_detected_and_not_applied_again(self):
        path = workbooks.write_xlsx(self.root / "fees.xlsx", workbooks.CLEAN_ROWS)
        async with self.db.session() as session:
            await service.import_file(
                session, path, settings=settings(), school_id=self.school_id, dry_run=False
            )
        async with self.db.session() as session:
            inspected, summary = await service.import_file(
                session, path, settings=settings(), school_id=self.school_id, dry_run=False
            )
            self.assertTrue(inspected.repeat_upload)
            self.assertIsNone(summary)
            self.assertFalse(inspected.is_importable)
        self.assertEqual(await self.counts(), {"batches": 1, "students": 3, "tasks": 3})

    async def test_row_verdicts_are_stored_for_review(self):
        rows = [workbooks.CLEAN_ROWS[0], ["Bad", "Parent", "12345", "5000", "2026-11-20", ""]]
        path = workbooks.write_xlsx(self.root / "mixed.xlsx", rows)
        async with self.db.session() as session:
            _, summary = await service.import_file(
                session, path, settings=settings(), school_id=self.school_id, dry_run=False
            )
        async with self.db.session() as session:
            stored = await import_repo.row_results(session, summary.batch_id)
            self.assertEqual(len(stored), 2)
            statuses = {row.status for row in stored}
            self.assertEqual(statuses, {"valid", "rejected"})
            rejected = [row for row in stored if row.status == "rejected"][0]
            self.assertIn("ten-digit", rejected.errors)
            self.assertEqual(rejected.parent_phone_masked, "*****")

    async def test_a_do_not_call_number_gets_a_blocked_task(self):
        path = workbooks.write_xlsx(self.root / "fees.xlsx", workbooks.CLEAN_ROWS[:1])
        async with self.db.session() as session:
            await repositories.add_do_not_call(
                session, phone="+919876500001", reason="asked not to be called"
            )
        async with self.db.session() as session:
            _, summary = await service.import_file(
                session, path, settings=settings(), school_id=self.school_id, dry_run=False
            )
            self.assertEqual(summary.tasks_blocked, 1)
        async with self.db.session() as session:
            task = (await session.execute(select(ReminderTask))).scalar_one()
            self.assertEqual(task.status, TaskStatus.BLOCKED.value)
            self.assertIn("asked not to be called", task.blocked_reason)
            # And it can never be claimed.
            self.assertIsNone(await repositories.claim_next_task(session))

    async def test_applying_the_same_content_twice_creates_nothing_new(self):
        """Even with a different file name the checksum catches the repeat."""
        first = workbooks.write_xlsx(self.root / "fees.xlsx", workbooks.CLEAN_ROWS)
        async with self.db.session() as session:
            await service.import_file(
                session, first, settings=settings(), school_id=self.school_id, dry_run=False
            )
        copied = self.root / "fees-copy.xlsx"
        copied.write_bytes(first.read_bytes())
        async with self.db.session() as session:
            inspected, _ = await service.import_file(
                session, copied, settings=settings(), school_id=self.school_id, dry_run=False
            )
        self.assertTrue(inspected.repeat_upload)
        self.assertEqual(await self.counts(), {"batches": 1, "students": 3, "tasks": 3})

    async def test_import_is_audited(self):
        path = workbooks.write_xlsx(self.root / "fees.xlsx", workbooks.CLEAN_ROWS)
        async with self.db.session() as session:
            await service.import_file(
                session, path, settings=settings(), school_id=self.school_id, dry_run=False
            )
        async with self.db.session() as session:
            entries = await audit_repo.recent(session)
            self.assertTrue(any(entry.action == "import.applied" for entry in entries))

    async def test_imported_tasks_are_claimable_exactly_once_each(self):
        path = workbooks.write_xlsx(self.root / "fees.xlsx", workbooks.CLEAN_ROWS)
        async with self.db.session() as session:
            await service.import_file(
                session, path, settings=settings(), school_id=self.school_id, dry_run=False
            )
        async with self.db.session() as session:
            claimed = []
            while True:
                task = await repositories.claim_next_task(session)
                if task is None:
                    break
                claimed.append(task.task_id)
            self.assertEqual(len(claimed), 3)
            self.assertEqual(len(set(claimed)), 3)


class TestCsvAndXlsEndToEnd(ImportServiceTestCase):
    async def test_csv_import(self):
        path = workbooks.write_csv(self.root / "fees.csv", workbooks.CLEAN_ROWS)
        async with self.db.session() as session:
            inspected, summary = await service.import_file(
                session, path, settings=settings(), school_id=self.school_id, dry_run=False
            )
        self.assertEqual(inspected.report.source_format, "csv")
        self.assertEqual(summary.students_created, 3)

    async def test_xls_import(self):
        path = workbooks.write_xls(self.root / "fees.xls", workbooks.CLEAN_ROWS)
        async with self.db.session() as session:
            inspected, summary = await service.import_file(
                session, path, settings=settings(), school_id=self.school_id, dry_run=False
            )
        self.assertEqual(inspected.report.source_format, "xls")
        self.assertEqual(summary.students_created, 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
