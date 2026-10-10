"""Schema migrations, driven from Python so the operator never types Alembic.

Alembic is the migration engine, but the operator runs ``python -m app.cli db
upgrade``. Two details are deliberate:

* the database URL is never read from ``alembic.ini``; it is injected from the
  validated settings, so no password is ever written into a versioned file.
* :func:`create_schema_for_tests` builds the same schema on an async
  connection, so the offline suite can exercise the real constraints and
  triggers without a database server.

The first revision applies the declarative metadata plus the safety triggers.
Doing that in one place keeps a single source of truth for the schema; later
revisions will be explicit so a review can see exactly what changed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from app.config.settings import Stage1Settings, load_stage1_settings

ALEMBIC_DIR = Path(__file__).resolve().parents[3] / "alembic"
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied."""


def _alembic_config(database_url: str):
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI) if ALEMBIC_INI.exists() else None)
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    # Injected, never read from a file: a password must not live in alembic.ini.
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_to_head(
    *,
    settings: Optional[Stage1Settings] = None,
    database_url: Optional[str] = None,
) -> str:
    """Apply every migration. Returns the resulting revision.

    Must be called outside a running event loop (Alembic drives its own async
    engine internally); the CLI does that before it starts the service.
    """
    from alembic import command
    from alembic.util.exc import CommandError

    # An explicit URL means the caller already knows the target, so the ambient
    # environment is not read at all (a stray $PORT must not stop a migration).
    url = database_url or (settings or load_stage1_settings(require_secrets=False)).database_url
    config = _alembic_config(url)
    try:
        command.upgrade(config, "head")
    except CommandError as exc:  # pragma: no cover - requires a broken install
        raise MigrationError(f"could not apply migrations: {exc}") from exc
    return current_revision(settings=settings, database_url=url)


def current_revision(
    *,
    settings: Optional[Stage1Settings] = None,
    database_url: Optional[str] = None,
) -> str:
    """The revision the database is currently at, or 'unknown'."""
    from alembic.runtime.migration import MigrationContext

    url = database_url or (settings or load_stage1_settings(require_secrets=False)).database_url

    async def _read() -> str:
        # Read through the async engine, so no extra synchronous driver (and no
        # extra dependency) is needed just to print a revision.
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                revision = await connection.run_sync(
                    lambda sync_connection: MigrationContext.configure(
                        sync_connection
                    ).get_current_revision()
                )
                return revision or "none"
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_read())
    except Exception:
        # Reading the revision is a diagnostic; a failure must not be fatal, and
        # inside a running event loop it is simply unavailable.
        return "unknown"


async def create_schema_for_tests(database) -> None:
    """Build the schema on an async engine (offline tests only)."""
    from .schema import create_schema

    async with database.engine.begin() as connection:
        await connection.run_sync(create_schema)


async def drop_schema_for_tests(database) -> None:
    from .schema import drop_schema

    async with database.engine.begin() as connection:
        await connection.run_sync(drop_schema)


def apply_migrations_sync(settings: Optional[Stage1Settings] = None) -> str:
    """Convenience wrapper used by the CLI when no loop is running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return upgrade_to_head(settings=settings)
    raise MigrationError(
        "run migrations before starting the event loop (use the CLI, not a coroutine)"
    )
