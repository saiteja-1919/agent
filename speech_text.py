"""Numbers, amounts and text hygiene for Veda.

Two jobs:

* the model must never *see* or *say* digits, because the Telugu voice reads
  "5000" as Telugu words and sounds robotic (P25), so amounts and dates are sent
  to the model as English words;
* names, notes and any other text typed by office staff are untrusted input
  (P14), so they are cleaned and capped before they reach a prompt.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import List, Optional

TELUGU_DIGITS = {
    "౦": "0", "౧": "1", "౨": "2", "౩": "3", "౪": "4",
    "౫": "5", "౬": "6", "౭": "7", "౮": "8", "౯": "9",
}

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = [
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
]

#: currency words and symbols staff may type in front of an amount
_CURRENCY_PREFIXES = ("₹", "rs.", "rs", "inr", "rupees", "rupee", "amount", ":")
_AMOUNT_NOISE = re.compile(r"[,\s_]|/-$|/-")

#: a number as a human writes it: 5000, 5,000, 5,000.50
_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_DIGIT_TAIL_RE = re.compile(r"[0-9][0-9,\.]*$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TELUGU_RE = re.compile(r"[\u0C00-\u0C7F]")
_LATIN_RE = re.compile(r"[A-Za-z]")

MAX_NAME_LEN = 80
MAX_SCHOOL_LEN = 100
MAX_NOTES_LEN = 400


# ---------------------------------------------------------------------- numbers
def _below_thousand(value: int) -> str:
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        return _TENS[tens] if ones == 0 else f"{_TENS[tens]}-{_ONES[ones]}"
    hundreds, rest = divmod(value, 100)
    if rest == 0:
        return f"{_ONES[hundreds]} hundred"
    return f"{_ONES[hundreds]} hundred and {_below_thousand(rest)}"


def number_to_words(value: int) -> str:
    """Indian-English words for a non-negative whole number.

    ``5000`` -> ``five thousand``, ``500000`` -> ``five lakh``,
    ``10000000`` -> ``one crore``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("number_to_words needs a whole number")
    if value < 0:
        raise ValueError("number_to_words needs a number of zero or more")
    if value == 0:
        return "zero"
    if value >= 10 ** 12:
        raise ValueError("number_to_words supports numbers below one thousand crore")

    parts: List[str] = []
    crore, rest = divmod(value, 10_000_000)
    lakh, rest = divmod(rest, 100_000)
    thousand, rest = divmod(rest, 1_000)
    if crore:
        parts.append(f"{_below_thousand(crore)} crore" if crore < 1000 else f"{number_to_words(crore)} crore")
    if lakh:
        parts.append(f"{_below_thousand(lakh)} lakh")
    if thousand:
        parts.append(f"{_below_thousand(thousand)} thousand")
    if rest:
        parts.append(_below_thousand(rest))
    return " ".join(parts)


def parse_amount(raw: str) -> Decimal:
    """Read an amount the way an office member might type it.

    Accepts ``5000``, ``5,000``, ``Rs. 5000``, ``₹5,000/-``, ``5000 rupees`` and
    Telugu digits. Raises ``ValueError`` with a plain message otherwise.
    """
    if raw is None:
        raise ValueError("amount is missing")
    text = str(raw).strip()
    for telugu, ascii_digit in TELUGU_DIGITS.items():
        text = text.replace(telugu, ascii_digit)
    lowered = text.lower()
    changed = True
    while changed:
        changed = False
        for prefix in _CURRENCY_PREFIXES:
            if lowered.startswith(prefix):
                lowered = lowered[len(prefix):].strip()
                changed = True
    lowered = lowered.replace("rupees", "").replace("rupee", "").replace("only", "")
    cleaned = _AMOUNT_NOISE.sub("", lowered)
    if not cleaned:
        raise ValueError(f"amount {raw!r} is not a number")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"amount {raw!r} is not a number") from None
    if value <= 0:
        raise ValueError(f"amount {raw!r} must be more than zero")
    if value >= Decimal(10 ** 10):
        raise ValueError(f"amount {raw!r} is too large to be a school fee")
    return value


def _decimal_words(value: Decimal) -> str:
    whole = int(value)
    words = number_to_words(whole)
    fraction = value - whole
    if fraction == 0:
        return words
    paise = int((fraction * 100).to_integral_value())
    if paise == 0:
        return words
    return f"{words} rupees and {number_to_words(paise)} paise"


