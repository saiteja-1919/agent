"""Deduplication keys must be stable, and safe to store and print."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.ingestion import deduplication


class TestIdentityHash(unittest.TestCase):
    def test_stable_for_the_same_person(self):
        first = deduplication.identity_hash("Alekhya Rao", "+919876500001")
        second = deduplication.identity_hash("Alekhya Rao", "+919876500001")
        self.assertEqual(first, second)

    def test_case_and_spacing_insensitive(self):
        a = deduplication.identity_hash("Alekhya Rao", "+919876500001")
        b = deduplication.identity_hash("  alekhya   rao ", "+919876500001")
        self.assertEqual(a, b)

    def test_different_numbers_are_different_people(self):
        a = deduplication.identity_hash("Alekhya Rao", "+919876500001")
        b = deduplication.identity_hash("Alekhya Rao", "+919876500002")
        self.assertNotEqual(a, b)

    def test_does_not_leak_the_name_or_number(self):
        value = deduplication.identity_hash("Alekhya Rao", "+919876500001")
        self.assertNotIn("alekhya", value.lower())
        self.assertNotIn("98765", value)

    def test_is_reasonably_short(self):
        self.assertEqual(len(deduplication.identity_hash("A", "+919876500001")), 12)


class TestPeriodAndBusinessKey(unittest.TestCase):
    def test_period_key_is_the_due_month(self):
        self.assertEqual(deduplication.period_key(date(2026, 11, 20)), "2026-11")

    def test_period_key_falls_back(self):
        self.assertEqual(deduplication.period_key(None, fallback="term2"), "term2")
        self.assertEqual(deduplication.period_key(None), "unknown")

    def test_business_key_is_deterministic(self):
        first = deduplication.business_key(
            school_id=1, student_name="Alekhya Rao", parent_phone="+919876500001", period="2026-11"
        )
        second = deduplication.business_key(
            school_id=1, student_name="alekhya  rao", parent_phone="+919876500001", period="2026-11"
        )
        self.assertEqual(first, second)

    def test_business_key_changes_with_the_period(self):
        a = deduplication.business_key(
            school_id=1, student_name="A", parent_phone="+919876500001", period="2026-11"
        )
        b = deduplication.business_key(
            school_id=1, student_name="A", parent_phone="+919876500001", period="2026-12"
        )
        self.assertNotEqual(a, b)

    def test_business_key_is_safe_to_store_and_print(self):
        key = deduplication.business_key(
            school_id=2, student_name="Alekhya Rao", parent_phone="+919876500001", period="2026-11"
        )
        self.assertNotIn("Alek", key)
        self.assertNotIn("98765", key)


class TestDescribeIdentity(unittest.TestCase):
    def test_masks_both_parts(self):
        described = deduplication.describe_identity("Alekhya Rao", "+919876500001")
        self.assertIn("A", described)
        self.assertNotIn("Alekhya", described)
        self.assertNotIn("9876500", described)


class TestFileChecksum(unittest.TestCase):
    def test_same_bytes_same_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.csv"
            second = Path(tmp) / "b.csv"
            first.write_bytes(b"student_name,parent_phone\nA,9876500001\n")
            second.write_bytes(first.read_bytes())
            self.assertEqual(
                deduplication.file_checksum(first), deduplication.file_checksum(second)
            )

    def test_different_bytes_different_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.csv"
            second = Path(tmp) / "b.csv"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            self.assertNotEqual(
                deduplication.file_checksum(first), deduplication.file_checksum(second)
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
