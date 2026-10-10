"""A promised date may only be saved when it is reliably extracted."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.domain.dateparse import extract_payment_date, looks_vague

TODAY = date(2026, 3, 15)  # a Sunday, so weekday arithmetic is easy to reason about


class TestReliableDates(unittest.TestCase):
    def test_relative_days_english(self):
        result = extract_payment_date("I will pay in two days sir", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, TODAY + timedelta(days=2))

    def test_relative_days_with_digits(self):
        result = extract_payment_date("after 5 days I will pay", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, TODAY + timedelta(days=5))

    def test_relative_days_romanised_telugu(self):
        result = extract_payment_date("rendu rojullo kaduthanu", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, TODAY + timedelta(days=2))

    def test_relative_days_telugu_script(self):
        result = extract_payment_date("రెండు రోజుల్లో కడతాను", TODAY)
        self.assertTrue(result.is_reliable, result)
        self.assertEqual(result.date, TODAY + timedelta(days=2))

    def test_relative_words_telugu_script(self):
        result = extract_payment_date("ఒక వారంలో కడతాను", TODAY)
        self.assertTrue(result.is_reliable, result)
        self.assertEqual(result.date, TODAY + timedelta(days=7))

    def test_tomorrow_all_spellings(self):
        for text in ("I will pay tomorrow", "repu kaduthanu", "రేపు కడతాను"):
            with self.subTest(text=text):
                result = extract_payment_date(text, TODAY)
                self.assertTrue(result.is_reliable, text)
                self.assertEqual(result.date, TODAY + timedelta(days=1))

    def test_today_is_a_date(self):
        result = extract_payment_date("ivvala kattestanu", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, TODAY)

    def test_day_after_tomorrow(self):
        self.assertEqual(
            extract_payment_date("day after tomorrow", TODAY).date, TODAY + timedelta(days=2)
        )
        self.assertEqual(extract_payment_date("ఎల్లుండి", TODAY).date, TODAY + timedelta(days=2))

    def test_next_week_is_accepted_at_lower_confidence(self):
        result = extract_payment_date("next week I will pay", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, TODAY + timedelta(days=7))
        self.assertLess(result.confidence, 0.8)

    def test_weekday_without_prefix_is_the_coming_one(self):
        result = extract_payment_date("I will pay on Sunday", TODAY)
        self.assertTrue(result.is_reliable)
        # "Sunday" said on a Sunday means the coming Sunday, not today.
        self.assertEqual(result.date, TODAY + timedelta(days=7))

    def test_weekday_next_is_further_out(self):
        result = extract_payment_date("next Saturday please", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertGreaterEqual((result.date - TODAY).days, 6)

    def test_day_month_english(self):
        result = extract_payment_date("I will pay on 20 March", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, date(2026, 3, 20))

    def test_month_day_english(self):
        result = extract_payment_date("March 20 is fine", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, date(2026, 3, 20))

    def test_iso_date(self):
        result = extract_payment_date("pay by 2026-04-02", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, date(2026, 4, 2))

    def test_unambiguous_numeric_date(self):
        result = extract_payment_date("I will pay on 20/04", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, date(2026, 4, 20))

    def test_month_end(self):
        result = extract_payment_date("by month end sir", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, date(2026, 3, 31))

    def test_unrelated_month_word_does_not_hide_a_real_date(self):
        # "pay 2 days" matches the shape of a month-day pair but "pay" is not a
        # month, so the later real date must still be found.
        result = extract_payment_date("you may pay 20 March", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, date(2026, 3, 20))


class TestRefusals(unittest.TestCase):
    def test_ambiguous_numeric_date_is_not_reliable(self):
        result = extract_payment_date("I will pay on 10/10", TODAY)
        self.assertFalse(result.is_reliable)
        self.assertTrue(result.is_ambiguous)
        self.assertEqual(result.reason, "ambiguous_numeric_date")

    def test_vague_answers_are_not_dates(self):
        for text in ("soon", "I will see", "చూస్తాను", "chustanu", "as early as possible"):
            with self.subTest(text=text):
                result = extract_payment_date(text, TODAY)
                self.assertFalse(result.is_reliable, text)
                self.assertTrue(result.is_vague, text)

    def test_vague_flag_helper(self):
        self.assertTrue(looks_vague("చూస్తాను అన్నాను"))
        self.assertFalse(looks_vague("I will pay on 20 March"))

    def test_past_reference_is_not_a_commitment(self):
        result = extract_payment_date("I paid yesterday", TODAY)
        self.assertFalse(result.is_reliable)
        self.assertEqual(result.reason, "past_date")

    def test_too_far_ahead_is_refused(self):
        result = extract_payment_date("I will pay in 400 days", TODAY)
        self.assertFalse(result.is_reliable)
        self.assertEqual(result.reason, "too_far_ahead")

    def test_soon_but_valid_offset_is_accepted(self):
        result = extract_payment_date("I will pay in 300 days", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, TODAY + timedelta(days=300))

    def test_empty_text(self):
        result = extract_payment_date("", TODAY)
        self.assertFalse(result.is_reliable)
        self.assertEqual(result.reason, "empty")

    def test_no_date_found(self):
        result = extract_payment_date("the fee is pending with the office", TODAY)
        self.assertFalse(result.is_reliable)

    def test_past_month_rolls_to_next_year(self):
        result = extract_payment_date("I will pay on 5 January", TODAY)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.date, date(2027, 1, 5))

    def test_invalid_day_for_month_is_refused_not_guessed(self):
        result = extract_payment_date("I will pay on 31 February", TODAY)
        self.assertFalse(result.is_reliable)

    def test_extraction_never_raises(self):
        for text in ("", "   ", "??", "\u0000", "5 5 5 5", "on the", "30/30/3030"):
            with self.subTest(text=text):
                extract_payment_date(text, TODAY)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
