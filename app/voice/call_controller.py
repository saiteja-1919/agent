"""Driving one call attempt from dial to recorded outcome (sections 14 and 19).

This is the loop that owns the *order* of a call, and order is where the safety
rules live:

1. **Classify the dial before anything else.** A request that never reached the
   provider is a certain ``NOT_DIALED``; a request whose response we never saw
   is ``UNCERTAIN_NEEDS_REVIEW`` and is never retried. Neither case opens a
   conversation.
2. **Play the opening, unbroken, then allow barge-in.**
3. **Persist before speaking.** When the controller says ``requires_persistence``,
   the note is written and committed *before* the reply is played. If it cannot
   be written, Veda says nothing and ends the call rather than claim it noted
   something it did not (priority rule 4).
4. **Finalize exactly once.** Whatever ends the call - hangup, silence, the
   ceiling, a transport failure - the outcome is written through
   :class:`~app.application.outcome_service.OutcomeService`, which is safe to
   call twice.

The telephony side is behind :class:`MediaSession`: dialing, playback and the
event stream are all protocol methods, so the whole loop is tested with a fake
and no socket, no provider and no credentials are touched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, List, Optional, Protocol, Sequence

from ..application.conversation_controller import ConversationController, ControllerTurn
from ..application.outcome_service import FinalizationResult, OutcomeService
from ..cache.response_cache import ResponseCache
from ..domain.enums import CallLifecycleState, DialResult, Outcome, ReasonCode
from ..domain.models import (
    CallMetrics,
    ConversationState,
    ReplyDecision,
    VerifiedFacts,
)
from ..integrations.database import audit_repo, call_repo, repositories
from .chunker import Segment, chunk_reply

log = logging.getLogger(__name__)

#: How the media session reports what happened. One event, one word, no PII.
EVENT_TURN = "turn"
EVENT_SILENCE = "silence"
EVENT_HANGUP = "hangup"
EVENT_MAX_DURATION = "max_duration"
EVENT_PROVIDER_ERROR = "provider_error"
EVENT_SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class SessionEvent:
    """One thing the media session reports to the conversation loop."""

    kind: str
    text: str = ""
    detail: str = ""


@dataclass(frozen=True)
class PlayResult:
    """What happened while saying one reply."""

    cache_hit: bool = False
    characters: int = 0
    segments: int = 0


class MediaSession(Protocol):
    """Dialing, playback and the inbound event stream for one call."""

    async def dial(self, phone: str) -> Any: ...

    async def begin_opening(self) -> None: ...

    async def end_opening(self) -> None: ...

    async def play(self, decision: ReplyDecision, segments: Sequence[Segment]) -> PlayResult: ...

    async def next_event(self, timeout: float) -> SessionEvent: ...

    async def hangup(self) -> None: ...


@dataclass
class CallContext:
    """Everything one call needs, all of it per-call and never shared."""

    attempt_id: int
    task_id: int
    phone: str
    facts: VerifiedFacts
    student_id: Optional[int] = None
    state: ConversationState = field(default_factory=ConversationState)
    metrics: CallMetrics = field(default_factory=CallMetrics)
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        # The record and the metrics both key off the attempt; keeping them in
        # the state means the controller's own record is already complete.
        self.state.attempt_id = self.attempt_id
        self.state.task_id = self.task_id
        self.metrics.attempt_id = self.attempt_id
        self.metrics.task_id = self.task_id
        if not self.state.facts.school_name and self.facts.school_name:
            self.state.facts = self.facts


class CallController:
    """Runs one call attempt to a recorded outcome."""

    def __init__(
        self,
        *,
        database: Any,
        media: MediaSession,
        settings: Any = None,
        llm: Any = None,
        responses: Optional[ResponseCache] = None,
        conversation: Optional[ConversationController] = None,
        outcomes: Optional[OutcomeService] = None,
        clock=staticmethod(time.monotonic),
    ) -> None:
        self.database = database
        self.media = media
        self.settings = settings
        self.llm = llm
        self.clock = clock
        self.responses = responses or ResponseCache()
        self.conversation = conversation
        self.outcomes = outcomes or OutcomeService()
        self._final_result: Optional[FinalizationResult] = None
        self._veda_hung_up = False
        self._end_hint: Optional[tuple] = None

    # ------------------------------------------------------------------ config
    @property
    def opener_pause_secs(self) -> float:
        return float(getattr(self.settings, "opener_pause_secs", 0.7) or 0.0)

    @property
    def max_call_secs(self) -> float:
        base = getattr(self.settings, "base", None)
        return float(getattr(base, "call_timeout_secs", 600) or 600)

    @property
    def max_response_chars(self) -> int:
        return int(getattr(self.settings, "max_response_chars", 400) or 400)

    # --------------------------------------------------------------------- run
    async def run(self, ctx: CallContext) -> FinalizationResult:
        """Dial, converse, finalize. Always returns a result; never leaks."""
        if self.conversation is None:
            self.conversation = ConversationController(
                state=ctx.state, responses=self.responses, settings=self.settings, llm=self.llm
            )

        dial = await self._dial(ctx)
        await self._record_dial(ctx, dial)

        if dial.dial_result is not DialResult.DIALED:
            if dial.dial_result is DialResult.NOT_DIALED:
                return await self._finalize(
                    ctx, Outcome.PROVIDER_ERROR, ReasonCode.DIAL_FAILED,
                    detail="the provider never created the call",
                )
            return await self._finalize(
                ctx, Outcome.UNCERTAIN_NEEDS_REVIEW, ReasonCode.DIAL_RESULT_UNKNOWN,
                detail="the dial request outcome could not be determined",
            )

        try:
            await self._converse(ctx)
        except asyncio.CancelledError:
            await self._safe_hangup()
            return await self._finalize(
                ctx, Outcome.CANCELLED, ReasonCode.APPLICATION_SHUTDOWN,
                detail="the process shut down during the call",
            )
        except Exception as exc:  # noqa: BLE001 - a call must still be recorded
            log.error("call %s failed: %s", ctx.attempt_id, type(exc).__name__)
            ctx.metrics.bump("provider_failures")
            await self._safe_hangup()
            return await self._finalize(
                ctx, Outcome.PROVIDER_ERROR, ReasonCode.STREAM_FAILED,
                detail=f"the media session failed: {type(exc).__name__}",
            )

        outcome, reason = self.conversation.derive_outcome()
        return await self._finalize(ctx, outcome, reason)

    # ------------------------------------------------------------------- dial
    async def _dial(self, ctx: CallContext) -> Any:
        try:
            return await self.media.dial(ctx.phone)
        except Exception as exc:  # noqa: BLE001
            # The adapter raised: we cannot prove the provider did not create a
            # call, so this is never reported as a safe NOT_DIALED.
            log.error("dial raised for attempt %s: %s", ctx.attempt_id, type(exc).__name__)
            from ..integrations.exotel.client import DialOutcome

            return DialOutcome(dial_result=DialResult.UNCERTAIN_NEEDS_REVIEW)

    async def _record_dial(self, ctx: CallContext, dial: Any) -> None:
        sid = getattr(dial, "provider_call_sid", None) or ""
        try:
            async with self.database.session() as session:
                await call_repo.set_dial_result(
                    session,
                    ctx.attempt_id,
                    dial_result=dial.dial_result,
                    provider_call_sid=sid or None,
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - the dial itself still decides
            log.error("could not record the dial result: %s", type(exc).__name__)
            ctx.metrics.bump("database_failures")

    # -------------------------------------------------------------- converse
    async def _converse(self, ctx: CallContext) -> None:
        await self._set_state(ctx, CallLifecycleState.ANSWERED)
        conversation = self.conversation

        # The scripted opening, played whole (section 10.1).
        await self.media.begin_opening()
        opening = conversation.begin()
        if not await self._play(ctx, opening.line_1):
            return
        if self.opener_pause_secs:
            await asyncio.sleep(self.opener_pause_secs)
        if not await self._play(ctx, opening.line_2):
            return
        conversation.on_opening_line_played(1)
        await self.media.end_opening()

        deadline = ctx.started_at + self.max_call_secs
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                self._veda_hung_up = True
                self._end_with(
                    Outcome.CANCELLED, ReasonCode.MAX_DURATION_REACHED,
                    "the call reached its maximum duration",
                )
                return
            try:
                event = await self.media.next_event(min(remaining, 5.0))
            except asyncio.TimeoutError:
                continue

            turn = await self._handle(ctx, event)
            if turn is None:
                return

    async def _handle(self, ctx: CallContext, event: SessionEvent) -> Optional[ControllerTurn]:
        conversation = self.conversation

        if event.kind == EVENT_HANGUP:
            # Someone hung up. If we had already decided to end, our ending is
            # the true one; otherwise the parent walked away mid-call.
            if not self._veda_hung_up:
                self._end_with(
                    Outcome.PARENT_DISCONNECTED, ReasonCode.PARENT_SILENT,
                    "the parent hung up during the call",
                )
            return None
        if event.kind == EVENT_MAX_DURATION:
            self._veda_hung_up = True
            self._end_with(
                Outcome.CANCELLED, ReasonCode.MAX_DURATION_REACHED,
                "the call reached its maximum duration",
            )
            return None
        if event.kind == EVENT_SHUTDOWN:
            self._end_with(
                Outcome.CANCELLED, ReasonCode.APPLICATION_SHUTDOWN,
                "the application shut down during the call",
            )
            return None
        if event.kind == EVENT_PROVIDER_ERROR:
            ctx.metrics.bump("provider_failures")
            self._end_with(
                Outcome.PROVIDER_ERROR, ReasonCode.STREAM_FAILED,
                f"the media stream failed: {event.detail or 'unknown'}",
            )
            return None

        if event.kind == EVENT_SILENCE:
            turn = conversation.on_silence()
        elif event.kind == EVENT_TURN:
            turn = await conversation.on_parent_turn(event.text)
        else:  # pragma: no cover - an unknown event is not an answer
            return None

        if turn is None:
            # The controller has nothing it may say: both prompts are spent, or
            # no candidate passed policy. Its own _final outcome is used.
            return None

        if not await self._speak(ctx, turn):
            return None

        ctx.metrics.bump("reply_count", 1)
        if turn.intent and turn.intent.value not in ("greeting_only", "silence"):
            ctx.metrics.bump("turn_count", 1)

        if turn.ends:
            self._veda_hung_up = True
            return None
        return turn

    # ---------------------------------------------------------------- speaking
    async def _play(self, ctx: CallContext, decision: ReplyDecision) -> bool:
        segments = chunk_reply(decision.text, max_chars=self.max_response_chars)
        if not segments:
            return True
        result = await self.media.play(decision, segments)
        if getattr(result, "cache_hit", False):
            ctx.metrics.bump("audio_cache_hits", 1)
        else:
            ctx.metrics.bump("audio_cache_misses", 1)
        ctx.metrics.add_characters(len(decision.text))
        return True

    async def _speak(self, ctx: CallContext, turn: ControllerTurn) -> bool:
        """Speak one decision, or refuse to speak it at all."""
        if not turn.speaks:
            return True
        if turn.requires_persistence and not await self._persist(ctx, turn):
            # Priority rule 4: the note could not be saved, so the sentence
            # that refers to it must not be said. Ending quietly is the only
            # honest option left.
            log.error("attempt %s could not save its note; ending the call", ctx.attempt_id)
            ctx.metrics.bump("database_failures", 1)
            return False
        return await self._play(ctx, turn.decision)

    # ------------------------------------------------------------- persistence
    async def _persist(self, ctx: CallContext, turn: ControllerTurn) -> bool:
        """Write everything the reply refers to, once, before it is spoken."""
        state = ctx.state
        note = {
            "identity_confirmed": bool(state.identity_confirmed),
            "promised_date": state.promised.promised_date.isoformat()
            if state.promised.promised_date else "",
            "principal_request": bool(state.principal_request),
            "already_paid_claim": bool(state.already_paid_claim),
            "callback_time": bool(state.callback.callback_time_text),
            "payment_date_questions_asked": state.payment_date_questions_asked,
            "intent": turn.intent.value,
        }
        for _ in (1, 2):  # one retry: a transient failure must not eat a call
            try:
                async with self.database.session() as session:
                    if state.do_not_call and ctx.phone:
                        await repositories.add_do_not_call(
                            session,
                            phone=ctx.phone,
                            reason="parent asked to stop calls",
                            source_attempt_id=ctx.attempt_id,
                        )
                    await audit_repo.record(
                        session,
                        action="conversation.note",
                        actor="veda",
                        entity="call_attempt",
                        entity_id=ctx.attempt_id,
                        details=note,
                    )
                    await session.commit()
                self.conversation.mark_persisted()
                return True
            except Exception as exc:  # noqa: BLE001 - reported, then retried once
                log.error("persisting the note failed: %s", type(exc).__name__)
        return False

    async def _set_state(self, ctx: CallContext, state: CallLifecycleState) -> None:
        try:
            async with self.database.session() as session:
                await call_repo.record_lifecycle_state(session, ctx.attempt_id, state)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - lifecycle is bookkeeping
            log.error("could not record lifecycle state: %s", type(exc).__name__)
            ctx.metrics.bump("database_failures", 1)

    # -------------------------------------------------------------- finalizing
    def _end_with(self, outcome: Outcome, reason: ReasonCode, detail: str) -> None:
        """Record how the call ended when something other than the controller
        decided it (a hangup, a ceiling, a dead stream)."""
        self._end_hint = (outcome, reason, detail)

    async def _finalize(
        self,
        ctx: CallContext,
        outcome: Outcome,
        reason: ReasonCode,
        *,
        detail: str = "",
    ) -> FinalizationResult:
        if self._final_result is not None:
            return self._final_result

        hint = getattr(self, "_end_hint", None)
        if hint is not None:
            outcome, reason, hint_detail = hint
            detail = detail or hint_detail

        duration = max(0, int(self.clock() - ctx.started_at))
        ctx.metrics.call_duration_seconds = duration

        if self.conversation is None:  # pragma: no cover - run() always builds it
            record = ctx.state.to_outcome_record()
            record = replace(record, outcome=outcome, reason_code=reason, outcome_detail=detail)
        else:
            record = self.conversation.finalize(outcome=outcome, reason=reason, detail=detail)

        try:
            async with self.database.session() as session:
                result = await self.outcomes.finalize(
                    session,
                    record=record,
                    metrics=ctx.metrics,
                    duration_seconds=duration,
                    do_not_call_phone=ctx.phone if record.do_not_call else "",
                    actor="veda",
                )
        except Exception as exc:  # noqa: BLE001 - nothing else can be done here
            log.error("finalizing attempt %s failed: %s", ctx.attempt_id, type(exc).__name__)
            self._final_result = FinalizationResult(record=record, created=False,
                                                    errors=(f"finalize: {type(exc).__name__}",))
            return self._final_result

        self._final_result = result
        return result

    async def _safe_hangup(self) -> None:
        try:
            await self.media.hangup()
        except Exception:  # noqa: BLE001 - hanging up a dead call is not a failure
            pass
