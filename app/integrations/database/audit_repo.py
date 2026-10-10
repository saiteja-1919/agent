"""Audit trail: who did what to the system.

Section 22 asks for auditability. The events worth keeping are the ones that
change what happens to a real parent: an import, an authorized batch, a single
call, a cancellation, a do-not-call entry, a schema migration, a cache rebuild.

Details are stored as JSON with names and numbers masked, because an audit record
must not become a second copy of the personal data the system is refusing to keep.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import utcnow

from .tables import AuditLog


async def record(
    session: AsyncSession,
    *,
    action: str,
    actor: str = "operator",
    entity: str = "",
    entity_id: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
    correlation_id: str = "",
) -> AuditLog:
    """Write one audit line. Never raises for unusable details."""
    try:
        payload = json.dumps(details or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        payload = "{}"
    row = AuditLog(
        at=utcnow(),
        actor=actor[:64],
        action=action[:64],
        entity=entity[:64],
        entity_id=entity_id,
        details=payload,
        correlation_id=correlation_id[:64],
    )
    session.add(row)
    await session.flush()
    return row


async def recent(session: AsyncSession, *, limit: int = 50, action: Optional[str] = None) -> List[AuditLog]:
    statement = select(AuditLog).order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit)
    if action:
        statement = statement.where(AuditLog.action == action)
    return list((await session.execute(statement)).scalars().all())
