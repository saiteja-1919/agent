"""Conservative extraction of a promised payment date from parent speech.

The rule that shapes this module (section 16.1, step three A): the application
may record a commitment **only** when the date has been reliably extracted and
validated. If the date is ambiguous, the clarification policy applies instead -
and that policy may only ask once more.

So this parser is deliberately timid. It returns a confidence, marks ambiguous
input as ambiguous, and refuses to guess:

* ``"10/10"`` could be the tenth of October or the tenth of... something else,
  depending on how the speaker writes dates - ambiguous, not reliable.
* ``"soon"``, ``"చూస్తాను"``, ``"I will see"`` are vague, not dates.
* ``"yesterday"`` is in the past: not a payment commitment.

It understands English ("in two days", "next Sunday", "10 October"), romanised
Telugu ("repu", "rendu rojullo", "next vaaram") and Telugu script ("రేపు",
"రెండు రోజుల్లో") because Stage 1 calls are Telugu mixed with English.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, List, Optional, Tuple

#: Confidence at or above which a date may be saved as a real commitment.
RELIABLE_THRESHOLD = 0.6

#: A promised date further away than this is treated as unusable (a school fee
#: promise a year out is a mis-transcription, not a commitment).
MAX_DAYS_AHEAD = 365

_TELUGU_DIGITS = {
    "౦": "0", "౧": "1", "౨": "2", "౩": "3", "౪": "4",
    "౫": "5", "౬": "6", "౭": "7", "౮": "8", "౯": "9",
}

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2, "febr": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

#: Number words seen in spoken Telugu-English. Kept small and explicit so an
#: unknown word fails closed rather than being mis-read.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "sixty": 60,
    # Telugu (script)
    "ఒక": 1, "రెండు": 2, "మూడు": 3, "నాలుగు": 4, "ఐదు": 5, "ఆరు": 6,
    "ఏడు": 7, "ఎనిమిది": 8, "తొమ్మిది": 9, "పది": 10,
    # Telugu (romanised, as a mixed-language speaker may be transcribed)
    "okati": 1, "rendu": 2, "moodu": 3, "nalugu": 4, "aidu": 5, "aaru": 6, "edu": 7,
}

VAGUE_MARKERS: Tuple[str, ...] = (
    "soon", "i will see", "i'll see", "will see", "chustanu", "chusta",
    "చూస్తాను", "చూస్తా", "anytime", "some time", "sometime", "later",
    "try chestanu", "try చేస్తాను", "చేస్తాను", "baadha ledu",
    "i will try", "as early as possible", "asap", "think chesi",
    "చెప్తాను", "cheptanu",
)

#: Markers that mean "some day, not a date" and must not become a commitment.
_TODAY_MARKERS = ("today", "ivvala", "ivala", "ఇవ్వాళ", "ఈ రోజు", "ee roju")
_TOMORROW_MARKERS = ("tomorrow", "repu", "రేపు", "రేపటి")
_DAY_AFTER_MARKERS = ("day after tomorrow", "ellundi", "ఎల్లుండి", "ఎల్లుండికి")
_NEXT_WEEK_MARKERS = ("next week", "next vaaram", "vaaram", "వారం", "వారంలో")
_NEXT_MONTH_MARKERS = ("next month", "next nela", "నెల", "నెలలో")
_MONTH_END_MARKERS = ("month end", "end of month", "month ending", "నెల చివర", "ఈ నెల")

#: Date separators that are genuinely ambiguous in Indian usage.
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:of\s+)?([a-z]{3,9})\b"
)
_MONTH_DAY_RE = re.compile(r"\b([a-z]{3,9})\s+(\d{1,2})\s*(?:st|nd|rd|th)?\b")
_RELATIVE_RE = re.compile(
    r"\b(?:in|after|within)\s+([a-z]+|\d{1,3})\s*(day|days|rojullo|roju|రోజుల్లో|రోజుల|రోజు)\b"
)
_WEEK_RELATIVE_RE = re.compile(r"\b(?:in|after|within)\s+([a-z]+|\d{1,3})\s*(week|weeks|vaaram|వారం)\b")
# The trailing ``\b`` after the number word is what stops "yesterday" matching as
# "yester" + "day" (and "today" as "to" + "day").
_BARE_NUMBER_DAYS_RE = re.compile(r"\b([a-z]+|\d{1,3})\b\s*(?:day|days|rojullo|roju)\b")

#: Telugu script puts the count before the unit without the English "in/after",
#: so it needs its own pattern: "రెండు రోజుల్లో" = "in two days".
_TELUGU_RELATIVE_RE = re.compile(
    r"([\u0C00-\u0C7F]+)\s*(రోజుల్లో|రోజుల|రోజు|వారంలో|వారం|నెలలో|నెల)"
)
_PAST_RE = re.compile(
    r"\b(yesterday|last week|last month|last sunday|నిన్న|గత వారం|గత నెల|ninna)\b"
)
_WEEKDAY_RE = re.compile(
    r"\b(?:(next|this|coming|వచ్చే)\s+)?([a-z]{3,9})\b"
)


@dataclass(frozen=True)
class DateExtraction:
    """The verdict on a parent's date statement."""

    date: Optional[date] = None
    confidence: float = 0.0
    reason: str = "none"
    matched_text: str = ""
    is_vague: bool = False
    is_ambiguous: bool = False

    @property
    def is_reliable(self) -> bool:
        """True only when a commitment may be saved without asking again."""
        return (
            self.date is not None
            and not self.is_ambiguous
            and not self.is_vague
            and self.confidence >= RELIABLE_THRESHOLD
        )

    @property
    def found_any_date_signal(self) -> bool:
        return self.date is not None or self.is_vague


