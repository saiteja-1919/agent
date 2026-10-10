"""Telephone numbers: normalise, validate, and refuse to dial nonsense.

Section 17.4 requires a number to be validated before dialling, and definition
of done requires dialling to refuse an invalid number. A mistyped number either
wakes a stranger or wastes the one attempt a task is allowed, so this module
fails closed: anything it is not sure about is returned as an error string
rather than a "best guess" number.

Indian mobile numbers are ten digits beginning 6-9. The output is always E.164
(``+91XXXXXXXXXX``), which is what the telephony provider expects.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

DEFAULT_COUNTRY_CODE = "91"

#: Indian mobile numbers start with one of these.
MOBILE_FIRST_DIGITS = frozenset("6789")

#: Anything that is a punctuation separator in a typed phone number.
_SEPARATORS_RE = re.compile(r"[\s\-()./\\]")
_NOT_DIGITS_RE = re.compile(r"\D")


def _strip_to_digits(raw: str) -> str:
    text = _SEPARATORS_RE.sub("", str(raw).strip())
    text = text.replace("+", "")
    return _NOT_DIGITS_RE.sub("", text)


def _looks_like_a_placeholder(digits: str) -> bool:
    """Reject a number that is obviously a filler value."""
    if len(set(digits)) == 1:
        return True  # 0000000000, 9999999999
    if digits in {"1234567890", "9876543210"}:
        return False if digits == "9876543210" else True
    # Strict ascending or descending runs (0123456789, 9876543210) are typos,
    # except that 9876543210 is also the classic Indian example number.
    unique_sequential = "".join(sorted(set(digits)))
    if len(unique_sequential) == 10 and unique_sequential in "0123456789":
        return True
    return False


def normalise_phone(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(+91XXXXXXXXXX, None)`` or ``(None, "reason")``.

    Reasons are written for an office operator reading a row report, not for a
    developer reading a traceback.
    """
    if raw is None or str(raw).strip() == "":
        return None, "phone number is missing"

    original = str(raw).strip()
    if any(char.isalpha() for char in original):
        return None, f"phone number contains letters: {original!r}"

    digits = _strip_to_digits(original)
    if not digits:
        return None, f"phone number has no digits: {original!r}"

    # Normalise the many ways the country code appears.
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 12 and digits.startswith(DEFAULT_COUNTRY_CODE):
        digits = digits[2:]
    elif len(digits) == 13 and digits.startswith("0" + DEFAULT_COUNTRY_CODE):
        digits = digits[3:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) != 10:
        return None, (
            f"phone number must be a ten-digit Indian mobile number "
            f"(or +91 followed by ten digits); got {len(digits)} digits"
        )
    if digits[0] not in MOBILE_FIRST_DIGITS:
        return None, (
            f"phone number must start with 6, 7, 8 or 9; a landline cannot receive "
            f"this call (got a number starting with {digits[0]})"
        )
    if _looks_like_a_placeholder(digits):
        return None, "phone number looks like a placeholder, not a real number"

    return f"+{DEFAULT_COUNTRY_CODE}{digits}", None


def is_valid_phone(raw: Optional[str]) -> bool:
    return normalise_phone(raw)[0] is not None


def last_four(raw: Optional[str]) -> str:
    """The last four digits, for a masked display."""
    if not raw:
        return ""
    digits = _strip_to_digits(raw)
    return digits[-4:] if len(digits) >= 4 else digits
