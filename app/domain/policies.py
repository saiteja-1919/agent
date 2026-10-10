"""Deterministic conversation policy: the rules Python owns, not the model.

Section 12.4 asks for exactly this split. A mistake in any of these areas is
unsafe, so none of them may depend on a language model behaving:

* the identity gate (no fee before the parent confirms),
* the payment-date question counter,
* do-not-call and stop requests,
* wrong number,
* silence handling,
* forbidden-content checks on every reply (no digits, no unverified amount or
  date, no OTP/card/UPI requests, no promises of waivers, no unsupported claims).

Everything else - understanding and wording - is left to the model inside the
section 16 prompt. A rule engine that tried to cover every intent in code-switched
Telugu would be brittle; this one covers only what must not be guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .dateparse import DateExtraction, extract_payment_date, looks_vague
from .enums import Intent
from .models import VerifiedFacts, first_word

#: Section 16.1: ask for the expected payment date no more than twice.
MAX_PAYMENT_DATE_QUESTIONS = 2

#: Section 16.1: one or two complete sentences, about fifteen to twenty-five words.
#: The hard ceiling is a little looser than the target on purpose: the binding
#: approved wordings in section 16.1 (for example the "already informed the
#: school" line) are three short sentences, and an approved line must never be
#: blocked by the length guard.
MAX_REPLY_WORDS = 40
MAX_REPLY_SENTENCES = 3

#: How many times Veda may prompt into silence before recording no response.
MAX_SILENCE_PROMPTS = 2

#: How many off-topic turns before the call closes politely (section 18).
MAX_OFF_TOPIC_TURNS = 3


class Violation(str):
    """Marker type for readability; violations are plain strings."""


#: Violation codes. Plain strings so they can be logged and asserted directly.
DIGITS_IN_SPEECH = "digits_in_speech"
FEE_DISCLOSED_BEFORE_IDENTITY = "fee_disclosed_before_identity"
FORBIDDEN_FINANCIAL_REQUEST = "forbidden_financial_request"
UNSUPPORTED_ACTION_CLAIM = "unsupported_action_claim"
PRINCIPAL_CLAIM_WITHOUT_RECORD = "principal_claim_without_record"
PROMISED_DISCOUNT = "promised_discount"
THREATENING_LANGUAGE = "threatening_language"
INVENTED_AMOUNT = "invented_amount"
TOO_LONG = "too_long"
TOO_MANY_QUESTIONS = "too_many_questions"
PAYMENT_QUESTION_LIMIT = "payment_question_limit"
REPEATED_OPENING = "repeated_opening"
REPEATED_ACKNOWLEDGEMENT = "repeated_acknowledgement"
EMPTY_REPLY = "empty_reply"
PROMPT_LEAKAGE = "prompt_leakage"
DEAD_END_CLOSING = "dead_end_closing"
UNSAFE_FOR_UNVERIFIED_LISTENER = "unsafe_for_unverified_listener"


# ------------------------------------------------------------------------ patterns
_FINANCIAL_REQUEST_PATTERNS: Tuple[str, ...] = (
    r"\botp\b", r"\bo\.t\.p\b", r"\bone[- ]time password\b", r"ఓటీపీ", r"ఓ టీ పీ",
    r"\bcvv\b", r"\bcvc\b", r"\bcard number\b", r"\bdebit card\b", r"\bcredit card\b",
    r"\bexpiry date\b", r"\bupi pin\b", r"\bupi\b", r"\bpin\b", r"పిన్", r"యూపీఐ",
    r"\bnet ?banking\b", r"\bbank account\b", r"\baccount number\b", r"\bifsc\b",
    r"\bpassword\b", r"\bpasscode\b", r"పాస్‌వర్డ్", r"\batm\b",
)

_PROMISE_PATTERNS: Tuple[str, ...] = (
    r"\bwaive\b", r"\bwaiver\b", r"\bwave off\b", r"\bdiscount\b", r"\bconcession\b",
    r"\bextension\b", r"\bextend\b", r"\binstallment plan\b", r"\breduce the fee\b",
    r"రాయితీ", r"తగ్గింపు", r"మాఫీ", r"వాయిదా", r"సడలింపు",
)

_THREAT_PATTERNS: Tuple[str, ...] = (
    r"\bcourt\b", r"\bpolice\b", r"\blegal action\b", r"\blegal proceedings\b",
    r"\bsue\b", r"కోర్టు", r"పోలీస", r"చట్టపరమైన",
)

#: Claims that the office record has already been corrected. Only allowed when
#: the application really saved something (the [ADDED] section 16.1 wording).
_UNSUPPORTED_UPDATE_PATTERNS: Tuple[str, ...] = (
    r"\bupdated\b", r"\bhas been updated\b", r"\bis updated\b", r"\bupdate (?:completed|done)\b",
    r"update\s*(?:అయింది|అయ్యింది|ఐపోయింది|చేశాము|చేశాం|చేసి పెట్టాను)",
    r"record\s*update", r"system\s*లో\s*update",
)

_PRINCIPAL_INFORMED_PATTERNS: Tuple[str, ...] = (
    r"principal\s*(?:గారికి|కి)\s*(?:చెప్పాను|చెప్పేశాను|inform\s*చేశాను|తెలియజేశాను)",
    r"informed the principal", r"principal has been informed", r"principal ki cheppanu",
    r"ప్రిన్సిపాల్‌కి చెప్పాను", r"meeting (?:has been )?arranged", r"మీటింగ్ పెట్టాను",
)

_PROMPT_LEAK_PATTERNS: Tuple[str, ...] = (
    r"system prompt", r"{{", r"}}", r"\bas an ai language model\b",
    r"\bmy instructions\b", r"నా instructions", r"\bprompt\b",
)

_DIGIT_RE = re.compile(r"[0-9]")
_TELUGU_DIGIT_RE = re.compile(r"[\u0C66-\u0C6F]")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\u0964\u3002]+")

#: Markers that mean "an amount is being spoken". Matched with word boundaries,
#: because a substring test would fire on "hours" (contains "rs") and block a
#: perfectly safe reply.
_AMOUNT_MARKER_RE = re.compile(
    r"\brupees?\b|\brs\.?\b|₹|రూపాయ|\bthousand\b|\blakhs?\b|\bcrore\b|వేల|లక్ష|కోటి"
)


#: Kept for callers that want the raw list (documentation and tests).
_AMOUNT_MARKERS: Tuple[str, ...] = (
    "rupees", "rupee", "rs", "₹", "రూపాయ", "రూపాయల",
    "thousand", "lakh", "lakhs", "crore", "వేల", "లక్ష", "కోటి",
)


def _has_amount_marker(text: str) -> bool:
    return bool(_AMOUNT_MARKER_RE.search(text))


_NUMBER_WORD_TOKENS: Tuple[str, ...] = (
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty",
    "seventy", "eighty", "ninety", "hundred", "thousand", "lakh", "lakhs", "crore",
)

_ACKNOWLEDGEMENT_WORDS: Tuple[str, ...] = ("సరే", "okay", "ok", "ఓకే", "sare")

_DEAD_END_CLOSINGS: FrozenSet[str] = frozenset(
    {"sorry", "సారీ", "sorry sir", "okay", "ok", "ok sir", "సరే", "hmm", "bye"}
)


# ------------------------------------------------------------------ identity gate
@dataclass(frozen=True)
class IdentityAnswer:
    """The deterministic reading of the parent's answer to the identity question."""

    confirmed: bool = False
    denied: bool = False
    third_party: bool = False
    wrong_number: bool = False
    unclear: bool = True
    matched: str = ""

    @property
    def needs_reask(self) -> bool:
        """Only an unclear answer justifies asking the identity question again."""
        return self.unclear


