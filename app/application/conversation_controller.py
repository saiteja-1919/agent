"""The conversation controller: everything that must not be left to a model.

Section 12.4. One object owns one call's conversation state and answers four
questions:

* who am I talking to (the identity gate),
* what may be said next (approved templates, and the model only where the
  spec allows it to word a reply),
* may I say it *yet* (the persistence contract below),
* how did the call end (the section 18 outcome ladder).

The persistence contract
------------------------
Priority rule 4: Veda may only say it noted something after the application has
really saved that note. The controller therefore never speaks on its own; it
returns a :class:`ControllerTurn` with ``requires_persistence=True`` whenever it
has changed call state that a reply refers to. The caller must:

1. persist ``state`` (and any side effect such as the do-not-call row) - on
   failure, **do not speak the decision**; substitute a reply that claims
   nothing, or hang up,
2. call :meth:`ConversationController.mark_persisted`,
3. only then play ``decision.text``.

Validation follows the same rule: a reply that claims an action is first
checked with the claim treated as unsaved, and is accepted only after the retry
shows it would be true. So an unsaved write can never produce a spoken claim.

Replies are chosen as "first candidate that passes every policy rule wins"
(:func:`app.domain.policies.validate_reply`). When nothing validates, Veda says
nothing and ends the call rather than speaking a line that breaks a rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from ..cache.response_cache import ResponseCache
from ..domain.enums import (
    ConversationPhase,
    Intent,
    Outcome,
    ReasonCode,
    ResponseSource,
    validate_outcome_reason,
)
from ..domain.models import (
    CallbackRequest,
    CallOutcomeRecord,
    ConversationState,
    ConversationTurn,
    PaymentCommitment,
    ReplyDecision,
)
from ..domain.policies import (
    can_ask_payment_date,
    can_prompt_silence,
    classify_identity_answer,
    classify_intent,
    is_pure_greeting,
    sanitise_reply,
    should_close_off_topic,
    validate_reply,
)
from .prompting import build_messages

#: Mirrors ``app.domain.policies._ACKNOWLEDGEMENT_WORDS``: "at most once a call".
_ACKNOWLEDGEMENT_WORDS: Tuple[str, ...] = ("సరే", "okay", "ok", "ఓకే", "sare")

#: Words that mean "call me later" was answered with a time.
_TIME_HINT_RE = re.compile(
    r"\bmorning\b|\bafternoon\b|\bevening\b|\btonight\b|\bnight\b|\bcall\b|"
    r"\bat\b|\bthis afternoon\b|\bnext week\b|\btoday\b|\btomorrow\b|"
    r"ఉదయం|మధ్యాహ్నం|సాయంత్రం|రాత్రి|తర్వాత|తరువాత|రేపు|ఈ రోజు|"
    r"\d{1,2}\s*[:.]\s*\d{2}",
    re.IGNORECASE,
)

#: A flat refusal to pay, read only after the financial-reason patterns so a
#: money problem is never recorded as a refusal.
_REFUSAL_RE = re.compile(
    r"\b(i )?(won'?t|will not|not going to) pay\b|\bnot paying\b|"
    r"\bcan'?t pay\b|\bcannot pay\b|\brefuse\b|"
    r"కట్టలేను|చెల్లించలేను|ఇవ్వలేను|వద్దు సర్|నేను కట్టను",
    re.IGNORECASE,
)

#: Yes / no, used for the two permission questions Veda asks (identity and the
#: principal request). Deliberately tiny: an ambiguous answer falls through.
_YES_RE = re.compile(
    r"^(yes|ya|yeah|yep|haan|han|ha|avunu|avnu|avunandi|అవును|అవునండి|ఔను|"
    r"అలాగే|సరే సర్|okay|ok|ఓకే)(\s+|$|[.,!?])",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"^(no|nope|kaadu|kadu|లేదు|కాదు|వద్దు)(\s|$|[.,!?])",
    re.IGNORECASE,
)


class LLMBackend(Protocol):
    """The slice of the Sarvam client the controller needs."""

    configured: bool

    async def complete_reply(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = 0.6,
        max_tokens: int = 300,
        cancelled: Optional[Any] = None,
    ) -> Any: ...


@dataclass(frozen=True)
class ControllerTurn:
    """One decision plus why, and what the caller must do before speaking it."""

    decision: ReplyDecision
    intent: Intent = Intent.UNKNOWN
    intent_confidence: float = 0.0
    requires_persistence: bool = False
    rejected: Tuple[str, ...] = ()

    @property
    def speaks(self) -> bool:
        return bool(self.decision.text)

    @property
    def ends(self) -> bool:
        """True only when the controller decided to end; an empty reply that does
        not end the call means "say nothing and keep listening", not "hang up"."""
        return self.decision.end_call_after


@dataclass(frozen=True)
class InterruptionVerdict:
    """What the audio pipeline should do when the parent talks over Veda."""

    stop_speaking: bool = False
    discard_veda_audio: bool = False
    restart_opening: bool = False
    keep_buffered_speech: bool = True


@dataclass(frozen=True)
class Opening:
    """The two scripted opening lines (section 10.1)."""

    line_1: ReplyDecision
    line_2: ReplyDecision

    @property
    def lines(self) -> Tuple[ReplyDecision, ReplyDecision]:
        return (self.line_1, self.line_2)


class ConversationController:
    """Owns exactly one call's conversation. Never shared between calls."""

    def __init__(
        self,
        *,
        state: ConversationState,
        responses: Optional[ResponseCache] = None,
        settings: Any = None,
        llm: Optional[LLMBackend] = None,
        today: Optional[date] = None,
    ) -> None:
        self.state = state
        self.responses = responses or ResponseCache()
        self.settings = settings
        self.llm = llm
        self.today = today or date.today()

        self.phase: ConversationPhase = ConversationPhase.NOT_STARTED
        #: Violations seen on rejected candidates; goes to metrics, never to speech.
        self.rejected: List[str] = []

        self._dirty = False
        self._final: Optional[Tuple[Outcome, Optional[ReasonCode]]] = None
        self._wrong_number = False
        self._identity_unconfirmed = False
        self._parent_spoke = False
        self._identity_reasks = 0
        self._closing_index = 0
        self._awaiting_callback_time = False
        self._awaiting_principal_permission = False
        self._off_topic_closed = False
        self._history: List[Dict[str, str]] = []

    # ------------------------------------------------------------------ helpers
    @property
    def facts(self):
        return self.state.facts

    @property
    def dirty(self) -> bool:
        """True when call state changed and has not been persisted yet."""
        return self._dirty

    def mark_persisted(self) -> None:
        """The caller saved ``state``; a claim about it is now true."""
        self._dirty = False

    def _values(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        values: Dict[str, str] = {
            "school_name": self.state.facts.school_name or "the school office",
            "student_name": self.state.facts.student_name,
            "parent_name": self.state.facts.parent_name,
            "amount_words": self.state.facts.amount_words,
        }
        if extra:
            values.update({k: v for k, v in extra.items() if v is not None})
        return values

    def _check(
        self,
        text: str,
        *,
        asks_payment_date: bool = False,
        is_closing: bool = False,
    ):
        """Validate once as things stand, then again as they will be when saved."""

        def run(action_saved: bool) -> Any:
            return validate_reply(
                text,
                facts=self.state.facts,
                previous_opening=self.state.previous_opening,
                acknowledgement_words_used=self.state.acknowledgement_words_used,
                is_closing=is_closing,
                asks_payment_date=asks_payment_date,
                principal_request_recorded=self.state.principal_request and action_saved,
                action_saved=action_saved,
            )

        persisted = not self._dirty
        verdict = run(persisted)
        if verdict.ok:
            return verdict
        if self._dirty:
            retry = run(True)
            if retry.ok:
                return retry
        return verdict

    def _remember(self, text: str) -> None:
        used = [word for word in _ACKNOWLEDGEMENT_WORDS if word in (text or "").lower()]
        self.state.remember_reply(text, used)

    def _audio_key(self, key: str, text: str) -> Optional[str]:
        if not self.responses.is_cacheable(key):
            return None
        return self.responses.audio_cache_key(
            key,
            text,
            voice=getattr(self.settings, "sarvam_tts_voice", "") if self.settings else "",
            model=getattr(self.settings, "sarvam_tts_model", "") if self.settings else "",
        )

    def _decide(
        self,
        key: str,
        *,
        values: Optional[Mapping[str, str]] = None,
        source: ResponseSource = ResponseSource.TEMPLATE,
        intent: Intent = Intent.UNKNOWN,
        confidence: float = 1.0,
        is_question: bool = False,
        asks_payment_date: bool = False,
        end: bool = False,
        is_closing: bool = False,
        outcome: Optional[Outcome] = None,
        reason: Optional[ReasonCode] = None,
    ) -> Optional[ControllerTurn]:
        """Render one approved template and accept it only if every rule allows it.

        Returns ``None`` when the template cannot be rendered (a fact is
        missing) or when policy rejects it, so callers can try the next
        candidate instead of speaking something unsafe.
        """
        template = self.responses.get(key)
        if template is None:
            return None
        try:
            text = template.render(self._values(values))
        except (KeyError, ValueError):
            return None

        verdict = self._check(text, asks_payment_date=asks_payment_date, is_closing=is_closing)
        if not verdict.ok:
            self.rejected.extend(verdict.violations)
            return None

        if asks_payment_date:
            # Count only once the reply is known to be speakable, so a rejected
            # candidate can never burn one of the two permitted questions.
            self.state.payment_date_questions_asked += 1
            self.state.facts = self.state.facts.with_payment_date_question()
            self._dirty = True

        decision = ReplyDecision(
            text=text,
            source=source,
            intent=intent,
            is_question=is_question or asks_payment_date or text.strip().endswith("?"),
            asks_payment_date=asks_payment_date,
            end_call_after=end,
            cache_audio_key=self._audio_key(key, text),
            outcome=outcome,
            reason_code=reason,
        )
        self._remember(text)
        self._track(decision)
        return ControllerTurn(
            decision=decision,
            intent=intent,
            intent_confidence=confidence,
            requires_persistence=self._dirty,
            rejected=tuple(verdict.violations),
        )

    def _track(self, decision: ReplyDecision) -> None:
        if decision.end_call_after:
            self.phase = ConversationPhase.CLOSING
            if decision.outcome is not None:
                self._final = (decision.outcome, decision.reason_code)
            return
        # Otherwise a reply was decided, so the next thing we expect is the
        # parent - except while the opening or the identity question is still
        # being collected, which are their own phases.
        if decision.text and self.phase not in (
            ConversationPhase.NOT_STARTED,
            ConversationPhase.PLAYING_GREETING,
            ConversationPhase.WAITING_FOR_IDENTITY_RESPONSE,
        ):
            self.phase = ConversationPhase.WAITING_FOR_PARENT_TURN

    def _silence_turn(self, intent: Intent = Intent.UNKNOWN) -> ControllerTurn:
        """The absolute last resort: a neutral line, or nothing at all."""
        for key in ("safe_fallback", "neutral_close", "closing_warm_1",
                    "closing_warm_2", "closing_warm_3"):
            turn = self._decide(key, source=ResponseSource.FALLBACK, intent=intent, end=True)
            if turn is not None:
                return turn
        # Nothing validated. Saying nothing is always safer than saying a line
        # that breaks a rule, and the caller hangs up on an empty decision.
        decision = ReplyDecision(
            text="", source=ResponseSource.FALLBACK, intent=intent, end_call_after=True
        )
        return ControllerTurn(decision=decision, intent=intent, requires_persistence=self._dirty)

    # ------------------------------------------------------------------ opening
    def begin(self) -> Opening:
        """Build the two scripted lines. Neither mentions the fee (section 10.1)."""
        if self.phase is not ConversationPhase.NOT_STARTED:
            raise RuntimeError("the opening can only be built once")
        self.phase = ConversationPhase.PLAYING_GREETING

        line1 = self._decide(
            "opener_line_1", source=ResponseSource.SCRIPTED, intent=Intent.GREETING_ONLY
        )
        if line1 is None:  # pragma: no cover - school name is always renderable
            raise RuntimeError("the first opening line could not be rendered")
        if self.state.facts.parent_name:
            key = "opener_line_2_with_parent"
        else:
            key = "opener_line_2_without_parent"
        line2 = self._decide(key, source=ResponseSource.SCRIPTED, intent=Intent.IDENTITY_CONFIRMED)
        if line2 is None:
            raise RuntimeError("the identity question could not be rendered")
        return Opening(line_1=line1.decision, line_2=line2.decision)

    def on_opening_line_played(self, index: int) -> None:
        """Report that opening line ``index`` (0 or 1) finished playing."""
        if index >= 1:
            self.phase = ConversationPhase.WAITING_FOR_IDENTITY_RESPONSE

    def on_interruption(self, *, during_opening: bool = False) -> InterruptionVerdict:
        """Section 10.2: the opening is never cut; everything after it may be.

        The buffered partial transcript becomes the parent's next turn: the
        caller passes it to :meth:`on_parent_turn` once Veda stops speaking.
        Veda never restarts the opening, because replaying an introduction is
        what makes a call sound like a machine.
        """
        if during_opening:
            return InterruptionVerdict(
                stop_speaking=False,
                discard_veda_audio=False,
                restart_opening=False,
                keep_buffered_speech=True,
            )
        return InterruptionVerdict(
            stop_speaking=True,
            discard_veda_audio=True,
            restart_opening=False,
            keep_buffered_speech=True,
        )

    # ------------------------------------------------------------ identity gate
    def on_identity_turn(self, text: str) -> Optional[ControllerTurn]:
        """Read the answer to the identity question. ``None`` means keep listening.

        No fee, amount or due date may be spoken until this returns a reply from
        the confirmed branch, and :func:`validate_reply` enforces that too.
        """
        if self.phase not in (
            ConversationPhase.WAITING_FOR_IDENTITY_RESPONSE,
            ConversationPhase.PLAYING_GREETING,
        ):
            raise RuntimeError(f"identity is not being collected in phase {self.phase}")

        cleaned = sanitise_reply(text)
        if not cleaned or is_pure_greeting(cleaned):
            # A greeting during the pause is not an answer (section 10.2).
            return None

        turn = self._read_identity(cleaned)
        if turn is not None:
            self._record_turn(cleaned, turn)
        return turn

    def _read_identity(self, cleaned: str) -> Optional[ControllerTurn]:
        self._parent_spoke = True
        classification = classify_intent(cleaned, today=self.today)
        intent = classification.intent

        if intent is Intent.DO_NOT_CALL:
            return self._do_not_call()
        if intent is Intent.WRONG_NUMBER:
            return self._wrong_number_turn()

        if intent is Intent.WHO_IS_CALLING:
            key = "who_is_calling_after_identity" if self.state.identity_confirmed else "who_is_calling"
            turn = self._decide(key, intent=Intent.WHO_IS_CALLING, confidence=classification.confidence)
            if turn is not None:
                return turn

        answer = classify_identity_answer(cleaned)
        if answer.confirmed:
            return self._confirm_identity(classification.confidence)
        if answer.wrong_number:
            return self._wrong_number_turn()
        if answer.denied:
            # Someone who is not the parent answered. Offer a callback, disclose
            # nothing (section 16.1 step one).
            return self._third_party_turn(classification.confidence)

        # Unclear: one polite re-ask, then give up rather than loop.
        self._identity_reasks += 1
        if self._identity_reasks <= 1:
            turn = self._decide("identity_reask", intent=Intent.IDENTITY_CONFIRMED)
            if turn is not None:
                self.phase = ConversationPhase.WAITING_FOR_IDENTITY_RESPONSE
                return turn
        self._identity_unconfirmed = True
        self._dirty = True
        self._final = (Outcome.COMPLETED, ReasonCode.NO_DATE_GIVEN)
        return self._rotating_closing(end=True) or self._silence_turn()

    def _confirm_identity(self, confidence: float = 1.0) -> ControllerTurn:
        self.state.identity_confirmed = True
        self.state.facts = self.state.facts.with_identity_confirmed()
        self._dirty = True
        self.phase = ConversationPhase.PLAYING_FEE_REMINDER
        turn = self._fee_reminder()
        if turn is None:  # pragma: no cover - amount is always renderable
            return self._silence_turn(Intent.IDENTITY_CONFIRMED)
        return ControllerTurn(
            decision=turn.decision,
            intent=Intent.IDENTITY_CONFIRMED,
            intent_confidence=confidence,
            requires_persistence=turn.requires_persistence,
            rejected=turn.rejected,
        )

    def _wrong_number_turn(self) -> ControllerTurn:
        self._wrong_number = True
        self._dirty = True
        self._final = (Outcome.WRONG_NUMBER, ReasonCode.WRONG_PERSON)
        turn = self._decide(
            "wrong_number",
            intent=Intent.WRONG_NUMBER,
            end=True,
            is_closing=True,
            outcome=Outcome.WRONG_NUMBER,
            reason=ReasonCode.WRONG_PERSON,
        )
        return turn or self._silence_turn(Intent.WRONG_NUMBER)

    def _third_party_turn(self, confidence: float = 1.0) -> ControllerTurn:
        self.state.third_party_answered = True
        self._dirty = True
        # We are no longer collecting an identity: the next thing we want is a
        # time to call back, so the caller must route through on_parent_turn.
        self.phase = ConversationPhase.WAITING_FOR_PARENT_TURN
        turn = self._decide("third_party", intent=Intent.THIRD_PARTY, confidence=confidence)
        if turn is None:
            return self._silence_turn(Intent.THIRD_PARTY)
        self._awaiting_callback_time = True
        self._final = (Outcome.NOT_PARENT, ReasonCode.THIRD_PARTY_ANSWERED)
        return turn

    def _do_not_call(self) -> ControllerTurn:
        self.state.do_not_call = True
        self._dirty = True
        self._final = (Outcome.DO_NOT_CALL, ReasonCode.PARENT_REQUESTED_NO_MORE_CALLS)
        turn = self._decide(
            "do_not_call_close",
            intent=Intent.DO_NOT_CALL,
            end=True,
            is_closing=True,
            outcome=Outcome.DO_NOT_CALL,
            reason=ReasonCode.PARENT_REQUESTED_NO_MORE_CALLS,
        )
        # The number is blocked as the line is said, so the caller must save the
        # do-not-call row before playing this decision.
        return turn or self._silence_turn(Intent.DO_NOT_CALL)

    def _fee_reminder(self) -> Optional[ControllerTurn]:
        if not self.state.facts.has_fee_facts():
            # No usable amount: never state one (section 16.1 step two).
            return self._decide(
                "neutral_close", intent=Intent.FEE_QUESTION, end=True, is_closing=True,
                outcome=Outcome.COMPLETED, reason=ReasonCode.NO_DATE_GIVEN,
            )
        if not can_ask_payment_date(self.state.payment_date_questions_asked):
            return self._decide("fee_question_answer", intent=Intent.FEE_QUESTION)
        return self._decide(
            "fee_reminder",
            source=ResponseSource.TEMPLATE,
            intent=Intent.FEE_QUESTION,
            is_question=True,
            asks_payment_date=True,
        )

    # ----------------------------------------------------------------- silence
    def on_silence(self) -> Optional[ControllerTurn]:
        """Nothing was heard. Prompt at most twice (section 10.3), then stop.

        Returns ``None`` when the call should end, either because both prompts
        are spent or because nothing could be said: the caller hangs up and
        calls :meth:`finalize`, which records no response.
        """
        if not can_prompt_silence(self.state.silence_prompts):
            self._final = (
                Outcome.NO_RESPONSE,
                ReasonCode.PARENT_SILENT if self._parent_spoke else ReasonCode.RING_TIMEOUT,
            )
            return None

        self.state.silence_prompts += 1
        self._dirty = True

        if self.phase is ConversationPhase.WAITING_FOR_IDENTITY_RESPONSE:
            if self._identity_reasks < 1:
                # The first silence prompt repeats the identity question rather
                # than a generic "are you there": the answer is what we need.
                self._identity_reasks += 1
                turn = self._decide("identity_reask", intent=Intent.IDENTITY_CONFIRMED)
                if turn is not None:
                    return turn
            else:
                turn = self._check_in()
                if turn is not None:
                    return turn
            # Nothing speakable: undo the count and let the caller hang up.
            self.state.silence_prompts -= 1
            return None

        turn = self._check_in()
        if turn is not None:
            return turn
        self.state.silence_prompts -= 1
        return None

    def _check_in(self) -> Optional[ControllerTurn]:
        """The approved "can you hear me?" lines.

        The first is the only non-opening phrase allowed in the audio cache
        (section 13.7); the second is worded differently so the second prompt
        never starts with the word the first one did.
        """
        key = "silence_check_in" if self.state.silence_prompts % 2 else "silence_check_in_2"
        return self._decide(
            key,
            source=ResponseSource.CHECK_IN,
            intent=Intent.SILENCE,
            is_question=True,
        )

    # ------------------------------------------------------------- parent turns
    async def on_parent_turn(self, text: str) -> ControllerTurn:
        """The main path: one finalized parent turn in, one decision out."""
        cleaned = sanitise_reply(text)
        if not cleaned:
            turn = self.on_silence() or self._end_no_response()
            self._record_turn("", turn)
            return turn
        turn = await self._route(cleaned)
        self._record_turn(cleaned, turn)
        return turn

    def _record_turn(self, parent_text: str, turn: ControllerTurn) -> None:
        """Keep an in-memory trace for metrics. Never persisted, never logged:
        ``parent_text`` is live transcript (section 17.8)."""
        self.state.turns.append(
            ConversationTurn(
                index=len(self.state.turns),
                parent_text=parent_text,
                intent=turn.intent,
                intent_confidence=turn.intent_confidence,
                decision=turn.decision,
            )
        )

    async def _route(self, cleaned: str) -> ControllerTurn:
        self._parent_spoke = True
        date_context = (
            self.state.payment_date_questions_asked > 0
            or self.state.clarification_for_date_asked
            or self._awaiting_callback_time
        )
        classification = classify_intent(cleaned, today=self.today, date_context=date_context)
        intent = classification.intent
        signals = classification.signals

        self.phase = ConversationPhase.CLASSIFYING_INTENT

        # ---- rules that win over everything else (section 16.1, section 18)
        if Intent.DO_NOT_CALL in signals:
            return self._do_not_call()
        if intent is Intent.WRONG_NUMBER:
            return self._wrong_number_turn()
        if intent is Intent.ASKED_TO_STOP:
            turn = self._decide(
                "asked_to_stop", intent=Intent.ASKED_TO_STOP, end=True, is_closing=True
            )
            return turn or self._silence_turn(Intent.ASKED_TO_STOP)
        if intent is Intent.GREETING_ONLY:
            # Not an answer to anything. Stay listening; silence handles a stall.
            self.phase = ConversationPhase.WAITING_FOR_PARENT_TURN
            return ControllerTurn(
                decision=ReplyDecision(text="", source=ResponseSource.SCRIPTED,
                                       intent=Intent.GREETING_ONLY),
                intent=Intent.GREETING_ONLY,
                intent_confidence=classification.confidence,
                requires_persistence=self._dirty,
            )

        # ---- "when should we call you back?" was answered. Checked before the
        # identity gate, because a third party never passes it.
        if self._awaiting_callback_time and self._records_callback(cleaned, classification):
            return self._close_with_callback(cleaned, classification)

        # ---- the gate itself: nothing below may run before it opens
        if not self.state.identity_confirmed:
            return await self._identity_phase(cleaned, classification)

        # ---- the principal permission question Veda asked earlier
        if self._awaiting_principal_permission:
            turn = self._principal_permission(cleaned, classification)
            if turn is not None:
                return turn

        # ---- deterministic answers (section 12.4: the minority that is safe
        # to answer in code, because a mistake would be costly)
        if intent is Intent.PAYMENT_DATE_GIVEN and classification.date and classification.date.is_reliable:
            return self._accept_date(classification)
        if intent is Intent.ALREADY_PAID:
            return self._already_paid(classification)
        if intent is Intent.PREVIOUS_REMINDER:
            return self._previous_reminder(classification)
        if intent is Intent.PRINCIPAL_REQUEST:
            return self._principal_request(classification)
        if intent is Intent.ILLNESS_OR_EMERGENCY:
            return self._illness(classification)
        if intent is Intent.FINANCIAL_REASON:
            return self._financial(classification)
        if intent is Intent.BUSY_OR_CALLBACK:
            return self._busy(classification)
        if intent is Intent.AI_QUESTION:
            key = "ai_identity" if self.state.identity_confirmed else "who_is_calling"
            turn = self._decide(key, intent=Intent.AI_QUESTION, confidence=classification.confidence)
            if turn is not None:
                return turn
        if intent is Intent.WHO_IS_CALLING:
            turn = self._decide(
                "who_is_calling_after_identity",
                intent=Intent.WHO_IS_CALLING,
                confidence=classification.confidence,
            )
            if turn is not None:
                return turn
        if intent is Intent.VAGUE_ANSWER:
            turn = self._vague(classification)
            if turn is not None:
                return turn
        if intent is Intent.IRRITATED:
            turn = self._decide(
                "irritated_acknowledge",
                intent=Intent.IRRITATED,
                confidence=classification.confidence,
            )
            if turn is not None:
                return turn
        if intent is Intent.FEE_QUESTION:
            turn = self._decide(
                "fee_question_answer",
                intent=Intent.FEE_QUESTION,
                confidence=classification.confidence,
            )
            if turn is not None:
                return turn
        if intent is Intent.OFF_TOPIC:
            turn = self._off_topic(classification)
            if turn is not None:
                return turn

        # ---- everything the model may word (section 12.4: the majority)
        return await self._model_turn(cleaned, classification)

    # ---------------------------------------------------- identity-phase turns
    async def _identity_phase(self, cleaned: str, classification) -> ControllerTurn:
        """Anything said before the gate opens. No amount, ever."""
        # A caller may route identity turns here instead of through
        # ``on_identity_turn``; accept a clear confirmation rather than
        # re-asking a parent who already said yes.
        answer = classify_identity_answer(cleaned)
        if answer.confirmed:
            return self._confirm_identity(classification.confidence)
        if answer.wrong_number:
            return self._wrong_number_turn()
        if answer.denied:
            return self._third_party_turn(classification.confidence)

        intent = classification.intent
        if intent in (Intent.AI_QUESTION, Intent.WHO_IS_CALLING, Intent.IRRITATED):
            key = "who_is_calling" if intent is not Intent.AI_QUESTION else "ai_identity"
            turn = self._decide(key, intent=intent, confidence=classification.confidence)
            if turn is not None:
                return turn
        # One re-ask, then close without disclosing anything.
        if self._identity_reasks < 1:
            self._identity_reasks += 1
            turn = self._decide("identity_reask", intent=Intent.IDENTITY_CONFIRMED)
            if turn is not None:
                # Still collecting identity, so the caller keeps using
                # on_identity_turn for the next answer.
                self.phase = ConversationPhase.WAITING_FOR_IDENTITY_RESPONSE
                return turn
        self._identity_unconfirmed = True
        self._final = (Outcome.COMPLETED, ReasonCode.NO_DATE_GIVEN)
        closing = self._rotating_closing(end=True)
        return closing or self._silence_turn(intent)

    # -------------------------------------------------------- deterministic set
    def _accept_date(self, classification) -> ControllerTurn:
        extraction = classification.date
        self.state.promised = PaymentCommitment(
            promised_date=extraction.date,
            raw_text=extraction.matched_text,
            confidence=extraction.confidence,
        )
        self._dirty = True
        turn = self._decide(
            "date_accepted",
            intent=Intent.PAYMENT_DATE_GIVEN,
            confidence=classification.confidence,
            end=True,
            is_closing=True,
            outcome=Outcome.COMPLETED,
            reason=ReasonCode.DATE_PROMISED,
        )
        # Priority rule 4: the date is spoken about only once this state is
        # persisted, which is what requires_persistence tells the caller.
        return turn or self._silence_turn(Intent.PAYMENT_DATE_GIVEN)

    def _already_paid(self, classification) -> ControllerTurn:
        self.state.already_paid_claim = True
        self._dirty = True
        turn = self._decide(
            "already_paid",
            intent=Intent.ALREADY_PAID,
            confidence=classification.confidence,
            end=True,
            is_closing=True,
            outcome=Outcome.COMPLETED,
            reason=ReasonCode.ALREADY_PAID,
        )
        return turn or self._silence_turn(Intent.ALREADY_PAID)

    def _previous_reminder(self, classification) -> ControllerTurn:
        """The owner-approved wording for "you already called me" (case A)."""
        if self.state.promised.promised_date is not None:
            key = "previous_reminder_with_date"
            turn = self._decide(key, intent=Intent.PREVIOUS_REMINDER,
                                confidence=classification.confidence, end=True, is_closing=True)
            if turn is not None:
                return turn
        if can_ask_payment_date(self.state.payment_date_questions_asked):
            turn = self._decide(
                "previous_reminder_ask_date",
                intent=Intent.PREVIOUS_REMINDER,
                confidence=classification.confidence,
                is_question=True,
                asks_payment_date=True,
            )
            if turn is not None:
                return turn
        turn = self._decide(
            "previous_reminder_no_date", intent=Intent.PREVIOUS_REMINDER,
            confidence=classification.confidence, end=True, is_closing=True,
        )
        return turn or self._silence_turn(Intent.PREVIOUS_REMINDER)

    def _principal_request(self, classification) -> ControllerTurn:
        """Case C: record the request first, then ask permission to relay it."""
        self.state.principal_request = True
        self._dirty = True
        turn = self._decide(
            "principal_request",
            intent=Intent.PRINCIPAL_REQUEST,
            confidence=classification.confidence,
            is_question=True,
        )
        if turn is None:
            # Could not ask permission; still recorded, and the outcome reflects
            # the request rather than pretending nothing was said.
            self._final = (Outcome.COMPLETED, ReasonCode.PRINCIPAL_REQUESTED)
            return self._silence_turn(Intent.PRINCIPAL_REQUEST)
        self._awaiting_principal_permission = True
        return turn

    def _principal_permission(self, cleaned: str, classification) -> Optional[ControllerTurn]:
        if _YES_RE.search(cleaned):
            self._awaiting_principal_permission = False
            turn = self._decide(
                "principal_agreed", intent=Intent.PRINCIPAL_REQUEST, end=True, is_closing=True,
                outcome=Outcome.COMPLETED, reason=ReasonCode.PRINCIPAL_REQUESTED,
            )
            return turn or self._silence_turn(Intent.PRINCIPAL_REQUEST)
        if _NO_RE.search(cleaned):
            self._awaiting_principal_permission = False
            turn = self._decide(
                "principal_declined", intent=Intent.PRINCIPAL_REQUEST, end=True, is_closing=True,
                outcome=Outcome.COMPLETED, reason=ReasonCode.PRINCIPAL_REQUESTED,
            )
            return turn or self._silence_turn(Intent.PRINCIPAL_REQUEST)
        # An answer we cannot read: let the normal routing handle it.
        return None

    def _illness(self, classification) -> ControllerTurn:
        self.state.illness_or_emergency = True
        self._dirty = True
        turn = self._decide(
            "illness_emergency",
            intent=Intent.ILLNESS_OR_EMERGENCY,
            confidence=classification.confidence,
            end=True,
            is_closing=True,
            outcome=Outcome.COMPLETED,
            reason=ReasonCode.ILLNESS_OR_EMERGENCY,
        )
        return turn or self._silence_turn(Intent.ILLNESS_OR_EMERGENCY)

    def _financial(self, classification) -> ControllerTurn:
        self.state.financial_reason = True
        self._dirty = True
        turn = self._decide(
            "financial_reason",
            intent=Intent.FINANCIAL_REASON,
            confidence=classification.confidence,
            end=True,
            is_closing=True,
            outcome=Outcome.COMPLETED,
            reason=ReasonCode.FINANCIAL_REASON,
        )
        return turn or self._silence_turn(Intent.FINANCIAL_REASON)

    def _busy(self, classification) -> ControllerTurn:
        turn = self._decide(
            "busy_callback",
            intent=Intent.BUSY_OR_CALLBACK,
            confidence=classification.confidence,
            is_question=True,
        )
        if turn is None:
            return self._silence_turn(Intent.BUSY_OR_CALLBACK)
        self._awaiting_callback_time = True
        return turn

    def _records_callback(self, cleaned: str, classification) -> bool:
        """True when this turn answers "when should we call you back?"."""
        if classification.intent in (
            Intent.DO_NOT_CALL, Intent.ASKED_TO_STOP, Intent.WRONG_NUMBER,
            Intent.PAYMENT_DATE_GIVEN, Intent.ALREADY_PAID,
        ):
            return False
        return bool(_TIME_HINT_RE.search(cleaned))

    def _close_with_callback(self, cleaned: str, classification) -> ControllerTurn:
        self.state.callback = CallbackRequest(
            callback_time_text=cleaned[:120],
            from_third_party=self.state.third_party_answered,
        )
        self._dirty = True
        self._awaiting_callback_time = False
        outcome = Outcome.NOT_PARENT if self.state.third_party_answered else Outcome.CALLBACK_REQUESTED
        reason = (
            ReasonCode.THIRD_PARTY_ANSWERED
            if self.state.third_party_answered
            else ReasonCode.CALLBACK_TIME_GIVEN
        )
        self._final = (outcome, reason)
        closing = self._rotating_closing(end=True, outcome=outcome, reason=reason)
        return closing or self._silence_turn(Intent.BUSY_OR_CALLBACK)

    def _vague(self, classification) -> Optional[ControllerTurn]:
        if can_ask_payment_date(self.state.payment_date_questions_asked):
            self.state.clarification_for_date_asked = True
            self._dirty = True
            return self._decide(
                "vague_first_time",
                intent=Intent.VAGUE_ANSWER,
                confidence=classification.confidence,
                is_question=True,
                asks_payment_date=True,
            )
        # Both questions spent: stop asking and let the model close politely.
        return None

    def _off_topic(self, classification) -> Optional[ControllerTurn]:
        self.state.off_topic_turns += 1
        self._dirty = True
        if should_close_off_topic(self.state.off_topic_turns):
            self._off_topic_closed = True
            self._final = (Outcome.OFF_TOPIC_LIMIT, ReasonCode.NO_DATE_GIVEN)
            return self._decide(
                "off_topic_close",
                intent=Intent.OFF_TOPIC,
                confidence=classification.confidence,
                end=True,
                is_closing=True,
                outcome=Outcome.OFF_TOPIC_LIMIT,
                reason=ReasonCode.NO_DATE_GIVEN,
            )
        return None

    def _rotating_closing(
        self,
        *,
        end: bool = True,
        outcome: Optional[Outcome] = None,
        reason: Optional[ReasonCode] = None,
    ) -> Optional[ControllerTurn]:
        """One of a few cacheable closings, never the previous reply's opening."""
        for _ in range(len(self.responses.templates)):
            key = self.responses.closing_key(self._closing_index)
            self._closing_index += 1
            turn = self._decide(
                key,
                source=ResponseSource.AUDIO_CACHE,
                intent=Intent.ASKED_TO_STOP,
                end=end,
                is_closing=True,
                outcome=outcome,
                reason=reason,
            )
            if turn is not None:
                return turn
        return None

    def _end_no_response(self) -> ControllerTurn:
        decision = ReplyDecision(
            text="", source=ResponseSource.FALLBACK, intent=Intent.SILENCE, end_call_after=True
        )
        return ControllerTurn(decision=decision, intent=Intent.SILENCE,
                              requires_persistence=self._dirty)

    # ------------------------------------------------------------------- model
    async def _model_turn(self, cleaned: str, classification) -> ControllerTurn:
        intent = classification.intent
        llm = self.llm
        if llm is None or not getattr(llm, "configured", True):
            return self._fallback(intent, classification.confidence)

        messages = build_messages(
            self.state.facts,
            cleaned,
            max_chars=getattr(self.settings, "llm_max_context_chars", 0) if self.settings else 0,
            history=self._history,
        )
        try:
            reply = await llm.complete_reply(
                messages,
                temperature=getattr(getattr(self.settings, "base", None), "llm_temperature", 0.6)
                if self.settings else 0.6,
                max_tokens=getattr(getattr(self.settings, "base", None), "llm_max_tokens", 300)
                if self.settings else 300,
            )
        except Exception:  # noqa: BLE001 - a model failure must never kill a call
            reply = None

        text = sanitise_reply(getattr(reply, "text", "") or "") if reply is not None else ""
        if not text or (reply is not None and getattr(reply, "error", None) is not None):
            return self._fallback(intent, classification.confidence)

        verdict = self._check(text)
        if not verdict.ok:
            self.rejected.extend(verdict.violations)
            return self._fallback(intent, classification.confidence, rejected=verdict.violations)

        decision = ReplyDecision(
            text=text,
            source=ResponseSource.LLM,
            intent=intent,
            is_question=text.strip().endswith("?"),
            end_call_after=should_close_off_topic(self.state.off_topic_turns),
        )
        self._remember(text)
        self._track(decision)
        self._history.append({"role": "user", "content": cleaned})
        self._history.append({"role": "assistant", "content": text})
        del self._history[:-8]
        return ControllerTurn(
            decision=decision,
            intent=intent,
            intent_confidence=classification.confidence,
            requires_persistence=self._dirty,
            rejected=tuple(verdict.violations),
        )

    def _fallback(
        self,
        intent: Intent,
        confidence: float = 0.0,
        rejected: Sequence[str] = (),
    ) -> ControllerTurn:
        """What Veda says when there is no model, or the model was rejected."""
        if self.state.identity_confirmed and intent is Intent.FEE_QUESTION:
            turn = self._decide("fee_question_answer", source=ResponseSource.FALLBACK, intent=intent)
            if turn is not None:
                return turn
        if intent is Intent.IRRITATED and self.state.identity_confirmed:
            turn = self._decide("irritated_acknowledge", source=ResponseSource.FALLBACK, intent=intent)
            if turn is not None:
                return turn
        if intent is Intent.AI_QUESTION:
            turn = self._decide("ai_identity", source=ResponseSource.FALLBACK, intent=intent)
            if turn is not None:
                return turn
        turn = self._silence_turn(intent)
        if rejected:
            self.rejected.extend(rejected)
        return ControllerTurn(
            decision=turn.decision,
            intent=intent,
            intent_confidence=confidence,
            requires_persistence=turn.requires_persistence,
            rejected=tuple(rejected),
        )

    # ----------------------------------------------------------------- outcome
    def derive_outcome(self) -> Tuple[Outcome, ReasonCode]:
        """The section 18 ladder, in priority order. Never guessed from text."""
        if self._final is not None:
            outcome, reason = self._final
            return outcome, reason or self._default_reason(outcome)

        state = self.state
        if state.do_not_call:
            return Outcome.DO_NOT_CALL, ReasonCode.PARENT_REQUESTED_NO_MORE_CALLS
        if self._wrong_number:
            return Outcome.WRONG_NUMBER, ReasonCode.WRONG_PERSON
        if state.third_party_answered:
            return Outcome.NOT_PARENT, ReasonCode.THIRD_PARTY_ANSWERED
        if state.already_paid_claim:
            return Outcome.COMPLETED, ReasonCode.ALREADY_PAID
        if state.illness_or_emergency:
            return Outcome.COMPLETED, ReasonCode.ILLNESS_OR_EMERGENCY
        if state.principal_request:
            return Outcome.COMPLETED, ReasonCode.PRINCIPAL_REQUESTED
        if state.promised.promised_date is not None:
            return Outcome.COMPLETED, ReasonCode.DATE_PROMISED
        if state.financial_reason:
            return Outcome.COMPLETED, ReasonCode.FINANCIAL_REASON
        if state.parent_declined:
            return Outcome.COMPLETED, ReasonCode.PARENT_DECLINED
        if state.callback.callback_time_text:
            return Outcome.CALLBACK_REQUESTED, ReasonCode.CALLBACK_TIME_GIVEN
        if self._off_topic_closed:
            return Outcome.OFF_TOPIC_LIMIT, ReasonCode.NO_DATE_GIVEN
        if self._identity_unconfirmed:
            return Outcome.COMPLETED, ReasonCode.NO_DATE_GIVEN
        if not self._parent_spoke:
            return Outcome.NO_RESPONSE, ReasonCode.RING_TIMEOUT
        if state.off_topic_turns > 0:
            return Outcome.OFF_TOPIC_LIMIT, ReasonCode.NO_DATE_GIVEN
        return Outcome.COMPLETED, ReasonCode.NO_DATE_GIVEN

    @staticmethod
    def _default_reason(outcome: Outcome) -> Optional[ReasonCode]:
        from ..domain.enums import DEFAULT_REASON_FOR_OUTCOME

        return DEFAULT_REASON_FOR_OUTCOME.get(outcome)

    def finalize(
        self,
        *,
        outcome: Optional[Outcome] = None,
        reason: Optional[ReasonCode] = None,
        detail: str = "",
    ) -> CallOutcomeRecord:
        """The immutable record of how the call ended (section 7.9).

        ``outcome``/``reason`` let the call controller override the derivation
        for telephony-only endings (ring timeout, provider failure, operator
        cancel); an override is checked against the legal combinations rather
        than trusted.
        """
        if outcome is None:
            derived_outcome, derived_reason = self.derive_outcome()
            outcome = derived_outcome
            reason = reason or derived_reason
        else:
            reason = reason or self._default_reason(outcome)
        validate_outcome_reason(outcome, reason)

        if detail == "" and self._identity_unconfirmed:
            detail = "identity never confirmed; no fee disclosed"
        if detail == "" and self._wrong_number:
            detail = "parent reported a wrong number"

        record = replace(
            self.state.to_outcome_record(),
            outcome=outcome,
            reason_code=reason,
            outcome_detail=detail,
        )
        self.phase = ConversationPhase.DONE
        return record
