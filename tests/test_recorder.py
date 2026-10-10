"""Recording a call: turns, flags, marks and atomic files (P3, P4, P5)."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import stubs

stubs.install()

from pipecat.frames.frames import (  # noqa: E402
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from recorder import AudioRecorder, CallRecorder, InputRecorder, LLMRecorder  # noqa: E402


def recorder():
    return CallRecorder(
        "abc123",
        to="+919876543210",
        from_number="+914041895032",
        student_name="Sai Teja",
        parent_name="Ramesh",
        school_name="Test Public School",
    )


class TurnTest(unittest.TestCase):
    def test_the_opener_is_recorded_as_its_own_turn(self):
        # P5: the greeting was missing from the transcript.
        rec = recorder()
        rec.note_greeting(["line one", "line two"])
        transcript = rec.build_transcript()
        self.assertEqual(transcript["greeting"], ["line one", "line two"])
        self.assertEqual(transcript["turns"][0]["kind"], "opener")
        self.assertEqual(transcript["turns"][0]["text"], "line one line two")

    def test_the_first_reply_is_not_merged_into_the_opener(self):
        rec = recorder()
        rec.note_greeting(["opener"])
        rec.note_assistant_text("సర్, fees pending ఉంది.")
        turns = rec.build_transcript()["turns"]
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[1]["kind"], "speech")

    def test_split_user_finals_join_into_one_turn(self):
        # P5: separate final transcripts overwrote each other.
        rec = recorder()
        rec.note_user_text("hello sir")
        rec.note_user_text("nenu cheppandi")
        turns = rec.build_transcript()["turns"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "hello sir nenu cheppandi")

    def test_a_new_user_turn_starts_after_a_reply(self):
        rec = recorder()
        rec.note_user_text("hello")
        rec.note_assistant_text("మాట్లాడుతున్నాను")
        rec.note_response_end()
        rec.note_user_text("avunu")
        turns = rec.build_transcript()["turns"]
        self.assertEqual([turn["role"] for turn in turns], ["user", "assistant", "user"])

    def test_an_interrupted_reply_becomes_its_own_finished_turn(self):
        # P5: interrupted replies merged into the next turn.
        rec = recorder()
        rec.note_user_text("hello")
        rec.note_assistant_text("సర్, మీ child ")
        rec.note_interruption()
        rec.note_user_text("ok listen")
        turns = rec.build_transcript()["turns"]
        self.assertEqual(turns[1]["interrupted"], True)
        self.assertEqual(turns[2]["text"], "ok listen")
        self.assertEqual(rec.interrupted_assistant_turns, 1)

    def test_a_trailing_user_turn_is_flagged_no_reply(self):
        rec = recorder()
        rec.note_user_text("hello")
        transcript = rec.build_transcript()
        self.assertTrue(transcript["turns"][0]["no_reply"])
        self.assertTrue(transcript["flags"]["no_reply"])

    def test_empty_text_is_ignored(self):
        rec = recorder()
        rec.note_user_text("   ")
        rec.note_assistant_text("")
        self.assertEqual(rec.turns, [])


class MarkTest(unittest.TestCase):
    def test_marks_are_recorded_once(self):
        rec = recorder()
        rec.mark("stream_parsed", at=100.0)
        rec.mark("stream_parsed", at=200.0)
        self.assertEqual(rec.mark_time("stream_parsed"), 100.0)
        self.assertEqual(rec.mark_order.count("stream_parsed"), 1)

    def test_latency_is_measured_from_the_final_transcript_to_the_first_audio(self):
        rec = recorder()
        rec.created_at = 100.0
        rec.note_user_text("hello", at=110.0)
        rec.note_first_audio(at=111.2)
        self.assertAlmostEqual(rec.response_latency_secs, 1.2)

    def test_the_first_audio_of_the_opener_is_measured_separately(self):
        rec = recorder()
        rec.created_at = 100.0
        rec.note_greeting(["opener"], at=101.0)
        rec.note_first_audio(at=102.5)
        turn = rec.build_transcript()["turns"][0]
        self.assertAlmostEqual(turn["first_audio_delay_secs"], 1.5)
        self.assertEqual(turn["first_audio_measured_from"] if "first_audio_measured_from" in turn else "opener_queued", "opener_queued")
        self.assertIsNone(rec.response_latency_secs)

    def test_the_latency_file_contains_every_required_mark(self):
        rec = recorder()
        rec.mark("stream_parsed", at=100.0)
        rec.mark("pipeline_built", at=100.4)
        rec.mark("client_connected", at=101.0)
        rec.note_greeting(["opener"], at=101.2)
        rec.note_user_text("hello", at=110.0)
        rec.note_first_audio(at=111.1)
        latency = rec.build_latency()
        for name in ("stream_parsed", "pipeline_built", "client_connected", "first_tts_audio"):
            self.assertIn(name, latency["marks"])
        self.assertEqual(latency["marks"]["stream_parsed"]["epoch"], 100.0)
        self.assertAlmostEqual(latency["marks"]["client_connected"]["since_stream_parsed_secs"], 1.0)
        self.assertAlmostEqual(latency["response_latency_secs"], 1.1)

    def test_gap_seconds_moves_the_audio_back_to_when_it_started(self):
        rec = recorder()
        rec.created_at = 100.0
        rec.note_user_text("hello", at=110.0)
        # The frame took 0.2 s of audio, so the reply really started 0.2 s earlier.
        rec.note_first_audio(at=111.0, gap_seconds=0.2)
        self.assertAlmostEqual(rec.response_latency_secs, 0.8)


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def test_both_files_are_written_and_the_latency_file_comes_first(self):
        # P4: the latency file used to be written after the transcript, so
        # stopping the server lost it.
        rec = recorder()
        rec.note_greeting(["opener"])
        rec.note_user_text("hello")
        rec.note_assistant_text("అలాగే సర్.")
        saved = rec.save(self.folder)
        self.assertIsNotNone(saved["transcript"])
        self.assertIsNotNone(saved["latency"])
        self.assertTrue(Path(saved["latency"]).exists())
        self.assertTrue(Path(saved["transcript"]).exists())
        self.assertTrue(Path(saved["latency"]).name.endswith("_latency.json"))
        self.assertIn(rec.call_ref, Path(saved["transcript"]).name)

    def test_latency_is_written_before_the_transcript(self):
        rec = recorder()
        rec.note_user_text("hello")
        saved = rec.save(self.folder)
        latency_mtime = Path(saved["latency"]).stat().st_mtime_ns
        transcript_mtime = Path(saved["transcript"]).stat().st_mtime_ns
        self.assertLessEqual(latency_mtime, transcript_mtime)

    def test_files_are_valid_json_without_temporary_leftovers(self):
        rec = recorder()
        rec.note_greeting(["opener"])
        saved = rec.save(self.folder, status="finished")
        data = json.loads(Path(saved["transcript"]).read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "finished")
        self.assertEqual(data["to"], "+919876543210")
        self.assertIn("flags", data)
        self.assertEqual([p.name for p in self.folder.iterdir() if ".tmp" in p.name], [])

    def test_saving_into_an_unwritable_folder_does_not_raise(self):
        rec = recorder()
        rec.note_user_text("hello")
        blocked = self.folder / "call.json"
        blocked.write_text("x", encoding="utf-8")
        saved = rec.save(blocked)  # a file where a folder should be
        self.assertIsNone(saved["transcript"])
        self.assertIsNone(saved["latency"])

    def test_file_names_include_the_call_reference(self):
        rec = recorder()
        saved = rec.save(self.folder)
        self.assertIn("abc123", Path(saved["latency"]).name)


class ProcessorTest(unittest.TestCase):
    def test_the_input_recorder_records_speech_and_interruptions(self):
        rec = recorder()
        processor = InputRecorder(rec)

        async def run():
            await processor.process_frame(TranscriptionFrame(text="hello", user_id="parent", timestamp="now"), None)
            await processor.process_frame(UserStartedSpeakingFrame(), None)
            await processor.process_frame(UserStoppedSpeakingFrame(), None)
            await processor.process_frame(InterruptionFrame(), None)

        asyncio.run(run())
        self.assertIn("user_final", rec.marks)
        self.assertIn("user_started_speaking", rec.marks)
        self.assertIn("user_stopped_speaking", rec.marks)
        self.assertEqual(len(processor.pushed), 4)

    def test_the_llm_recorder_records_text_and_the_end_of_a_reply(self):
        rec = recorder()
        processor = LLMRecorder(rec)
        asyncio.run(_feed(processor, [LLMTextFrame(text="సర్, "), LLMTextFrame(text="fees pending."), LLMFullResponseEndFrame()]))
        self.assertEqual(rec.turns[-1]["text"], "సర్, fees pending.")
        self.assertTrue(rec.turns[-1]["closed"])

    def test_the_audio_recorder_marks_the_first_audio_and_calls_back(self):
        rec = recorder()
        stops = []
        processor = AudioRecorder(rec, on_tts_stopped=lambda: stops.append(True))
        asyncio.run(
            _feed(
                processor,
                [
                    TTSAudioRawFrame(audio=b"\x00" * 3200, sample_rate=8000, num_channels=1),
                    TTSAudioRawFrame(audio=b"\x00" * 3200, sample_rate=8000, num_channels=1),
                    TTSStoppedFrame(),
                    TTSAudioRawFrame(audio=b"\x00" * 3200, sample_rate=8000, num_channels=1),
                ],
            )
        )
        # Two frames of one reply, then one frame of the next reply.
        self.assertEqual(rec.audio_replies, 2)
        self.assertTrue(stops)

    def test_a_failing_recorder_never_breaks_the_call(self):
        # P3: recording errors must never break a live call.
        class Exploding(CallRecorder):
            def note_user_text(self, text, at=None):
                raise RuntimeError("disk on fire")

        processor = InputRecorder(Exploding("ref"))
        asyncio.run(processor.process_frame(TranscriptionFrame(text="hello", user_id="p", timestamp="now"), None))
        self.assertEqual(len(processor.pushed), 1)


async def _feed(processor, frames):
    for frame in frames:
        await processor.process_frame(frame, None)


if __name__ == "__main__":
    unittest.main()