_YES_PATTERNS: Tuple[str, ...] = (
    r"\bavunu\b", r"\bavnu\b", r"\bavunandi\b", r"అవును", r"అవునండి", r"ఔను", r"అవు",
    r"\byes\b", r"\bya\b", r"\byeah\b", r"\byep\b", r"\bhaan\b", r"\bhan\b", r"\bha\b",
    r"\bhou\b", r"\bcorrect\b", r"\bcheppandi\b", r"చెప్పండి", r"\bcheppu\b", r"\btell me\b",
    r"\bspeaking\b", r"\bthis is\b", r"\bnenu\b", r"నేను",
)

_NO_PATTERNS: Tuple[str, ...] = (
    r"\bkaadu\b", r"\bkadu\b", r"కాదు", r"\bledu\b", r"లేదు", r"\bno\b", r"\bnot\b",
    r"\bwrong person\b", r"తప్పు వ్యక్తి",
)

_WRONG_NUMBER_PATTERNS: Tuple[str, ...] = (
    r"wrong number", r"తప్పు నంబర", r"తప్పు నెంబర", r"wrong no\b", r"tappu number",
    r"no such person", r"ఎవరూ లేరు", r"not my child", r"నా పిల్లాడు కాదు", r"not my daughter",
    r"ఇది నా", r"not my son",
)

