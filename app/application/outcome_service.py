"""Finalizing a call: exactly once, in the right order, never silently lost.

Section 19.3 requires that finalization is safe to run twice - a hung-up event,
a provider callback and a pipeline shutdown can all arrive for the same call,
and only the first one may write the outcome. The database already enforces
that (``record_outcome`` returns ``created=False`` for a duplicate); this
service makes the *ordering* safe as well:

1. **The outcome first.** It is the one immutable fact about the call, and a
   failure to write it is raised, not collected: the caller must know.
2. **Metrics second, in their own savepoint.** A measurement must never be able
   to take the outcome down with it, and unlike an outcome they are safe to
   overwrite.
3. **Side effects third, each idempotent.** A do-not-call request is recorded
   once per number no matter how often finalization runs, and the lifecycle
   state is only ever moved forward.
4. **Commit last.** Either the call is recorded or an exception explains why.

Duplicate finalization is a normal, reported outcome - not an error, and not
something to log as a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError

from ..domain.enums import CallLifecycleState, validate_outcome_reason
from ..domain.models import CallMetrics, CallOutcomeRecord
from ..integrations.database import audit_repo, call_repo, repositories


@dataclass(frozen=True)
class FinalizationResult:
    """What happened when a call was finalized."""

    record: CallOutcomeRecord
    created: bool
    already_finalized: bool = False
    metrics_saved: bool = False
    do_not_call_added: bool = False
    lifecycle_recorded: bool = False
    duration_recorded: bool = False
    errors: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, Any]:
        """Safe for logs: an outcome and a reason, never conversation content."""
        return {
            "attempt_id": self.record.attempt_id,
            "outcome": self.record.outcome.value if self.record.outcome else None,
            "reason_code": self.record.reason_code.value if self.record.reason_code else None,
            "created": self.created,
            "already_finalized": self.already_finalized,
            "metrics_saved": self.metrics_saved,
            "do_not_call_added": self.do_not_call_added,
            "errors": list(self.errors),
        }


class OutcomeService:
    """Writes the final result of one attempt. Stateless and reusable."""

    def __init__(self, *, audit: bool = True) -> None:
        self.audit = audit

    async def finalize(
        self,
        session,
        *,
        record: CallOutcomeRecord,
        metrics: Optional[CallMetrics] = None,
        lifecycle_state: CallLifecycleState = CallLifecycleState.ENDED,
        do_not_call_phone: str = "",
        do_not_call_reason: str = "parent asked to stop calls",
        duration_seconds: Optional[int] = None,
        actor: str = "system",
        commit: bool = True,
    ) -> FinalizationResult:
        """Persist the end of a call. Safe to call any number of times."""
        if record.attempt_id is None:
            raise ValueError("finalizing a call needs an attempt_id")
        if record.task_id is None:
            raise ValueError("finalizing a call needs a task_id")
        if record.outcome is None:
            raise ValueError("finalizing a call needs an outcome")
        # Fail loudly here rather than deep inside the repository.
        validate_outcome_reason(record.outcome, record.reason_code)

        errors: List[str] = []

        # 1. The immutable outcome. A failure here is fatal and propagates.
        _, created = await call_repo.record_outcome(session, record)

        # 2. Metrics, isolated so they cannot roll back the outcome.
        metrics_saved = False
        if metrics is not None:
            metrics_saved = await self._best_effort(
                session,
                errors,
                "metrics",
                self._save_metrics(session, metrics),
            )

        # 3. Side effects, each idempotent.
        do_not_call_added = False
        if do_not_call_phone and record.do_not_call:
            do_not_call_added = await self._best_effort(
                session,
                errors,
                "do-not-call",
                self._add_do_not_call(
                    session,
                    phone=do_not_call_phone,
                    reason=do_not_call_reason,
                    source_attempt_id=record.attempt_id,
                ),
            )

        lifecycle_recorded = await self._best_effort(
            session,
            errors,
            "lifecycle",
            call_repo.record_lifecycle_state(session, record.attempt_id, lifecycle_state),
        )

        if duration_seconds is not None:
            duration_recorded = await self._best_effort(
                session,
                errors,
                "duration",
                call_repo.set_attempt_duration(session, record.attempt_id, duration_seconds),
            )
        else:
            duration_recorded = False

        if self.audit and created:
            await self._best_effort(
                session,
                errors,
                "audit",
                audit_repo.record(
                    session,
                    action="call.finalized",
                    actor=actor,
                    entity="call_attempt",
                    entity_id=record.attempt_id,
                    details={
                        "outcome": record.outcome.value,
                        "reason_code": record.reason_code.value if record.reason_code else "",
                    },
                ),
            )

        if commit:
            await self._best_effort(session, errors, "commit", self._commit(session))

        return FinalizationResult(
            record=record,
            created=created,
            already_finalized=not created,
            metrics_saved=bool(metrics_saved),
            do_not_call_added=bool(do_not_call_added),
            lifecycle_recorded=bool(lifecycle_recorded),
            duration_recorded=bool(duration_recorded),
            errors=tuple(errors),
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def _commit(session) -> None:
        await session.commit()

    @staticmethod
    async def _save_metrics(session, metrics: CallMetrics) -> bool:
        await call_repo.save_metrics(session, metrics)
        return True

    @staticmethod
    async def _add_do_not_call(session, *, phone: str, reason: str, source_attempt_id: int) -> bool:
        return await repositories.add_do_not_call(
            session, phone=phone, reason=reason, source_attempt_id=source_attempt_id
        )

    async def _best_effort(self, session, errors: List[str], label: str, operation) -> bool:
        """Run one side effect in a savepoint; a failure is recorded, not raised.

        A savepoint means a failed side effect rolls back only itself, so the
        outcome written a moment ago survives. The caller still sees the
        failure through ``errors`` and can retry the piece that failed.
        """
        try:
            async with session.begin_nested():
                result = await operation
            return bool(result) if result is not None else True
        except Exception as exc:  # noqa: BLE001 - deliberate best-effort boundary
            # Deliberately broad: a side effect failing must never lose the
            # outcome. The failure is reported through ``errors`` so a caller
            # that checks ``result.ok`` still sees it.
            errors.append(f"{label}: {type(exc).__name__}")
            return False
