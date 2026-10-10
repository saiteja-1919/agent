"""Stage 1 configuration must fail loudly and keep secrets out of logs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config import ConfigError, Settings
from app.config.settings import (
    DEFAULT_DATABASE_URL,
    Stage1Settings,
    load_stage1_settings,
)


def env(**kwargs) -> dict:
    """A minimal, non-secret environment for the settings tests."""
    return {key: str(value) for key, value in kwargs.items()}


class TestDatabaseUrl(unittest.TestCase):
    def test_default_is_postgres_asyncpg(self):
        settings = Stage1Settings.from_env(env())
        self.assertTrue(settings.is_postgres)
        self.assertEqual(settings.database_url, DEFAULT_DATABASE_URL)

    def test_psycopg_async_url_is_rejected(self):
        """Windows pitfall 19: psycopg 3 async cannot run on the default loop."""
        with self.assertRaises(ConfigError) as caught:
            Stage1Settings.from_env(env(DATABASE_URL="postgresql+psycopg://veda:veda@localhost/veda"))
        self.assertIn("asyncpg", str(caught.exception))

    def test_plain_sync_postgres_url_is_rejected(self):
        with self.assertRaises(ConfigError):
            Stage1Settings.from_env(env(DATABASE_URL="postgresql://veda:veda@localhost/veda"))

    def test_sqlite_test_url_is_allowed(self):
        settings = Stage1Settings.from_env(env(DATABASE_URL="sqlite+aiosqlite:///:memory:"))
        self.assertTrue(settings.is_sqlite)

    def test_veda_prefixed_url_wins(self):
        settings = Stage1Settings.from_env(
            env(
                DATABASE_URL="postgresql+asyncpg://a@h/db",
                VEDA_DATABASE_URL="postgresql+asyncpg://b@h/db",
            )
        )
        self.assertIn("//b@", settings.database_url)

    def test_redacted_url_hides_the_password(self):
        settings = Stage1Settings.from_env(
            env(DATABASE_URL="postgresql+asyncpg://veda:sup3rs3cret@127.0.0.1:5432/veda")
        )
        redacted = settings.redacted_database_url()
        self.assertNotIn("sup3rs3cret", redacted)
        self.assertIn("veda:***@", redacted)


class TestTranscriptRefusal(unittest.TestCase):
    def test_transcripts_are_off_by_default(self):
        settings = Stage1Settings.from_env(env())
        self.assertFalse(settings.store_transcripts)

    def test_single_flag_cannot_enable_storage(self):
        with self.assertRaises(ConfigError) as caught:
            Stage1Settings.from_env(env(STORE_TRANSCRIPTS="true"))
        self.assertIn("STORE_TRANSCRIPTS=true is refused", str(caught.exception))

    def test_explicit_acknowledgement_allows_storage(self):
        settings = Stage1Settings.from_env(
            env(
                STORE_TRANSCRIPTS="true",
                TRANSCRIPT_STORAGE_ACKNOWLEDGED="true",
                TRANSCRIPT_RETENTION_DAYS="7",
                DATABASE_URL="postgresql+asyncpg://veda:veda@localhost/veda",
            )
        )
        self.assertTrue(settings.store_transcripts)

    def test_long_retention_is_rejected_when_storage_is_on(self):
        with self.assertRaises(ConfigError) as caught:
            Stage1Settings.from_env(
                env(
                    STORE_TRANSCRIPTS="true",
                    TRANSCRIPT_STORAGE_ACKNOWLEDGED="true",
                    TRANSCRIPT_RETENTION_DAYS="120",
                )
            )
        self.assertIn("RETENTION", str(caught.exception))

    def test_storage_with_sqlite_is_rejected(self):
        with self.assertRaises(ConfigError):
            Stage1Settings.from_env(
                env(
                    STORE_TRANSCRIPTS="true",
                    TRANSCRIPT_STORAGE_ACKNOWLEDGED="true",
                    DATABASE_URL="sqlite+aiosqlite:///:memory:",
                )
            )

    def test_falsey_values_do_not_enable_storage(self):
        settings = Stage1Settings.from_env(env(STORE_TRANSCRIPTS="false"))
        self.assertFalse(settings.store_transcripts)


class TestAttemptPolicy(unittest.TestCase):
    def test_default_is_one_attempt(self):
        self.assertEqual(Stage1Settings.from_env(env()).max_attempts_per_task, 1)

    def test_more_than_one_attempt_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            Stage1Settings.from_env(env(MAX_ATTEMPTS_PER_TASK="2"))
        self.assertIn("MAX_ATTEMPTS_PER_TASK", str(caught.exception))

    def test_zero_attempts_is_out_of_range(self):
        with self.assertRaises(ConfigError):
            Stage1Settings.from_env(env(MAX_ATTEMPTS_PER_TASK="0"))


class TestBounds(unittest.TestCase):
    def test_non_numeric_value_is_reported_by_name(self):
        with self.assertRaises(ConfigError) as caught:
            Stage1Settings.from_env(env(RING_TIMEOUT_SECS="soon"))
        self.assertIn("RING_TIMEOUT_SECS", str(caught.exception))

    def test_out_of_range_number_is_reported_with_the_limit(self):
        with self.assertRaises(ConfigError) as caught:
            Stage1Settings.from_env(env(MAX_SIMULTANEOUS_CALLS="900"))
        message = str(caught.exception)
        self.assertIn("MAX_SIMULTANEOUS_CALLS", message)
        self.assertIn("20", message)

    def test_every_problem_is_listed_together(self):
        with self.assertRaises(ConfigError) as caught:
            Stage1Settings.from_env(
                env(RING_TIMEOUT_SECS="nope", LLM_TIMEOUT_SECS="-5", LOG_LEVEL="LOUD")
            )
        message = str(caught.exception)
        self.assertIn("RING_TIMEOUT_SECS", message)
        self.assertIn("LLM_TIMEOUT_SECS", message)
        self.assertIn("LOG_LEVEL", message)

    def test_log_level_is_upper_cased(self):
        self.assertEqual(Stage1Settings.from_env(env(LOG_LEVEL="debug")).log_level, "DEBUG")

    def test_currency_must_be_three_letters(self):
        with self.assertRaises(ConfigError):
            Stage1Settings.from_env(env(DEFAULT_CURRENCY="RUPEE"))

    def test_opener_pause_is_configurable(self):
        settings = Stage1Settings.from_env(env(OPENER_PAUSE_SECS="0.35"))
        self.assertAlmostEqual(settings.opener_pause_secs, 0.35)


class TestSecretsNeverAppear(unittest.TestCase):
    def _settings_with_secrets(self) -> Stage1Settings:
        return Stage1Settings.from_env(
            env(
                EXOTEL_ACCOUNT_SID="AC00000000000000000000000000000000",
                EXOTEL_API_KEY="exotel-key-do-not-log",
                EXOTEL_API_TOKEN="exotel-token-do-not-log",
                SARVAM_API_KEY="sarvam-key-do-not-log",
                STREAM_TOKEN="stream-token-do-not-log",
                EXOTEL_CALLBACK_TOKEN="callback-token-do-not-log",
                VEDA_API_TOKEN="api-token-do-not-log",
                DATABASE_URL="postgresql+asyncpg://veda:db-password-do-not-log@localhost/veda",
            )
        )

    def test_safe_summary_contains_no_secret_values(self):
        summary = self._settings_with_secrets().safe_summary()
        blob = json.dumps(summary)
        for secret in (
            "exotel-key-do-not-log",
            "exotel-token-do-not-log",
            "sarvam-key-do-not-log",
            "stream-token-do-not-log",
            "callback-token-do-not-log",
            "api-token-do-not-log",
            "db-password-do-not-log",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_safe_summary_reports_presence_not_values(self):
        summary = self._settings_with_secrets().safe_summary()
        self.assertEqual(summary["sarvam_api_key"], "set")
        self.assertEqual(summary["exotel_callback_token"], "set")
        self.assertEqual(summary["api_token"], "set")

    def test_missing_secrets_are_reported_as_missing(self):
        summary = Stage1Settings.from_env(env()).safe_summary()
        self.assertEqual(summary["sarvam_api_key"], "missing")
        self.assertEqual(summary["api_token"], "missing")


class TestCompositionWithExistingConfig(unittest.TestCase):
    def test_existing_keys_are_reused_not_duplicated(self):
        settings = Stage1Settings.from_env(
            env(SCHOOL_NAME="Sri Vidya High School", VAD_STOP_SECS="0.4")
        )
        self.assertEqual(settings.base.school_name, "Sri Vidya High School")
        self.assertAlmostEqual(settings.base.vad_stop_secs, 0.4)

    def test_school_name_property_reads_through(self):
        settings = Stage1Settings.from_env(env(SCHOOL_NAME="Sri Vidya High School"))
        self.assertEqual(settings.school_name, "Sri Vidya High School")
        self.assertEqual(settings.spoken_school_name, "Sri Vidya High School")

    def test_telugu_school_name_wins_for_speech(self):
        settings = Stage1Settings.from_env(
            env(SCHOOL_NAME="Sri Vidya High School", SCHOOL_NAME_TELUGU="శ్రీ విద్య")
        )
        self.assertEqual(settings.spoken_school_name, "శ్రీ విద్య")
        self.assertEqual(settings.school_name, "Sri Vidya High School")

    def test_require_school_rejects_the_placeholder(self):
        with self.assertRaises(ConfigError):
            Stage1Settings.from_env(env(SCHOOL_NAME="the school"), require_school=True)

    def test_require_secrets_is_opt_in(self):
        # Offline work must be possible without provider credentials.
        Stage1Settings.from_env(env())
        with self.assertRaises(ConfigError):
            Stage1Settings.from_env(env(), require_secrets=True)

    def test_existing_settings_can_be_injected(self):
        base = Settings.from_env(
            env(SCHOOL_NAME="Sri Vidya High School"),
            require_secrets=False,
            require_school=False,
        )
        settings = Stage1Settings.from_env(env(), base=base)
        self.assertIs(settings.base, base)


class TestDirectoriesAndOverrides(unittest.TestCase):
    def test_ensure_directories_creates_every_configured_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Stage1Settings.from_env(
                env(
                    DATA_DIR=root / "d",
                    AUDIO_CACHE_DIR=root / "a",
                    IMPORT_DIR=root / "i",
                    EXPORT_DIR=root / "e",
                )
            )
            settings.ensure_directories()
            for path in (root / "d", root / "a", root / "i", root / "e"):
                self.assertTrue(path.is_dir(), path)

    def test_ensure_directories_never_raises_for_a_live_call(self):
        settings = Stage1Settings.from_env(env(DATA_DIR="\0:/not a path"))
        settings.ensure_directories()  # must not raise

    def test_with_overrides_returns_a_copy(self):
        settings = Stage1Settings.from_env(env())
        changed = settings.with_overrides(max_simultaneous_calls=1)
        self.assertEqual(changed.max_simultaneous_calls, 1)
        self.assertEqual(settings.max_simultaneous_calls, 2)


class TestLoader(unittest.TestCase):
    def test_loader_is_a_pure_function_of_the_mapping(self):
        settings = load_stage1_settings(env(SCHOOL_NAME="Sri Vidya High School"), env_path=None)
        self.assertEqual(settings.school_name, "Sri Vidya High School")

    def test_loader_does_not_touch_the_environment_when_given_a_mapping(self):
        import os

        before = dict(os.environ)
        load_stage1_settings(env(SCHOOL_NAME="Sri Vidya High School"), env_path=None)
        self.assertEqual(dict(os.environ), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