_THIRD_PARTY_PATTERNS: Tuple[str, ...] = (
    r"\bhe is not available\b", r"\bshe is not available\b", r"\bnot available\b",
    r"\bi am (?:his|her) (?:son|daughter|brother|sister|father|mother)\b",
    r"\baayaniki\b", r"\bమా అబ్బాయి\b", r"\bమా నాన్న\b", r"\bమా అమ్మ\b",
    r"\bparent (?:is )?(?:not )?(?:here|available)\b", r"లేరు", r"\bbayata\b",
)


def classify_identity_answer(text: str) -> IdentityAnswer:
    """Read a yes/no/third-party/wrong-number answer from the parent's turn.

    Deliberately conservative: anything it is not sure about is ``unclear``,
    which permits one polite re-ask and forbids disclosure.
    """
    lowered = (text or "").lower().strip()
    if not lowered:
        return IdentityAnswer()

    if _matches(_WRONG_NUMBER_PATTERNS, lowered):
        return IdentityAnswer(denied=True, wrong_number=True, unclear=False, matched="wrong_number")
    if _matches(_THIRD_PARTY_PATTERNS, lowered):
        return IdentityAnswer(denied=True, third_party=True, unclear=False, matched="third_party")
    if _matches(_NO_PATTERNS, lowered):
        return IdentityAnswer(denied=True, unclear=False, matched="no")
    if _matches(_YES_PATTERNS, lowered):
        return IdentityAnswer(confirmed=True, unclear=False, matched="yes")
    return IdentityAnswer()


def is_pure_greeting(text: str) -> bool:
    """True for "hello", "హలో", "hmm" and similar - not an identity answer.

    Section 10.2: a greeting said during the pause or during the identity
    question must never be treated as the answer to it.
    """
    stripped = re.sub(r"[^\w\u0C00-\u0C7F]+", " ", (text or "").lower()).strip()
    if not stripped:
        return True
    words = [word for word in stripped.split() if word]
    greeting_words = {
        "hello", "hi", "hey", "hmm", "hm", "haan", "han", "ha", "sir", "madam",
        "హలో", "హాయ్", "హ్మ్", "అండి", "అమ్మా", "నమస్తే", "నమస్కారం", "చెప్పండి",
        # "సర్"/"సరే" are addresses and acknowledgements, never proof of identity:
        # "హలో సర్" said during the identity question must not confirm it.
        "సర్", "సరే", "అయ్యా", "మేడం", "గారు",
    }
    # "చెప్పండి" on its own means "go ahead" - a greeting, not identity proof.
    if words and all(word in greeting_words for word in words):
        return True
    return False


# --------------------------------------------------------------------- intent read
@dataclass(frozen=True)
class IntentClassification:
    """A conservative reading of the parent's turn, plus every signal seen."""

    intent: Intent = Intent.UNKNOWN
    confidence: float = 0.0
    matched: str = ""
    signals: FrozenSet[Intent] = frozenset()
    date: Optional[DateExtraction] = None


