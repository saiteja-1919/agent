"""The Telugu voice: splitting, config, audio frames and fallbacks."""

import asyncio
import base64
import json
import unittest

import stubs

stubs.install()

from pipecat.frames.frames import (  # noqa: E402
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from sarvam_tts import SarvamTTS, split_for_speech  # noqa: E402


def service(**kwargs):
    options = {
        "api_key": "key",
        "model": "bulbul:v3",
        "language_code": "te-IN",
        "speaker": "shubh",
        "sample_rate": 8000,
        "pace": 1.15,
        "temperature": 0.6,
    }
    options.update(kwargs)
    return SarvamTTS(**options)


def connected(tts, messages=None):
    """Pretend the socket is already open."""
    connection = stubs.FakeConnection(messages=messages or [])
    tts.connection = connection
    tts.connected = True
    tts.config_sent = True
    return connection


class SplitTest(unittest.TestCase):
    def test_short_text_is_one_piece(self):
        self.assertEqual(split_for_speech("అలాగే సర్."), ["అలాగే సర్."])

    def test_long_text_is_split_without_losing_words(self):
        text = " ".join(["word"] * 100)
        pieces = split_for_speech(text, max_len=60)
        self.assertTrue(all(len(piece) <= 60 for piece in pieces))
        self.assertEqual(" ".join(pieces).split(), text.split())

    def test_empty_text(self):
        self.assertEqual(split_for_speech(""), [])
        self.assertEqual(split_for_speech("   "), [])

    def test_sentences_are_kept_separate(self):
        pieces = split_for_speech("first sentence. second sentence.", max_len=150)
        self.assertEqual(len(pieces), 2)


class ConfigTest(unittest.TestCase):
    def test_the_url_asks_for_the_completion_event(self):
        url = service().url()
        self.assertIn("model=bulbul:v3", url)
        self.assertIn("send_completion_event=true", url)

    def test_min_buffer_size_is_never_below_thirty(self):
        # Sarvam requires at least 30.
        self.assertEqual(service(min_buffer_size=5).config_message()["data"]["min_buffer_size"], 30)
        self.assertEqual(service(min_buffer_size=60).config_message()["data"]["min_buffer_size"], 60)

    def test_the_config_carries_every_voice_setting(self):
        data = service().config_message()["data"]
        self.assertEqual(data["language_code"], "te-IN")
        self.assertEqual(data["speaker"], "shubh")
        self.assertEqual(data["speech_sample_rate"], "8000")
        self.assertEqual(data["output_audio_codec"], "linear16")
        self.assertAlmostEqual(data["pace"], 1.15)
        self.assertAlmostEqual(data["temperature"], 0.6)
        self.assertTrue(data["enable_preprocessing"])

    def test_the_fallback_config_keeps_only_the_basics(self):
        data = service().minimal_config_message()["data"]
        self.assertEqual(data["speaker"], "shubh")
        self.assertNotIn("temperature", data)


class TextFlowTest(unittest.TestCase):
    def test_a_sentence_is_sent_as_soon_as_it_is_complete(self):
        tts = service()
        connection = connected(tts)

        async def run():
            await tts.process_frame(LLMTextFrame(text="Hello sir, "), None)
            await tts.process_frame(LLMTextFrame(text="fees pending ఉంది. "), None)
            await tts.process_frame(LLMFullResponseEndFrame(), None)
            # Sarvam reports the end of the spoken reply.
            await tts._handle_message(json.dumps({"type": "event", "data": {"event_type": "final"}}))

        asyncio.run(run())
        payloads = connection.json_sent()
        texts = [payload["data"]["text"] for payload in payloads if payload["type"] == "text"]
        self.assertEqual(texts, ["Hello sir, fees pending ఉంది."])
        self.assertEqual([p["type"] for p in payloads].count("flush"), 1)
        self.assertIn("TTSStartedFrame", tts.pushed_types())
        self.assertIn("TTSStoppedFrame", tts.pushed_types())

    def test_a_lost_final_event_still_ends_the_utterance(self):
        # Rule 8: every external call needs a timeout and a fallback.
        tts = service(flush_timeout_secs=0.01)
        connected(tts)

        async def run():
            await tts.process_frame(TTSSpeakFrame(text="hello"), None)
            await asyncio.sleep(0.05)

        asyncio.run(run())
        self.assertIn("TTSStoppedFrame", tts.pushed_types())

    def test_a_scripted_line_is_spoken_whole(self):
        # The two opening lines travel this path.
        tts = service()
        connection = connected(tts)
        asyncio.run(tts.process_frame(TTSSpeakFrame(text="Hello sir, నేను Veda."), None))
        payloads = connection.json_sent()
        self.assertEqual(payloads[0]["data"]["text"], "Hello sir, నేను Veda.")
        self.assertEqual(payloads[-1]["type"], "flush")

    def test_a_very_long_reply_is_split_for_the_socket(self):
        tts = service(max_chunk_length=40)
        connection = connected(tts)

        async def run():
            await tts.process_frame(LLMTextFrame(text="word " * 60), None)
            await tts.process_frame(LLMFullResponseEndFrame(), None)

        asyncio.run(run())
        texts = [p["data"]["text"] for p in connection.json_sent() if p["type"] == "text"]
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(text) <= 60 for text in texts))

    def test_an_interruption_drops_the_pending_text(self):
        tts = service()
        connected(tts)

        async def run():
            await tts.process_frame(LLMTextFrame(text="half a sentence without end"), None)
            await tts.process_frame(InterruptionFrame(), None)
            await asyncio.sleep(0)

        asyncio.run(run())
        self.assertEqual(tts._pending_text, "")
        self.assertIn("TTSStoppedFrame", tts.pushed_types())

    def test_a_send_failure_still_finishes_the_utterance(self):
        tts = service()
        connection = connected(tts)
        connection.send_error = OSError("socket gone")

        async def run():
            await tts.process_frame(TTSSpeakFrame(text="hello"), None)

        asyncio.run(run())
        self.assertGreaterEqual(tts.failures, 1)
        self.assertIn("TTSStoppedFrame", tts.pushed_types())
        self.assertTrue(tts.errors)


