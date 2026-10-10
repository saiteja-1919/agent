"""The database must enforce the rules Python is not allowed to be trusted with.

These tests run against real SQLite with foreign keys enabled and the real
triggers installed, so they exercise the same *rules* the PostgreSQL deployment
relies on (the PostgreSQL SQL itself is verified separately, on a real server,
in the deployment checklist).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.config.settings import Stage1Settings
from app.domain.enums import (
    CallLifecycleState,
    DialResult,
    LatencyStage,
    Outcome,
    ProviderEventKind,
    ReasonCode,
    TaskStatus,
)
from app.domain.events import CallStatusEvent, DialResultEvent
from app.domain.models import CallMetrics, CallOutcomeRecord, StudentRecord, LatencyTrace, utcnow
from app.integrations.database import call_repo, repositories
from app.integrations.database.engine import Database, database_from_url
from app.integrations.database.migrate import create_schema_for_tests, upgrade_to_head
from app.integrations.database.schema import (
    ATTEMPT_REASSIGN_MESSAGE,
    OUTCOME_IMMUTABLE_MESSAGE,
)
from app.integrations.database.tables import (
    ALL_TABLES,
    CallAttempt,
    CallMetric,
    CallOutcome,
    ImportBatch,
    ProviderEventRow,
    ReminderTask,
    School,
    Student,
)


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared in-memory database with the real schema and triggers."""

    async def asyncSetUp(self) -> None:
        self.db: Database = database_from_url("sqlite+aiosqlite:///:memory:")
        await create_schema_for_tests(self.db)

    async def asyncTearDown(self) -> None:
        await self.db.dispose()

    async def seed(self, *, phone: str = "9876543210", name: str = "Sai Teja", business_key: str = "2026-10_fee:S1"):
        async with self.db.session() as session:
            school = await repositories.ensure_school(session, name="Sri Vidya High School")
            student, created = await repositories.upsert_student(
                session,
                StudentRecord(
                    school_id=school.id,
                    student_name=name,
                    parent_name="Ramesh",
                    parent_phone=phone,
                    amount=Decimal("5000"),
                    due_date=date(2026, 10, 20),
                ),
            )
            task, _ = await repositories.create_task(
                session, student_id=student.id, school_id=school.id, business_key=business_key
            )
            return school.id, student.id, task.id


class TestSchemaShape(DatabaseTestCase):
    async def test_every_declared_table_exists(self):
        async with self.db.engine.connect() as connection:
            names = await connection.run_sync(lambda conn: set(conn.dialect.get_table_names(conn)))
        for table in ALL_TABLES:
            self.assertIn(table.__tablename__, names)

    async def test_no_transcript_or_recording_table_exists(self):
        """Section 17.8: the absence of the table is the strongest guarantee."""
        async with self.db.engine.connect() as connection:
            names = await connection.run_sync(lambda conn: set(conn.dialect.get_table_names(conn)))
        for forbidden in ("transcripts", "recordings", "call_transcripts", "audio_recordings"):
            self.assertNotIn(forbidden, names)

    async def test_triggers_are_installed(self):
        async with self.db.engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
            names = {row[0] for row in rows}
        self.assertIn("trg_forbid_outcome_change", names)
        self.assertIn("trg_forbid_attempt_reassign", names)