_PATTERN_INTENTS: Tuple[Tuple[Intent, float, Tuple[str, ...]], ...] = (
    (
        Intent.WRONG_NUMBER,
        0.95,
        _WRONG_NUMBER_PATTERNS,
    ),
    (
        Intent.DO_NOT_CALL,
        0.95,
        (
            r"do not call", r"don'?t call", r"stop calling", r"never call",
            r"call చేయకండి", r"కాల్ చేయకండి", r"మళ్ళీ కాల్", r"ఫోన్ చేయకండి",
            r"remove my number", r"block my number", r"don'?t disturb",
            r"డిస్టర్బ్ చేయకండి",
        ),
    ),
    (
        Intent.ASKED_TO_STOP,
        0.9,
        (
            r"\bbye\b", r"\bgood ?bye\b", r"end the call", r"hang up", r"cut the call",
            r"phone పెట్టండి", r"పెట్టేయండి", r"కట్ చేయండి", r"వెళ్తాను", r"\bthat'?s all\b",
        ),
    ),
    (
        Intent.ALREADY_PAID,
        0.85,
        (
            r"already paid", r"\bpaid\b", r"paid yesterday", r"i have paid",
            r"చెల్లించ", r"కట్టేశా", r"కట్టాను", r"kattesanu", r"kattanu", r"చెల్లించాను",
            r"payment చేశాను", r"fee కట్ట", r"జమ చేశాను",
        ),
    ),
    (
        Intent.PREVIOUS_REMINDER,
        0.8,
        (
            r"already (?:called|phoned|told)", r"you called (?:me )?yesterday",
            r"called me yesterday", r"i already told", r"నిన్న కాల్", r"ముందే చెప్ప",
            r"malli enduku", r"మళ్ళీ ఎందుకు", r"why do you call again",
        ),
    ),
    (
        Intent.ILLNESS_OR_EMERGENCY,
        0.85,
        (
            r"\bhospital\b", r"\bsick\b", r"\bill\b", r"not well", r"\baccident\b",
            r"emergency", r"\bsurgery\b", r"అనారోగ్య", r"ఆసుపత్రి", r"అత్యవసర",
            r"మరణ", r"అమ్మాయి అనారోగ్య", r"jaabita", r"\bdeath\b", r"funeral",
        ),
    ),
    (
        Intent.PRINCIPAL_REQUEST,
        0.9,
        (
            r"principal", r"ప్రిన్సిపాల్", r"head ?master", r"మేనేజర్", r"\bmanager\b",
            r"management", r"\bcorrespondent\b", r"ప్రధానోపాధ్యాయ",
        ),
    ),
    (
        Intent.AI_QUESTION,
        0.9,
        (
            r"are you (?:a )?(?:human|robot|bot|machine|ai)",
            r"\brobot\b", r"\bchatbot\b", r"\bautomated\b", r"recording",
            r"మీరు మనిషా", r"రోబో", r"మెషినా", r"మనిషివా", r"automated assistant",
        ),
    ),
    (
        Intent.PRINCIPAL_REQUEST,  # "i want to talk to the office" is the same path
        0.6,
        (r"\boffice\b.*(?:talk|మాట్లాడ)", r"ప్రధాన",),
    ),
    (
        Intent.FEE_QUESTION,
        0.8,
        (
            r"how much", r"what is the fee", r"fee (?:amount|details)", r"due date",
            r"how many rupees", r"ఎంత", r"ఫీజు", r"due ఎప్పుడు", r"receipt", r"రసీదు",
            r"late fee", r"fine", r"payment method", r"ఎలా కట్టాలి", r"online",
        ),
    ),
    (
        Intent.FINANCIAL_REASON,
        0.75,
        (
            r"salary", r"జీతం", r"money (?:is )?not", r"డబ్బు లేదు", r"\bloan\b",
            r"financial", r"no money", r"కష్టం", r"ఇబ్బంది", r"కష్టాల్లో", r"income",
            r"business loss", r"నష్టం",
        ),
    ),
    (
        Intent.BUSY_OR_CALLBACK,
        0.8,
        (
            r"\bbusy\b", r"in a meeting", r"driving", r"call me later", r"call back",
            r"later call", r"తర్వాత", r"తరువాత", r"బిజీ", r"call చేయండి", r"సమయం లేదు",
            r"not a good time",
        ),
    ),
    (
        Intent.IRRITATED,
        0.8,
        (
            r"why do you", r"stop disturbing", r"annoying", r"irritating", r"too many calls",
            r"ఎందుకు కాల్", r"డిస్టర్బ్", r"disturb", r"every day", r"రోజూ", r"తొందర",
        ),
    ),
    (
        Intent.WHO_IS_CALLING,
        0.85,
        (
            r"who is (?:this|calling|that)", r"who are you", r"may i know who",
            r"ఎవరు", r"ఎవరు మాట్లాడుతున్నారు", r"ఎవరండి", r"which school",
            r"ఏ స్కూల్", r"ఏ స్కూల్ నుండి",
        ),
    ),
    (
        Intent.OFF_TOPIC,
        0.5,
        (
            r"\bcricket\b", r"\bmovie\b", r"\bweather\b", r"\bpolitics\b", r"\bhello veda\b",
        ),
    ),
)