class AudioFlowTest(unittest.TestCase):
    def test_audio_messages_become_audio_frames(self):
        tts = service()
        connected(tts)
        audio = b"\x01\x02" * 100
        message = json.dumps({"type": "audio", "data": {"audio": base64.b64encode(audio).decode()}})
        asyncio.run(tts._handle_message(message))
        frames = [f for f in tts.pushed if type(f).__name__ == "TTSAudioRawFrame"]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].audio, audio)
        self.assertEqual(frames[0].sample_rate, 8000)

    def test_the_final_event_ends_the_utterance(self):
        tts = service()
        connected(tts)
        ended = []
        tts._on_utterance_end = lambda: ended.append(True)
        asyncio.run(tts._begin_utterance())
        asyncio.run(tts._handle_message(json.dumps({"type": "event", "data": {"event_type": "final"}})))
        self.assertTrue(ended)
        self.assertIn("TTSStoppedFrame", tts.pushed_types())

    def test_an_error_is_reported_and_the_config_is_retried(self):
        tts = service()
        connection = connected(tts)
        tts.config_sent = False
        asyncio.run(tts._handle_message(json.dumps({"type": "error", "data": {"message": "bad config field"}})))
        self.assertTrue(tts.errors)
        retried = [p for p in connection.json_sent() if p["type"] == "config"]
        self.assertEqual(len(retried), 1)
        self.assertNotIn("temperature", retried[0]["data"])

    def test_broken_messages_do_not_raise(self):
        tts = service()
        connected(tts)
        asyncio.run(tts._handle_message("not json"))
        asyncio.run(tts._handle_message(json.dumps({"type": "audio", "data": {"audio": "!!not base64!!"}})))
        self.assertEqual(tts.pushed, [])


class SynthesizeTest(unittest.TestCase):
    def test_audio_is_collected_for_the_cached_opener(self):
        audio = b"\x00\x01" * 800
        connector = stubs.Connector(
            [
                stubs.FakeConnection(
                    messages=[
                        json.dumps({"type": "audio", "data": {"audio": base64.b64encode(audio[:800]).decode()}}),
                        json.dumps({"type": "audio", "data": {"audio": base64.b64encode(audio[800:]).decode()}}),
                        json.dumps({"type": "event", "data": {"event_type": "final"}}),
                    ]
                )
            ]
        )
        tts = service(connector=connector, emit_frames=False)
        result = asyncio.run(tts.synthesize("hello sir", timeout=5))
        self.assertEqual(result, audio)
        self.assertEqual(tts.pushed, [])

    def test_a_failed_synthesis_returns_nothing(self):
        tts = service(connector=stubs.Connector([]), emit_frames=False)
        self.assertEqual(asyncio.run(tts.synthesize("hello", timeout=0.2)), b"")


if __name__ == "__main__":
    unittest.main()