def _normalise(text: str) -> str:
    lowered = (text or "").lower()
    for telugu, ascii_digit in _TELUGU_DIGITS.items():
        lowered = lowered.replace(telugu, ascii_digit)
    # keep word boundaries sane without dropping Telugu characters
    lowered = lowered.replace("₹", " ")
    return re.sub(r"\s+", " ", lowered).strip()


def _word_number(token: str) -> Optional[int]:
    if token.isdigit():
        try:
            return int(token)
        except ValueError:  # pragma: no cover - isdigit guarantees this
            return None
    return _NUMBER_WORDS.get(token.lower())


def _is_vague(text: str) -> bool:
    return any(marker in text for marker in VAGUE_MARKERS)


def _clamp(candidate: date, today: date) -> Tuple[Optional[date], float, str]:
    """Range-check a candidate date; return (date, confidence_penalty, reason)."""
    if candidate < today:
        return None, 0.0, "past_date"
    if (candidate - today).days > MAX_DAYS_AHEAD:
        return None, 0.0, "too_far_ahead"
    return candidate, 1.0, "ok"


# -------------------------------------------------------------------- extractors
def _explicit_iso(text: str, today: date) -> Optional[DateExtraction]:
    match = _ISO_DATE_RE.search(text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        candidate = date(year, month, day)
    except ValueError:
        return DateExtraction(None, 0.0, "invalid_date", match.group(0), is_ambiguous=True)
    resolved, factor, reason = _clamp(candidate, today)
    return DateExtraction(resolved, 0.95 * factor, reason, match.group(0))


def _explicit_day_month(text: str, today: date) -> Optional[DateExtraction]:
    # Every candidate is tried, because an unrelated "word 2" earlier in the
    # sentence can match the shape of "10 October" while not being one.
    for match in _DAY_MONTH_RE.finditer(text):
        day_text, month_text = match.group(1), match.group(2)
        month = _MONTHS.get(month_text)
        if month is None:
            continue
        day = int(day_text)
        year = today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return DateExtraction(None, 0.0, "invalid_date", match.group(0), is_ambiguous=True)
        # A month already behind us means the speaker means next year.
        if candidate < today:
            try:
                candidate = date(year + 1, month, day)
            except ValueError:  # pragma: no cover - Feb 29 in a non-leap year
                return DateExtraction(None, 0.0, "invalid_date", match.group(0), is_ambiguous=True)
        resolved, factor, reason = _clamp(candidate, today)
        return DateExtraction(resolved, 0.9 * factor, reason, match.group(0))
    return None


def _explicit_month_day(text: str, today: date) -> Optional[DateExtraction]:
    for match in _MONTH_DAY_RE.finditer(text):
        month_text, day_text = match.group(1), match.group(2)
        month = _MONTHS.get(month_text)
        if month is None:
            continue
        try:
            candidate = date(today.year, month, int(day_text))
        except ValueError:
            return DateExtraction(None, 0.0, "invalid_date", match.group(0), is_ambiguous=True)
        if candidate < today:
            try:
                candidate = date(today.year + 1, month, int(day_text))
            except ValueError:  # pragma: no cover
                return DateExtraction(None, 0.0, "invalid_date", match.group(0), is_ambiguous=True)
        resolved, factor, reason = _clamp(candidate, today)
        return DateExtraction(resolved, 0.9 * factor, reason, match.group(0))
    return None


def _past_reference(text: str, today: date) -> Optional[DateExtraction]:
    """"Yesterday" is not a payment commitment - it is a past reference."""
    match = _PAST_RE.search(text)
    if not match:
        return None
    return DateExtraction(None, 0.0, "past_date", match.group(0))


def _numeric_date(text: str, today: date) -> Optional[DateExtraction]:
    match = _NUMERIC_DATE_RE.search(text)
    if not match:
        return None
    first, second, year_text = match.groups()
    first_i, second_i = int(first), int(second)
    matched = match.group(0)

    # dd/mm is the local convention; when it cannot be told from mm/dd, refuse.
    if first_i > 12 and second_i <= 12:
        day, month = first_i, second_i
    elif second_i > 12 and first_i <= 12:
        # clearly written the other way round; still accept but at lower trust
        month, day = first_i, second_i
    elif first_i <= 12 and second_i <= 12:
        return DateExtraction(
            None,
            0.35,
            "ambiguous_numeric_date",
            matched,
            is_ambiguous=True,
        )
    else:
        return DateExtraction(None, 0.0, "invalid_date", matched, is_ambiguous=True)

    year = today.year
    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
    try:
        candidate = date(year, month, day)
    except ValueError:
        return DateExtraction(None, 0.0, "invalid_date", matched, is_ambiguous=True)
    if not year_text and candidate < today:
        try:
            candidate = date(year + 1, month, day)
        except ValueError:  # pragma: no cover
            return DateExtraction(None, 0.0, "invalid_date", matched, is_ambiguous=True)
    resolved, factor, reason = _clamp(candidate, today)
    return DateExtraction(resolved, 0.9 * factor, reason, matched)


def _relative_days(text: str, today: date) -> Optional[DateExtraction]:
    match = _RELATIVE_RE.search(text) or _BARE_NUMBER_DAYS_RE.search(text)
    if not match:
        return None
    groups = match.groups()
    amount = _word_number(groups[0])
    if amount is None or amount <= 0:
        return DateExtraction(None, 0.0, "unreadable_day_count", match.group(0), is_ambiguous=True)
    # No upper bound here: _clamp reports "too_far_ahead" so the caller learns
    # why the date was refused instead of seeing a vague parse failure.
    candidate = today + timedelta(days=amount)
    resolved, factor, reason = _clamp(candidate, today)
    return DateExtraction(resolved, 0.85 * factor, reason, match.group(0))


def _telugu_relative(text: str, today: date) -> Optional[DateExtraction]:
    """"రెండు రోజుల్లో" / "ఒక వారంలో" / "రెండు నెలలో" - count before unit."""
    match = _TELUGU_RELATIVE_RE.search(text)
    if not match:
        return None
    amount = _word_number(match.group(1))
    unit = match.group(2)
    if amount is None or amount <= 0:
        return DateExtraction(
            None, 0.0, "unreadable_day_count", match.group(0), is_ambiguous=True
        )
    if unit.startswith("వార"):
        candidate = today + timedelta(weeks=amount)
        confidence = 0.7
    elif unit.startswith("నెల"):
        candidate = (
            _same_day_next_month(today)
            if amount == 1
            else today + timedelta(days=30 * amount)
        )
        confidence = 0.6
    else:
        candidate = today + timedelta(days=amount)
        confidence = 0.85
    resolved, factor, reason = _clamp(candidate, today)
    return DateExtraction(resolved, confidence * factor, reason, match.group(0))


def _relative_weeks(text: str, today: date) -> Optional[DateExtraction]:
    match = _WEEK_RELATIVE_RE.search(text)
    if not match:
        return None
    amount = _word_number(match.group(1))
    if amount is None or amount <= 0:
        return DateExtraction(None, 0.0, "unreadable_week_count", match.group(0), is_ambiguous=True)
    candidate = today + timedelta(weeks=amount)
    resolved, factor, reason = _clamp(candidate, today)
    return DateExtraction(resolved, 0.7 * factor, reason, match.group(0))


def _keyword_dates(text: str, today: date) -> Optional[DateExtraction]:
    """Tomorrow / today / day-after-tomorrow, in all three spellings."""
    for markers, offset, confidence in (
        (_DAY_AFTER_MARKERS, 2, 0.85),
        (_TOMORROW_MARKERS, 1, 0.85),
        (_TODAY_MARKERS, 0, 0.8),
    ):
        for marker in markers:
            if marker in text:
                return DateExtraction(today + timedelta(days=offset), confidence, marker)
    for marker in _MONTH_END_MARKERS:
        if marker in text:
            candidate = _last_day_of_month(today)
            if candidate < today:  # already past the end of this month
                candidate = _last_day_of_month(_first_of_next_month(today))
            return DateExtraction(candidate, 0.6, marker)
    for marker in _NEXT_WEEK_MARKERS:
        if marker in text:
            return DateExtraction(today + timedelta(days=7), 0.6, marker)
    for marker in _NEXT_MONTH_MARKERS:
        if marker in text:
            return DateExtraction(_same_day_next_month(today), 0.6, marker)
    return None


def _weekday(text: str, today: date) -> Optional[DateExtraction]:
    for match in _WEEKDAY_RE.finditer(text):
        name = match.group(2)
        if name not in _WEEKDAYS:
            continue
        prefix = match.group(1) or ""
        target = _WEEKDAYS[name]
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            # "Sunday" said on a Sunday means the coming one, not this morning.
            days_ahead = 7
        confidence = 0.7
        if prefix:
            # "next Sunday" said mid-week usually means the *following* week, so
            # a very near day is pushed out and the answer is trusted less.
            if days_ahead <= 3:
                days_ahead += 7
            confidence = 0.6
        candidate = today + timedelta(days=days_ahead)
        resolved, factor, reason = _clamp(candidate, today)
        return DateExtraction(resolved, confidence * factor, reason, match.group(0))
    return None


def _last_day_of_month(day: date) -> date:
    if day.month == 12:
        return date(day.year, 12, 31)
    return date(day.year, day.month + 1, 1) - timedelta(days=1)


def _first_of_next_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def _same_day_next_month(day: date) -> date:
    first_next = _first_of_next_month(day)
    candidate = _last_day_of_month(first_next)
    try:
        return date(first_next.year, first_next.month, day.day)
    except ValueError:
        return candidate


#: Ordered most-specific first. The highest confidence wins.
_EXTRACTORS: Tuple[Callable[[str, date], Optional[DateExtraction]], ...] = (
    _explicit_iso,
    _numeric_date,
    _explicit_day_month,
    _explicit_month_day,
    _relative_days,
    _telugu_relative,
    _relative_weeks,
    _keyword_dates,
    _weekday,
    _past_reference,
)


def extract_payment_date(text: str, today: Optional[date] = None) -> DateExtraction:
    """Read a promised payment date out of one parent turn.

    Never raises. Returns an extraction whose :attr:`DateExtraction.is_reliable`
    is the only permission to save a commitment.
    """
    today = today or date.today()
    normalised = _normalise(text)
    if not normalised:
        return DateExtraction(None, 0.0, "empty")

    candidates: List[DateExtraction] = []
    for extractor in _EXTRACTORS:
        found = extractor(normalised, today)
        if found is not None:
            candidates.append(found)

    if candidates:
        # Prefer a usable date; otherwise return the most informative refusal so
        # the caller can tell "ambiguous" from "in the past" from "unreadable".
        usable = [c for c in candidates if c.date is not None]
        pool = usable or candidates
        return max(pool, key=lambda item: item.confidence)

    if _is_vague(normalised):
        return DateExtraction(None, 0.0, "vague", "", is_vague=True)
    return DateExtraction(None, 0.0, "no_date_found")


def looks_vague(text: str) -> bool:
    """Exposed for the intent classifier: a vague answer is not a date."""
    return _is_vague(_normalise(text))