#: Words that show the turn is about paying, used to decide whether a generic
#: "today"/"tomorrow" really is a payment promise.
_PAYMENT_CUE_RE = re.compile(
    r"\bpay\b|\bpaid\b|\bpayment\b|\bpaying\b|\bfee\b|\bfees\b|\bamount\b|"
    r"\btransfer\b|\bdeposit\b|katta|kattu|kadutha|kadatha|chestanu|chesta\b|"
    r"కడ|కట్ట|చెల్లించ|ఫీజు|ఫీసు|జమా|వేస్తాను| చేస్తాను"
)


def classify_intent(
    text: str, *, today=None, date_context: bool = False
) -> IntentClassification:
    """Read the intent of one finalized parent turn.

    Only the highest-confidence match becomes the primary intent; every match is
    also reported in ``signals`` so the controller can record, for example, that
    the parent asked for the principal *and* gave a date.

    ``date_context`` says the controller has already asked when the parent will
    pay. That matters because a bare "tomorrow" is a real commitment when it
    answers the payment-date question, while an unrelated "the weather is nice
    today" must never be saved as one. Generic keyword dates (today, tomorrow,
    next week, month end) are therefore only accepted when the question was
    asked or the turn itself talks about paying; explicit dates such as
    "20 March" or "in two days" are trusted either way.
    """
    lowered = (text or "").lower().strip()
    if not lowered:
        return IntentClassification(Intent.SILENCE, 0.9, "empty")

    signals: set = set()
    matches: List[Tuple[Intent, float, str]] = []
    for intent, confidence, patterns in _PATTERN_INTENTS:
        matched = _first_match(patterns, lowered)
        if matched:
            signals.add(intent)
            matches.append((intent, confidence, matched))

    date_extraction = (
        extract_payment_date(lowered, today=today)
        if today
        else extract_payment_date(lowered)
    )

    if is_pure_greeting(lowered):
        signals.add(Intent.GREETING_ONLY)
        return IntentClassification(Intent.GREETING_ONLY, 0.8, "greeting", frozenset(signals))

    # A keyword date carries no amount of specificity on its own; only an
    # explicit date, a counted number of days, or a weekday is self-contained.
    generic_keyword_date = (
        date_extraction.date is not None and date_extraction.reason != "ok"
    )
    date_accepted = date_extraction.is_reliable and (
        not generic_keyword_date
        or date_context
        or bool(_PAYMENT_CUE_RE.search(lowered))
    )
    if date_accepted:
        matches.append((Intent.PAYMENT_DATE_GIVEN, 0.9, date_extraction.matched_text))
        signals.add(Intent.PAYMENT_DATE_GIVEN)
    elif date_extraction.is_vague or looks_vague(lowered):
        signals.add(Intent.VAGUE_ANSWER)

    if not matches:
        if Intent.VAGUE_ANSWER in signals:
            return IntentClassification(
                Intent.VAGUE_ANSWER, 0.6, "vague", frozenset(signals), date_extraction
            )
        return IntentClassification(Intent.UNKNOWN, 0.3, "", frozenset(signals), date_extraction)

    best_intent, best_confidence, matched = max(matches, key=lambda item: item[1])
    return IntentClassification(
        best_intent, best_confidence, matched, frozenset(signals), date_extraction
    )


