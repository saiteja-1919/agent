"""Names spoken correctly, with a hand-editable file (P27)."""

import json
import tempfile
import unittest
from pathlib import Path

import stubs

stubs.install()

from name_speech import (  # noqa: E402
    NameSpeaker,
    NameSpeechAuthError,
    convert_details_for_speech,
)


class FakePoster:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append(payload)
        answer = self.answers.pop(0) if self.answers else (500, {})
        if callable(answer):
            return answer(payload)
        return answer


def ok(text):
    return 200, {"transliterated_text": text}


class SpeakingTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.cache = self.folder / "name_pronunciations.json"

    def speaker(self, poster, enabled=True):
        return NameSpeaker(
            "key",
            cache_path=self.cache,
            enabled=enabled,
            poster=poster,
        )

    def test_a_name_is_converted_and_cached(self):
        poster = FakePoster([ok("సాయి తేజ")])
        speaker = self.speaker(poster)
        self.assertEqual(speaker.speak("Sai Teja"), "సాయి తేజ")
        self.assertTrue(self.cache.exists())
        self.assertEqual(json.loads(self.cache.read_text(encoding="utf-8"))["sai teja"], "సాయి తేజ")

    def test_the_second_call_uses_the_cache(self):
        poster = FakePoster([ok("సాయి తేజ")])
        speaker = self.speaker(poster)
        speaker.speak("Sai Teja")
        speaker.speak("sai teja")
        self.assertEqual(len(poster.calls), 1)

    def test_a_hand_edited_file_is_obeyed(self):
        # The owner must be able to fix a pronunciation by hand.
        self.cache.write_text(json.dumps({"sai teja": "సాయి తేజా"}), encoding="utf-8")
        poster = FakePoster([ok("WRONG")])
        speaker = self.speaker(poster)
        self.assertEqual(speaker.speak("Sai Teja"), "సాయి తేజా")
        self.assertEqual(poster.calls, [])

    def test_a_corrupt_file_does_not_stop_a_call(self):
        self.cache.write_text("{ not json at all", encoding="utf-8")
        poster = FakePoster([ok("సాయి తేజ")])
        speaker = self.speaker(poster)
        self.assertEqual(speaker.speak("Sai Teja"), "సాయి తేజ")

    def test_a_file_that_is_not_a_dictionary_is_ignored(self):
        self.cache.write_text("[1, 2, 3]", encoding="utf-8")
        speaker = self.speaker(FakePoster([ok("సాయి తేజ")]))
        self.assertEqual(speaker.speak("Sai Teja"), "సాయి తేజ")

    def test_text_already_in_telugu_is_left_alone(self):
        poster = FakePoster([])
        speaker = self.speaker(poster)
        self.assertEqual(speaker.speak("సాయి తేజ"), "సాయి తేజ")
        self.assertEqual(poster.calls, [])

    def test_a_network_failure_falls_back_to_the_typed_spelling(self):
        def broken(url, headers, payload, timeout):
            raise OSError("no network")

        speaker = self.speaker(broken)
        self.assertEqual(speaker.speak("Sai Teja"), "Sai Teja")

    def test_an_unusable_answer_is_tried_with_the_auto_source(self):
        poster = FakePoster([ok("Sai Teja"), ok("సాయి తేజ")])
        speaker = self.speaker(poster)
        self.assertEqual(speaker.speak("Sai Teja"), "సాయి తేజ")
        self.assertEqual(poster.calls[0]["source_language_code"], "en-IN")
        self.assertEqual(poster.calls[1]["source_language_code"], "auto")

    def test_an_answer_with_english_letters_left_over_is_refused(self):
        # P27: a name must never come back half English, half Telugu.
        poster = FakePoster([ok("Sai తేజ"), ok("Sai తేజ")])
        speaker = self.speaker(poster)
        self.assertEqual(speaker.speak("Sai Teja"), "Sai Teja")

    def test_a_401_stops_immediately(self):
        poster = FakePoster([(401, {})])
        speaker = self.speaker(poster)
        with self.assertRaises(NameSpeechAuthError):
            speaker.speak("Sai Teja")

    def test_conversion_can_be_switched_off(self):
        poster = FakePoster([ok("సాయి తేజ")])
        speaker = self.speaker(poster, enabled=False)
        self.assertEqual(speaker.speak("Sai Teja"), "Sai Teja")
        self.assertEqual(poster.calls, [])

    def test_without_a_key_the_typed_spelling_is_kept(self):
        speaker = NameSpeaker("", cache_path=self.cache)
        self.assertEqual(speaker.speak("Sai Teja"), "Sai Teja")

    def test_empty_values_stay_empty(self):
        speaker = self.speaker(FakePoster([]))
        self.assertEqual(speaker.speak(""), "")

    def test_several_values_at_once(self):
        poster = FakePoster([ok("సాయి తేజ"), ok("టెస్ట్ స్కూల్")])
        speaker = self.speaker(poster)
        result = speaker.speak_all({"student_name": "Sai Teja", "school_name": "Test School"})
        self.assertEqual(result["student_name"], "సాయి తేజ")
        self.assertEqual(result["school_name"], "టెస్ట్ స్కూల్")


class ConvertDetailsTest(unittest.TestCase):
    def test_helper_converts_and_saves(self):
        folder = Path(tempfile.mkdtemp())
        cache = folder / "names.json"
        result = convert_details_for_speech(
            {"student_name": "Sai Teja"},
            "key",
            cache_path=cache,
            poster=FakePoster([ok("సాయి తేజ")]),
        )
        self.assertEqual(result["student_name"], "సాయి తేజ")
        self.assertTrue(cache.exists())


if __name__ == "__main__":
    unittest.main()
