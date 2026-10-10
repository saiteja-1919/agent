"""Initial Veda Stage 1 schema, including the database-level safety triggers.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-10

The tables come from the declarative metadata so the schema has exactly one
definition; the triggers come from
:mod:`app.integrations.database.schema`, which emits the PostgreSQL form in
production and the SQLite form in the offline tests. Later revisions will be
written out explicitly so a reviewer can see each change.

What this creates, and why it is safe:

* ``uq_attempt_per_task`` - a second call attempt for one reminder task is
  impossible at the database level.
* ``trg_forbid_outcome_change`` - a recorded outcome can never be overwritten.
* ``trg_forbid_attempt_reassign`` - an attempt can never be moved to a different
  task, which would otherwise be a way to smuggle a second call onto a task.
* no transcript table and no recording table exist at all.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.integrations.database.schema import create_schema, drop_schema

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    create_schema(op.get_bind())


def downgrade() -> None:
    drop_schema(op.get_bind())
