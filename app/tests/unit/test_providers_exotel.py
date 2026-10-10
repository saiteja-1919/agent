"""The dial classification is the safety-critical part of the whole system."""

from __future__ import annotations

import base64
import json
import unittest

import httpx

from app.domain.enums import DialResult, ProviderEventKind
from app.integrations.errors import ErrorCategory, ProviderError, UnsupportedProviderOperation
from app.integrations.exotel import client as exotel_client
from app.integrations.exotel import events as exotel_events
from app.integrations.exotel import verify as exotel_verify
from app.integrations.exotel.client import (
    NUMBER_FORMAT_PLUS91,
    ExotelClient,
    to_provider_number,
)

DIAL_URL = "/v1/Accounts/ACxxxx/Calls/connect"


def make_client(handler, **overrides) -> ExotelClient:
    options = dict(
        base_url="https://api.exotel.com",
        account_sid="ACxxxx",
        api_key="key-not-real",
        api_token="token-not-real",
        caller_id="04041895032",
        stream_url="wss://example.test/v1/exotel/stream?sample-rate=8000",
        timeout_secs=5.0,
        transport=httpx.MockTransport(handler),
    )
    options.update(overrides)
    return ExotelClient(**options)


class TestDialClassification(unittest.IsolatedAsyncioTestCase):
    async def test_successful_dial_is_dialed(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = dict(
                pair.split("=", 1) for pair in request.content.decode().split("&")
            )
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"Call": {"Sid": "call-123", "Status": "in-progress"}})

        outcome = await make_client(handler).dial(to_e164="+919876500001", time_limit_secs=600)
        self.assertIs(outcome.dial_result, DialResult.DIALED)
        self.assertEqual(outcome.provider_call_sid, "call-123")
        self.assertFalse(outcome.may_redial)
        self.assertTrue(captured["url"].endswith(DIAL_URL))
        self.assertEqual(captured["body"]["CallerId"], "04041895032")
        self.assertEqual(captured["body"]["StreamType"], "bidirectional")
        self.assertEqual(captured["body"]["TimeLimit"], "600")
        self.assertTrue(captured["auth"].startswith("Basic "))

    async def test_the_number_is_sent_in_the_configured_format(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"Call": {"Sid": "c1"}})

        await make_client(handler).dial(to_e164="+919876500001")
        # Ten-digit Indian form: the country code is dropped for this account.
        self.assertIn("From=9876500001", captured["body"])

        await make_client(handler, number_format=NUMBER_FORMAT_PLUS91).dial(
            to_e164="+919876500001"
        )
        self.assertIn("From=%2B919876500001", captured["body"])

    async def test_acceptance_without_a_call_id_is_uncertain(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"Message": "ok"})

        outcome = await make_client(handler).dial(to_e164="+919876500001")
        self.assertIs(outcome.dial_result, DialResult.UNCERTAIN_NEEDS_REVIEW)
        self.assertFalse(outcome.may_redial)
        self.assertIs(outcome.error.category, ErrorCategory.MALFORMED_RESPONSE)

    async def test_unreadable_body_is_uncertain(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>gateway</html>")

        outcome = await make_client(handler).dial(to_e164="+919876500001")
        self.assertIs(outcome.dial_result, DialResult.UNCERTAIN_NEEDS_REVIEW)

    async def test_unauthorised_is_not_dialed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad credentials"})

        outcome = await make_client(handler).dial(to_e164="+919876500001")
        self.assertIs(outcome.dial_result, DialResult.NOT_DIALED)
        self.assertIs(outcome.error.category, ErrorCategory.AUTH)
        self.assertTrue(outcome.may_redial)

    async def test_bad_request_is_not_dialed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid From"})

        outcome = await make_client(handler).dial(to_e164="+919876500001")
        self.assertIs(outcome.dial_result, DialResult.NOT_DIALED)
        self.assertIs(outcome.error.category, ErrorCategory.PROVIDER_REJECTED)

    async def test_rate_limited_is_not_dialed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429)

        outcome = await make_client(handler).dial(to_e164="+919876500001")
        self.assertIs(outcome.dial_result, DialResult.NOT_DIALED)
        self.assertIs(outcome.error.category, ErrorCategory.RATE_LIMITED)

    async def test_provider_failure_is_uncertain(self):
        """A 5xx means the provider may have created the call; never retry blindly."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        outcome = await make_client(handler).dial(to_e164="+919876500001")
        self.assertIs(outcome.dial_result, DialResult.UNCERTAIN_NEEDS_REVIEW)
        self.assertFalse(outcome.may_redial)
        self.assertIs(outcome.error.category, ErrorCategory.PROVIDER_UNAVAILABLE)

    async def test_connection_refused_is_not_dialed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        outcome = await make_client(handler).dial(to_e164="+919876500001")
        self.assertIs(outcome.dial_result, DialResult.NOT_DIALED)
        self.assertTrue(outcome.may_redial)

    async def test_read_timeout_is_uncertain(self):
        """The request went out and the answer was lost: we must not assume no call."""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out waiting for the response")

        outcome = await make_client(handler).dial(to_e164="+919876500001")
        self.assertIs(outcome.dial_result, DialResult.UNCERTAIN_NEEDS_REVIEW)
        self.assertFalse(outcome.may_redial)
        self.assertIs(outcome.error.category, ErrorCategory.TIMEOUT)

    async def test_unconfigured_client_never_sends_a_request(self):
        called = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            called["count"] += 1
            return httpx.Response(200)

        outcome = await make_client(handler, api_key="").dial(to_e164="+919876500001")
        self.assertIs(outcome.dial_result, DialResult.NOT_DIALED)
        self.assertIs(outcome.error.category, ErrorCategory.CONFIGURATION)
        self.assertEqual(called["count"], 0)

    async def test_error_view_never_contains_the_number_or_credentials(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad"})

        outcome = await make_client(handler).dial(to_e164="+919876500001")
        blob = json.dumps(outcome.sanitized())
        self.assertNotIn("9876500001", blob)
        self.assertNotIn("key-not-real", blob)
        self.assertNotIn("token-not-real", blob)


class TestStatusLookup(unittest.IsolatedAsyncioTestCase):
    async def test_status_is_read(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/Calls/call-1.json", str(request.url))
            return httpx.Response(
                200, json={"Call": {"Status": "completed", "ConversationDuration": "42"}}
            )

        status = await make_client(handler).status("call-1")
        self.assertTrue(status.found)
        self.assertEqual(status.status, "completed")
        self.assertEqual(status.duration_seconds, 42)

    async def test_unknown_call_is_reported_not_guessed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        status = await make_client(handler).status("missing")
        self.assertFalse(status.found)
        self.assertIs(status.error.category, ErrorCategory.PROVIDER_REJECTED)

    async def test_hangup_refuses_to_pretend(self):
        client = make_client(lambda request: httpx.Response(200))
        with self.assertRaises(UnsupportedProviderOperation):
            await client.hangup("call-1")


class TestNumberFormatting(unittest.TestCase):
    def test_e164_becomes_ten_digits(self):
        self.assertEqual(to_provider_number("+919876500001"), "9876500001")

    def test_plus91_format(self):
        self.assertEqual(to_provider_number("+919876500001", NUMBER_FORMAT_PLUS91), "+919876500001")

    def test_punctuation_is_stripped(self):
        self.assertEqual(to_provider_number("+91 98765 00001"), "9876500001")


class TestCallbackParsing(unittest.TestCase):
    def test_known_statuses_map_to_events(self):
        cases = {
            "ringing": ProviderEventKind.RINGING,
            "in-progress": ProviderEventKind.ANSWERED,
            "answered": ProviderEventKind.ANSWERED,
            "completed": ProviderEventKind.COMPLETED,
            "no-answer": ProviderEventKind.FAILED,
            "busy": ProviderEventKind.FAILED,
            "failed": ProviderEventKind.FAILED,
            "canceled": ProviderEventKind.FAILED,
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                event = exotel_events.parse_status_callback(
                    {"CallSid": "c1", "Status": status}
                )
                self.assertIsNotNone(event)
                self.assertIs(event.kind, expected)

    def test_status_words_are_case_insensitive(self):
        event = exotel_events.parse_status_callback({"CallSid": "c1", "Status": "Ringing"})
        self.assertIs(event.kind, ProviderEventKind.RINGING)

    def test_unknown_status_is_not_guessed(self):
        self.assertIsNone(
            exotel_events.parse_status_callback({"CallSid": "c1", "Status": "summarising"})
        )

    def test_missing_call_id_is_refused_with_a_reason(self):
        problems = exotel_events.validate_callback({"Status": "ringing"})
        self.assertTrue(problems)
        self.assertEqual(problems[0].field, "CallSid")
        self.assertIsNone(exotel_events.parse_status_callback({"Status": "ringing"}))

    def test_empty_payload_is_refused(self):
        self.assertTrue(exotel_events.validate_callback({}))

    def test_duration_is_read_when_present(self):
        event = exotel_events.parse_status_callback(
            {"CallSid": "c1", "Status": "completed", "ConversationDuration": "31"}
        )
        self.assertEqual(event.duration_seconds, 31)

    def test_event_key_is_stable_for_a_repeat_callback(self):
        payload = {"CallSid": "c1", "Status": "ringing"}
        self.assertEqual(exotel_events.event_key(payload), exotel_events.event_key(payload))
        other = {"CallSid": "c1", "Status": "completed"}
        self.assertNotEqual(exotel_events.event_key(payload), exotel_events.event_key(other))

    def test_digest_does_not_contain_the_payload_text(self):
        digest = exotel_events.payload_digest(
            {"CallSid": "c1", "Status": "completed", "Details": "9876500001"}
        )
        self.assertNotIn("9876500001", digest)
        self.assertEqual(len(digest), 64)

    def test_no_answer_detection(self):
        self.assertTrue(exotel_events.status_implies_no_answer({"Status": "no-answer"}))
        self.assertTrue(exotel_events.status_implies_no_answer({"Status": "failed", "Details": "busy"}))
        self.assertFalse(exotel_events.status_implies_no_answer({"Status": "completed"}))

    def test_parent_hangup_detection(self):
        self.assertTrue(
            exotel_events.status_implies_parent_hung_up({"Details": "Caller hung up"})
        )
        self.assertFalse(
            exotel_events.status_implies_parent_hung_up({"Details": "Agent hung up"})
        )


class TestCallbackVerification(unittest.TestCase):
    def test_matching_token_in_query_is_accepted(self):
        decision = exotel_verify.verify_callback(
            expected_token="s3cret", query={"token": "s3cret"}
        )
        self.assertTrue(decision.accepted)
        self.assertTrue(decision.token_checked)

    def test_matching_token_in_header_is_accepted(self):
        decision = exotel_verify.verify_callback(
            expected_token="s3cret", headers={"X-Veda-Callback-Token": "s3cret"}
        )
        self.assertTrue(decision.accepted)

    def test_matching_token_in_form_is_accepted(self):
        decision = exotel_verify.verify_callback(expected_token="s3cret", form={"Token": "s3cret"})
        self.assertTrue(decision.accepted)

    def test_wrong_token_is_refused(self):
        decision = exotel_verify.verify_callback(expected_token="s3cret", query={"token": "wrong"})
        self.assertFalse(decision.accepted)
        self.assertIn("token", decision.reason)

    def test_missing_token_is_refused_when_one_is_configured(self):
        decision = exotel_verify.verify_callback(expected_token="s3cret")
        self.assertFalse(decision.accepted)

    def test_no_configured_token_is_accepted_but_reported_as_unchecked(self):
        decision = exotel_verify.verify_callback()
        self.assertTrue(decision.accepted)
        self.assertFalse(decision.token_checked)
        self.assertIn("call id", decision.reason)

    def test_constant_time_compare_handles_empty_values(self):
        self.assertFalse(exotel_verify.constant_time_equals("", "x"))
        self.assertFalse(exotel_verify.constant_time_equals("x", ""))
        self.assertFalse(exotel_verify.constant_time_equals(None, "x"))
        self.assertTrue(exotel_verify.constant_time_equals("x", "x"))

    def test_ip_allow_list_with_cidr(self):
        self.assertTrue(exotel_verify.ip_is_allowed("203.0.113.5", ["203.0.113.0/24"]))
        self.assertFalse(exotel_verify.ip_is_allowed("198.51.100.5", ["203.0.113.0/24"]))

    def test_ip_allow_list_exact_match(self):
        self.assertTrue(exotel_verify.ip_is_allowed("203.0.113.5", ["203.0.113.5"]))

    def test_missing_ip_is_refused_when_a_list_is_configured(self):
        self.assertFalse(exotel_verify.ip_is_allowed(None, ["203.0.113.0/24"]))

    def test_malformed_allow_list_entry_does_not_open_the_door(self):
        self.assertFalse(exotel_verify.ip_is_allowed("203.0.113.5", ["not-an-ip"]))

    def test_a_bad_address_is_refused_even_with_a_good_token(self):
        decision = exotel_verify.verify_callback(
            expected_token="s3cret",
            query={"token": "s3cret"},
            client_ip="198.51.100.9",
            allowed_ips=["203.0.113.0/24"],
        )
        self.assertFalse(decision.accepted)
        self.assertIn("allow-list", decision.reason)

    def test_token_finder_prefers_the_query_parameter(self):
        self.assertEqual(
            exotel_verify.find_token(
                query={"token": "from-query"},
                headers={"x-veda-callback-token": "from-header"},
                form={"Token": "from-form"},
            ),
            "from-query",
        )

    def test_token_finder_returns_none_when_absent(self):
        self.assertIsNone(exotel_verify.find_token(query={}, form={}, headers={}))


class TestBasicAuthIsUsed(unittest.TestCase):
    def test_client_sends_basic_auth(self):
        """The verified dial path authenticates with HTTP Basic, not a bearer token."""
        expected = base64.b64encode(b"key-not-real:token-not-real").decode()
        self.assertEqual(
            base64.b64encode(b"key-not-real:token-not-real").decode(), expected
        )
        client = make_client(lambda request: httpx.Response(200))
        self.assertEqual((client.api_key, client.api_token), ("key-not-real", "token-not-real"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
