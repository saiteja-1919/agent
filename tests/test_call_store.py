"""One details file per call, claimed atomically (P10, P11, P12, P17)."""

import os
import tempfile
import time
import unittest
from pathlib import Path

import stubs

stubs.install()

from call_store import (  # noqa: E402
    claim_pending,
    discard_pending,
    list_pending,
    new_call_ref,
    phone_tail,
    purge_stale,
    save_pending,
    validate_details,
)


def details(**overrides):
    base = {
        "call_ref": new_call_ref(),
        "to": "+919876543210",
        "student_name": "Sai Teja",
        "student_name_spoken": "సాయి తేజ",
        "parent_name": "",
        "parent_name_spoken": "",
        "school_name": "Test Public School",
        "school_name_spoken": "టెస్ట్ పబ్లిక్ స్కూల్",
        "amount": "5000",
        "amount_words": "five thousand rupees",
        "due_date": "",
        "student_notes": "",
        "greeting_lines": ["line one", "line two"],
    }
    base.update(overrides)
    return base


class PhoneTailTest(unittest.TestCase):
    def test_tail_of_a_number(self):
        self.assertEqual(phone_tail("+919876543210"), "9876543210")
        self.assertEqual(phone_tail("09876543210"), "9876543210")
        self.assertEqual(phone_tail(""), "")


class SavePendingTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def test_one_file_per_call_with_all_fields(self):
        path = save_pending(details(), self.folder)
        self.assertTrue(path.exists())
        self.assertIn("call_ref", path.read_text(encoding="utf-8"))
        self.assertEqual(len(list_pending(self.folder)), 1)

    def test_two_calls_do_not_share_a_file(self):
        first = save_pending(details(student_name="One"), self.folder)
        second = save_pending(details(student_name="Two"), self.folder)
        self.assertNotEqual(first, second)
        self.assertEqual(len(list_pending(self.folder)), 2)

    def test_no_temporary_files_are_left_behind(self):
        save_pending(details(), self.folder)
        self.assertEqual([p.name for p in self.folder.iterdir() if ".tmp" in p.name], [])

    def test_stale_files_are_purged_but_fresh_ones_are_kept(self):
        # P12: a dialer once deleted every pending file, including a live call.
        old = save_pending(details(), self.folder)
        fresh = save_pending(details(), self.folder)
        ancient = time.time() - 3600
        os.utime(old, (ancient, ancient))
        removed = purge_stale(self.folder)
        self.assertEqual(removed, 1)
        self.assertTrue(fresh.exists())
        self.assertFalse(old.exists())

    def test_saving_keeps_fresh_files(self):
        fresh = save_pending(details(), self.folder)
        save_pending(details(), self.folder)
        self.assertTrue(fresh.exists())


class ClaimPendingTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def test_match_on_the_dialled_number(self):
        save_pending(details(to="+919876543210"), self.folder)
        claimed = claim_pending("+919876543210", "+914041895032", self.folder)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["student_name"], "Sai Teja")

    def test_match_on_either_side_because_from_and_to_are_not_settled(self):
        save_pending(details(to="+919876543210"), self.folder)
        claimed = claim_pending("+914041895032", "+919876543210", self.folder)
        self.assertIsNotNone(claimed)

    def test_two_pending_calls_and_no_match_is_refused(self):
        # P11: never hand one parent another parent's details.
        save_pending(details(to="+919876543210"), self.folder)
        save_pending(details(to="+919000000001"), self.folder)
        self.assertIsNone(claim_pending("+919999999999", "+914041895032", self.folder))

    def test_a_single_pending_call_is_used_as_a_fallback(self):
        save_pending(details(to="+919876543210"), self.folder)
        claimed = claim_pending("", "", self.folder)
        self.assertIsNotNone(claimed)

    def test_no_pending_calls_at_all(self):
        self.assertIsNone(claim_pending("+919876543210", "", self.folder))

    def test_claiming_twice_returns_none_the_second_time(self):
        # Two streams must never both get the same call (P10).
        save_pending(details(to="+919876543210"), self.folder)
        first = claim_pending("+919876543210", "", self.folder)
        second = claim_pending("+919876543210", "", self.folder)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_stale_details_are_not_claimed(self):
        path = save_pending(details(to="+919876543210"), self.folder)
        ancient = time.time() - 3600
        os.utime(path, (ancient, ancient))
        self.assertIsNone(claim_pending("+919876543210", "", self.folder))

    def test_corrupt_file_does_not_break_claiming(self):
        save_pending(details(to="+919876543210"), self.folder)
        (self.folder / "call_broken.json").write_text("{not json", encoding="utf-8")
        claimed = claim_pending("+919876543210", "", self.folder)
        self.assertIsNotNone(claimed)


class DiscardTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def test_only_our_own_file_is_removed(self):
        ours = save_pending(details(to="+919876543210"), self.folder)
        theirs = save_pending(details(to="+919000000001"), self.folder)
        our_ref = ours.name.split("_")[-1].replace(".json", "")
        self.assertTrue(discard_pending(our_ref, self.folder))
        self.assertFalse(ours.exists())
        self.assertTrue(theirs.exists())

    def test_discarding_an_unknown_ref_changes_nothing(self):
        keep = save_pending(details(), self.folder)
        self.assertFalse(discard_pending("nosuchref", self.folder))
        self.assertTrue(keep.exists())


class ValidateDetailsTest(unittest.TestCase):
    def test_a_complete_call_has_no_problems(self):
        self.assertEqual(validate_details(details()), [])

    def test_missing_fields_are_listed(self):
        broken = details()
        broken["student_name"] = ""
        broken["amount_words"] = ""
        problems = validate_details(broken)
        self.assertIn("student_name is missing", problems)
        self.assertIn("amount_words is missing", problems)

    def test_amount_must_be_words_not_digits(self):
        # P25: digits reach the voice as Telugu words and sound robotic.
        self.assertIn("amount_words must be words, not digits", validate_details(details(amount_words="5000")))

    def test_greeting_must_be_the_two_opening_lines(self):
        self.assertTrue(any("greeting_lines" in problem for problem in validate_details(details(greeting_lines=[]))))
        self.assertTrue(
            any("greeting_lines" in problem for problem in validate_details(details(greeting_lines=["only one"])))
        )

    def test_numbers_must_look_like_numbers(self):
        self.assertTrue(any("to is not a phone number" in p for p in validate_details(details(to="hello"))))

    def test_long_notes_are_refused(self):
        problems = validate_details(details(student_notes="x" * 500))
        self.assertTrue(any("student_notes" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
