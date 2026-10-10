"""Finalization must be exactly once, in the right order, and lossless (19.3).

These run against real SQLite with the real triggers installed, because the
guarantees being tested are the ones the database makes.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.application.outcome_service import OutcomeService
from app.domain.enums import (
    CallLifecycleState,
    Outcome,
    ReasonCode,
)
from app.domain.models import CallMetrics, CallOutcomeRecord, StudentRecord
from app.integrations.database import call_repo, repositories
from app.integrations.database.engine import Database, database_from_url
from app.integrations.database.migrate import create_schema_for_tests
from app.integrations.database.tables import AuditLog, CallAttempt, CallOutcome, DoNotCall


class OutcomeServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db: Database = database_from_url("sqlite+aiosqlite:///:memory:")
        await create_schema_for_tests(self.db)
        self.service = OutcomeService()
        self.school_id, self.student_id, self.task_id = await self._seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(
                session, task_id=self.task_id, student_id=self.student_id
            )
            self.attempt_id = attempt.id
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.db.dispose()

    async def _seed(self):
        async with self.db.session() as session:
            school = await repositories.ensure_school(session, name="Sri Vidya High School")
            student, _ = await repositories.upsert_student(
                session,
                StudentRecord(
                    school_id=school.id,
                    student_name="Sai Teja",
                    parent_name="Ramesh",
                    parent_phone="9876543210",
                    amount=Decimal("5000"),
                    due_date=date(2026, 10, 20),
                ),
            )
            task, _ = await repositories.create_task(
                session, student_id=student.id, school_id=school.id,
                business_key="2026-10_fee:S1",
            )
            await session.commit()
            return school.id, student.id, task.id

    def record(self, **overrides) -> CallOutcomeRecord:
        values = dict(
            attempt_id=self.attempt_id,
            task_id=self.task_id,
            student_id=self.student_id,
            outcome=Outcome.COMPLETED,
            reason_code=ReasonCode.DATE_PROMISED,
            promised_date=date(2026, 10, 20),
            promised_date_text="twentieth October",
            reply_count=4,
        )
        values.update(overrides)
        return CallOutcomeRecord(**values)


class TestIdempotentFinalization(OutcomeServiceTestCase):
    async def test_finalizing_twice_writes_one_row(self):
        async with self.db.session() as session:
            first = await self.service.finalize(session, record=self.record())
        self.assertTrue(first.created)
        self.assertFalse(first.already_finalized)
        self.assertTrue(first.ok, first.errors)

        async with self.db.session() as session:
            second = await self.service.finalize(
                session,
                record=self.record(reason_code=ReasonCode.NO_DATE_GIVEN),
            )
        self.assertFalse(second.created)
        self.assertTrue(second.already_finalized)

        async with self.db.session() as session:
            rows = (await session.execute(select(CallOutcome))).scalars().all()
        self.assertEqual(len(rows), 1)
        # The first write wins; a duplicate finalizer cannot rewrite history.
        self.assertEqual(rows[0].reason_code, ReasonCode.DATE_PROMISED.value)

    async def test_the_database_refuses_to_change_a_recorded_outcome(self):
        async with self.db.session() as session:
            await self.service.finalize(session, record=self.record())
        async with self.db.session() as session:
            with self.assertRaises(Exception) as caught:
                row = (await session.execute(select(CallOutcome))).scalars().one()
                row.outcome = Outcome.DO_NOT_CALL.value
                await session.flush()
            await session.rollback()
        message = str(caught.exception).lower()
        self.assertIn("a recorded outcome cannot be changed", message)

    async def test_metrics_survive_and_are_saved_exactly_once(self):
        metrics = CallMetrics(
            attempt_id=self.attempt_id,
            task_id=self.task_id,
            turn_count=3,
            reply_count=4,
            audio_cache_hits=2,
            audio_cache_misses=1,
        )
        async with self.db.session() as session:
            result = await self.service.finalize(session, record=self.record(), metrics=metrics)
        self.assertTrue(result.metrics_saved, result.errors)

        async with self.db.session() as session:
            await self.service.finalize(
                session,
                record=self.record(),
                metrics=CallMetrics(attempt_id=self.attempt_id, task_id=self.task_id, turn_count=9),
            )
        async with self.db.session() as session:
            from app.integrations.database.tables import CallMetric

            rows = (await session.execute(select(CallMetric))).scalars().all()
        self.assertEqual(len(rows), 1)

    async def test_a_failing_side_effect_cannot_lose_the_outcome(self):
        """Metrics fail, the outcome still commits, and the failure is reported."""
        broken = CallMetrics(attempt_id=None, task_id=None)  # repository refuses this
        async with self.db.session() as session:
            result = await self.service.finalize(session, record=self.record(), metrics=broken)
        self.assertTrue(result.created)
        self.assertFalse(result.metrics_saved)
        self.assertFalse(result.ok)
        self.assertTrue(any("metrics" in error for error in result.errors), result.errors)

        async with self.db.session() as session:
            rows = (await session.execute(select(CallOutcome))).scalars().all()
        self.assertEqual(len(rows), 1)

    async def test_do_not_call_is_recorded_once_even_when_finalized_twice(self):
        async with self.db.session() as session:
            first = await self.service.finalize(
                session,
                record=self.record(
                    outcome=Outcome.DO_NOT_CALL,
                    reason_code=ReasonCode.PARENT_REQUESTED_NO_MORE_CALLS,
                    do_not_call=True,
                ),
                do_not_call_phone="9876543210",
            )
        self.assertTrue(first.do_not_call_added, first.errors)

        async with self.db.session() as session:
            second = await self.service.finalize(
                session,
                record=self.record(
                    outcome=Outcome.DO_NOT_CALL,
                    reason_code=ReasonCode.PARENT_REQUESTED_NO_MORE_CALLS,
                    do_not_call=True,
                ),
                do_not_call_phone="9876543210",
            )
        async with self.db.session() as session:
            rows = (await session.execute(select(DoNotCall))).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertFalse(second.errors)

    async def test_finalization_moves_the_attempt_to_ended(self):
        async with self.db.session() as session:
            await self.service.finalize(
                session,
                record=self.record(),
                lifecycle_state=CallLifecycleState.ENDED,
                duration_seconds=42,
            )
        async with self.db.session() as session:
            attempt = (await session.execute(select(CallAttempt))).scalars().one()
        self.assertEqual(attempt.status, CallLifecycleState.ENDED.value)
        self.assertEqual(attempt.duration_seconds, 42)

    async def test_an_audit_line_is_written_for_the_finalizer_only(self):
        async with self.db.session() as session:
            await self.service.finalize(session, record=self.record())
        async with self.db.session() as session:
            await self.service.finalize(session, record=self.record())
        async with self.db.session() as session:
            rows = (await session.execute(select(AuditLog))).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "call.finalized")
        # The audit line records the decision, never conversation content.
        details = rows[0].details or ""
        self.assertNotIn("transcript", str(details).lower())

    async def test_programming_errors_are_refused_before_touching_the_database(self):
        async with self.db.session() as session:
            with self.assertRaises(ValueError):
                await self.service.finalize(
                    session, record=CallOutcomeRecord(outcome=Outcome.COMPLETED)
                )
            await session.rollback()
        async with self.db.session() as session:
            rows = (await session.execute(select(CallOutcome))).scalars().all()
        self.assertEqual(rows, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
