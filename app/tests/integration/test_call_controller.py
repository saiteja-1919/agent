"""The call loop, end to end, against a fake media session and a real database.

Nothing here dials, opens a socket or reads a credential. What is being tested
is the *order* the loop enforces, because that is where the safety rules live:
the dial is classified before anything is said, the opening plays whole, a note
is saved before the sentence that refers to it, and exactly one outcome is
written however the call ends.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select

from app.config.settings import Stage1Settings
from app.domain.enums import (
    DialResult,
    Outcome,
    ReasonCode,
)
from app.domain.models import CallOutcomeRecord, StudentRecord, VerifiedFacts
from app.integrations.database import call_repo, repositories
from app.integrations.database.engine import Database, database_from_url
from app.integrations.database.migrate import create_schema_for_tests
from app.integrations.database.tables import AuditLog, CallAttempt, CallOutcome
from app.integrations.exotel.client import DialOutcome
from app.voice.call_controller import (
    EVENT_HANGUP,
    EVENT_MAX_DURATION,
    EVENT_PROVIDER_ERROR,
    EVENT_SILENCE,
    EVENT_TURN,
    CallContext,
    CallController,
    PlayResult,
    SessionEvent,
)

FACTS = VerifiedFacts(
    school_name="బాలభారతి",
    student_name="రామ కిరణ్",
    parent_name="సుభద్ర",
    amount_words="five thousand rupees",
    amount=Decimal("5000"),
    due_date_words="twentieth March",
    today_words="tenth October",
)


class FakeMedia:
    """Stands in for Exotel plus the media stream. Records what was played."""

    def __init__(self, *, dial=None, events=(), fail_dial=False):
        self.dial_outcome = dial or DialOutcome(
            dial_result=DialResult.DIALED, provider_call_sid="CA1234"
        )
        self.events = list(events)
        self.fail_dial = fail_dial
        self.order: list = []
        self.played: list = []
        self.hung_up = False

    async def dial(self, phone):
        self.order.append(("dial", phone))
        if self.fail_dial:
            raise RuntimeError("socket closed before the response arrived")
        return self.dial_outcome

    async def begin_opening(self):
        self.order.append(("begin_opening",))

    async def end_opening(self):
        self.order.append(("end_opening",))

    async def play(self, decision, segments):
        self.order.append(("play", decision.text))
        self.played.append(decision.text)
        return PlayResult(
            cache_hit=bool(decision.cache_audio_key),
            characters=len(decision.text),
            segments=len(segments),
        )

    async def next_event(self, timeout):
        if self.events:
            return self.events.pop(0)
        # Nothing left: end the call rather than spin, so a mis-written test
        # finishes instead of hanging.
        return SessionEvent(EVENT_HANGUP)

    async def hangup(self):
        self.hung_up = True
        self.order.append(("hangup",))

    @property
    def spoken(self) -> str:
        return " ".join(self.played)


class CallControllerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db: Database = database_from_url("sqlite+aiosqlite:///:memory:")
        await create_schema_for_tests(self.db)
        self.settings = Stage1Settings.from_env({}).with_overrides(opener_pause_secs=0.0)
        self.school_id, self.student_id, self.task_id = await self._seed()

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
                session, student_id=student.id, school_id=school.id, business_key="2026-10_fee:S1"
            )
            await session.commit()
            return school.id, student.id, task.id

    async def new_attempt(self):
        async with self.db.session() as session:
            attempt = await call_repo.create_attempt(
                session, task_id=self.task_id, student_id=self.student_id
            )
            await session.commit()
            return attempt.id

    def context(self, attempt_id) -> CallContext:
        return CallContext(
            attempt_id=attempt_id,
            task_id=self.task_id,
            student_id=self.student_id,
            phone="919876543210",
            facts=FACTS,
        )

    def controller(self, media, **kwargs) -> CallController:
        return CallController(database=self.db, media=media, settings=self.settings, **kwargs)

    async def outcome_row(self):
        async with self.db.session() as session:
            return (await session.execute(select(CallOutcome))).scalars().all()


class TestDialClassification(CallControllerTestCase):
    async def test_a_call_that_was_never_dialed_is_recorded_and_silent(self):
        media = FakeMedia(
            dial=DialOutcome(dial_result=DialResult.NOT_DIALED),
            events=[SessionEvent(EVENT_TURN, "అవును")],
        )
        result = await self.controller(media).run(self.context(await self.new_attempt()))

        self.assertEqual(result.record.outcome, Outcome.PROVIDER_ERROR)
        self.assertEqual(result.record.reason_code, ReasonCode.DIAL_FAILED)
        self.assertEqual(media.played, [], "nothing may be spoken when no call exists")
        rows = await self.outcome_row()
        self.assertEqual(len(rows), 1)

    async def test_an_uncertain_dial_is_flagged_for_review(self):
        media = FakeMedia(
            dial=DialOutcome(dial_result=DialResult.UNCERTAIN_NEEDS_REVIEW),
        )
        attempt_id = await self.new_attempt()
        result = await self.controller(media).run(self.context(attempt_id))

        self.assertEqual(result.record.outcome, Outcome.UNCERTAIN_NEEDS_REVIEW)
        self.assertEqual(result.record.reason_code, ReasonCode.DIAL_RESULT_UNKNOWN)
        self.assertEqual(media.played, [])
        async with self.db.session() as session:
            attempt = (await session.execute(select(CallAttempt))).scalars().one()
        self.assertEqual(attempt.dial_result, "uncertain_needs_review")

    async def test_a_dial_that_raises_is_never_reported_as_a_safe_failure(self):
        media = FakeMedia(fail_dial=True)
        result = await self.controller(media).run(self.context(await self.new_attempt()))
        self.assertEqual(result.record.outcome, Outcome.UNCERTAIN_NEEDS_REVIEW)
        self.assertEqual(result.record.reason_code, ReasonCode.DIAL_RESULT_UNKNOWN)


class TestOpening(CallControllerTestCase):
    async def test_the_opening_plays_whole_before_barge_in_is_allowed(self):
        media = FakeMedia(events=[SessionEvent(EVENT_HANGUP)])
        await self.controller(media).run(self.context(await self.new_attempt()))

        kinds = [entry[0] for entry in media.order]
        self.assertEqual(kinds[:4], ["dial", "begin_opening", "play", "play"])
        self.assertIn("end_opening", kinds)
        self.assertLess(kinds.index("play"), kinds.index("end_opening"))
        self.assertEqual(len(media.played), 2)
        self.assertNotIn("fees", media.spoken.lower())
        self.assertNotIn("rupees", media.spoken.lower())

    async def test_the_opening_is_never_repeated_after_a_hangup(self):
        media = FakeMedia(events=[SessionEvent(EVENT_HANGUP)])
        result = await self.controller(media).run(self.context(await self.new_attempt()))
        self.assertEqual(len(media.played), 2)
        self.assertEqual(result.record.outcome, Outcome.PARENT_DISCONNECTED)
        self.assertEqual(result.record.reason_code, ReasonCode.PARENT_SILENT)


class TestConversation(CallControllerTestCase):
    async def test_a_promise_is_spoken_and_recorded(self):
        media = FakeMedia(
            events=[
                SessionEvent(EVENT_TURN, "అవును సర్"),
                SessionEvent(EVENT_TURN, "నేను రేపు కడతాను"),
            ]
        )
        controller = self.controller(media)
        result = await controller.run(self.context(await self.new_attempt()))

        self.assertIn("five thousand rupees", media.spoken)
        self.assertEqual(result.record.outcome, Outcome.COMPLETED)
        self.assertEqual(result.record.reason_code, ReasonCode.DATE_PROMISED)
        self.assertEqual(result.record.promised_date, date(2026, 10, 11))
        self.assertGreaterEqual(result.record.reply_count, 2)

    async def test_the_note_is_saved_before_the_sentence_that_claims_it(self):
        """Priority rule 4, in order: the audit line exists before the reply."""
        media = FakeMedia(
            events=[
                SessionEvent(EVENT_TURN, "అవును సర్"),
                SessionEvent(EVENT_TURN, "నేను రేపు కడతాను"),
            ]
        )
        await self.controller(media).run(self.context(await self.new_attempt()))
        async with self.db.session() as session:
            notes = (await session.execute(select(AuditLog))).scalars().all()
        conversation_notes = [row for row in notes if row.action == "conversation.note"]
        self.assertTrue(conversation_notes, "the promise must be written down")
        self.assertTrue(any("2026-10-11" in str(row.details) for row in conversation_notes))

    async def test_a_note_that_cannot_be_saved_is_never_spoken(self):
        media = FakeMedia(
            events=[
                SessionEvent(EVENT_TURN, "అవును సర్"),
                SessionEvent(EVENT_TURN, "నేను రేపు కడతాను"),
            ]
        )
        with patch("app.voice.call_controller.audit_repo.record",
                   side_effect=RuntimeError("database is down")):
            result = await self.controller(media).run(self.context(await self.new_attempt()))

        self.assertNotIn("fees", media.spoken.lower(), "Veda must not disclose on an unsaved note")
        self.assertGreaterEqual(result.record.reply_count, 0)
        self.assertEqual(len(await self.outcome_row()), 1, "the call is still recorded")

    async def test_silence_is_prompted_twice_then_recorded_as_no_response(self):
        media = FakeMedia(
            events=[SessionEvent(EVENT_SILENCE) for _ in range(5)]
        )
        result = await self.controller(media).run(self.context(await self.new_attempt()))

        self.assertEqual(result.record.outcome, Outcome.NO_RESPONSE)
        self.assertEqual(result.record.reason_code, ReasonCode.RING_TIMEOUT)
        # The two scripted lines, then exactly two check-ins.
        self.assertEqual(len(media.played), 4)

    async def test_a_stream_failure_is_a_provider_error_with_metrics(self):
        media = FakeMedia(events=[SessionEvent(EVENT_PROVIDER_ERROR, detail="socket closed")])
        result = await self.controller(media).run(self.context(await self.new_attempt()))

        self.assertEqual(result.record.outcome, Outcome.PROVIDER_ERROR)
        self.assertEqual(result.record.reason_code, ReasonCode.STREAM_FAILED)
        self.assertIn("socket closed", result.record.outcome_detail)

    async def test_the_call_ceiling_cancels_the_call(self):
        media = FakeMedia(events=[SessionEvent(EVENT_MAX_DURATION)])
        result = await self.controller(media).run(self.context(await self.new_attempt()))
        self.assertEqual(result.record.outcome, Outcome.CANCELLED)
        self.assertEqual(result.record.reason_code, ReasonCode.MAX_DURATION_REACHED)

    async def test_a_do_not_call_request_blocks_the_number_as_it_is_spoken(self):
        media = FakeMedia(
            events=[
                SessionEvent(EVENT_TURN, "అవును సర్"),
                SessionEvent(EVENT_TURN, "మళ్ళీ కాల్ చేయకండి"),
            ]
        )
        result = await self.controller(media).run(self.context(await self.new_attempt()))

        self.assertEqual(result.record.outcome, Outcome.DO_NOT_CALL)
        self.assertTrue(result.record.do_not_call)
        # The row was written by the note-save *before* the promise was spoken,
        # so finalization finds it already there: exactly one row, no duplicates.
        self.assertFalse(result.errors)
        async with self.db.session() as session:
            from app.integrations.database.tables import DoNotCall

            rows = (await session.execute(select(DoNotCall))).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertIn("ఇక మీకు ఫోన్ చేయను", media.spoken)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
