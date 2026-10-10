"""The Sarvam client must stream, respect cancellation, and never leak a prompt."""

from __future__ import annotations

import json
import unittest

import httpx

from app.config.settings import Stage1Settings
from app.integrations.errors import ErrorCategory
from app.integrations.sarvam import clients as sarvam


def sse(*chunks: dict, done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def text_chunk(text: str, usage: dict | None = None) -> dict:
    chunk = {"choices": [{"delta": {"content": text}, "index": 0}]}
    if usage:
        chunk["usage"] = usage
    return chunk


def make_client(handler, **overrides) -> sarvam.SarvamLLMClient:
    options = dict(
        api_key="sarvam-key-not-real",
        model="sarvam-105b-conversations",
        timeout_secs=5.0,
        transport=httpx.MockTransport(handler),
    )
    options.update(overrides)
    return sarvam.SarvamLLMClient(**options)


class TestBaseUrlResolution(unittest.TestCase):
    def test_verified_v1_model_uses_v1(self):
        self.assertEqual(
            sarvam.resolve_base_url("sarvam-105b-conversations"),
            "https://api.sarvam.ai/v1",
        )

    def test_other_models_use_v2(self):
        self.assertEqual(sarvam.resolve_base_url("sarvam-105b"), "https://api.sarvam.ai/v2")

    def test_override_wins(self):
        self.assertEqual(
            sarvam.resolve_base_url("sarvam-105b-conversations", "https://proxy.test/v1"),
            "https://proxy.test/v1",
        )

    def test_client_uses_the_resolved_base(self):
        client = make_client(lambda request: httpx.Response(200))
        self.assertTrue(client.base_url.endswith("/v1"))


class TestCompleteReply(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_text_is_collected(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, content=sse(text_chunk("సర్ "), text_chunk("ధన్యవాదాలు")))

        reply = await make_client(handler).complete_reply(
            [{"role": "system", "content": "you are Veda"}, {"role": "user", "content": "hi"}]
        )
        self.assertTrue(reply.ok)
        self.assertEqual(reply.text, "సర్ ధన్యవాదాలు")
        self.assertIsNotNone(reply.first_token_ms)
        self.assertTrue(captured["url"].endswith("/v1/chat/completions"))
        self.assertEqual(captured["body"]["model"], "sarvam-105b-conversations")
        self.assertTrue(captured["body"]["stream"])
        # Both headers, exactly as the installed client sends them.
        self.assertIn("api-subscription-key", captured["headers"])
        self.assertIn("authorization", {key.lower() for key in captured["headers"]})

    async def test_usage_is_recorded_when_the_service_reports_it(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=sse(text_chunk("హలో", usage={"prompt_tokens": 120, "completion_tokens": 18})),
            )

        reply = await make_client(handler).complete_reply([])
        self.assertEqual(reply.prompt_tokens, 120)
        self.assertEqual(reply.completion_tokens, 18)

    async def test_usage_stays_none_when_the_service_reports_nothing(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=sse(text_chunk("హలో")))

        reply = await make_client(handler).complete_reply([])
        self.assertIsNone(reply.prompt_tokens)

    async def test_malformed_chunks_are_skipped_not_fatal(self):
        payload = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: not json\n\ndata: [DONE]\n\n'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payload)

        reply = await make_client(handler).complete_reply([])
        self.assertEqual(reply.text, "ok")

    async def test_unauthorised_is_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad key"})

        reply = await make_client(handler).complete_reply([])
        self.assertFalse(reply.ok)
        self.assertIs(reply.error.category, ErrorCategory.AUTH)

    async def test_provider_failure_is_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        reply = await make_client(handler).complete_reply([])
        self.assertIs(reply.error.category, ErrorCategory.PROVIDER_UNAVAILABLE)

    async def test_timeout_is_reported_not_raised(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        reply = await make_client(handler).complete_reply([])
        self.assertFalse(reply.ok)
        self.assertIs(reply.error.category, ErrorCategory.TIMEOUT)

    async def test_cancellation_stops_generation(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=sse(text_chunk("first "), text_chunk("second "), text_chunk("third"))
            )

        reply = await make_client(handler).complete_reply([], cancelled=lambda: True)
        self.assertTrue(reply.cancelled)
        self.assertEqual(reply.text, "")

    async def test_unconfigured_client_does_not_call_the_service(self):
        called = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            called["count"] += 1
            return httpx.Response(200)

        reply = await make_client(handler, api_key="").complete_reply([])
        self.assertIs(reply.error.category, ErrorCategory.CONFIGURATION)
        self.assertEqual(called["count"], 0)

    async def test_error_view_never_contains_the_prompt_or_the_reply(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        reply = await make_client(handler).complete_reply(
            [{"role": "user", "content": "student Alekhya Rao owes five thousand rupees"}]
        )
        blob = json.dumps(reply.sanitized())
        self.assertNotIn("Alekhya", blob)
        self.assertNotIn("five thousand", blob)

    async def test_stream_reply_yields_text_pieces(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=sse(text_chunk("one "), text_chunk("two")))

        pieces = [piece async for piece in make_client(handler).stream_reply([])]
        self.assertEqual("".join(pieces), "one two")


class TestProcessorFactories(unittest.TestCase):
    def settings(self) -> Stage1Settings:
        return Stage1Settings.from_env({"SARVAM_API_KEY": "key-not-real"})

    def test_stt_processor_uses_the_realtime_model_and_stream_rate(self):
        settings = Stage1Settings.from_env(
            {
                "SARVAM_API_KEY": "key-not-real",
                "EXOTEL_STREAM_URL": "wss://example.test/v1/exotel/stream?sample-rate=8000",
            }
        )
        processor = sarvam.build_stt_processor(settings)
        self.assertEqual(processor.model, "saaras:v3-realtime")
        self.assertEqual(processor.sample_rate, 8000)
        self.assertEqual(processor.language_code, "te-IN")

    def test_tts_processor_carries_voice_and_pace(self):
        processor = sarvam.build_tts_processor(self.settings())
        self.assertEqual(processor.model, "bulbul:v3")
        self.assertEqual(processor.speaker, "shubh")
        self.assertGreaterEqual(processor.min_buffer_size, 30)

    def test_stt_model_keeps_a_realtime_suffix_only_once(self):
        settings = Stage1Settings.from_env(
            {"SARVAM_API_KEY": "key", "SARVAM_STT_MODEL": "saaras:v3-realtime"}
        )
        processor = sarvam.build_stt_processor(settings)
        self.assertEqual(processor.model, "saaras:v3-realtime")

    def test_overrides_win(self):
        processor = sarvam.build_tts_processor(self.settings(), speaker="kavya")
        self.assertEqual(processor.speaker, "kavya")


class TestTranscriptNormalisation(unittest.TestCase):
    def test_whitespace_is_collapsed(self):
        self.assertEqual(sarvam.normalise_transcript("  hello   sir \n"), "hello sir")

    def test_control_characters_are_removed(self):
        self.assertEqual(sarvam.normalise_transcript("hello\x07sir"), "hellosir")

    def test_telugu_is_preserved(self):
        self.assertEqual(sarvam.normalise_transcript("అవును సర్"), "అవును సర్")

    def test_empty_input(self):
        self.assertEqual(sarvam.normalise_transcript(""), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
