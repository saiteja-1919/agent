"""The deterministic layer must hold even when the model misbehaves."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from app.domain.enums import Intent
from app.domain.models import VerifiedFacts
from app.domain.policies import (
    DEAD_END_CLOSING,
    DIGITS_IN_SPEECH,
    EMPTY_REPLY,
    FEE_DISCLOSED_BEFORE_IDENTITY,
    FORBIDDEN_FINANCIAL_REQUEST,
    INVENTED_AMOUNT,
    MAX_OFF_TOPIC_TURNS,
    MAX_PAYMENT_DATE_QUESTIONS,
    MAX_SILENCE_PROMPTS,
    PAYMENT_QUESTION_LIMIT,
    PRINCIPAL_CLAIM_WITHOUT_RECORD,
    PROMISED_DISCOUNT,
    PROMPT_LEAKAGE,
    REPEATED_ACKNOWLEDGEMENT,
    REPEATED_OPENING,
    THREATENING_LANGUAGE,
    TOO_LONG,
    TOO_MANY_QUESTIONS,
    UNSUPPORTED_ACTION_CLAIM,
    UNSUPPORTED_ACTION_CLAIM as _UNSUPPORTED,
    can_ask_payment_date,
    can_prompt_silence,
    classify_identity_answer,
    classify_intent,
    is_pure_greeting,
    mask_name,
    mask_phone,
    sanitise_reply,
    should_close_off_topic,
    validate_reply,
)

TODAY = date(2026, 3, 15)

FACTS = VerifiedFacts(
    school_name="విద్యాలయం",
    student_name="సాయి తేజ",
    parent_name="రమేష్",
    amount_words="five thousand rupees",
    amount=Decimal("5000"),
    due_date_words="tenth October",
    today_words="fifteenth March",
)

FACTS_CONFIRMED = FACTS.with_identity_confirmed()


class TestIdentityAnswers(unittest.TestCase):
    def test_clear_yes(self):
        for text in ("avunu nenu a chepandi", "అవును", "yes speaking", "haan cheppandi"):
            with self.subTest(text=text):
                answer = classify_identity_answer(text)
                self.assertTrue(answer.confirmed, text)
                self.assertFalse(answer.unclear)

    def test_clear_no(self):
        for text in ("kaadu", "కాదు", "no he is not here"):
            with self.subTest(text=text):
                answer = classify_identity_answer(text)
                self.assertTrue(answer.denied, text)

    def test_wrong_number(self):
        answer = classify_identity_answer("sorry wrong number")
        self.assertTrue(answer.wrong_number)
        self.assertFalse(answer.unclear)

    def test_third_party(self):
        answer = classify_identity_answer("he is not available, I am his son")
        self.assertTrue(answer.third_party)

    def test_unclear_is_the_safe_default(self):
        answer = classify_identity_answer("hmm the weather is nice")
        self.assertTrue(answer.unclear)
        self.assertFalse(answer.confirmed)
        self.assertFalse(answer.denied)
        self.assertTrue(answer.needs_reask)

    def test_empty_is_unclear(self):
        self.assertTrue(classify_identity_answer("").unclear)


class TestGreetingDetection(unittest.TestCase):
    def test_pure_greetings(self):
        for text in ("hello", "హలో", "hmm", "haan", "hello sir", "చెప్పండి", ""):
            with self.subTest(text=text):
                self.assertTrue(is_pure_greeting(text), text)

    def test_a_real_answer_is_not_a_greeting(self):
        for text in ("avunu nenu matladutunnanu", "kaadu", "అవును"):
            with self.subTest(text=text):
                self.assertFalse(is_pure_greeting(text), text)


class TestIntentClassification(unittest.TestCase):
    def _intent(self, text, **kwargs):
        return classify_intent(text, today=TODAY, **kwargs)

    def test_wrong_number(self):
        self.assertEqual(self._intent("sorry wrong number").intent, Intent.WRONG_NUMBER)

    def test_do_not_call(self):
        result = self._intent("please don't call me again")
        self.assertEqual(result.intent, Intent.DO_NOT_CALL)

    def test_asked_to_stop(self):
        self.assertEqual(self._intent("ok bye").intent, Intent.ASKED_TO_STOP)

    def test_already_paid(self):
        result = self._intent("I already paid the fee last week")
        self.assertEqual(result.intent, Intent.ALREADY_PAID)

    def test_previous_reminder_complaint(self):
        result = self._intent("I already told the school, why do you call again")
        self.assertIn(Intent.PREVIOUS_REMINDER, result.signals)

    def test_illness(self):
        self.assertEqual(self._intent("my wife is in hospital").intent, Intent.ILLNESS_OR_EMERGENCY)

    def test_principal_request(self):
        self.assertEqual(
            self._intent("I want to talk to the principal").intent, Intent.PRINCIPAL_REQUEST
        )

    def test_ai_question(self):
        self.assertEqual(self._intent("are you a robot?").intent, Intent.AI_QUESTION)

    def test_fee_question(self):
        self.assertEqual(self._intent("how much is the fee?").intent, Intent.FEE_QUESTION)

    def test_financial_reason(self):
        self.assertEqual(
            self._intent("I have no money, salary not received").intent,
            Intent.FINANCIAL_REASON,
        )

    def test_busy(self):
        self.assertEqual(self._intent("sorry I am busy driving").intent, Intent.BUSY_OR_CALLBACK)

    def test_silence(self):
        self.assertEqual(self._intent("").intent, Intent.SILENCE)

    def test_greeting_only(self):
        self.assertEqual(self._intent("hello").intent, Intent.GREETING_ONLY)

    def test_who_is_calling(self):
        self.assertEqual(self._intent("who is this?").intent, Intent.WHO_IS_CALLING)

    def test_payment_date_with_cue(self):
        result = self._intent("I will pay tomorrow")
        self.assertEqual(result.intent, Intent.PAYMENT_DATE_GIVEN)
        self.assertTrue(result.date.is_reliable)

    def test_payment_date_explicit_needs_no_context(self):
        result = self._intent("I will pay on 20 March")
        self.assertEqual(result.intent, Intent.PAYMENT_DATE_GIVEN)

    def test_bare_keyword_date_needs_context(self):
        # In the payment flow the controller has asked, so "repu" is an answer.
        result = self._intent("repu", date_context=True)
        self.assertEqual(result.intent, Intent.PAYMENT_DATE_GIVEN)

    def test_keyword_date_outside_payment_context_is_not_a_commitment(self):
        result = self._intent("the weather is nice today")
        self.assertNotEqual(result.intent, Intent.PAYMENT_DATE_GIVEN)
        self.assertNotIn(Intent.PAYMENT_DATE_GIVEN, result.signals)

    def test_vague_answer(self):
        result = self._intent("చూస్తాను")
        self.assertEqual(result.intent, Intent.VAGUE_ANSWER)
        self.assertFalse(result.date.is_reliable)

    def test_unknown_is_low_confidence(self):
        result = self._intent("the school bus was late")
        self.assertEqual(result.intent, Intent.UNKNOWN)
        self.assertLess(result.confidence, 0.5)

    def test_off_topic(self):
        self.assertEqual(self._intent("did you watch the cricket match").intent, Intent.OFF_TOPIC)


class TestApprovedWordingsPassPolicy(unittest.TestCase):
    """The binding wordings in section 16.1 must never be blocked by a guard."""

    def test_fee_line(self):
        verdict = validate_reply(
            "సర్, మీ child school fees five thousand rupees ఇంకా pending ఉంది సర్. "
            "ఎప్పటికల్లా కడతారో తెలుసుకోవచ్చా?",
            facts=FACTS_CONFIRMED,
            asks_payment_date=True,
        )
        self.assertTrue(verdict.ok, verdict.violations)

    def test_already_informed_line_with_date(self):
        verdict = validate_reply(
            "సారీ సర్, మా record లో ఇంకా update కాలేదు. ఆ date ఇప్పుడే note చేసుకుంటాను. "
            "Thank you, have a nice day.",
            facts=FACTS_CONFIRMED,
            is_closing=True,
            action_saved=True,
        )
        self.assertTrue(verdict.ok, verdict.violations)

    def test_principal_wording(self):
        verdict = validate_reply(
            "ఈ విషయం principal గారికి inform చేయమని office కి note చేస్తాను సర్. "
            "Principal గారు free అయ్యాక office నుండి మీకు inform చేయమంటారా?",
            facts=FACTS_CONFIRMED,
            principal_request_recorded=True,
        )
        self.assertTrue(verdict.ok, verdict.violations)

    def test_silence_check_in(self):
        verdict = validate_reply("హలో సర్, వినిపిస్తోందా?", facts=FACTS_CONFIRMED)
        self.assertTrue(verdict.ok, verdict.violations)

    def test_wrong_number_wording(self):
        verdict = validate_reply(
            "సారీ సర్, wrong number అయినట్టుంది. Disturb చేసినందుకు sorry, thank you.",
            facts=FACTS,
        )
        self.assertTrue(verdict.ok, verdict.violations)

    def test_third_party_wording_reveals_no_fee(self):
        verdict = validate_reply(
            "ఓకే సర్, parent గారితో school విషయం మాట్లాడాలి. ఎప్పుడు call చేస్తే బాగుంటుంది సర్?",
            facts=FACTS,
        )
        self.assertTrue(verdict.ok, verdict.violations)

    def test_ai_identity_wording(self):
        verdict = validate_reply(
            "అవును సర్, నేను Veda ని, school తరపున call చేస్తున్న automated assistant ని.",
            facts=FACTS_CONFIRMED,
        )
        self.assertTrue(verdict.ok, verdict.violations)

    def test_no_hours_false_positive(self):
        """\"hours\" contains \"rs\"; a substring test would wrongly block this."""
        verdict = validate_reply(
            "సర్, మీరు two hours back call చేసారా?", facts=FACTS
        )
        self.assertNotIn(FEE_DISCLOSED_BEFORE_IDENTITY, verdict.violations)


