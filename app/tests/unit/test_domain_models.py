"""Domain entities: no fabricated measurements, no lost structured results."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from app.domain.enums import ActionNeeded, LatencyStage, Outcome, ReasonCode
from app.domain.models import (
    CallMetrics,
    CallOutcomeRecord,
    ConversationState,
    ImportReport,
    ImportRowResult,
    LatencyTrace,
    PaymentCommitment,
    VerifiedFacts,
    first_word,
)
from app.domain.enums import ImportRowStatus


class TestVerifiedFacts(unittest.TestCase):
    def test_identity_can_only_be_confirmed_forward(self):
        facts = VerifiedFacts(school_name="s", student_name="n")
        self.assertFalse(facts.identity_confirmed)
        self.assertTrue(facts.with_identity_confirmed().identity_confirmed)

    def test_payment_question_counter_increments(self):
        facts = VerifiedFacts(school_name="s", student_name="n")
        self.assertEqual(facts.with_payment_date_question().payment_date_questions_asked, 1)

    def test_missing_amount_is_not_speakable(self):
        facts = VerifiedFacts(school_name="s", student_name="n", amount_words="")
        self.assertFalse(facts.has_fee_facts())

    def test_present_amount_is_speakable(self):
        facts = VerifiedFacts(
            school_name="s", student_name="n", amount_words="five thousand rupees",
            amount=Decimal("5000"),
        )
        self.assertTrue(facts.has_fee_facts())


class TestLatencyTrace(unittest.TestCase):
    def test_marks_are_idempotent(self):
        trace = LatencyTrace()
        trace.mark(LatencyStage.T1_END_OF_TURN, monotonic=10.0)
        trace.mark(LatencyStage.T1_END_OF_TURN, monotonic=99.0)
        self.assertEqual(trace.monotonic[LatencyStage.T1_END_OF_TURN.value], 10.0)

    def test_missing_stage_is_none_not_a_guess(self):
        trace = LatencyTrace()
        trace.mark(LatencyStage.T2_FINAL_TRANSCRIPT, monotonic=1.0)
        trace.mark(LatencyStage.T3_FIRST_LLM_TOKEN, monotonic=1.4)
        derived = trace.derived()
        self.assertEqual(derived["llm_first_token_ms"], 400)
        self.assertIsNone(derived["turn_detection_ms"])
        self.assertIsNone(derived["tts_startup_ms"])

    def test_response_start_uses_the_best_available_audio_mark(self):
        trace = LatencyTrace()
        trace.mark(LatencyStage.T0_LAST_PARENT_AUDIO, monotonic=0.0)
        trace.mark(LatencyStage.T4_FIRST_TTS_AUDIO, monotonic=1.25)
        derived = trace.derived()
        self.assertEqual(derived["response_start_ms"], 1250)

    def test_response_start_is_none_without_parent_audio(self):
        """A cache hit with no recorded parent audio must not invent a latency."""
        trace = LatencyTrace()
        trace.mark(LatencyStage.T4_FIRST_TTS_AUDIO, monotonic=1.0)
        self.assertIsNone(trace.derived()["response_start_ms"])

    def test_cache_lookup_latency(self):
        trace = LatencyTrace()
        trace.mark(LatencyStage.CACHE_LOOKUP_START, monotonic=2.0)
        trace.mark(LatencyStage.CACHE_LOOKUP_END, monotonic=2.05)
        self.assertEqual(trace.derived()["cache_lookup_ms"], 50)

    def test_has_and_t0_flags(self):
        trace = LatencyTrace()
        self.assertFalse(trace.has)
        self.assertFalse(trace.t0_present)
        trace.mark(LatencyStage.T0_LAST_PARENT_AUDIO, monotonic=0.0)
        self.assertTrue(trace.has)
        self.assertTrue(trace.t0_present)


class TestCallMetrics(unittest.TestCase):
    def test_bump_increments_a_counter(self):
        metrics = CallMetrics()
        metrics.bump("audio_cache_hits")
        metrics.bump("audio_cache_hits", 2)
        self.assertEqual(metrics.audio_cache_hits, 3)

    def test_bump_rejects_an_unknown_counter(self):
        metrics = CallMetrics()
        with self.assertRaises(AttributeError):
            metrics.bump("cache_hitz")

    def test_bump_rejects_a_non_counter_field(self):
        metrics = CallMetrics()
        with self.assertRaises(AttributeError):
            metrics.bump("latency")


class TestConversationState(unittest.TestCase):
    def test_reply_openings_are_tracked_for_the_repetition_rule(self):
        state = ConversationState()
        state.remember_reply("సర్, note చేసుకున్నాను")
        state.remember_reply("ఆ date చెప్పండి")
        self.assertEqual(state.previous_opening, "ఆ")
        self.assertEqual(state.reply_count, 2)

    def test_acknowledgement_words_are_remembered_once(self):
        state = ConversationState()
        state.remember_reply("సరే సర్", acknowledgement=("సరే",))
        state.remember_reply("సరే, note చేశాను", acknowledgement=("సరే",))
        self.assertEqual(state.acknowledgement_words_used, ["సరే"])

    def test_outcome_snapshot_carries_the_structured_results(self):
        state = ConversationState(attempt_id=7, task_id=3)
        state.promised = PaymentCommitment(
            promised_date=date(2026, 3, 20), raw_text="20 March", confidence=0.9
        )
        state.principal_request = True
        state.already_paid_claim = True
        state.payment_date_questions_asked = 2
        state.remember_reply("ఆ date note చేసుకున్నాను")
        record = state.to_outcome_record()
        self.assertEqual(record.attempt_id, 7)
        self.assertEqual(record.task_id, 3)
        self.assertEqual(record.promised_date, date(2026, 3, 20))
        self.assertTrue(record.principal_request)
        self.assertTrue(record.already_paid_claim)
        self.assertEqual(record.payment_date_questions_asked, 2)
        self.assertEqual(record.reply_count, 1)

    def test_first_word_strips_punctuation(self):
        self.assertEqual(first_word("సర్, ఈ విషయం"), "సర్")
        self.assertEqual(first_word("  hello! sir"), "hello")
        self.assertEqual(first_word(""), "")


class TestOutcomeActions(unittest.TestCase):
    def test_do_not_call_asks_for_removal(self):
        record = CallOutcomeRecord(outcome=Outcome.DO_NOT_CALL)
        self.assertEqual(record.action_needed, ActionNeeded.REMOVE_FROM_CALLING)

    def test_uncertain_dial_needs_review(self):
        record = CallOutcomeRecord(outcome=Outcome.UNCERTAIN_NEEDS_REVIEW)
        self.assertEqual(record.action_needed, ActionNeeded.REVIEW_DIAL_RESULT)

    def test_principal_request_is_flagged_for_the_office(self):
        record = CallOutcomeRecord(
            outcome=Outcome.COMPLETED,
            reason_code=ReasonCode.PRINCIPAL_REQUESTED,
            principal_request=True,
        )
        self.assertEqual(record.action_needed, ActionNeeded.CALL_PRINCIPAL_INFORM)

    def test_already_paid_needs_a_record_check(self):
        record = CallOutcomeRecord(
            outcome=Outcome.COMPLETED,
            reason_code=ReasonCode.ALREADY_PAID,
            already_paid_claim=True,
        )
        self.assertEqual(record.action_needed, ActionNeeded.CHECK_PAYMENT_RECORD)

    def test_plain_completed_needs_nothing(self):
        record = CallOutcomeRecord(
            outcome=Outcome.COMPLETED, reason_code=ReasonCode.DATE_PROMISED
        )
        self.assertEqual(record.action_needed, ActionNeeded.NONE)


class TestImportReport(unittest.TestCase):
    def _report(self) -> ImportReport:
        return ImportReport(
            source_path="fees.xlsx",
            rows=(
                ImportRowResult(1, ImportRowStatus.VALID, student_name="A"),
                ImportRowResult(2, ImportRowStatus.REJECTED, errors=("bad amount",)),
                ImportRowResult(3, ImportRowStatus.DUPLICATE, duplicate_of=1),
            ),
        )

    def test_filters(self):
        report = self._report()
        self.assertEqual(len(report.valid_rows), 1)
        self.assertEqual(len(report.rejected_rows), 1)
        self.assertEqual(len(report.duplicate_rows), 1)

    def test_counts(self):
        counts = self._report().counts
        self.assertEqual(counts["valid"], 1)
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(counts["duplicate"], 1)
        self.assertEqual(counts["skipped"], 0)

    def test_importable_when_there_is_a_valid_row(self):
        self.assertTrue(self._report().is_importable())

    def test_not_importable_when_all_rows_failed(self):
        report = ImportReport(rows=(ImportRowResult(1, ImportRowStatus.REJECTED),))
        self.assertFalse(report.is_importable())

    def test_not_importable_when_the_file_is_a_repeat(self):
        report = ImportReport(
            rows=(ImportRowResult(1, ImportRowStatus.VALID),), duplicate_file=True
        )
        self.assertFalse(report.is_importable())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
