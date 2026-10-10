"""Exotel telephony: dial a parent, and be honest about what happened.

The request shape is not invented. ``make_call.py`` in this repository already
dials Exotel in production with:

* ``POST {base}/v1/Accounts/{account_sid}/Calls/connect``
* HTTP Basic auth (API key + API token)
* form fields ``From`` (the parent), ``CallerId`` (the ExoPhone), ``StreamUrl``,
  ``StreamType=bidirectional`` and ``TimeLimit``
* status polling via ``GET {base}/v1/Accounts/{account_sid}/Calls/{sid}.json``

That shape is reused here rather than re-derived from prose. What this module
adds is the part the old dialer did not have: a **classified** result. Every
outcome becomes exactly one of

* ``DIALED`` - the provider returned a call id, so a call exists;
* ``NOT_DIALED`` - we know for certain no call was created (refused, unauthorised,
  rate limited, never connected);
* ``UNCERTAIN_NEEDS_REVIEW`` - the request may have created a call and we lost
  the answer.

Only ``NOT_DIALED`` may ever be dialled again, and the caller decides that, never
this module and never automatically (section 19.1).

The number format for ``From`` is documented as ten digits or ``+91``-prefixed;
the exact accepted spelling is confirmed on the first live call
(``EXOTEL_NUMBER_FORMAT``), because guessing it wrong would ring the wrong person
rather than fail loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

import httpx
from loguru import logger

from app.domain.enums import DialResult
from app.integrations.errors import (
    ErrorCategory,
    ProviderError,
    UnsupportedProviderOperation,
    classify_exception,
    classify_status,
)

#: How the parent number is spelled in the dial request. The ten-digit Indian
#: form is what Exotel documents for the connect API; ``plus91`` is available for
#: an account that requires it. Neither is invented as "the" answer - the first
#: live call confirms which one the account accepts.
NUMBER_FORMAT_DIGITS = "digits"
NUMBER_FORMAT_PLUS91 = "plus91"


@dataclass(frozen=True)
class DialOutcome:
    """The classified result of one dial request."""

    dial_result: DialResult
    provider_call_sid: Optional[str] = None
    error: Optional[ProviderError] = None
    http_status: Optional[int] = None
    raw_keys: tuple = ()

    @property
    def may_redial(self) -> bool:
        """True only for a proven ``NOT_DIALED`` result."""
        return self.dial_result.may_redial

    def sanitized(self) -> Dict[str, Any]:
        return {
            "dial_result": self.dial_result.value,
            "http_status": self.http_status,
            "error": self.error.sanitized() if self.error else None,
        }


@dataclass(frozen=True)
class ProviderStatus:
    """What the provider says about a call, for reconciliation."""

    provider_call_sid: str = ""
    status: str = ""
    duration_seconds: Optional[int] = None
    found: bool = False
    error: Optional[ProviderError] = None


def to_provider_number(e164: str, number_format: str = NUMBER_FORMAT_DIGITS) -> str:
    """Spell a number the way the dial request wants it.

    Input is the canonical E.164 form the importer stores (``+919876500001``).
    """
    digits = "".join(char for char in (e164 or "") if char.isdigit())
    if number_format == NUMBER_FORMAT_PLUS91:
        return f"+{digits}"
    if digits.startswith("91") and len(digits) == 12:
        return digits[2:]
    return digits


class ExotelClient:
    """A small async Exotel client. Never logs a number or a response body."""

    def __init__(
        self,
        *,
        base_url: str,
        account_sid: str,
        api_key: str,
        api_token: str,
        caller_id: str,
        stream_url: str,
        timeout_secs: float = 20.0,
        number_format: str = NUMBER_FORMAT_DIGITS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.account_sid = account_sid
        self.api_key = api_key
        self.api_token = api_token
        self.caller_id = caller_id
        self.stream_url = stream_url
        self.timeout_secs = timeout_secs
        self.number_format = number_format
        self._transport = transport

    # ------------------------------------------------------------------ helpers
    @property
    def configured(self) -> bool:
        return bool(
            self.base_url
            and self.account_sid
            and self.api_key
            and self.api_token
            and self.caller_id
            and self.stream_url
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_secs,
            auth=(self.api_key, self.api_token),
            transport=self._transport,
            headers={"Accept": "application/json"},
        )

    def _dial_url(self) -> str:
        return f"/v1/Accounts/{self.account_sid}/Calls/connect"

    def _status_url(self, provider_call_sid: str) -> str:
        return f"/v1/Accounts/{self.account_sid}/Calls/{provider_call_sid}.json"

    # -------------------------------------------------------------------- dial
    async def dial(
        self,
        *,
        to_e164: str,
        stream_url: Optional[str] = None,
        time_limit_secs: Optional[int] = None,
        extra_fields: Optional[Mapping[str, str]] = None,
    ) -> DialOutcome:
        """Ask Exotel to call the parent and connect the media stream.

        Never raises for a provider problem: the result carries the reason, so a
        caller cannot forget to handle a failure and cannot mistake one for a
        success.
        """
        if not self.configured:
            return DialOutcome(
                dial_result=DialResult.NOT_DIALED,
                error=ProviderError(
                    category=ErrorCategory.CONFIGURATION,
                    message="Exotel is not fully configured; refusing to dial",
                    provider="exotel",
                ),
            )

        payload: Dict[str, str] = {
            "From": to_provider_number(to_e164, self.number_format),
            "CallerId": self.caller_id,
            "StreamUrl": stream_url or self.stream_url,
            "StreamType": "bidirectional",
        }
        if time_limit_secs:
            payload["TimeLimit"] = str(int(time_limit_secs))
        if extra_fields:
            payload.update(dict(extra_fields))

        request_sent = False
        try:
            async with self._client() as client:
                request = client.build_request("POST", self._dial_url(), data=payload)
                # Everything after this line means the request may have arrived.
                request_sent = True
                response = await client.send(request)
        except Exception as exc:  # any transport failure at all
            category, retryable = classify_exception(exc, request_sent=request_sent)
            error = ProviderError(
                category=category,
                message=f"the dial request failed ({type(exc).__name__})",
                provider="exotel",
                retryable=retryable,
            )
            result = (
                DialResult.NOT_DIALED if error.dial_result_is_certain else DialResult.UNCERTAIN_NEEDS_REVIEW
            )
            logger.warning("exotel dial: {}", error)
            return DialOutcome(dial_result=result, error=error)

        if response.status_code >= 400:
            category = classify_status(response.status_code)
            error = ProviderError(
                category=category,
                message="Exotel refused the dial request",
                provider="exotel",
                http_status=response.status_code,
                retryable=False,
            )
            result = (
                DialResult.NOT_DIALED
                if category is not ErrorCategory.PROVIDER_UNAVAILABLE
                else DialResult.UNCERTAIN_NEEDS_REVIEW
            )
            logger.warning("exotel dial: {}", error)
            return DialOutcome(
                dial_result=result, error=error, http_status=response.status_code
            )

        body = _safe_json(response)
        if body is None:
            # A 2xx we cannot read: a call may exist, so this is never retried.
            error = ProviderError(
                category=ErrorCategory.MALFORMED_RESPONSE,
                message="Exotel accepted the dial but returned an unreadable body",
                provider="exotel",
                http_status=response.status_code,
            )
            return DialOutcome(
                dial_result=DialResult.UNCERTAIN_NEEDS_REVIEW,
                error=error,
                http_status=response.status_code,
            )

        call_node = body.get("Call") if isinstance(body, dict) else None
        if not isinstance(call_node, dict):
            call_node = body if isinstance(body, dict) else {}
        provider_call_sid = _pick(call_node, ("Sid", "CallSid", "CallSid ", "id"))

        if not provider_call_sid:
            error = ProviderError(
                category=ErrorCategory.MALFORMED_RESPONSE,
                message="Exotel accepted the dial but returned no call id",
                provider="exotel",
                http_status=response.status_code,
            )
            return DialOutcome(
                dial_result=DialResult.UNCERTAIN_NEEDS_REVIEW,
                error=error,
                http_status=response.status_code,
                raw_keys=tuple(sorted(str(key) for key in call_node)),
            )

        return DialOutcome(
            dial_result=DialResult.DIALED,
            provider_call_sid=str(provider_call_sid),
            http_status=response.status_code,
            raw_keys=tuple(sorted(str(key) for key in call_node)),
        )

    # ------------------------------------------------------------------ status
    async def status(self, provider_call_sid: str) -> ProviderStatus:
        """Ask the provider about a call. Used for reconciliation, never for metrics
        that were not actually reported."""
        if not self.configured or not provider_call_sid:
            return ProviderStatus(
                provider_call_sid=provider_call_sid,
                error=ProviderError(
                    category=ErrorCategory.CONFIGURATION,
                    message="Exotel is not configured for a status lookup",
                    provider="exotel",
                ),
            )
        try:
            async with self._client() as client:
                response = await client.get(self._status_url(provider_call_sid))
        except Exception as exc:
            category, retryable = classify_exception(exc, request_sent=False)
            return ProviderStatus(
                provider_call_sid=provider_call_sid,
                error=ProviderError(
                    category=category,
                    message=f"the status request failed ({type(exc).__name__})",
                    provider="exotel",
                    retryable=retryable,
                ),
            )

        if response.status_code == 404:
            return ProviderStatus(
                provider_call_sid=provider_call_sid,
                found=False,
                error=ProviderError(
                    category=ErrorCategory.PROVIDER_REJECTED,
                    message="Exotel does not know this call id",
                    provider="exotel",
                    http_status=404,
                ),
            )
        if response.status_code >= 400:
            return ProviderStatus(
                provider_call_sid=provider_call_sid,
                error=ProviderError(
                    category=classify_status(response.status_code),
                    message="Exotel refused the status request",
                    provider="exotel",
                    http_status=response.status_code,
                ),
            )

        body = _safe_json(response) or {}
        call_node = body.get("Call") if isinstance(body, dict) and isinstance(body.get("Call"), dict) else body
        if not isinstance(call_node, dict):
            return ProviderStatus(
                provider_call_sid=provider_call_sid,
                error=ProviderError(
                    category=ErrorCategory.MALFORMED_RESPONSE,
                    message="the status response could not be read",
                    provider="exotel",
                ),
            )

        duration = _pick(call_node, ("ConversationDuration", "Duration", "duration"))
        try:
            duration_seconds = int(float(duration)) if duration is not None else None
        except (TypeError, ValueError):
            duration_seconds = None

        return ProviderStatus(
            provider_call_sid=provider_call_sid,
            status=str(_pick(call_node, ("Status", "status")) or ""),
            duration_seconds=duration_seconds,
            found=True,
        )

    # ------------------------------------------------------------------ hangup
    async def hangup(self, provider_call_sid: str) -> None:
        """Deliberately unsupported until the endpoint is verified.

        Ending a call is not something to guess at: the wrong request could end
        somebody else's call. Stage 1 bounds every call with ``TimeLimit`` on the
        dial request and stops sending audio when the conversation is over, and
        this method says so instead of pretending.
        """
        raise UnsupportedProviderOperation(
            "forcing a hangup",
            provider="exotel",
            detail=(
                "calls are bounded by TimeLimit and ended by closing the media "
                "stream; a disconnect endpoint must be verified on the account first"
            ),
        )


def _safe_json(response: httpx.Response) -> Optional[dict]:
    """Parse JSON without ever keeping the body in memory longer than needed."""
    try:
        body = response.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _pick(node: Mapping[str, Any], keys: tuple) -> Optional[Any]:
    for key in keys:
        if key in node and node[key] not in (None, ""):
            return node[key]
    return None


def client_from_settings(settings, *, transport: Optional[httpx.AsyncBaseTransport] = None) -> ExotelClient:
    """Build the client from validated settings (no credential ever hard-coded)."""
    return ExotelClient(
        base_url=settings.base.exotel_api_base,
        account_sid=settings.base.exotel_account_sid,
        api_key=settings.base.exotel_api_key,
        api_token=settings.base.exotel_api_token,
        caller_id=settings.base.exotel_caller_id,
        stream_url=settings.base.exotel_stream_url,
        timeout_secs=settings.provider_timeout_secs,
        transport=transport,
    )