class TestReplyViolations(unittest.TestCase):
    def test_empty_reply(self):
        verdict = validate_reply("   ", facts=FACTS_CONFIRMED)
        self.assertFalse(verdict.ok)
        self.assertIn(EMPTY_REPLY, verdict.violations)

    def test_digits_are_never_spoken(self):
        verdict = validate_reply("మీ fee 5000 rupees pending", facts=FACTS_CONFIRMED)
        self.assertIn(DIGITS_IN_SPEECH, verdict.violations)

    def test_telugu_digits_are_never_spoken(self):
        verdict = validate_reply("మీ fee ౫౦౦౦ pending", facts=FACTS_CONFIRMED)
        self.assertIn(DIGITS_IN_SPEECH, verdict.violations)

    def test_fee_before_identity(self):
        verdict = validate_reply(
            "మీ fees five thousand rupees pending ఉంది", facts=FACTS
        )
        self.assertIn(FEE_DISCLOSED_BEFORE_IDENTITY, verdict.violations)

    def test_otp_request(self):
        verdict = validate_reply("please tell me the OTP you received", facts=FACTS_CONFIRMED)
        self.assertIn(FORBIDDEN_FINANCIAL_REQUEST, verdict.violations)

    def test_upi_pin_request(self):
        verdict = validate_reply("share your UPI PIN to pay now", facts=FACTS_CONFIRMED)
        self.assertIn(FORBIDDEN_FINANCIAL_REQUEST, verdict.violations)

    def test_card_details_request(self):
        verdict = validate_reply("give me your card number and CVV", facts=FACTS_CONFIRMED)
        self.assertIn(FORBIDDEN_FINANCIAL_REQUEST, verdict.violations)

    def test_discount_promise(self):
        verdict = validate_reply("I will give you a discount sir", facts=FACTS_CONFIRMED)
        self.assertIn(PROMISED_DISCOUNT, verdict.violations)

    def test_extension_promise(self):
        verdict = validate_reply("we can give an extension of one month", facts=FACTS_CONFIRMED)
        self.assertIn(PROMISED_DISCOUNT, verdict.violations)

    def test_threatening_language(self):
        verdict = validate_reply("we will take legal action against you", facts=FACTS_CONFIRMED)
        self.assertIn(THREATENING_LANGUAGE, verdict.violations)

    def test_unsupported_update_claim(self):
        verdict = validate_reply(
            "మీ record update అయింది సర్", facts=FACTS_CONFIRMED, action_saved=False
        )
        self.assertIn(_UNSUPPORTED, verdict.violations)

    def test_update_claim_allowed_when_something_was_saved(self):
        verdict = validate_reply(
            "మీ record update అయింది సర్", facts=FACTS_CONFIRMED, action_saved=True
        )
        self.assertNotIn(_UNSUPPORTED, verdict.violations)

    def test_principal_claim_without_record(self):
        verdict = validate_reply(
            "I have informed the principal about this", facts=FACTS_CONFIRMED
        )
        self.assertIn(PRINCIPAL_CLAIM_WITHOUT_RECORD, verdict.violations)

    def test_invented_amount(self):
        verdict = validate_reply(
            "మీ fees ten thousand rupees pending", facts=FACTS_CONFIRMED
        )
        self.assertIn(INVENTED_AMOUNT, verdict.violations)

    def test_verified_amount_is_fine(self):
        verdict = validate_reply(
            "మీ fees five thousand rupees pending ఉంది", facts=FACTS_CONFIRMED
        )
        self.assertNotIn(INVENTED_AMOUNT, verdict.violations)

    def test_too_long(self):
        verdict = validate_reply(
            " ".join(["సర్"] * 45), facts=FACTS_CONFIRMED
        )
        self.assertIn(TOO_LONG, verdict.violations)

    def test_too_many_questions(self):
        verdict = validate_reply("ఎప్పుడు కడతారు? ఎంత కడతారు?", facts=FACTS_CONFIRMED)
        self.assertIn(TOO_MANY_QUESTIONS, verdict.violations)

    def test_payment_question_limit(self):
        facts = VerifiedFacts(
            school_name="విద్యాలయం",
            student_name="సాయి తేజ",
            amount_words="five thousand rupees",
            identity_confirmed=True,
            payment_date_questions_asked=MAX_PAYMENT_DATE_QUESTIONS,
        )
        verdict = validate_reply("ఎప్పుడు కడతారు సర్?", facts=facts, asks_payment_date=True)
        self.assertIn(PAYMENT_QUESTION_LIMIT, verdict.violations)

    def test_repeated_opening_word(self):
        verdict = validate_reply(
            "సర్, ఈ విషయం note చేసుకున్నాను", facts=FACTS_CONFIRMED, previous_opening="సర్"
        )
        self.assertIn(REPEATED_OPENING, verdict.violations)

    def test_repeated_sare(self):
        verdict = validate_reply(
            "సరే సర్, note చేసుకున్నాను",
            facts=FACTS_CONFIRMED,
            acknowledgement_words_used=("సరే",),
        )
        self.assertIn(REPEATED_ACKNOWLEDGEMENT, verdict.violations)

    def test_prompt_leakage(self):
        verdict = validate_reply("my instructions say to ask about the fee", facts=FACTS_CONFIRMED)
        self.assertIn(PROMPT_LEAKAGE, verdict.violations)

    def test_placeholder_leakage(self):
        verdict = validate_reply("మీ fees {{amount_words}} pending", facts=FACTS_CONFIRMED)
        self.assertIn(PROMPT_LEAKAGE, verdict.violations)

    def test_dead_end_closing(self):
        verdict = validate_reply("sorry", facts=FACTS_CONFIRMED, is_closing=True)
        self.assertIn(DEAD_END_CLOSING, verdict.violations)

    def test_verdict_reports_every_violation_not_just_the_first(self):
        verdict = validate_reply(
            "మీ fee 5000 rupees pending, share the OTP sir", facts=FACTS_CONFIRMED
        )
        self.assertIn(DIGITS_IN_SPEECH, verdict.violations)
        self.assertIn(FORBIDDEN_FINANCIAL_REQUEST, verdict.violations)


