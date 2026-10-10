"""Metadata for cached audio. The bytes live on disk; the truth lives here.

Keeping the metadata in the database rather than beside the files means a
deleted or half-written file is detected instead of silently served, an old
template version can be invalidated with one statement, and the office can see
what the system has cached without reading a directory.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import utcnow

from .tables import AudioAsset


async def get_asset(session: AsyncSession, cache_key: str) -> Optional[AudioAsset]:
    return (
        await session.execute(select(AudioAsset).where(AudioAsset.cache_key == cache_key))
    ).scalar_one_or_none()


async def upsert_asset(
    session: AsyncSession,
    *,
    cache_key: str,
    template_key: str,
    text: str,
    text_checksum: str,
    language: str,
    voice: str,
    model: str,
    audio_format: str,
    sample_rate: int,
    storage_path: str,
    file_checksum: str,
    file_size: int,
    duration_ms: Optional[int],
    template_version: str,
) -> AudioAsset:
    """Record one cached file, replacing any earlier entry for the same key."""
    existing = await get_asset(session, cache_key)
    if existing is not None:
        existing.template_key = template_key
        existing.text = text
        existing.text_checksum = text_checksum
        existing.language = language
        existing.voice = voice
        existing.model = model
        existing.audio_format = audio_format
        existing.sample_rate = sample_rate
        existing.storage_path = storage_path
        existing.file_checksum = file_checksum
        existing.file_size = file_size
        existing.duration_ms = duration_ms
        existing.template_version = template_version
        existing.invalidated_at = None
        await session.flush()
        return existing

    asset = AudioAsset(
        cache_key=cache_key,
        template_key=template_key,
        text=text,
        text_checksum=text_checksum,
        language=language,
        voice=voice,
        model=model,
        audio_format=audio_format,
        sample_rate=sample_rate,
        storage_path=storage_path,
        file_checksum=file_checksum,
        file_size=file_size,
        duration_ms=duration_ms,
        template_version=template_version,
        created_at=utcnow(),
    )
    try:
        # Savepoint: a duplicate key here is another writer winning, and that
        # must not discard whatever the caller already did in this transaction.
        async with session.begin_nested():
            session.add(asset)
            await session.flush()
    except IntegrityError:
        return await get_asset(session, cache_key) or asset
    return asset


async def touch_asset(session: AsyncSession, asset_id: int, *, used_at: Optional[datetime] = None) -> None:
    """Count a use. Cheap, and the only write a cache hit performs."""
    await session.execute(
        update(AudioAsset)
        .where(AudioAsset.id == asset_id)
        .values(use_count=AudioAsset.use_count + 1, last_used_at=used_at or utcnow())
    )


async def invalidate_asset(session: AsyncSession, asset_id: int) -> None:
    """Mark an entry unusable. The row is kept so a bad file is not re-found."""
    await session.execute(
        update(AudioAsset).where(AudioAsset.id == asset_id).values(invalidated_at=utcnow())
    )


async def invalidate_template(
    session: AsyncSession, template_key: str, *, limit: int = 500
) -> List[AudioAsset]:
    """Invalidate every cached file for one approved phrase."""
    rows = (
        await session.execute(
            select(AudioAsset)
            .where(AudioAsset.template_key == template_key, AudioAsset.invalidated_at.is_(None))
            .limit(limit)
        )
    ).scalars().all()
    for row in rows:
        await invalidate_asset(session, row.id)
    return list(rows)


async def invalidate_version(
    session: AsyncSession, template_version: str, *, limit: int = 1000
) -> List[AudioAsset]:
    """Invalidate everything cached under an older prompt/template version."""
    rows = (
        await session.execute(
            select(AudioAsset)
            .where(AudioAsset.template_version != template_version, AudioAsset.invalidated_at.is_(None))
            .limit(limit)
        )
    ).scalars().all()
    for row in rows:
        await invalidate_asset(session, row.id)
    return list(rows)


async def purge_invalidated(session: AsyncSession, *, limit: int = 1000) -> int:
    """Forget invalidated rows once their files are gone. Returns rows removed."""
    rows = (
        await session.execute(
            select(AudioAsset).where(AudioAsset.invalidated_at.is_not(None)).limit(limit)
        )
    ).scalars().all()
    ids = [row.id for row in rows]
    if ids:
        await session.execute(delete(AudioAsset).where(AudioAsset.id.in_(ids)))
    return len(ids)


async def assets(session: AsyncSession, *, limit: int = 100) -> List[AudioAsset]:
    return list((await session.execute(select(AudioAsset).limit(limit))).scalars().all())


async def summary(session: AsyncSession) -> dict:
    """Counts for the operational endpoints and the cache report."""
    rows = await assets(session, limit=10000)
    live = [row for row in rows if row.invalidated_at is None]
    return {
        "total": len(rows),
        "live": len(live),
        "invalidated": len(rows) - len(live),
        "bytes": sum(row.file_size or 0 for row in live),
        "hits": sum(row.use_count for row in rows),
    }
