"""The audio cache must never serve a file it cannot prove is correct (13.7).

Every test here writes real bytes to a temporary directory and reads them back
through the validation path, because the failure this cache exists to prevent is
a wrong or truncated recording being played to a parent.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.cache.audio_cache import (
    MIN_AUDIO_BYTES,
    AudioCache,
)
from app.cache.response_cache import ResponseCache
from app.integrations.database.engine import Database, database_from_url
from app.integrations.database.migrate import create_schema_for_tests

SPEECH = b"\x00\x01" * 400  # 800 bytes of plausible 8 kHz mu-law silence
OPENER_TEXT = ResponseCache().render("opener_line_1", {"school_name": "బాలభారతి"})


class AudioCacheTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db: Database = database_from_url("sqlite+aiosqlite:///:memory:")
        await create_schema_for_tests(self.db)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = AudioCache(root=self.root, responses=ResponseCache(), voice="shubh", model="bulbul:v3")

    async def asyncTearDown(self) -> None:
        await self.db.dispose()
        self.tmp.cleanup()

    async def session(self):
        return self.db.session()


class TestWhatMayBeCached(AudioCacheTestCase):
    async def test_only_the_opening_closings_and_checkin_are_cacheable(self):
        keys = set(self.cache.cacheable_keys)
        self.assertIn("opener_line_1", keys)
        self.assertIn("silence_check_in", keys)
        self.assertIn("closing_warm_1", keys)
        # Ordinary replies must be synthesised each time (section 13.7).
        self.assertNotIn("fee_reminder", keys)
        self.assertNotIn("date_accepted", keys)

    async def test_a_reply_that_may_not_be_cached_is_refused_even_if_asked(self):
        async with self.db.session() as session:
            result = await self.cache.store(
                session, "fee_reminder", "సర్, fees ఉంది సర్.", SPEECH
            )
            await session.commit()
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, "not_stored:not_cacheable")

    async def test_looking_up_an_uncacheable_phrase_is_a_miss_without_io(self):
        async with self.db.session() as session:
            result = await self.cache.lookup(session, "fee_reminder", "whatever")
            await session.commit()
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, "miss:not_cacheable")


class TestStoreAndRead(AudioCacheTestCase):
    async def test_a_stored_phrase_is_a_hit_and_increases_its_use_count(self):
        async with self.db.session() as session:
            stored = await self.cache.store(session, "opener_line_1", OPENER_TEXT, SPEECH, duration_ms=2000)
            await session.commit()
        self.assertTrue(stored.hit, stored.reason)
        self.assertTrue(stored.path.is_file())

        async with self.db.session() as session:
            hit = await self.cache.lookup(session, "opener_line_1", OPENER_TEXT)
            await session.commit()
        self.assertTrue(hit.hit, hit.reason)
        self.assertEqual(hit.path, stored.path)
        self.assertEqual(hit.audio_bytes, len(SPEECH))
        self.assertEqual(self.cache.hits, 1)

    async def test_a_different_text_is_a_different_key_and_a_miss(self):
        async with self.db.session() as session:
            await self.cache.store(session, "opener_line_1", OPENER_TEXT, SPEECH)
            await session.commit()
        other = ResponseCache().render("opener_line_1", {"school_name": "విద్యాలయం"})
        async with self.db.session() as session:
            miss = await self.cache.lookup(session, "opener_line_1", other)
            await session.commit()
        self.assertFalse(miss.hit)
        self.assertEqual(miss.reason, "miss:new")

    async def test_changing_the_voice_misses_rather_than_serving_the_old_file(self):
        async with self.db.session() as session:
            await self.cache.store(session, "opener_line_1", OPENER_TEXT, SPEECH)
            await session.commit()
        other = AudioCache(root=self.root, responses=ResponseCache(), voice="other", model="bulbul:v3")
        async with self.db.session() as session:
            miss = await other.lookup(session, "opener_line_1", OPENER_TEXT)
            await session.commit()
        self.assertFalse(miss.hit)
        self.assertEqual(miss.reason, "miss:new")

    async def test_a_failed_synthesis_is_never_stored(self):
        async with self.db.session() as session:
            short = await self.cache.store(session, "opener_line_1", OPENER_TEXT, b"\x00\x01")
            empty = await self.cache.store(session, "opener_line_1", OPENER_TEXT, b"")
            await session.commit()
        self.assertFalse(short.hit)
        self.assertEqual(short.reason, "not_stored:audio_too_short")
        self.assertFalse(empty.hit)
        self.assertEqual(empty.reason, "not_stored:empty_audio")
        self.assertEqual(list(self.root.glob("*.pcm")), [])

    async def test_an_absurd_duration_is_refused(self):
        async with self.db.session() as session:
            result = await self.cache.store(
                session, "opener_line_1", OPENER_TEXT, SPEECH, duration_ms=999999
            )
            await session.commit()
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, "not_stored:duration_too_long")


class TestValidationOnRead(AudioCacheTestCase):
    async def _store(self) -> Path:
        async with self.db.session() as session:
            stored = await self.cache.store(session, "opener_line_1", OPENER_TEXT, SPEECH, duration_ms=1500)
            await session.commit()
        return stored.path

    async def _lookup(self):
        async with self.db.session() as session:
            result = await self.cache.lookup(session, "opener_line_1", OPENER_TEXT)
            await session.commit()
            return result

    async def test_a_missing_file_is_a_miss_that_is_also_deleted(self):
        path = await self._store()
        path.unlink()
        result = await self._lookup()
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, "miss:file_missing")

    async def test_a_truncated_file_is_rejected_by_its_size(self):
        path = await self._store()
        path.write_bytes(SPEECH[: len(SPEECH) // 2])
        result = await self._lookup()
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, "miss:file_size_mismatch")
        # The bytes are gone and the row stays behind, marked, so the bad
        # entry is reported rather than quietly re-discovered.
        self.assertFalse(path.is_file())
        second = await self._lookup()
        self.assertEqual(second.reason, "miss:invalidated")

    async def test_a_file_of_the_right_size_but_wrong_bytes_is_rejected_by_checksum(self):
        path = await self._store()
        # Same length, different content: only the checksum can tell.
        path.write_bytes(SPEECH[:-2] + b"\xff\xfe")
        result = await self._lookup()
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, "miss:file_checksum_mismatch")
        self.assertFalse(path.is_file())

    async def test_an_empty_file_is_rejected(self):
        path = await self._store()
        path.write_bytes(b"")
        result = await self._lookup()
        self.assertFalse(result.hit)
        self.assertIn(result.reason, ("miss:file_empty", "miss:file_size_mismatch"))

    async def test_a_template_version_bump_misses_then_purges_the_old_recording(self):
        path = await self._store()
        newer = AudioCache(
            root=self.root, responses=ResponseCache(),
            voice="shubh", model="bulbul:v3", template_version="v2",
        )
        async with self.db.session() as session:
            result = await newer.lookup(session, "opener_line_1", OPENER_TEXT)
            await session.commit()
        self.assertFalse(result.hit, "a new version must never serve the old audio")
        self.assertEqual(result.reason, "miss:new")
        self.assertTrue(path.is_file(), "the old file belongs to the old version key")

        async with self.db.session() as session:
            purged = await newer.invalidate_version(session)
            await session.commit()
        self.assertEqual(purged, 1)
        self.assertFalse(path.is_file())

    async def test_invalidating_a_template_removes_the_bytes(self):
        path = await self._store()
        async with self.db.session() as session:
            removed = await self.cache.invalidate_template(session, "opener_line_1")
            await session.commit()
        self.assertEqual(removed, 1)
        self.assertFalse(path.is_file())

    async def test_an_untouched_second_lookup_is_still_a_hit(self):
        await self._store()
        first = await self._lookup()
        second = await self._lookup()
        self.assertTrue(first.hit and second.hit)
        self.assertEqual(self.cache.hits, 2)
        self.assertEqual(self.cache.invalidations, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