def amount_to_words(amount) -> str:
    """``"five thousand rupees"`` - what the model must hear and say."""
    if isinstance(amount, Decimal):
        value = amount
    else:
        value = parse_amount(str(amount)) if isinstance(amount, str) else Decimal(str(amount))
    if value <= 0:
        raise ValueError("amount must be more than zero")
    if value == value.to_integral_value():
        return f"{number_to_words(int(value))} rupees"
    return _decimal_words(value)


def _number_match_to_words(match: str) -> str:
    text = match.replace(",", "")
    if "." in text:
        whole, frac = text.split(".", 1)
        words = number_to_words(int(whole))
        digits = " ".join(_ONES[int(d)] for d in frac)
        return f"{words} point {digits}"
    return number_to_words(int(text))


def digits_to_words(text: str) -> str:
    """Replace every digit run in a sentence with English words."""
    if not text:
        return text
    return _NUMBER_RE.sub(lambda m: _number_match_to_words(m.group(0)), text)


class DigitWordBuffer:
    """Turns digits into words even when a number is split across chunks.

    A streaming model can send ``"the fee is 5"`` and then ``"000 rupees"``.
    Feeding both pieces here produces ``"the fee is five thousand rupees"`` and
    never speaks a half number (open issue 4).
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._pending = ""

    def feed(self, text: str) -> str:
        """Return the part that is safe to speak, holding back a trailing number.

        A chunk that ends in digits may be the first half of a longer number
        ("5" then "000"), so the whole trailing number is held until the next
        chunk arrives or the reply ends.
        """
        if not self.enabled:
            return text
        if self._pending:
            text = self._pending + text
            self._pending = ""
        tail = _DIGIT_TAIL_RE.search(text)
        if tail:
            self._pending = tail.group(0)
            text = text[: tail.start()]
        return digits_to_words(text)

    def flush(self) -> str:
        if not self.enabled or not self._pending:
            return ""
        held, self._pending = self._pending, ""
        return digits_to_words(held)


# ------------------------------------------------------------------ text hygiene
def clean_text(value: Optional[str], max_len: int, *, allow_newlines: bool = False) -> str:
    """Make staff-typed text safe for a prompt (P14).

    Removes control characters, flattens newlines, drops ``{{ }}`` markers so a
    spreadsheet cell cannot inject or re-substitute a placeholder, collapses
    spaces and caps the length.
    """
    if value is None:
        return ""
    text = str(value)
    text = text.replace("{{", "").replace("}}", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if allow_newlines:
        text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
    else:
        text = text.replace("\n", " ")
    text = _CONTROL_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text


def clean_name(value: Optional[str], max_len: int = MAX_NAME_LEN) -> str:
    return clean_text(value, max_len)


def clean_school_name(value: Optional[str]) -> str:
    return clean_text(value, MAX_SCHOOL_LEN)


def clean_notes(value: Optional[str]) -> str:
    return clean_text(value, MAX_NOTES_LEN)


def has_telugu(text: str) -> bool:
    return bool(_TELUGU_RE.search(text or ""))


def latin_letters(text: str) -> str:
    return "".join(_LATIN_RE.findall(text or ""))


def spoken_date(value: Optional[str]) -> str:
    """A due date as the model should read it: digits become English words."""
    text = clean_text(value, 60)
    return digits_to_words(text)


_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
    7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth",
    13: "thirteenth", 14: "fourteenth", 15: "fifteenth", 16: "sixteenth",
    17: "seventeenth", 18: "eighteenth", 19: "nineteenth", 20: "twentieth",
    21: "twenty-first", 22: "twenty-second", 23: "twenty-third", 24: "twenty-fourth",
    25: "twenty-fifth", 26: "twenty-sixth", 27: "twenty-seventh", 28: "twenty-eighth",
    29: "twenty-ninth", 30: "thirtieth", 31: "thirty-first",
}


def ordinal_words(value: int) -> str:
    """``6`` -> ``sixth``. Used for the day of the month in the prompt."""
    if value in _ORDINALS:
        return _ORDINALS[value]
    return f"number {number_to_words(value)}"


def year_words(value: int) -> str:
    """``2026`` -> ``two thousand and twenty six`` (never digits)."""
    if 2000 <= value < 2100:
        rest = value - 2000
        if rest == 0:
            return "two thousand"
        return f"two thousand and {number_to_words(rest)}"
    return number_to_words(value)