class TestOneAttemptPerTask(DatabaseTestCase):
    async def test_second_attempt_is_refused_by_the_repository(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            first = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            self.assertIsNotNone(first.id)
            with self.assertRaises(call_repo.DuplicateAttemptError):
                await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)

    async def test_the_unique_index_refuses_a_second_row_even_without_the_repository(self):
        """Bypassing the repository must not bypass the rule."""
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)

        async with self.db.session() as session:
            with self.assertRaises(IntegrityError):
                session.add(
                    CallAttempt(
                        task_id=task_id,
                        student_id=student_id,
                        provider="exotel",
                        status="created",
                        dial_result="not_dialed",
                        prompt_version="v1",
                        started_at=utcnow(),
                        created_at=utcnow(),
                        updated_at=utcnow(),
                    )
                )
                await session.flush()
            await session.rollback()

    async def test_undialed_attempt_can_be_replaced_but_a_dialed_one_cannot(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            attempt_id = attempt.id
            self.assertTrue(await call_repo.delete_attempt_if_undialed(session, task_id))
            # A replacement may only be created because the first row is gone;
            # otherwise the unique index would have refused it.
            replacement = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            self.assertIsNotNone(replacement.id)
            self.assertEqual(replacement.dial_result, DialResult.NOT_DIALED.value)

            await call_repo.set_dial_result(
                session, replacement.id, dial_result=DialResult.DIALED, provider_call_sid="sid-1"
            )
            self.assertFalse(await call_repo.delete_attempt_if_undialed(session, task_id))

    async def test_uncertain_attempt_can_never_be_downgraded(self):
        """A duplicated or late callback must not erase a possible call."""
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            await call_repo.set_dial_result(
                session, attempt.id, dial_result=DialResult.UNCERTAIN_NEEDS_REVIEW
            )
            self.assertFalse(
                await call_repo.set_dial_result(session, attempt.id, dial_result=DialResult.NOT_DIALED)
            )
            refreshed = await call_repo.get_attempt(session, attempt.id)
            self.assertEqual(refreshed.dial_result, DialResult.UNCERTAIN_NEEDS_REVIEW.value)

            # Learning that the call really happened is an allowed upgrade.
            self.assertTrue(
                await call_repo.set_dial_result(session, attempt.id, dial_result=DialResult.DIALED)
            )
            refreshed = await call_repo.get_attempt(session, attempt.id)
            self.assertEqual(refreshed.dial_result, DialResult.DIALED.value)


class TestOutcomeImmutability(DatabaseTestCase):
    async def _record_completed(self, session, attempt, task_id, student_id) -> CallOutcome:
        row, created = await call_repo.record_outcome(
            session,
            CallOutcomeRecord(
                attempt_id=attempt.id,
                task_id=task_id,
                student_id=student_id,
                outcome=Outcome.COMPLETED,
                reason_code=ReasonCode.DATE_PROMISED,
                promised_date=date(2026, 10, 20),
                promised_date_text="20 October",
                payment_date_questions_asked=1,
            ),
        )
        self.assertTrue(created)
        return row

    async def test_outcome_is_written_once(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            await self._record_completed(session, attempt, task_id, student_id)
            again, created = await call_repo.record_outcome(
                session,
                CallOutcomeRecord(
                    attempt_id=attempt.id,
                    task_id=task_id,
                    student_id=student_id,
                    outcome=Outcome.WRONG_NUMBER,
                    reason_code=ReasonCode.WRONG_PERSON,
                ),
            )
            self.assertFalse(created)
            self.assertEqual(again.outcome, Outcome.COMPLETED.value)

    async def test_trigger_rejects_an_update_of_the_outcome(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            row = await self._record_completed(session, attempt, task_id, student_id)
            try:
                await session.execute(
                    text("UPDATE call_outcomes SET outcome = 'cancelled' WHERE id = :id"),
                    {"id": row.id},
                )
                self.fail("the database allowed a final outcome to be changed")
            except IntegrityError as exc:
                self.assertIn(OUTCOME_IMMUTABLE_MESSAGE, str(exc))
            finally:
                await session.rollback()

    async def test_trigger_allows_updating_an_unprotected_column(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            row = await self._record_completed(session, attempt, task_id, student_id)
            await session.execute(
                text("UPDATE call_outcomes SET outcome_detail = 'exported' WHERE id = :id"),
                {"id": row.id},
            )
            # The UPDATE went around the ORM, so refresh the row explicitly.
            await session.refresh(row)
            self.assertEqual(row.outcome_detail, "exported")

    async def test_trigger_rejects_reassigning_an_attempt_to_another_task(self):
        school_id, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            other_task, _ = await repositories.create_task(
                session,
                student_id=student_id,
                school_id=school_id,
                business_key="2026-10_fee:S2",
            )
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            try:
                await session.execute(
                    text("UPDATE call_attempts SET task_id = :t WHERE id = :id"),
                    {"t": other_task.id, "id": attempt.id},
                )
                self.fail("an attempt was moved to a different reminder task")
            except IntegrityError as exc:
                self.assertIn(ATTEMPT_REASSIGN_MESSAGE, str(exc))
            finally:
                await session.rollback()

    async def test_illegal_outcome_reason_pair_is_refused_before_the_database(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            with self.assertRaises(ValueError):
                await call_repo.record_outcome(
                    session,
                    CallOutcomeRecord(
                        attempt_id=attempt.id,
                        task_id=task_id,
                        outcome=Outcome.NO_RESPONSE,
                        reason_code=ReasonCode.DATE_PROMISED,
                    ),
                )

    async def test_outcome_needs_a_task_id(self):
        async with self.db.session() as session:
            with self.assertRaises(ValueError):
                await call_repo.record_outcome(
                    session,
                    CallOutcomeRecord(
                        attempt_id=1, outcome=Outcome.COMPLETED, reason_code=ReasonCode.NO_DATE_GIVEN
                    ),
                )


class TestLifecycleGuards(DatabaseTestCase):
    async def test_lifecycle_only_moves_forward(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            self.assertTrue(await call_repo.record_lifecycle_state(session, attempt.id, CallLifecycleState.RINGING))
            self.assertTrue(await call_repo.record_lifecycle_state(session, attempt.id, CallLifecycleState.ANSWERED))
            # An out-of-order "ringing" callback must not drag it backwards.
            self.assertFalse(await call_repo.record_lifecycle_state(session, attempt.id, CallLifecycleState.RINGING))
            refreshed = await call_repo.get_attempt(session, attempt.id)
            self.assertEqual(refreshed.status, CallLifecycleState.ANSWERED.value)
            self.assertIsNotNone(refreshed.answered_at)

    async def test_ended_is_terminal(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            await call_repo.record_lifecycle_state(session, attempt.id, CallLifecycleState.ANSWERED)
            self.assertTrue(await call_repo.record_lifecycle_state(session, attempt.id, CallLifecycleState.ENDED))
            self.assertFalse(await call_repo.record_lifecycle_state(session, attempt.id, CallLifecycleState.IN_PROGRESS))
            refreshed = await call_repo.get_attempt(session, attempt.id)
            self.assertEqual(refreshed.status, CallLifecycleState.ENDED.value)
            self.assertIsNotNone(refreshed.ended_at)

    async def test_provider_sid_lookup_and_unknown_sid(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            await call_repo.set_dial_result(
                session, attempt.id, dial_result=DialResult.DIALED, provider_call_sid="call-abc"
            )
            found = await call_repo.get_attempt_by_provider_sid(session, "call-abc")
            self.assertIsNotNone(found)
            self.assertEqual(found.id, attempt.id)
            self.assertIsNone(await call_repo.get_attempt_by_provider_sid(session, "call-unknown"))


class TestAtomicClaiming(DatabaseTestCase):
    async def test_claim_returns_a_task_then_none(self):
        school_id, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            claimed = await repositories.claim_next_task(session)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.task_id, task_id)
            self.assertEqual(claimed.parent_phone, "9876543210")
            self.assertEqual(claimed.amount, Decimal("5000.00"))
            self.assertEqual(claimed.school_name, "Sri Vidya High School")
            self.assertIsNone(await repositories.claim_next_task(session))

    async def test_claim_is_exclusive(self):
        await self.seed()
        async with self.db.session() as session_a, self.db.session() as session_b:
            first = await repositories.claim_next_task(session_a)
            second = await repositories.claim_next_task(session_b)
            self.assertIsNotNone(first)
            self.assertIsNone(second, "two workers claimed the same task")

    async def test_claim_skips_a_task_that_already_has_an_attempt(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            self.assertIsNone(await repositories.claim_next_task(session))

    async def test_claim_skips_a_do_not_call_student(self):
        school_id, student_id, task_id = await self.seed(phone="9876500000")
        async with self.db.session() as session:
            await repositories.add_do_not_call(session, phone="9876500000", reason="asked not to be called")
            self.assertIsNone(await repositories.claim_next_task(session))
            self.assertTrue(await repositories.is_do_not_call(session, "9876500000"))

    async def test_do_not_call_is_idempotent_and_flags_existing_students(self):
        await self.seed(phone="9876500001")
        async with self.db.session() as session:
            self.assertTrue(await repositories.add_do_not_call(session, phone="9876500001"))
            self.assertFalse(await repositories.add_do_not_call(session, phone="9876500001"))
            students = (
                await session.execute(select(Student).where(Student.parent_phone == "9876500001"))
            ).scalars().all()
            self.assertTrue(all(student.do_not_call for student in students))

    async def test_claim_can_be_scoped_and_excluded(self):
        await self.seed(phone="9876500002", business_key="k1")
        await self.seed(phone="9876500003", name="Other Child", business_key="k2")
        async with self.db.session() as session:
            first = await repositories.claim_next_task(session)
            second = await repositories.claim_next_task(session, exclude_student_ids=[first.student_id])
            self.assertIsNotNone(second)
            self.assertNotEqual(first.student_id, second.student_id)


class TestIdempotentCreation(DatabaseTestCase):
    async def test_business_key_makes_a_task_idempotent(self):
        school_id, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            again, created = await repositories.create_task(
                session,
                student_id=student_id,
                school_id=school_id,
                business_key="2026-10_fee:S1",
            )
            self.assertFalse(created)
            self.assertEqual(again.id, task_id)
            total = (await session.execute(select(func.count(ReminderTask.id)))).scalar()
            self.assertEqual(total, 1)

    async def test_reimporting_a_student_updates_instead_of_duplicating(self):
        school_id, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            student, created = await repositories.upsert_student(
                session,
                StudentRecord(
                    school_id=school_id,
                    student_name="Sai Teja",
                    parent_name="Ramesh",
                    parent_phone="9876543210",
                    amount=Decimal("6000"),
                ),
            )
            self.assertFalse(created)
            self.assertEqual(student.id, student_id)
            self.assertEqual(student.amount, Decimal("6000.00"))
            total = (await session.execute(select(func.count(Student.id)))).scalar()
            self.assertEqual(total, 1)

    async def test_school_is_created_once(self):
        await self.seed()
        async with self.db.session() as session:
            await repositories.ensure_school(session, name="Sri Vidya High School")
            total = (await session.execute(select(func.count(School.id)))).scalar()
            self.assertEqual(total, 1)


class TestConstraints(DatabaseTestCase):
    async def test_non_positive_amount_is_refused(self):
        async with self.db.session() as session:
            school = await repositories.ensure_school(session, name="Sri Vidya High School")
            try:
                session.add(
                    Student(
                        school_id=school.id,
                        student_name="Zero Fee",
                        parent_phone="9876599999",
                        amount=Decimal("0"),
                        created_at=utcnow(),
                        updated_at=utcnow(),
                    )
                )
                await session.flush()
                self.fail("a zero amount was accepted")
            except IntegrityError:
                pass
            finally:
                await session.rollback()

    async def test_foreign_keys_are_enforced(self):
        async with self.db.session() as session:
            try:
                session.add(
                    Student(
                        school_id=999999,
                        student_name="Orphan",
                        parent_phone="9876588888",
                        created_at=utcnow(),
                        updated_at=utcnow(),
                    )
                )
                await session.flush()
                self.fail("a student with a missing school was accepted")
            except IntegrityError:
                pass
            finally:
                await session.rollback()

    async def test_unknown_task_status_is_refused(self):
        school_id, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            try:
                await session.execute(
                    text("UPDATE reminder_tasks SET status = 'nonsense' WHERE id = :id"),
                    {"id": task_id},
                )
                self.fail("an invalid task status was accepted")
            except IntegrityError:
                pass
            finally:
                await session.rollback()


class TestProviderEventDeduplication(DatabaseTestCase):
    async def test_the_same_event_is_only_processed_once(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            event = CallStatusEvent(
                kind=ProviderEventKind.RINGING,
                provider_call_sid="call-1",
                provider_event_id="evt-1",
            )
            self.assertTrue(await call_repo.record_provider_event(session, event, attempt_id=attempt.id))
            self.assertFalse(await call_repo.record_provider_event(session, event, attempt_id=attempt.id))
            total = (await session.execute(select(func.count(ProviderEventRow.id)))).scalar()
            self.assertEqual(total, 1)

    async def test_a_different_event_is_not_merged(self):
        async with self.db.session() as session:
            first = CallStatusEvent(
                kind=ProviderEventKind.RINGING, provider_event_id="evt-1", provider_call_sid="c1"
            )
            second = DialResultEvent(
                kind=ProviderEventKind.DIAL_RESULT, provider_event_id="evt-2", provider_call_sid="c1"
            )
            self.assertTrue(await call_repo.record_provider_event(session, first))
            self.assertTrue(await call_repo.record_provider_event(session, second))

    async def test_events_without_an_id_fall_back_to_kind_and_call(self):
        first = CallStatusEvent(kind=ProviderEventKind.ANSWERED, provider_call_sid="c9")
        second = CallStatusEvent(kind=ProviderEventKind.ANSWERED, provider_call_sid="c9")
        self.assertEqual(first.dedupe_key(), second.dedupe_key())
        different = CallStatusEvent(kind=ProviderEventKind.COMPLETED, provider_call_sid="c9")
        self.assertNotEqual(first.dedupe_key(), different.dedupe_key())


class TestMetrics(DatabaseTestCase):
    async def test_metrics_round_trip_including_latency(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            trace = LatencyTrace(attempt_id=attempt.id, turn_index=1)
            trace.mark(LatencyStage.T2_FINAL_TRANSCRIPT, monotonic=1.0)
            trace.mark(LatencyStage.T3_FIRST_LLM_TOKEN, monotonic=1.3)
            metrics = CallMetrics(attempt_id=attempt.id, task_id=task_id, tts_calls=3, latency=[trace])
            await call_repo.save_metrics(session, metrics)

            stored = await call_repo.get_metrics(session, attempt.id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.tts_calls, 3)
            samples = await call_repo.latency_samples(session)
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0]["llm_first_token_ms"], 300)
            self.assertEqual(samples[0]["turn_index"], 1)

    async def test_metrics_are_updated_not_duplicated(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            await call_repo.save_metrics(session, CallMetrics(attempt_id=attempt.id, task_id=task_id, tts_calls=1))
            await call_repo.save_metrics(session, CallMetrics(attempt_id=attempt.id, task_id=task_id, tts_calls=5))
            total = (await session.execute(select(func.count(CallMetric.id)))).scalar()
            self.assertEqual(total, 1)
            stored = await call_repo.get_metrics(session, attempt.id)
            self.assertEqual(stored.tts_calls, 5)


class TestRecoveryAndReporting(DatabaseTestCase):
    async def test_stale_attempts_are_found(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            await call_repo.set_dial_result(session, attempt.id, dial_result=DialResult.DIALED)
            # Backdate the attempt so it looks abandoned by a dead process.
            await session.execute(
                text("UPDATE call_attempts SET started_at = :t WHERE id = :id"),
                {"t": (utcnow() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S.%f"), "id": attempt.id},
            )
            stale = await call_repo.stale_attempts(session, older_than_minutes=30)
            self.assertEqual([row.id for row in stale], [attempt.id])
            self.assertEqual(await call_repo.open_attempt_count(session), 1)

            await call_repo.record_lifecycle_state(session, attempt.id, CallLifecycleState.ENDED)
            self.assertEqual(await call_repo.stale_attempts(session, older_than_minutes=30), [])

    async def test_export_carries_promised_dates_and_masks_the_number(self):
        _, student_id, task_id = await self.seed(phone="9876543210")
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            await call_repo.record_outcome(
                session,
                CallOutcomeRecord(
                    attempt_id=attempt.id,
                    task_id=task_id,
                    student_id=student_id,
                    outcome=Outcome.COMPLETED,
                    reason_code=ReasonCode.DATE_PROMISED,
                    promised_date=date(2026, 10, 20),
                    promised_date_text="20 October",
                ),
            )
            rows = await call_repo.export_rows(session)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.student_name, "Sai Teja")
            self.assertEqual(row.promised_date, date(2026, 10, 20))
            self.assertEqual(row.parent_phone_masked, "******3210")
            self.assertNotIn("98765", row.parent_phone_masked)
            self.assertEqual(row.action_needed, "none")

    async def test_export_flags_a_principal_request(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            await call_repo.record_outcome(
                session,
                CallOutcomeRecord(
                    attempt_id=attempt.id,
                    task_id=task_id,
                    student_id=student_id,
                    outcome=Outcome.COMPLETED,
                    reason_code=ReasonCode.PRINCIPAL_REQUESTED,
                    principal_request=True,
                ),
            )
            rows = await call_repo.export_rows(session)
            self.assertTrue(rows[0].principal_request)
            self.assertEqual(rows[0].action_needed, "call_principal_inform")

    async def test_task_and_outcome_counts(self):
        _, student_id, task_id = await self.seed()
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(session, task_id=task_id, student_id=student_id)
            await call_repo.record_outcome(
                session,
                CallOutcomeRecord(
                    attempt_id=attempt.id,
                    task_id=task_id,
                    student_id=student_id,
                    outcome=Outcome.NO_RESPONSE,
                    reason_code=ReasonCode.RING_TIMEOUT,
                ),
            )
            self.assertEqual(await call_repo.outcome_counts(session), {"no_response": 1})
            self.assertEqual(await call_repo.reason_counts(session), {"ring_timeout": 1})
            counts = await repositories.task_counts(session)
            self.assertEqual(counts.get(TaskStatus.NEW.value), 1)

    async def test_preview_masks_numbers_and_never_contacts_anyone(self):
        await self.seed(phone="9876543210")
        async with self.db.session() as session:
            rows = await repositories.preview_tasks(session)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].parent_phone_masked, "******3210")
            self.assertFalse(rows[0].has_attempt)


class TestMigrationsOnARealFile(unittest.TestCase):
    """The migration itself must install the guarantees, not just the test helper."""

    def test_upgrade_creates_tables_and_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "veda_migrate_test.db"
            url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
            revision = upgrade_to_head(database_url=url)
            self.assertEqual(revision, "0001_initial")

            connection = sqlite3.connect(db_path)
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                self.assertIn("call_attempts", tables)
                self.assertIn("call_outcomes", tables)
                self.assertIn("alembic_version", tables)
                self.assertIn("trg_forbid_outcome_change", triggers)

                # Prove the migrated trigger really fires.
                now = "2026-10-10 10:00:00.000000"
                connection.execute(
                    "INSERT INTO schools (id, name, name_telugu, timezone, languages, template_version, created_at, updated_at)"
                    " VALUES (1, 'S', '', 'Asia/Kolkata', 'te-IN', 'v1', ?, ?)",
                    (now, now),
                )
                connection.execute(
                    "INSERT INTO students (id, school_id, student_name, parent_name, parent_phone, currency, notes,"
                    " source_document, do_not_call, created_at, updated_at)"
                    " VALUES (1, 1, 'Sai', 'Ramesh', '9876543210', 'INR', '', '', 0, ?, ?)",
                    (now, now),
                )
                connection.execute(
                    "INSERT INTO reminder_tasks (id, student_id, school_id, reminder_type, business_key, status,"
                    " created_at, updated_at) VALUES (1, 1, 1, 'fee_pending', 'k1', 'new', ?, ?)",
                    (now, now),
                )
                connection.execute(
                    "INSERT INTO call_attempts (id, task_id, student_id, provider, status, dial_result,"
                    " prompt_version, started_at, created_at, updated_at)"
                    " VALUES (1, 1, 1, 'exotel', 'dialing', 'dialed', 'v1', ?, ?, ?)",
                    (now, now, now),
                )
                connection.execute(
                    "INSERT INTO call_outcomes (id, attempt_id, task_id, student_id, outcome, reason_code,"
                    " outcome_detail, promised_date_text, callback_time_text, principal_request, already_paid_claim,"
                    " do_not_call, parent_declined, financial_reason, illness_or_emergency, third_party_answered,"
                    " payment_date_questions_asked, reply_count, prompt_version, recorded_at, updated_at)"
                    " VALUES (1, 1, 1, 1, 'completed', 'date_promised', '', '', '', 0, 0, 0, 0, 0, 0, 0, 1, 2, 'v1', ?, ?)",
                    (now, now),
                )
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    connection.execute("UPDATE call_outcomes SET outcome = 'cancelled' WHERE id = 1")
                self.assertIn(OUTCOME_IMMUTABLE_MESSAGE, str(caught.exception))
            finally:
                connection.close()

    def test_upgrade_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite+aiosqlite:///{(Path(tmp) / 'twice.db').as_posix()}"
            self.assertEqual(upgrade_to_head(database_url=url), "0001_initial")
            self.assertEqual(upgrade_to_head(database_url=url), "0001_initial")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
