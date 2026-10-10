"""Verifying that a callback really came from the provider.

Section 17.2 says plainly: **do not invent a webhook signature algorithm.** Exotel's
signature mechanism has not been verified against the live account, so nothing
here pretends to check one. What is implemented are the three mechanisms the
clarifications do allow, after the fact:

1. a **secret token** in the callback URL (or the ``X-Veda-Callback-Token``
   header), compared in constant time;
2. an optional **allow-list of provider addresses**, using real CIDR matching;
3. **matching the provider call id to an internal attempt**, which is what makes
   a forged callback useless even if it gets past the first two.

If no token is configured, callbacks are still accepted but every one is matched
against an internal attempt before anything happens to it - and the health
of that setting is reported by ``config check`` so the operator can see it.
"""

from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

#: Where a callback is being read from. Only the fields Veda actually uses.
TOKEN_QUERY_KEYS = ("token", "stream_token", "callback_token")
TOKEN_HEADER_KEYS = ("x-veda-callback-token", "x-callback-token")
TOKEN_FORM_KEYS = ("Token", "token", "CallbackToken")


@dataclass(frozen=True)
class VerificationDecision:
    """The result of verifying a callback."""

    accepted: bool
    reason: str = ""
    token_checked: bool = False
    ip_checked: bool = False

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.accepted


def constant_time_equals(provided: Optional[str], expected: Optional[str]) -> bool:
    """Compare two secrets without leaking their length or content via timing."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def find_token(
    *,
    query: Optional[Mapping[str, str]] = None,
    form: Optional[Mapping[str, str]] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Pull the shared token out of a callback, wherever it was put."""
    if query:
        for key in TOKEN_QUERY_KEYS:
            if query.get(key):
                return str(query[key])
    if headers:
        lowered = {str(key).lower(): value for key, value in headers.items()}
        for key in TOKEN_HEADER_KEYS:
            if lowered.get(key):
                return str(lowered[key])
    if form:
        for key in TOKEN_FORM_KEYS:
            if form.get(key):
                return str(form[key])
    return None


def ip_is_allowed(client_ip: Optional[str], allowed: Sequence[str]) -> bool:
    """True when ``client_ip`` matches one of the allowed addresses or CIDR blocks."""
    if not allowed:
        return True  # nothing to check
    if not client_ip:
        return False
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowed:
        candidate = str(entry).strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                if address in ipaddress.ip_network(candidate, strict=False):
                    return True
            elif address == ipaddress.ip_address(candidate):
                return True
        except ValueError:
            # A malformed allow-list entry must not turn into an open door.
            continue
    return False


def verify_callback(
    *,
    expected_token: str = "",
    query: Optional[Mapping[str, str]] = None,
    form: Optional[Mapping[str, str]] = None,
    headers: Optional[Mapping[str, str]] = None,
    client_ip: Optional[str] = None,
    allowed_ips: Iterable[str] = (),
) -> VerificationDecision:
    """Decide whether to accept a callback.

    A configured token must match. An unconfigured token is reported as
    ``token_checked=False`` so the caller can count how many callbacks relied on
    the weaker checks, which is exactly what a security review needs to see.
    """
    allowed = tuple(allowed_ips)
    ip_ok = ip_is_allowed(client_ip, allowed)
    if not ip_ok:
        return VerificationDecision(
            accepted=False,
            reason="the callback came from an address outside the allow-list",
            ip_checked=True,
        )

    if not expected_token:
        return VerificationDecision(
            accepted=True,
            reason="no callback token is configured; authenticity relies on matching the call id",
            token_checked=False,
            ip_checked=bool(allowed),
        )

    provided = find_token(query=query, form=form, headers=headers)
    if constant_time_equals(provided, expected_token):
        return VerificationDecision(accepted=True, token_checked=True, ip_checked=bool(allowed))

    return VerificationDecision(
        accepted=False,
        reason="the callback token is missing or wrong",
        token_checked=True,
        ip_checked=bool(allowed),
    )
