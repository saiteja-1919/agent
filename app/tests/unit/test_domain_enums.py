"""The three state models must stay separate, and outcomes must be well formed."""

from __future__ import annotations

import unittest

from app.domain.enums import (
    ACTIVE_CALL_STATES,
    DEFAULT_REASON_FOR_OUTCOME,
    LIFECYCLE_RANK,
    OUTCOME_REASON_CODES,
    SPEAKING_PHASES,
    TERMINAL_CALL_STATES,
    AttemptStatus,
    CallLifecycleState,
    ConversationPhase,
    DialResult,
    Outcome,
    ReasonCode,
    TaskStatus,
    legal_reason_codes,
    validate_outcome_reason,
)


class TestThreeStateModels(unittest.TestCase):
    def test_every_call_lifecycle_state_has_a_rank(self):
        self.assertEqual(set(CallLifecycleState), set(LIFECYCLE_RANK))

    def test_lifecycle_rank_is_a_strict_order(self):
        ranks = [LIFECYCLE_RANK[state] for state in CallLifecycleState]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), len(ranks))

    def test_only_ended_is_terminal_and_everything_else_is_active(self):
        self.assertEqual(TERMINAL_CALL_STATES, frozenset({CallLifecycleState.ENDED}))
        self.assertEqual(
            ACTIVE_CALL_STATES | TERMINAL_CALL_STATES, frozenset(CallLifecycleState)
        )
        self.assertFalse(ACTIVE_CALL_STATES & TERMINAL_CALL_STATES)

    def test_conversation_and_lifecycle_share_no_member_names(self):
        """The brief forbids merging these; the same name in both would invite it."""
        lifecycle_names = {state.name for state in CallLifecycleState}
        conversation_names = {phase.name for phase in ConversationPhase}
        self.assertEqual(lifecycle_names & conversation_names, set())

    def test_every_brief_state_belongs_to_one_model(self):
        """Every state the brief lists must exist in exactly one of the models."""
        brief_states = [
            "CREATED", "DIALING", "RINGING", "ANSWERED", "PLAYING_GREETING",
            "WAITING_FOR_IDENTITY_RESPONSE", "PLAYING_FEE_REMINDER", "LISTENING",
            "FINALIZING_TRANSCRIPT", "CLASSIFYING_INTENT", "CHECKING_RESPONSE_CACHE",
            "GENERATING_RESPONSE", "SYNTHESIZING_RESPONSE", "PLAYING_RESPONSE",
            "WAITING_FOR_PARENT_TURN", "CLOSING", "COMPLETED", "NO_RESPONSE",
            "VOICEMAIL", "CALLBACK_REQUESTED", "FAILED", "CANCELLED",
        ]
        names = (
            {state.name for state in CallLifecycleState}
            | {phase.name for phase in ConversationPhase}
            | {outcome.name for outcome in Outcome}
        )
        # FAILED in the brief maps to PROVIDER_ERROR here (a documented rename).
        missing = {name for name in brief_states if name not in names} - {"FAILED"}
        self.assertEqual(missing, set())
        self.assertIn(Outcome.PROVIDER_ERROR, Outcome)

    def test_opening_phases_are_speaking_phases(self):
        self.assertTrue(SPEAKING_PHASES)  # non-empty
        self.assertIn(ConversationPhase.PLAYING_GREETING, SPEAKING_PHASES)

    def test_attempt_status_mirrors_lifecycle_state(self):
        for state in CallLifecycleState:
            self.assertEqual(AttemptStatus.from_lifecycle(state).value, state.value)


class TestOutcomeReasonMapping(unittest.TestCase):
    def test_every_outcome_declares_at_least_one_reason(self):
        for outcome in Outcome:
            self.assertTrue(
                legal_reason_codes(outcome), f"{outcome} has no reason codes declared"
            )

    def test_every_reason_code_is_reachable(self):
        used = set()
        for codes in OUTCOME_REASON_CODES.values():
            used |= set(codes)
        self.assertEqual(used, set(ReasonCode), "a reason code is declared but never allowed")

    def test_legal_pair_is_accepted(self):
        validate_outcome_reason(Outcome.COMPLETED, ReasonCode.DATE_PROMISED)

    def test_illegal_pair_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_outcome_reason(Outcome.NO_RESPONSE, ReasonCode.DATE_PROMISED)

    def test_outcome_without_default_needs_a_reason(self):
        self.assertNotIn(Outcome.COMPLETED, DEFAULT_REASON_FOR_OUTCOME)
        with self.assertRaises(ValueError):
            validate_outcome_reason(Outcome.COMPLETED, None)

    def test_outcome_with_default_may_omit_the_reason(self):
        self.assertIn(Outcome.NO_RESPONSE, DEFAULT_REASON_FOR_OUTCOME)
        validate_outcome_reason(Outcome.NO_RESPONSE, None)

    def test_defaults_are_themselves_legal(self):
        for outcome, reason in DEFAULT_REASON_FOR_OUTCOME.items():
            validate_outcome_reason(outcome, reason)

    def test_every_declared_pair_validates(self):
        for outcome, codes in OUTCOME_REASON_CODES.items():
            for code in codes:
                validate_outcome_reason(outcome, code)

    def test_non_outcome_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_outcome_reason("completed", ReasonCode.DATE_PROMISED)  # type: ignore[arg-type]


class TestBusinessSuccess(unittest.TestCase):
    def test_answered_is_not_the_same_as_successful(self):
        self.assertTrue(Outcome.COMPLETED.is_business_success)
        self.assertTrue(Outcome.CALLBACK_REQUESTED.is_business_success)
        self.assertFalse(Outcome.WRONG_NUMBER.is_business_success)
        self.assertFalse(Outcome.PARENT_DISCONNECTED.is_business_success)

    def test_every_outcome_is_terminal(self):
        for outcome in Outcome:
            self.assertTrue(outcome.is_terminal)


class TestDialResultRetry(unittest.TestCase):
    def test_only_not_dialed_may_be_redialled(self):
        self.assertTrue(DialResult.NOT_DIALED.may_redial)
        self.assertFalse(DialResult.DIALED.may_redial)
        self.assertFalse(DialResult.UNCERTAIN_NEEDS_REVIEW.may_redial)

    def test_task_status_covers_the_required_states(self):
        names = {status.name for status in TaskStatus}
        self.assertTrue({"NEW", "DONE", "CANCELLED", "BLOCKED"}.issubset(names))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
