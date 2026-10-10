"""Approved replies, in the wording the owner dictated.

Two different things live here and must not be confused (section 5.4):

* the **response template cache** - approved *text* plus the rule for when it may
  be used. This contains every reply Veda may say without the model.
* the **audio cache** (see :mod:`app.cache.audio_cache`) - previously synthesised
  audio for an *exact* approved phrase. Only the fixed opening lines, a few
  rotating closing lines and the silence check-in are allowed in there
  (section 13.7), because reusing the same audio for ordinary replies makes calls
  sound scripted.

The wordings are the binding ones from section 16.1 and Appendix B, kept verbatim
so the tests can prove the policy guards do not block an approved line. Placeholders
are rendered strictly: a missing value or a leftover ``{{...}}`` raises rather than
speaking something broken.

Templates that only make sense *somewhere else in the conversation* carry a
``phase`` hint, and the identity gate is enforced twice: by never offering these
templates before confirmation, and by :func:`validate_reply` rejecting any reply
that mentions an amount while identity is unconfirmed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

#: Rotating closings: a few, as section 13.7 permits, each starting differently
#: so two consecutive calls never open the same way.
CLOSING_KEYS = ("closing_warm_1", "closing_warm_2", "closing_warm_3")

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


@dataclass(frozen=True)
class ResponseTemplate:
    """One approved reply."""

    key: str
    text: str
    language: str = "te-IN"
    #: True only for phrases allowed to be pre-rendered into the audio cache.
    cacheable_audio: bool = False
    placeholders: tuple = ()
    #: A note for reviewers: what this template is for.
    purpose: str = ""

    def render(self, values: Mapping[str, str]) -> str:
        """Fill the placeholders strictly."""
        missing: List[str] = []

        def replace(match: "re.Match[str]") -> str:
            name = match.group(1)
            if name not in values:
                missing.append(name)
                return ""
            return str(values[name])

        rendered = _PLACEHOLDER_RE.sub(replace, self.text)
        if missing:
            raise KeyError(
                f"template {self.key!r} needs values for: " + ", ".join(sorted(set(missing)))
            )
        leftovers = _PLACEHOLDER_RE.findall(rendered)
        if leftovers or "{{" in rendered or "}}" in rendered:
            raise ValueError(f"template {self.key!r} still contains a placeholder")
        return rendered.strip()


# --------------------------------------------------------------------- wordings
# fmt: off
APPROVED_TEMPLATES: Dict[str, ResponseTemplate] = {
    # The two scripted opening lines (section 10.1). Line 2 is the identity
    # question and is a separate controlled turn; neither line mentions the fee.
    "opener_line_1": ResponseTemplate(
        key="opener_line_1",
        text="Hello sir, నేను Veda, {{school_name}} నుండి మాట్లాడుతున్నాను.",
        cacheable_audio=True,
        placeholders=("school_name",),
        purpose="introduction; played first, never interrupted",
    ),
    "opener_line_2_with_parent": ResponseTemplate(
        key="opener_line_2_with_parent",
        text="నేను {{student_name}} parent {{parent_name}} గారితో మాట్లాడుతున్నానా?",
        cacheable_audio=True,
        placeholders=("student_name", "parent_name"),
        purpose="identity question when a parent name is on the record",
    ),
    "opener_line_2_without_parent": ResponseTemplate(
        key="opener_line_2_without_parent",
        text="నేను {{student_name}} parent గారితో మాట్లాడుతున్నానా?",
        cacheable_audio=True,
        placeholders=("student_name",),
        purpose="identity question when no parent name is known",
    ),
    "identity_reask": ResponseTemplate(
        key="identity_reask",
        # Opens with "ఈ", not "సర్", so it never collides with the fee reminder
        # that follows a confirmed answer (the no-repeated-opening rule).
        text="ఈ call {{school_name}} నుండి {{student_name}} గారి school విషయం కోసం. "
             "మీరు {{student_name}} parent గారా?",
        placeholders=("school_name", "student_name"),
        purpose="one polite re-ask when the identity answer was unclear",
    ),
    "who_is_calling": ResponseTemplate(
        key="who_is_calling",
        # No student name and no fee word: the identity gate blocks any reply that
        # pairs the student with a fee marker, and this one is said before it opens.
        text="నేను Veda ని, {{school_name}} నుండి మాట్లాడుతున్నాను సర్. "
             "School నుండి ముఖ్యమైన విషయం మాట్లాడాలి సర్.",
        placeholders=("school_name",),
        purpose="answer 'who is calling' before identity is confirmed",
    ),
    "who_is_calling_after_identity": ResponseTemplate(
        key="who_is_calling_after_identity",
        text="నేను Veda ని, {{school_name}} నుండి {{student_name}} గారి school fee "
             "విషయం మాట్లాడటానికి call చేశాను సర్.",
        placeholders=("school_name", "student_name"),
        purpose="answer 'who is calling' once the parent is verified",
    ),
    # Fee reminder, only reachable once identity is confirmed.
    "fee_reminder": ResponseTemplate(
        key="fee_reminder",
        text="సర్, మీ child school fees {{amount_words}} ఇంకా pending ఉంది సర్. "
             "ఎప్పటికల్లా కడతారో తెలుసుకోవచ్చా?",
        placeholders=("amount_words",),
        purpose="the fee line; asks the first payment-date question",
    ),
    "date_accepted": ResponseTemplate(
        key="date_accepted",
        text="ఆ date note చేసుకున్నాను సర్. Thank you, have a nice day.",
        purpose="the parent gave a usable date",
    ),
    "vague_first_time": ResponseTemplate(
        key="vague_first_time",
        text="ఒక date చెప్పగలరా సర్, note చేసుకుంటాను?",
        purpose="asks once more after a vague answer",
    ),
    "financial_reason": ResponseTemplate(
        key="financial_reason",
        text="అర్థమైంది సర్. మీకు వీలైనంత త్వరగా కట్టండి సర్. Thank you, have a nice day.",
        purpose="a money problem: empathy, no further date question",
    ),
    "illness_emergency": ResponseTemplate(
        key="illness_emergency",
        text="అయ్యో, అలాగా సర్. మీరు care తీసుకోండి సర్. ధన్యవాదాలు.",
        purpose="illness or an emergency: sympathy, no payment question",
    ),
    "already_paid": ResponseTemplate(
        key="already_paid",
        text="ఓహ్ అలాగా సర్, sorry for the disturbance. మేము office లో check చేసుకుంటాం. Thank you.",
        purpose="the parent says the fee was already paid; never asks for proof",
    ),
    # The owner-approved wording for a parent who was already contacted.
    "previous_reminder_with_date": ResponseTemplate(
        key="previous_reminder_with_date",
        text="సారీ సర్, మా record లో ఇంకా update కాలేదు. ఆ date ఇప్పుడే note చేసుకుంటాను. "
             "Thank you, have a nice day.",
        purpose="already contacted, and the parent gave a date now",
    ),
    "previous_reminder_ask_date": ResponseTemplate(
        key="previous_reminder_ask_date",
        text="సారీ సర్, మా record లో update కాలేదు. మీరు చెప్పిన date చెప్తే ఇప్పుడే "
             "note చేసుకుంటాను సర్.",
        purpose="already contacted, no date yet, question limit allows one ask",
    ),
    "previous_reminder_no_date": ResponseTemplate(
        key="previous_reminder_no_date",
        text="పర్వాలేదు సర్, office లో check చేసి update చేస్తాం. Disturb చేసినందుకు sorry, thank you.",
        purpose="already contacted and no date available; never ends with only 'sorry'",
    ),
    "busy_callback": ResponseTemplate(
        key="busy_callback",
        text="ఎప్పుడు call చేయమంటారు సర్?",
        purpose="the parent is busy or asked for a later call",
    ),
    "third_party": ResponseTemplate(
        key="third_party",
        text="ఓకే సర్, parent గారితో school విషయం మాట్లాడాలి. ఎప్పుడు call చేస్తే బాగుంటుంది సర్?",
        purpose="someone else answered: no fee and no amount",
    ),
    "wrong_number": ResponseTemplate(
        key="wrong_number",
        text="సారీ సర్, wrong number అయినట్టుంది. Disturb చేసినందుకు sorry, thank you.",
        purpose="wrong number: apologise, disclose nothing, end",
    ),
    "irritated_acknowledge": ResponseTemplate(
        key="irritated_acknowledge",
        text="సర్, ఈ call {{school_name}} fee విషయం కోసం. Disturb చేసినందుకు sorry సర్.",
        placeholders=("school_name",),
        purpose="stay calm when the parent is annoyed",
    ),
    "ai_identity": ResponseTemplate(
        key="ai_identity",
        # "అవును" was removed: the same pattern fires on "are you human?", and a
        # bare yes would be the wrong answer to that.
        text="సర్, నేను Veda ని, school తరపున call చేస్తున్న automated assistant ని.",
        purpose="honest answer when asked whether Veda is a person",
    ),
    "fee_question_answer": ResponseTemplate(
        key="fee_question_answer",
        text="సర్, {{student_name}} గారి school fees {{amount_words}} ఇంకా pending ఉంది సర్.",
        placeholders=("student_name", "amount_words"),
        purpose="restates the verified amount; asks no question, so the counter is untouched",
    ),
    "asked_to_stop": ResponseTemplate(
        key="asked_to_stop",
        text="సరే సర్, ఇబ్బంది పెట్టను. ధన్యవాదాలు సర్.",
        purpose="the parent asked to end the call; the one use of \u0c38\u0c30\u0c47 per call",
    ),
    "do_not_call_close": ResponseTemplate(
        key="do_not_call_close",
        text="అలాగే సర్, ఇక మీకు ఫోన్ చేయను. ధన్యవాదాలు సర్.",
        purpose="honours a do-not-call request; the number is blocked as it is said",
    ),
    "silence_check_in": ResponseTemplate(
        key="silence_check_in",
        text="హలో సర్, వినిపిస్తోందా?",
        cacheable_audio=True,
        purpose="first check-in after silence; the only non-opening cached phrase",
    ),
    "silence_check_in_2": ResponseTemplate(
        key="silence_check_in_2",
        # The second prompt cannot repeat "హలో": no two consecutive replies may
        # start with the same word, and this is the reply that follows the first.
        text="అలాగే సర్, వినిపిస్తోందా సర్?",
        purpose="second check-in after silence, worded differently on purpose",
    ),
    "off_topic_close": ResponseTemplate(
        key="off_topic_close",
        text="సర్, మీకు వీలైనప్పుడు fee విషయం చూసుకోండి. ధన్యవాదాలు సర్.",
        purpose="close politely after repeated off-topic turns",
    ),
    "already_paid_unknown": ResponseTemplate(
        key="already_paid_unknown",
        text="సర్, ఈ విషయం office లో check చేయమని note చేస్తాను. ధన్యవాదాలు సర్.",
        purpose="safe reply when the model output was rejected",
    ),
    # Principal request (section 16.1, case C) with the real Stage 1 mechanism.
    "principal_request": ResponseTemplate(
        key="principal_request",
        text="ఈ విషయం principal గారికి inform చేయమని office కి note చేస్తాను సర్. "
             "Principal గారు free అయ్యాక office నుండి మీకు inform చేయమంటారా?",
        purpose="asks for permission to pass the request to the office",
    ),
    "principal_agreed": ResponseTemplate(
        key="principal_agreed",
        # Deliberately not "ఓకే": that word is spent once per call on the
        # owner-approved stop wording, and this reply must not compete for it.
        text="అలాగే సర్, office నుండి మీకు inform చేస్తాం. Thank you, have a nice day.",
        purpose="the parent agreed to be contacted by the office",
    ),
    "principal_declined": ResponseTemplate(
        key="principal_declined",
        text="అలాగే సర్, మీకు వీలైనప్పుడు principal గారితో మాట్లాడండి. Thank you.",
        purpose="the parent will approach the principal directly",
    ),
    # Rotating closings - cacheable audio, a few of them, different openings.
    "closing_warm_1": ResponseTemplate(
        key="closing_warm_1",
        text="ధన్యవాదాలు సర్, మీ రోజు బాగుండాలి.",
        cacheable_audio=True,
        purpose="closing after a promise",
    ),
    "closing_warm_2": ResponseTemplate(
        key="closing_warm_2",
        text="Thank you సర్, have a nice day.",
        cacheable_audio=True,
        purpose="closing after a promise",
    ),
    "closing_warm_3": ResponseTemplate(
        key="closing_warm_3",
        text="మీకు ధన్యవాదాలు సర్, శుభాకాంక్షలు.",
        cacheable_audio=True,
        purpose="closing after a promise",
    ),
    # Last-resort replies. They must start with words the other closings do not,
    # so the no-repeated-opening rule can always be satisfied by picking another.
    "safe_fallback": ResponseTemplate(
        key="safe_fallback",
        text="అలాగే సర్, ఈ విషయం office లో check చేసి మీకు inform చేస్తాం. ధన్యవాదాలు సర్.",
        purpose="used when a model reply failed validation",
    ),
    "neutral_close": ResponseTemplate(
        key="neutral_close",
        text="పర్వాలేదు సర్, office లో ఈ విషయం చూసుకుంటాం. ధన్యవాదాలు సర్.",
        purpose="says nothing about the fee; the last line before giving up",
    ),
}
# fmt: on


@dataclass
class ResponseCache:
    """Approved text lookup with strict rendering and an audio-cache key rule."""

    templates: Dict[str, ResponseTemplate] = field(default_factory=lambda: dict(APPROVED_TEMPLATES))
    language: str = "te-IN"
    voice: str = ""
    model: str = ""
    template_version: str = "v1"

    def get(self, key: str) -> Optional[ResponseTemplate]:
        return self.templates.get(key)

    def require(self, key: str) -> ResponseTemplate:
        template = self.templates.get(key)
        if template is None:
            raise KeyError(f"no approved response template is registered as {key!r}")
        return template

    def render(self, key: str, values: Optional[Mapping[str, str]] = None) -> str:
        return self.require(key).render(values or {})

    def render_candidates(
        self, keys: Sequence[str], values: Optional[Mapping[str, str]] = None
    ) -> List[tuple]:
        """Render several candidates, skipping any that cannot be rendered.

        Used by the controller's "first candidate that passes policy wins" logic:
        a template whose facts are missing is dropped rather than spoken.
        """
        rendered: List[tuple] = []
        for key in keys:
            template = self.templates.get(key)
            if template is None:
                continue
            try:
                rendered.append((key, template, template.render(values or {})))
            except (KeyError, ValueError):
                continue
        return rendered

    def audio_candidates(self) -> List[ResponseTemplate]:
        """Only the phrases section 13.7 allows to be pre-rendered."""
        return [template for template in self.templates.values() if template.cacheable_audio]

    def is_cacheable(self, key: str) -> bool:
        template = self.templates.get(key)
        return bool(template and template.cacheable_audio)

    def audio_cache_key(
        self,
        key: str,
        text: str,
        *,
        language: Optional[str] = None,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        template_version: Optional[str] = None,
        sample_rate: int = 8000,
        audio_format: str = "pcm",
    ) -> str:
        """The cache key for one exact phrase.

        Every property that changes the audio is part of the key, so a different
        voice, model, language, text, version, rate or format can never be served
        the wrong file (section 13.2).
        """
        parts = [
            key,
            text,
            language or self.language,
            voice or self.voice,
            model or self.model,
            template_version or self.template_version,
            str(sample_rate),
            audio_format,
        ]
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
        return digest[:32]

    def closing_key(self, index: int) -> str:
        """Rotate through the approved closings."""
        return CLOSING_KEYS[index % len(CLOSING_KEYS)]

    def closing(self, index: int) -> str:
        return self.render(self.closing_key(index))


def text_checksum(value) -> str:
    """Checksum of spoken text or of audio bytes.

    Used two ways: to detect a cached recording whose approved wording changed,
    and to detect a cached file whose bytes were truncated or corrupted.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
    else:
        payload = (value or "").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def templates_requiring_audio(templates: Optional[Iterable[ResponseTemplate]] = None) -> List[str]:
    """Keys that ``validate-audio`` should build and verify."""
    source = templates if templates is not None else APPROVED_TEMPLATES.values()
    return [template.key for template in source if template.cacheable_audio]
