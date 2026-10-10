"""Configuration tests: every range, URL rule and token rule (P16, P29, P30)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import stubs

stubs.install()

from config import (  # noqa: E402
    BULBUL_V3_VOICES,
    ConfigError,
    Settings,
    load_settings,
)

BASE = {
    "EXOTEL_ACCOUNT_SID": "sid",
    "EXOTEL_API_KEY": "key",
    "EXOTEL_API_TOKEN": "token",
    "EXOTEL_CALLER_ID": "04041895032",
    "EXOTEL_STREAM_URL": "wss://abc.trycloudflare.com/v1/exotel/stream",
    "SARVAM_API_KEY": "sarvam-key",
    "SCHOOL_NAME": "Test Public School",
}


def build(**overrides):
    env = dict(BASE)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return Settings.from_env(env)


class DefaultsTest(unittest.TestCase):
    def test_defaults_match_the_agreed_values(self):
        settings = build()
        self.assertEqual(settings.sarvam_tts_voice, "shubh")
        self.assertAlmostEqual(settings.sarvam_tts_pace, 1.15)
        self.assertAlmostEqual(settings.sarvam_tts_temperature, 0.6)
        self.assertAlmostEqual(settings.llm_temperature, 0.6)
        self.assertEqual(settings.llm_max_tokens, 300)
        self.assertEqual(settings.school_name, "Test Public School")

    def test_vad_defaults_are_tuned_for_latency(self):
        # P7: 0.6 s of the old stop time was pure waiting.
        settings = build()
        self.assertAlmostEqual(settings.vad_stop_secs, 0.35)
        self.assertAlmostEqual(settings.vad_start_secs, 0.2)
        self.assertAlmostEqual(settings.user_turn_stop_timeout_secs, 0.5)
        self.assertAlmostEqual(settings.vad_min_volume, 0.6)

    def test_timeouts_are_never_empty(self):
        # P2: an idle timeout of None kept calls open forever.
        settings = build()
        self.assertEqual(settings.idle_timeout_secs, 60)
        self.assertEqual(settings.call_timeout_secs, 600)

    def test_stt_model_gets_the_realtime_suffix(self):
        self.assertEqual(build().sarvam_stt_realtime_model, "saaras:v3-realtime")
        self.assertEqual(
            build(SARVAM_STT_MODEL="saaras:v3-realtime").sarvam_stt_realtime_model,
            "saaras:v3-realtime",
        )

    def test_directories_hang_off_the_app_directory(self):
        folder = Path(tempfile.mkdtemp())
        settings = Settings(app_dir=folder)
        self.assertEqual(settings.transcripts_dir, folder / "transcripts")
        self.assertEqual(settings.pending_dir, folder / ".pending_calls")

    def test_safe_summary_hides_secrets(self):
        settings = build(SARVAM_API_KEY="super-secret-value", STREAM_TOKEN="")
        summary = settings.safe_summary()
        self.assertEqual(summary["sarvam_api_key"], "set")
        self.assertNotIn("super-secret-value", " ".join(str(value) for value in summary.values()))
        self.assertNotIn("EXOTEL_STREAM_URL", summary.get("exotel_stream_url", ""))

    def test_health_url_is_https(self):
        self.assertEqual(
            build().public_health_url,
            "https://abc.trycloudflare.com/health",
        )


class RangeTest(unittest.TestCase):
    def check(self, key, value, expected_fragment):
        with self.assertRaises(ConfigError) as caught:
            build(**{key: value})
        self.assertIn(key, str(caught.exception))
        self.assertIn(expected_fragment, str(caught.exception))

    def test_pace_range(self):
        self.check("SARVAM_TTS_PACE", "0.4", "0.5")
        self.check("SARVAM_TTS_PACE", "2.5", "2.0")
        self.assertAlmostEqual(build(SARVAM_TTS_PACE="2.0").sarvam_tts_pace, 2.0)

    def test_tts_temperature_range(self):
        self.check("SARVAM_TTS_TEMPERATURE", "0.001", "0.01")
        self.check("SARVAM_TTS_TEMPERATURE", "3", "2.0")

    def test_llm_temperature_range(self):
        self.check("LLM_TEMPERATURE", "1.6", "1.5")
        self.assertAlmostEqual(build(LLM_TEMPERATURE="0").llm_temperature, 0.0)

    def test_token_range(self):
        self.check("LLM_MAX_TOKENS", "50", "60")
        self.check("LLM_MAX_TOKENS", "5000", "2000")
        self.assertEqual(build(LLM_MAX_TOKENS="500").llm_max_tokens, 500)

    def test_vad_ranges(self):
        self.check("VAD_STOP_SECS", "0.2", "0.3")
        self.check("VAD_STOP_SECS", "2", "1.5")
        self.check("VAD_START_SECS", "0.01", "0.05")
        self.check("VAD_MIN_VOLUME", "1.5", "1.0")
        self.check("USER_TURN_STOP_TIMEOUT_SECS", "0.1", "0.3")

    def test_call_timeout_floor(self):
        self.check("CALL_TIMEOUT_SECS", "20", "30")

    def test_idle_timeout_rule(self):
        self.assertEqual(build(IDLE_TIMEOUT_SECS="0").idle_timeout_secs, 0)
        self.assertEqual(build(IDLE_TIMEOUT_SECS="10").idle_timeout_secs, 10)
        self.check("IDLE_TIMEOUT_SECS", "5", "at least 10")

    def test_non_numeric_values_are_refused(self):
        self.check("SARVAM_TTS_PACE", "fast", "must be a number")
        self.check("PORT", "http", "whole number")


class VoiceTest(unittest.TestCase):
    def test_unknown_voice_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            build(SARVAM_TTS_VOICE="not-a-voice")
        self.assertIn("SARVAM_TTS_VOICE", str(caught.exception))

    def test_all_v3_voices_are_accepted(self):
        for voice in BULBUL_V3_VOICES:
            self.assertEqual(build(SARVAM_TTS_VOICE=voice).sarvam_tts_voice, voice)

    def test_uppercase_voice_is_lowered(self):
        # Sarvam speaker names are case sensitive and must be lower case.
        self.assertEqual(build(SARVAM_TTS_VOICE="Shubh").sarvam_tts_voice, "shubh")


class SchoolNameTest(unittest.TestCase):
    def test_missing_school_name_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            build(SCHOOL_NAME="")
        self.assertIn("SCHOOL_NAME", str(caught.exception))

    def test_placeholder_school_name_is_refused(self):
        # P16: the school was once spoken as "the school".
        with self.assertRaises(ConfigError) as caught:
            build(SCHOOL_NAME="The School")
        self.assertIn("placeholder", str(caught.exception))

    def test_unrelated_shell_variables_are_ignored(self):
        # A stray PORT or TOKEN from another program must not break Veda.
        env = dict(BASE)
        env.update({"SOME_OTHER_TOOL_PORT": "0", "TOKEN": "whatever"})
        settings = Settings.from_env(env)
        self.assertEqual(settings.port, 8000)

    def test_generic_host_and_port_can_be_renamed(self):
        env = dict(BASE)
        env.update({"PORT": "0", "VEDA_PORT": "9001", "VEDA_HOST": "127.0.0.1"})
        settings = Settings.from_env(env)
        self.assertEqual(settings.port, 9001)

    def test_school_name_can_be_waived_for_tools(self):
        settings = Settings.from_env({"SCHOOL_NAME": ""}, require_secrets=False, require_school=False)
        self.assertEqual(settings.school_name, "")


class CallerIdTest(unittest.TestCase):
    def test_landline_exophone_is_accepted(self):
        self.assertEqual(build(EXOTEL_CALLER_ID="04041895032").exotel_caller_id, "04041895032")
        self.assertEqual(build(EXOTEL_CALLER_ID="+914041895032").exotel_caller_id, "+914041895032")

    def test_nonsense_caller_id_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            build(EXOTEL_CALLER_ID="not-a-number")
        self.assertIn("EXOTEL_CALLER_ID", str(caught.exception))

    def test_missing_caller_id_is_refused_for_a_real_call(self):
        with self.assertRaises(ConfigError) as caught:
            build(EXOTEL_CALLER_ID="")
        self.assertIn("EXOTEL_CALLER_ID", str(caught.exception))


class StreamUrlTest(unittest.TestCase):
    def test_doubled_scheme_is_refused(self):
        # P29: wss://https://host was one of the mistakes.
        with self.assertRaises(ConfigError) as caught:
            build(EXOTEL_STREAM_URL="wss://https://abc.trycloudflare.com/v1/exotel/stream")
        self.assertIn("doubled scheme", str(caught.exception))

    def test_wrong_scheme_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            build(EXOTEL_STREAM_URL="https://abc.trycloudflare.com/v1/exotel/stream")
        self.assertIn("ws://", str(caught.exception))

    def test_wrong_path_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            build(EXOTEL_STREAM_URL="wss://abc.trycloudflare.com/stream")
        self.assertIn("path must be", str(caught.exception))

    def test_alternate_paths_are_accepted(self):
        for path in ("/v1/exotel/stream", "/v1/exotel/stream/ws", "/ws"):
            self.assertEqual(build(EXOTEL_STREAM_URL=f"wss://host{path}").stream_path, path)

    def test_sample_rate_must_be_8000(self):
        with self.assertRaises(ConfigError) as caught:
            build(EXOTEL_STREAM_URL="wss://host/v1/exotel/stream?sample-rate=16000")
        self.assertIn("sample-rate", str(caught.exception))
        self.assertEqual(
            build(EXOTEL_STREAM_URL="wss://host/v1/exotel/stream?sample-rate=8000").stream_path,
            "/v1/exotel/stream",
        )

    def test_too_long_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            build(EXOTEL_STREAM_URL="wss://host/v1/exotel/stream?x=" + "a" * 700)
        self.assertIn("600", str(caught.exception))

    def test_token_must_appear_in_the_url(self):
        with self.assertRaises(ConfigError) as caught:
            build(STREAM_TOKEN="abcdefghijklmno")
        self.assertIn("?token=", str(caught.exception))

    def test_matching_token_is_accepted(self):
        settings = build(
            STREAM_TOKEN="abcdefghijklmno",
            EXOTEL_STREAM_URL="wss://host/v1/exotel/stream?token=abcdefghijklmno",
        )
        self.assertEqual(settings.stream_token, "abcdefghijklmno")

    def test_token_rules(self):
        with self.assertRaises(ConfigError) as caught:
            build(
                STREAM_TOKEN="short",
                EXOTEL_STREAM_URL="wss://host/v1/exotel/stream?token=short",
            )
        self.assertIn("12 characters", str(caught.exception))
        with self.assertRaises(ConfigError) as caught:
            bad = "abcdefghijkl mnop"
            build(
                STREAM_TOKEN=bad,
                EXOTEL_STREAM_URL="wss://host/v1/exotel/stream?token=abcdefghijkl",
            )
        self.assertIn("letters, digits", str(caught.exception))

    def test_missing_stream_url_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            build(EXOTEL_STREAM_URL="")
        self.assertIn("EXOTEL_STREAM_URL", str(caught.exception))


class SecretsTest(unittest.TestCase):
    def test_missing_secrets_are_listed(self):
        with self.assertRaises(ConfigError) as caught:
            build(SARVAM_API_KEY="", EXOTEL_API_KEY="")
        message = str(caught.exception)
        self.assertIn("SARVAM_API_KEY is missing", message)
        self.assertIn("EXOTEL_API_KEY is missing", message)

    def test_tools_can_skip_secret_checks(self):
        settings = Settings.from_env({"SCHOOL_NAME": "Test"}, require_secrets=False, require_school=False)
        self.assertEqual(settings.sarvam_api_key, "")


class DotEnvTest(unittest.TestCase):
    def test_env_file_wins_over_a_stale_shell_variable(self):
        # P29: a stale PowerShell $env: setting once overrode .env.
        folder = Path(tempfile.mkdtemp())
        env_file = folder / ".env"
        env_file.write_text(
            "SCHOOL_NAME=From The Env File\nEXOTEL_STREAM_URL=wss://host/v1/exotel/stream\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {
                "SCHOOL_NAME": "From The Shell",
                "EXOTEL_STREAM_URL": "wss://wrong/v1/exotel/stream",
                "UNRELATED_TOOL_SETTING": "ignored",
            },
            clear=True,
        ):
            settings = load_settings(
                require_secrets=False, require_school=False, env_path=env_file
            )
        self.assertEqual(settings.school_name, "From The Env File")
        self.assertEqual(settings.exotel_stream_url, "wss://host/v1/exotel/stream")


if __name__ == "__main__":
    unittest.main()