def _first_match(patterns: Iterable[str], text: str) -> str:
    for pattern in patterns:
        if re.search(pattern, text):
            return pattern
    return ""


def _matches(patterns: Iterable[str], text: str) -> bool:
    return bool(_first_match(patterns, text))


# ------------------------------------------------------------------- reply checks
@dataclass(frozen=True)
class PolicyVerdict:
    """The result of checking a reply before it is spoken."""

    ok: bool = True
    violations: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok


def _sentence_count(text: str) -> int:
    parts = [part for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    return max(1, len(parts))


def _word_count(text: str) -> int:
    return len([word for word in re.split(r"\s+", text.strip()) if word])


def _amount_tokens(text: str) -> FrozenSet[str]:
    """Number words that appear next to an amount marker in ``text``."""
    lowered = text.lower()
    if not _has_amount_marker(lowered):
        return frozenset()
    tokens = set()
    for token in _NUMBER_WORD_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            tokens.add(token)
    for digit in re.findall(r"\d+", lowered):
        tokens.add(digit)
    return frozenset(tokens)


def validate_reply(
    text: str,
    *,
    facts: VerifiedFacts,
    previous_opening: str = "",
    acknowledgement_words_used: Sequence[str] = (),
    is_closing: bool = False,
    asks_payment_date: bool = False,
    principal_request_recorded: bool = False,
    action_saved: bool = False,
) -> PolicyVerdict:
    """Check one reply against every mandatory rule (sections 12.4, 14.3, 16.1).

    ``action_saved`` is the honest flag for "the application really wrote
    something down": Veda may only say it noted a date or a request when that is
    true (priority rule 4).

    Returns every violation rather than the first, so a test can assert the full
    set and a caller can decide which are fatal.
    """
    violations: List[str] = []
    notes: List[str] = []
    candidate = (text or "").strip()

    if not candidate:
        return PolicyVerdict(False, (EMPTY_REPLY,))

    # 1. Never speak digits (Appendix A pitfall 5).
    if _DIGIT_RE.search(candidate) or _TELUGU_DIGIT_RE.search(candidate):
        violations.append(DIGITS_IN_SPEECH)

    # 2. Never reveal the fee before the parent confirmed who they are.
    lowered = candidate.lower()
    if not facts.identity_confirmed:
        if _has_amount_marker(lowered) or _amount_tokens(candidate):
            violations.append(FEE_DISCLOSED_BEFORE_IDENTITY)
            violations.append(UNSAFE_FOR_UNVERIFIED_LISTENER)
        if facts.student_name and facts.student_name in candidate and any(
            marker in lowered for marker in ("pending", "fee", "ఫీజు", "బాకీ")
        ):
            violations.append(UNSAFE_FOR_UNVERIFIED_LISTENER)

    # 3. Never request prohibited financial information.
    if _matches(_FINANCIAL_REQUEST_PATTERNS, lowered):
        violations.append(FORBIDDEN_FINANCIAL_REQUEST)

    # 4. Never promise a discount, waiver or extension.
    if _matches(_PROMISE_PATTERNS, lowered):
        violations.append(PROMISED_DISCOUNT)

    # 5. Never threaten.
    if _matches(_THREAT_PATTERNS, lowered):
        violations.append(THREATENING_LANGUAGE)

    # 6. Never claim an update that did not happen.
    if _matches(_UNSUPPORTED_UPDATE_PATTERNS, lowered) and not action_saved:
        violations.append(UNSUPPORTED_ACTION_CLAIM)

    # 7. Never claim the principal was informed unless the request was saved.
    if _matches(_PRINCIPAL_INFORMED_PATTERNS, lowered) and not principal_request_recorded:
        violations.append(PRINCIPAL_CLAIM_WITHOUT_RECORD)

    # 8. Never invent an amount.
    if facts.identity_confirmed:
        verified_tokens = _amount_tokens(facts.amount_words) if facts.amount_words else frozenset()
        spoken = _amount_tokens(candidate)
        if spoken and not spoken.issubset(verified_tokens):
            violations.append(INVENTED_AMOUNT)

    # 9. Shape: short, one question, one or two sentences.
    if _word_count(candidate) > MAX_REPLY_WORDS or _sentence_count(candidate) > MAX_REPLY_SENTENCES:
        violations.append(TOO_LONG)
    if candidate.count("?") > 1:
        violations.append(TOO_MANY_QUESTIONS)

    # 10. Never ask for the payment date more than twice.
    if asks_payment_date and facts.payment_date_questions_asked >= MAX_PAYMENT_DATE_QUESTIONS:
        violations.append(PAYMENT_QUESTION_LIMIT)

    # 11. Never start two consecutive replies with the same word.
    opening = first_word(candidate)
    if previous_opening and opening and opening == previous_opening:
        violations.append(REPEATED_OPENING)

    # 12. "సరే" at most once per call.
    for word in _ACKNOWLEDGEMENT_WORDS:
        if word in candidate and word in tuple(acknowledgement_words_used):
            violations.append(REPEATED_ACKNOWLEDGEMENT)
            break

    # 13. Never leak internal instructions.
    if _matches(_PROMPT_LEAK_PATTERNS, lowered):
        violations.append(PROMPT_LEAKAGE)

    # 14. Never end on a bare apology or "okay" (section 16.1, case B).
    if is_closing and lowered.strip(" .!?") in _DEAD_END_CLOSINGS:
        violations.append(DEAD_END_CLOSING)

    if not violations:
        notes.append("ok")
    return PolicyVerdict(ok=not violations, violations=tuple(violations), notes=tuple(notes))


# ------------------------------------------------------------------------ sanitise
_MARKDOWN_RE = re.compile(r"[*`#>~]")
#: Underscores are only emphasis at a word edge. Stripping every underscore
#: would turn "{{amount_words}}" into "amountwords" and quietly mangle text.
_EDGE_UNDERSCORE_RE = re.compile(
    r"(?<![0-9A-Za-z\u0C00-\u0C7F])_+|_+(?![0-9A-Za-z\u0C00-\u0C7F])"
)
_WHITESPACE_RE = re.compile(r"\s+")


def sanitise_reply(text: str) -> str:
    """Make model output printable and speakable before policy validation.

    Removes Markdown decoration (the runtime prompt is plain text), strips
    ``{{ }}`` markers so a leftover placeholder can never be spoken, and
    collapses whitespace. Digits are **not** silently kept: converting them is
    the caller's explicit choice via :func:`speech_text.digits_to_words`, so the
    amount check in :func:`validate_reply` still sees the original.
    """
    cleaned = (text or "").replace("{{", "").replace("}}", "")
    cleaned = _MARKDOWN_RE.sub("", cleaned)
    cleaned = _EDGE_UNDERSCORE_RE.sub("", cleaned)
    cleaned = cleaned.replace("“", '"').replace("”", '"').replace("’", "'")
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


# ------------------------------------------------------------- question accounting
def can_ask_payment_date(asked_count: int) -> bool:
    """Section 16.1: at most two payment-date questions in the whole call."""
    return asked_count < MAX_PAYMENT_DATE_QUESTIONS


def can_prompt_silence(prompts_so_far: int) -> bool:
    return prompts_so_far < MAX_SILENCE_PROMPTS


def should_close_off_topic(off_topic_turns: int) -> bool:
    return off_topic_turns >= MAX_OFF_TOPIC_TURNS


# ------------------------------------------------------------------------- masking
_PHONE_DIGITS_RE = re.compile(r"\d")


def mask_phone(phone: Optional[str]) -> str:
    """``+919876543210`` -> ``+91******3210``; safe for logs and exports."""
    if not phone:
        return ""
    digits = "".join(_PHONE_DIGITS_RE.findall(phone))
    if len(digits) < 6:
        return "*" * len(digits)
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def mask_name(name: Optional[str]) -> str:
    """``Sai Teja`` -> ``S*** T***``; enough to identify a row, not a person."""
    if not name:
        return ""
    parts = [part for part in str(name).split() if part]
    return " ".join(f"{part[0]}{'*' * max(1, len(part) - 1)}" for part in parts)
