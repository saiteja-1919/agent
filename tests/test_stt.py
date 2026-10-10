"""Speech recognition: timestamps, bad messages, reconnects, quiet drops (P1)."""

import asyncio
import base64
import json
import unittest

import stubs

stubs.install()

from pipecat.frames.frames import InputAudioRawFrame, StartFrame  # noqa: E402
from sarvam_stt import SarvamRealtimeSTT, build_transcription  # noqa: E402


def service(**kwargs):
    options = {
        "api_key": "key",
        "model": "saaras:v3-realtime",
        "language_code": "te-IN",
        "sample_rate": 8000,
    }
    options.update(kwargs)
    return SarvamRealtimeSTT(**options)


class FrameTest(unittest.TestCase):
    def test_a_transcript_frame_always_has_its_timestamp(self):
        # P1: a frame built without a timestamp silenced a whole call.
        frame = build_transcription("hello")
        self.assertEqual(frame.text, "hello")
        self.assertTrue(frame.timestamp)

    def test_the_url_carries_every_agreed_parameter(self):
        url = service().url()
        for fragment in (
            "model=saaras:v3-realtime",
            "language_code=te-IN",
            "stream_type=fast",
            "endpointing=vad",
            "encoding=linear16",
            "sample_rate=8000",
            "mode=codemix",
        ):
            self.assertIn(fragment, url)


class AudioTest(unittest.TestCase):
    def test_audio_is_sent_as_base64_pcm(self):
        stt = service()
        connection = stubs.FakeConnection()
        stt.connection = connection
        stt.connected = True
        pcm = b"\x01\x02" * 160
        asyncio.run(stt.process_frame(InputAudioRawFrame(audio=pcm, sample_rate=8000, num_channels=1), None))
        payload = connection.json_sent()[0]
        self.assertEqual(payload["event"], "audio_input")
        self.assertEqual(base64.b64decode(payload["audio"]), pcm)

    def test_audio_before_the_socket_is_open_is_dropped_and_counted(self):
        stt = service()
        asyncio.run(stt.process_frame(InputAudioRawFrame(audio=b"\x00" * 10, sample_rate=8000, num_channels=1), None))
        self.assertEqual(stt.dropped_frames, 1)
        self.assertEqual(stt.connection, None)

    def test_dropped_audio_is_only_logged_once_a_second(self):
        # P1: the old code logged every dropped frame and flooded the console.
        import sarvam_stt

        warnings = []
        original = sarvam_stt.logger.warning
        sarvam_stt.logger.warning = lambda message, *args, **kwargs: warnings.append(message)
        try:
            stt = service()
            for _ in range(50):
                stt._log_drop()
        finally:
            sarvam_stt.logger.warning = original
        self.assertEqual(len(warnings), 1)
        self.assertEqual(stt.dropped_frames, 50)

    def test_a_send_failure_does_not_raise(self):
        stt = service()
        connection = stubs.FakeConnection(send_error=OSError("socket gone"))
        stt.connection = connection
        stt.connected = True
        asyncio.run(stt.process_frame(InputAudioRawFrame(audio=b"\x00" * 10, sample_rate=8000, num_channels=1), None))
        self.assertFalse(stt.connected)
        self.assertEqual(stt.dropped_frames, 1)


class StopSoon:
    """A sleep() replacement that stops the retry loop after one wait."""

    def __init__(self, stt):
        self.stt = stt

    async def __call__(self, delay):
        self.stt._stopping = True


class MessageTest(unittest.TestCase):
    def read(self, messages):
        """Push prepared socket messages through the receive loop."""
        stt = service()
        stt.connection = stubs.FakeConnection(messages=messages)
        stt.connected = True

        async def pump():
            await stt._receive_loop()
            await asyncio.sleep(0)  # let the queued frame pushes run

        asyncio.run(pump())
        return stt

    def test_a_final_transcript_becomes_a_frame(self):
        stt = self.read(['{"event":"transcript.final","text":"నమస్తే సర్"}'])
        texts = [getattr(frame, "text", "") for frame in stt.pushed]
        self.assertIn("నమస్తే సర్", texts)
        self.assertEqual(stt.finals, 1)

    def test_partial_transcripts_are_ignored(self):
        stt = self.read(['{"event":"transcript.partial","text":"నము"}'])
        self.assertEqual(stt.finals, 0)
        self.assertEqual(stt.pushed, [])

    def test_a_bad_message_never_ends_the_loop(self):
        # P1: one exception used to stop all speech recognition.
        stt = self.read(
            [
                "this is not json",
                '{"event":"unknown.event"}',
                '{"event":"transcript.final","text":"అవును"}',
            ]
        )
        texts = [getattr(frame, "text", "") for frame in stt.pushed]
        self.assertEqual(texts, ["అవును"])

    def test_an_error_event_is_reported_not_fatal(self):
        stt = self.read(['{"event":"error","message":"quota"}'])
        self.assertIn("quota", stt.errors)

    def test_binary_messages_are_ignored(self):
        stt = self.read([b"\x00\x01"])
        self.assertEqual(stt.pushed, [])


class DroppingConnector:
    """Every connection opens and then drops immediately."""

    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, url, **kwargs):
        self.calls.append(url)
        return stubs.FakeConnection(messages=[RuntimeError("connection dropped")])


class RefusingConnector:
    """The server cannot be reached at all."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, url, **kwargs):
        self.calls += 1
        raise OSError("cannot connect")


class ReconnectTest(unittest.TestCase):
    def test_the_socket_reconnects_by_itself(self):
        # P1: speech recognition must come back on its own after a drop.
        connector = DroppingConnector()
        sleep = stubs.StopAfter(2)
        stt = service(connector=connector, sleep=sleep)

        async def run():
            with self.assertRaises(asyncio.CancelledError):
                await stt._run()

        asyncio.run(run())
        self.assertEqual(len(connector.calls), 2)
        self.assertEqual(stt.reconnects, 2)
        # The socket came up both times, so the wait stays at the base delay;
        # a socket that cannot connect at all backs off further (tested below).
        self.assertEqual(sleep.delays, [0.5, 0.5])

    def test_backoff_grows_but_is_capped(self):
        connector = RefusingConnector()
        sleep = stubs.StopAfter(5)
        stt = service(connector=connector, sleep=sleep, reconnect_max_secs=2.0)

        async def run():
            with self.assertRaises(asyncio.CancelledError):
                await stt._run()

        asyncio.run(run())
        self.assertEqual(connector.calls, 5)
        self.assertEqual(sleep.delays, [0.5, 1.0, 2.0, 2.0, 2.0])

    def test_start_opens_the_socket_and_stop_closes_it(self):
        connector = stubs.Connector(
            [stubs.FakeConnection(messages=[OSError("drop")]) for _ in range(4)]
        )
        stt = service(connector=connector)

        async def run():
            await stt.process_frame(StartFrame(), None)
            await asyncio.sleep(0.05)
            self.assertTrue(connector.calls)
            await stt.stop()

        asyncio.run(run())
        self.assertFalse(stt.connected)
        self.assertEqual(stt.stats()["finals"], 0)


if __name__ == "__main__":
    unittest.main()
