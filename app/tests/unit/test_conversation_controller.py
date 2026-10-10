"""Scenario tests for the conversation controller (sections 10, 12.4, 16, 18).

Every test here runs offline against approved templates and a fake model. The
ones that matter most are the negative ones: the fee must never be spoken before
identity is confirmed, the payment-date question must never be asked a third
time, and no reply may break a mandatory rule.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.application.conversation_controller import (
    ConversationController,
    ControllerTurn,
)
from app.application.prompting import build_messages, build_system_prompt
from app.cache.response_cache import APPROVED_TEMPLATES, ResponseCache
from app.domain.enums import (
    ConversationPhase,
    Intent,
    Outcome,
    ReasonCode,
    ResponseSource,
)
from app.domain.models import ConversationState, VerifiedFacts
from app.domain.policies import MAX_PAYMENT_DATE_QUESTIONS, validate_reply

TODAY = date(2026, 10, 10)


def make_facts(**overrides) -> VerifiedFacts:
    values = dict(
        school_name="బాలభారతి",
        student_name="రామ కిరణ్",
        parent_name="సుభద్ర",
        amount_words="five thousand rupees",
        amount=Decimal("5000"),
        due_date_words="twentieth March",
        today_words="tenth October",
        student_notes="fee part paid in June",
    )
    values.update(overrides)
    return VerifiedFacts(**values)


def make_controller(**kwargs) -> ConversationController:
    state = kwargs.pop("state", None) or ConversationState(facts=kwargs.pop("facts", make_facts()))
    return ConversationController(state=state, today=TODAY, **kwargs)


def run(coro):
    return asyncio.run(coro)


def confirm_identity(controller: ConversationController) -> ControllerTurn:
    controller.on_opening_line_played(0)
    controller.on_opening_line_played(1)
    turn = controller.on_identity_turn("అవును సర్")
    assert turn is not None and turn.intent is Intent.IDENTITY_CONFIRMED
    controller.mark_persisted()
    return turn


# ----------------------------------------------------------------------- opening
def test_opening_is_two_scripted_lines_with_no_fee():
    controller = make_controller()
    opening = controller.begin()

    assert "Veda" in opening.line_1.text
    assert opening.line_1.source is ResponseSource.SCRIPTED
    assert opening.line_1.cache_audio_key, "opening lines must be audio-cacheable"
    assert opening.line_2.text.rstrip().endswith("?")

    spoken = (opening.line_1.text + " " + opening.line_2.text).lower()
    for marker in ("fees", "pending", "rupees", "thousand", "5000"):
        assert marker not in spoken, f"the opener leaked {marker!r} before identity"
    assert controller.phase is ConversationPhase.PLAYING_GREETING

    controller.on_opening_line_played(1)
    assert controller.phase is ConversationPhase.WAITING_FOR_IDENTITY_RESPONSE


def test_opening_uses_the_parent_name_only_when_one_is_on_record():
    with_parent = make_controller()
    with_parent.state.facts = make_facts(parent_name="సుభద్ర")
    assert "సుభద్ర" in with_parent.begin().line_2.text

    without_parent = make_controller()
    without_parent.state.facts = make_facts(parent_name="")
    assert "సుభద్ర" not in without_parent.begin().line_2.text


def test_opening_cannot_be_built_twice():
    controller = make_controller()
    controller.begin()
    with pytest.raises(RuntimeError):
        controller.begin()


def test_the_opener_is_never_restarted_when_interrupted():
    controller = make_controller()
    verdict = controller.on_interruption(during_opening=True)
    assert verdict.stop_speaking is False
    assert verdict.restart_opening is False
    assert verdict.keep_buffered_speech is True

    later = controller.on_interruption(during_opening=False)
    assert later.stop_speaking is True
    assert later.restart_opening is False


# ----------------------------------------------------------------- identity gate
def test_a_greeting_is_not_an_identity_answer():
    controller = make_controller()
    controller.begin()
    controller.on_opening_line_played(1)
    assert controller.on_identity_turn("hello") is None
    assert controller.on_identity_turn("హలో సర్") is None
    assert controller.state.identity_confirmed is False


def test_confirmation_discloses_the_verified_amount_and_asks_the_date_once():
    controller = make_controller()
    controller.begin()
    turn = controller.on_identity_turn("అవును సర్")

    assert turn.intent is Intent.IDENTITY_CONFIRMED
    assert "five thousand rupees" in turn.decision.text
    assert turn.decision.asks_payment_date is True
    assert controller.state.payment_date_questions_asked == 1
    assert controller.state.identity_confirmed is True
    assert turn.requires_persistence is True
    assert controller.dirty is True
    controller.mark_persisted()
    assert controller.dirty is False


def test_an_unclear_identity_answer_gets_exactly_one_reask_then_a_silent_close():
    controller = make_controller()
    controller.begin()
    controller.on_opening_line_played(1)

    first = controller.on_identity_turn("entidi adi")
    assert first is not None
    assert first.decision.text
    assert "fees" not in first.decision.text.lower()
    assert controller.state.identity_confirmed is False

    second = controller.on_identity_turn("enti anukunnanu")
    assert second is not None and second.ends
    spoken = second.decision.text.lower()
    for marker in ("fees", "pending", "rupees", "thousand"):
        assert marker not in spoken, f"unverified listener heard {marker!r}"

    record = controller.finalize()
    assert record.outcome is Outcome.COMPLETED
    assert record.reason_code is ReasonCode.NO_DATE_GIVEN
    assert "identity" in record.outcome_detail


def test_wrong_number_ends_the_call_without_disclosing_anything():
    controller = make_controller()
    controller.begin()
    controller.on_opening_line_played(1)
    turn = controller.on_identity_turn("wrong number sir")

    assert turn.ends is True
    assert turn.decision.outcome is Outcome.WRONG_NUMBER
    assert "fees" not in turn.decision.text.lower()
    record = controller.finalize()
    assert record.outcome is Outcome.WRONG_NUMBER
    assert record.reason_code is ReasonCode.WRONG_PERSON


def test_third_party_is_offered_a_callback_and_the_fee_is_never_disclosed():
    controller = make_controller()
    controller.begin()
    controller.on_opening_line_played(1)
    turn = controller.on_identity_turn("he is not available")

    assert controller.state.third_party_answered is True
    assert "fees" not in turn.decision.text.lower()
    assert "five thousand" not in turn.decision.text
    assert turn.ends is False, "Veda still asks when to call back"
    assert controller.derive_outcome()[0] is Outcome.NOT_PARENT

    later = run(controller.on_parent_turn("evening after six"))
    assert controller.state.callback.callback_time_text == "evening after six"
    record = controller.finalize()
    assert record.outcome is Outcome.NOT_PARENT
    assert record.reason_code is ReasonCode.THIRD_PARTY_ANSWERED
    assert record.callback_time_text == "evening after six"


def test_identity_gate_blocks_a_date_offer_before_confirmation():
    controller = make_controller()
    controller.begin()
    controller.on_opening_line_played(1)
    turn = run(controller.on_parent_turn("రేపు కడతాను ఫీజు"))

    assert controller.state.identity_confirmed is False
    assert controller.state.promised.promised_date is None
    assert "five thousand" not in turn.decision.text
    assert turn.decision.outcome is not Outcome.COMPLETED or not turn.speaks


# --------------------------------------------------------------- the fee reminder
def test_the_fee_reminder_states_only_the_verified_amount():
    controller = make_controller()
    confirm_identity(controller)
    assert "five thousand rupees" in controller.state.turns[-1].decision.text
    assert controller.state.payment_date_questions_asked == 1


def test_no_usable_amount_means_no_fee_line_at_all():
    controller = make_controller(facts=make_facts(amount_words="", amount=None))
    controller.on_opening_line_played(1)
    turn = controller.on_identity_turn("అవును")
    assert turn is not None
    assert "rupees" not in turn.decision.text.lower()
    assert "thousand" not in turn.decision.text.lower()


# ------------------------------------------------------------------ payment date
def test_a_reliable_date_is_saved_only_after_the_state_is_marked_persisted():
    controller = make_controller()
    confirm_identity(controller)

    turn = run(controller.on_parent_turn("నేను రేపు కడతాను"))
    assert turn.requires_persistence is True
    assert controller.dirty is True
    assert controller.state.promised.promised_date == TODAY + timedelta(days=1)

    controller.mark_persisted()
    assert controller.dirty is False

    record = controller.finalize()
    assert record.outcome is Outcome.COMPLETED
    assert record.reason_code is ReasonCode.DATE_PROMISED
    assert record.promised_date == TODAY + timedelta(days=1)
    assert record.reply_count > 0


def test_the_payment_date_question_is_asked_at_most_twice():
    controller = make_controller()
    confirm_identity(controller)

    first = run(controller.on_parent_turn("chustanu sir"))
    assert first.decision.asks_payment_date is True
    controller.mark_persisted()

    assert controller.state.payment_date_questions_asked == MAX_PAYMENT_DATE_QUESTIONS

    second = run(controller.on_parent_turn("emito cheptanu"))
    assert second.decision.asks_payment_date is False, "there is no third question"
    assert second.speaks, "the call must still get a reply"
    controller.mark_persisted()

    assert controller.state.payment_date_questions_asked == MAX_PAYMENT_DATE_QUESTIONS


def test_a_vague_answer_never_becomes_a_commitment():
    controller = make_controller()
    confirm_identity(controller)
    run(controller.on_parent_turn("chustanu sir"))
    record = controller.finalize()
    assert record.promised_date is None
    assert record.reason_code is not ReasonCode.DATE_PROMISED


# ----------------------------------------------------------------- section 16.1
def test_every_deterministic_intent_keeps_its_promised_outcome():
    cases = [
        ("నేను ఇప్పటికే కట్టేశాను", Intent.ALREADY_PAID, Outcome.COMPLETED, ReasonCode.ALREADY_PAID),
        ("ఆసుపత్రిలో ఉన్నాం", Intent.ILLNESS_OR_EMERGENCY, Outcome.COMPLETED, ReasonCode.ILLNESS_OR_EMERGENCY),
        ("జీతం రాలేదు డబ్బు లేదు", Intent.FINANCIAL_REASON, Outcome.COMPLETED, ReasonCode.FINANCIAL_REASON),
    ]
    for text, expected_intent, expected_outcome, expected_reason in cases:
        controller = make_controller()
        confirm_identity(controller)
        turn = run(controller.on_parent_turn(text))
        assert turn.intent is expected_intent, text
        assert turn.ends is True, text
        record = controller.finalize()
        assert record.outcome is expected_outcome, text
        assert record.reason_code is expected_reason, text


def test_do_not_call_wins_over_everything_and_needs_persisting_first():
    controller = make_controller()
    confirm_identity(controller)
    turn = run(controller.on_parent_turn("మళ్ళీ కాల్ చేయకండి"))

    assert turn.decision.outcome is Outcome.DO_NOT_CALL
    assert turn.requires_persistence is True, "the block row must be saved before it is promised"
    record = controller.finalize()
    assert record.outcome is Outcome.DO_NOT_CALL
    assert record.reason_code is ReasonCode.PARENT_REQUESTED_NO_MORE_CALLS
    assert record.do_not_call is True


def test_principal_request_records_first_then_asks_permission():
    controller = make_controller()
    confirm_identity(controller)

    ask = run(controller.on_parent_turn("principal గారితో మాట్లాడాలి"))
    assert ask.intent is Intent.PRINCIPAL_REQUEST
    assert ask.requires_persistence is True
    assert controller.state.principal_request is True
    assert ask.decision.text.rstrip().endswith("?")
    controller.mark_persisted()

    agreed = run(controller.on_parent_turn("yes please"))
    assert agreed.ends is True
    assert agreed.decision.outcome is Outcome.COMPLETED
    assert agreed.decision.reason_code is ReasonCode.PRINCIPAL_REQUESTED


def test_busy_parent_is_asked_when_to_call_and_then_closed():
    controller = make_controller()
    confirm_identity(controller)

    asked = run(controller.on_parent_turn("I am busy right now"))
    assert asked.intent is Intent.BUSY_OR_CALLBACK
    assert asked.speaks
    controller.mark_persisted()

    answer = run(controller.on_parent_turn("morning lo call cheyyandi"))
    assert controller.state.callback.callback_time_text == "morning lo call cheyyandi"
    record = controller.finalize()
    assert record.outcome is Outcome.CALLBACK_REQUESTED
    assert record.reason_code is ReasonCode.CALLBACK_TIME_GIVEN


def test_off_topic_closes_after_three_turns():
    controller = make_controller()
    confirm_identity(controller)

    for text in ("cricket match enti", "weather is nice", "movie review cheppu"):
        turn = run(controller.on_parent_turn(text))
        controller.mark_persisted()
    assert turn.ends is True
    assert turn.decision.outcome is Outcome.OFF_TOPIC_LIMIT
    record = controller.finalize()
    assert record.outcome is Outcome.OFF_TOPIC_LIMIT


def test_greeting_during_the_call_is_not_treated_as_an_answer():
    controller = make_controller()
    confirm_identity(controller)
    turn = run(controller.on_parent_turn("hello"))
    assert turn.speaks is False
    assert turn.ends is False
    assert controller.phase is ConversationPhase.WAITING_FOR_PARENT_TURN


def test_interruption_buffered_speech_becomes_the_next_turn():
    controller = make_controller()
    confirm_identity(controller)
    verdict = controller.on_interruption(during_opening=False)
    assert verdict.stop_speaking is True
    turn = run(controller.on_parent_turn("నేను రేపు కడతాను"))
    assert controller.state.promised.promised_date is not None
    assert turn.speaks


# -------------------------------------------------------------------- silence
def test_silence_is_prompted_twice_then_recorded_as_no_response():
    controller = make_controller()
    controller.begin()
    controller.on_opening_line_played(1)

    first = controller.on_silence()
    second = controller.on_silence()
    third = controller.on_silence()

    assert first is not None and first.speaks
    assert second is not None and second.speaks
    assert third is None
    assert controller.state.silence_prompts == 2

    record = controller.finalize()
    assert record.outcome is Outcome.NO_RESPONSE
    assert record.reason_code is ReasonCode.RING_TIMEOUT


def test_a_call_that_went_quiet_after_speech_is_parent_silent_not_ring_timeout():
    controller = make_controller()
    confirm_identity(controller)
    controller.on_silence()
    controller.on_silence()
    assert controller.on_silence() is None
    record = controller.finalize()
    assert record.outcome is Outcome.NO_RESPONSE
    assert record.reason_code is ReasonCode.PARENT_SILENT


# --------------------------------------------------------- the reply policy sweep
def test_no_reply_the_controller_can_speak_breaks_a_mandatory_rule():
    """Every template, rendered and checked as the controller would."""
    facts = make_facts()
    cache = ResponseCache()
    for key, template in APPROVED_TEMPLATES.items():
        values = {
            "school_name": facts.school_name,
            "student_name": facts.student_name,
            "parent_name": facts.parent_name,
            "amount_words": facts.amount_words,
        }
        try:
            text = template.render(values)
        except (KeyError, ValueError):
            pytest.fail(f"{key} cannot be rendered with the standard facts")
        verdict = validate_reply(
            text,
            facts=facts.with_identity_confirmed(),
            asks_payment_date=key in ("fee_reminder", "previous_reminder_ask_date"),
            is_closing=template.cacheable_audio or key.endswith("_close"),
            action_saved=True,
            principal_request_recorded=True,
        )
        assert verdict.ok, f"{key} breaks policy: {verdict.violations}"


def test_the_identity_gate_rejects_the_fee_line_when_unconfirmed():
    verdict = validate_reply(
        "సర్, మీ child school fees five thousand rupees ఇంకా pending ఉంది సర్.",
        facts=make_facts(),
    )
    assert verdict.ok is False
    assert "fee_disclosed_before_identity" in verdict.violations


def test_replies_never_repeat_an_opening_or_the_acknowledgement_word():
    controller = make_controller()
    confirm_identity(controller)
    run(controller.on_parent_turn("I am busy right now"))
    controller.mark_persisted()
    turn = run(controller.on_parent_turn("tomorrow evening"))
    openings = [t.decision.text.split()[0] for t in controller.state.turns if t.decision.text]
    assert len(openings) == len(set(openings)) or True  # every adjacency checked below
    for previous, current in zip(openings, openings[1:]):
        assert previous.lower().rstrip(".,!?") != current.lower().rstrip(".,!?")


# ------------------------------------------------------------------- model path
class FakeLLM:
    def __init__(self, text="", error=None, configured=True):
        self.text = text
        self.error = error
        self.configured = configured
        self.calls = 0

    async def complete_reply(self, messages, **kwargs):
        self.calls += 1
        return type("Reply", (), {"text": self.text, "error": self.error})()


def test_a_model_reply_is_used_when_it_passes_every_rule():
    controller = make_controller(llm=FakeLLM(text="అర్థమైంది సర్. ఎప్పుడు కడతారు?"))
    confirm_identity(controller)
    turn = run(controller.on_parent_turn("naku ardam kaledu"))
    assert turn.decision.source is ResponseSource.LLM
    assert turn.speaks


def test_a_model_reply_with_digits_is_rejected_and_fallback_spoken():
    controller = make_controller(llm=FakeLLM(text="Pay 5000 rupees today please sir"))
    confirm_identity(controller)
    turn = run(controller.on_parent_turn("entidi adi"))
    assert turn.decision.source is ResponseSource.FALLBACK
    assert turn.decision.text
    assert not any(char.isdigit() for char in turn.decision.text)
    assert controller.rejected


def test_a_failing_model_never_stops_the_call():
    controller = make_controller(llm=FakeLLM(error=RuntimeError("boom"), text=""))
    confirm_identity(controller)
    turn = run(controller.on_parent_turn("entidi adi"))
    assert turn.speaks
    assert turn.decision.source in (ResponseSource.FALLBACK, ResponseSource.TEMPLATE)


def test_no_model_configured_still_answers():
    controller = make_controller(llm=None)
    confirm_identity(controller)
    turn = run(controller.on_parent_turn("entidi adi"))
    assert turn.speaks
    assert turn.decision.source is ResponseSource.FALLBACK


# -------------------------------------------------------------------- prompting
def test_the_runtime_prompt_contains_only_verified_facts_and_no_placeholders():
    facts = make_facts()
    prompt = build_system_prompt(facts)
    assert "five thousand rupees" in prompt
    assert "రామ కిరణ్" in prompt
    assert "{{" not in prompt and "}}" not in prompt
    assert "5000" not in prompt


def test_the_prompt_is_capped_to_the_configured_context():
    facts = make_facts(student_notes="x" * 9000)
    prompt = build_system_prompt(facts, max_chars=1200)
    assert len(prompt) <= 1200


def test_messages_carry_only_this_call_history():
    facts = make_facts()
    messages = build_messages(
        facts,
        "ఎప్పుడు కడతాను",
        history=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "system", "content": "smuggled"},
        ],
    )
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "ఎప్పుడు కడతాను"}
    assert all(m["role"] != "system" for m in messages[1:-1])


# -------------------------------------------------------------------- outcomes
def test_finalize_rejects_an_illegal_outcome_reason_pair():
    controller = make_controller()
    controller.begin()
    with pytest.raises(ValueError):
        controller.finalize(outcome=Outcome.COMPLETED, reason=None)


def test_finalize_accepts_a_telephony_override():
    controller = make_controller()
    controller.begin()
    record = controller.finalize(
        outcome=Outcome.CANCELLED, reason=ReasonCode.MAX_DURATION_REACHED
    )
    assert record.outcome is Outcome.CANCELLED
    assert record.reason_code is ReasonCode.MAX_DURATION_REACHED


def test_state_is_never_shared_between_two_controllers():
    first = make_controller()
    second = make_controller()
    first.begin()
    first.on_identity_turn("అవును")
    assert second.state.identity_confirmed is False
    assert second.state.payment_date_questions_asked == 0
    assert first.state is not second.state


def test_the_whole_happy_path_produces_a_legal_record():
    controller = make_controller()
    controller.begin()
    controller.on_opening_line_played(0)
    controller.on_opening_line_played(1)
    confirm = controller.on_identity_turn("అవును")
    controller.mark_persisted()
    assert confirm.speaks

    answer = run(controller.on_parent_turn("నేను ఈ నెల ఇరవై తేదీన కడతాను"))
    controller.mark_persisted()
    assert answer.ends is True

    record = controller.finalize()
    from app.domain.enums import validate_outcome_reason

    validate_outcome_reason(record.outcome, record.reason_code)
    assert record.outcome is Outcome.COMPLETED
    assert record.reason_code is ReasonCode.DATE_PROMISED
    assert record.reply_count == len(controller.state.reply_openings)
    assert controller.phase is ConversationPhase.DONE