class TestSanitiseReply(unittest.TestCase):
    def test_markdown_removed(self):
        self.assertEqual(sanitise_reply("**hello** _sir_"), "hello sir")

    def test_placeholders_removed(self):
        self.assertEqual(sanitise_reply("{{amount_words}} rupees"), "amount_words rupees")

    def test_whitespace_collapsed(self):
        self.assertEqual(sanitise_reply("hello   \n\n sir "), "hello sir")

    def test_curly_quotes_become_straight(self):
        self.assertEqual(sanitise_reply("“hello”"), '"hello"')


class TestQuestionAccounting(unittest.TestCase):
    def test_payment_date_question_limit_is_two(self):
        self.assertTrue(can_ask_payment_date(0))
        self.assertTrue(can_ask_payment_date(1))
        self.assertFalse(can_ask_payment_date(MAX_PAYMENT_DATE_QUESTIONS))
        self.assertFalse(can_ask_payment_date(5))

    def test_silence_prompts(self):
        self.assertTrue(can_prompt_silence(0))
        self.assertFalse(can_prompt_silence(MAX_SILENCE_PROMPTS))

    def test_off_topic_limit(self):
        self.assertFalse(should_close_off_topic(0))
        self.assertTrue(should_close_off_topic(MAX_OFF_TOPIC_TURNS))


class TestMasking(unittest.TestCase):
    def test_phone_is_masked_to_last_four(self):
        masked = mask_phone("+919876543210")
        self.assertTrue(masked.endswith("3210"))
        self.assertNotIn("987654", masked)

    def test_short_phone_is_fully_masked(self):
        self.assertEqual(mask_phone("12345"), "*****")

    def test_empty_phone(self):
        self.assertEqual(mask_phone(""), "")
        self.assertEqual(mask_phone(None), "")

    def test_name_is_masked(self):
        self.assertEqual(mask_name("Sai Teja"), "S** T***")

    def test_empty_name(self):
        self.assertEqual(mask_name(""), "")
        self.assertEqual(mask_name(None), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
