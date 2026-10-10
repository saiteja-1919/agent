"""Async database access for Veda.

Design notes that matter on this platform:

* The URL is ``postgresql+asyncpg://`` in production. Appendix A pitfall 19:
  psycopg 3's async mode does not work on the default Windows event loop, so the
  settings layer refuses that driver rather than letting it fail inside a call.
* ``pool_pre_ping`` is on: a school office PC and a local database will lose
  connections to sleep/lock screens, and a stale connection must be recycled
  instead of surfacing as a failed call.
* Every session is short-lived and its own transaction. No provider request is
  ever made while a transaction is open (section 20).
* SQLite is supported *only* so the offline test suite can exercise the real
  repositories and constraints without a server. It enables foreign keys
  explicitly, because SQLite does not enforce them by default - a test that
  passed without that would prove nothing.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config.settings import Stage1Settings


def build_engine(settings: Stage1Settings) -> AsyncEngine:
    """Create the async engine for the configured database."""
    url = settings.database_url
    options: Dict[str, Any] = {"echo": False, "pool_pre_ping": True, "future": True}

    if url.startswith("postgresql"):
        options.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=1800,
            connect_args={
                "server_settings": {
                    # A runaway query must not hold a call's transaction open.
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                    "application_name": "veda",
                },
                "timeout": settings.provider_timeout_secs,
            },
        )
    else:
        # SQLite (tests/dev only). One shared connection so an in-memory
        # database is visible to every session.
        options.update(
            connect_args={"timeout": 30},
            poolclass=StaticPool,
        )

    engine = create_async_engine(url, **options)

    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            try:
                # Without this, foreign keys are advisory on SQLite and the
                # one-attempt-per-task guarantee would not be tested at all.
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    return engine


@dataclass
class Database:
    """The engine plus its session factory, with a health check."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    @classmethod
    def from_settings(cls, settings: Stage1Settings) -> "Database":
        engine = build_engine(settings)
        return cls(
            engine=engine,
            session_factory=async_sessionmaker(
                engine, expire_on_commit=False, autoflush=False
            ),
        )

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A short-lived session. Commits on success, rolls back on any error."""
        session = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            with contextlib.suppress(Exception):
                await session.rollback()
            raise
        finally:
            await session.close()

    async def healthy(self) -> bool:
        """True when a trivial query succeeds. Never raises."""
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()

    @property
    def dialect_name(self) -> str:
        return self.engine.dialect.name


def database_from_url(url: str, **overrides: Any) -> Database:
    """Build a Database from a bare URL (tests and ad-hoc tools)."""
    settings = Stage1Settings.from_env({"DATABASE_URL": url}).with_overrides(**overrides)
    return Database.from_settings(settings)
