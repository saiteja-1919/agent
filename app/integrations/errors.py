"""One vocabulary for provider failures, shared by telephony and speech.

Section 19 asks for explicit handling of connection failures, rate limits,
timeouts, ambiguous results, malformed responses and unsupported operations.
Section 15 forbids inventing metrics, and section 17.8 forbids logging personal
data - so every failure is reduced to a *category* plus a short, sanitised
message, and the categories live in one place.

The single most important distinction in this file is the one the whole project
turns on: a failure that means "the request definitely never created a call"
(:attr:`ErrorCategory.NOT_SENT`) versus one that means "we do not know"
(:attr:`ErrorCategory.TIMEOUT` after the bytes went out, :attr:`ErrorCategory.PROVIDER_UNAVAILABLE`).
Only the first may ever be retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class ErrorCategory(str, Enum):
    """Sanitised failure categories. Safe to log; carry no payload."""

    NONE = "none"
    AUTH = "auth"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    NOT_SENT = "not_sent"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"
    CONFIGURATION = "configuration"
    OTHER = "other"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


@dataclass(frozen=True)
class ProviderError(Exception):
    """A provider failure with a category, and whether it may be retried.

    ``retryable`` is only ever true for an operation that is safe to repeat. For
    dialling it stays false even after a timeout, because a repeat could ring a
    parent twice - the caller must resolve the ambiguity by asking the provider
    instead (section 19.1).
    """

    category: ErrorCategory = ErrorCategory.OTHER
    message: str = ""
    provider: str = ""
    http_status: Optional[int] = None
    retryable: bool = False

    def __str__(self) -> str:
        parts = [self.category.value]
        if self.provider:
            parts.append(self.provider)
        if self.http_status is not None:
            parts.append(f"HTTP {self.http_status}")
        if self.message:
            parts.append(self.message)
        return ": ".join(parts)

    def sanitized(self) -> Dict[str, Any]:
        """A log-safe view. Never contains request or response bodies."""
        return {
            "category": self.category.value,
            "provider": self.provider,
            "http_status": self.http_status,
            "retryable": self.retryable,
        }

    @property
    def dial_result_is_certain(self) -> bool:
        """True only when we know for sure that no call was created."""
        return self.category in {
            ErrorCategory.NOT_SENT,
            ErrorCategory.AUTH,
            ErrorCategory.RATE_LIMITED,
            ErrorCategory.PROVIDER_REJECTED,
            ErrorCategory.CONFIGURATION,
            ErrorCategory.UNSUPPORTED,
        }


class UnsupportedProviderOperation(ProviderError):
    """The provider does not support the operation, and we will not pretend it does."""

    def __init__(self, operation: str, provider: str = "", detail: str = "") -> None:
        message = f"{provider or 'the provider'} does not support {operation}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(
            category=ErrorCategory.UNSUPPORTED,
            message=message,
            provider=provider,
            retryable=False,
        )


#: HTTP statuses that mean the request was received and refused, so no call exists.
_REJECTED_STATUSES = {400, 401, 402, 403, 404, 405, 409, 412, 422}
#: Statuses that mean the provider accepted the request but could not complete it.
_UNAVAILABLE_STATUSES = {500, 502, 503, 504, 507, 508}


def classify_status(status: Optional[int], *, body_hint: str = "") -> ErrorCategory:
    """Classify an HTTP status. Never inspects the body, to avoid leaking data."""
    if status is None:
        return ErrorCategory.OTHER
    if status == 401 or status == 403:
        return ErrorCategory.AUTH
    if status == 429:
        return ErrorCategory.RATE_LIMITED
    if status in _REJECTED_STATUSES:
        return ErrorCategory.PROVIDER_REJECTED
    if status in _UNAVAILABLE_STATUSES:
        return ErrorCategory.PROVIDER_UNAVAILABLE
    if status >= 500:
        return ErrorCategory.PROVIDER_UNAVAILABLE
    if status >= 400:
        return ErrorCategory.PROVIDER_REJECTED
    return ErrorCategory.OTHER


def classify_exception(exc: BaseException, *, request_sent: bool) -> tuple[ErrorCategory, bool]:
    """Classify a transport exception.

    ``request_sent`` says whether the request had reached the point of being sent.
    It is what separates "we never asked" from "we may have asked and lost the
    answer", and it is the caller's job to know the difference.
    """
    name = type(exc).__name__
    if name in {"ConnectTimeout", "ConnectError", "ConnectionRefusedError", "socket.gaierror"}:
        # The connection itself never completed, so the request was not sent.
        return ErrorCategory.NOT_SENT, True
    if name in {"ReadTimeout", "WriteTimeout", "PoolTimeout", "RemoteProtocolError", "ReadError", "WriteError"}:
        return (
            (ErrorCategory.TIMEOUT if "Timeout" in name else ErrorCategory.CONNECTION),
            not request_sent,
        )
    if name in {"TimeoutException"}:
        return ErrorCategory.TIMEOUT, not request_sent
    if name in {"ConnectError"}:
        return ErrorCategory.NOT_SENT, True
    if isinstance(exc, TimeoutError):
        return ErrorCategory.TIMEOUT, not request_sent
    if isinstance(exc, (ConnectionError, OSError)):
        return ErrorCategory.CONNECTION, not request_sent
    return ErrorCategory.OTHER, False
