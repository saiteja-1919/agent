"""Database-level guarantees that Python must not be trusted to keep.

Section 0.2 requires the one-attempt-per-task rule to be enforced *in the
database*, not only in Python, and section 10 requires a final outcome to be
immutable. Both are enforced here so that a bug, a race between two workers, or
a restart mid-call cannot break them.

The same file emits PostgreSQL (/plpgsql) and SQLite trigger SQL, so the offline
test suite exercises the *same rule* it will rely on in production rather than a
Python imitation of it. The SQLite form is not a substitute for the PostgreSQL
one - it is the test double for it, and the PostgreSQL form is verified on a real
server in the deployment checklist.
"""

from __future__ import annotations

from typing import List

from sqlalchemy import Connection

from .tables import Base

#: The reason text a rejected write carries. Asserted by the tests so a trigger
#: that silently stopped firing would be noticed.
OUTCOME_IMMUTABLE_MESSAGE = "a recorded outcome cannot be changed"
ATTEMPT_REASSIGN_MESSAGE = "a call attempt cannot be moved to another reminder task"

_POSTGRES_TRIGGERS: List[str] = [
    """
    CREATE OR REPLACE FUNCTION veda_forbid_outcome_change() RETURNS trigger AS $$
    BEGIN
        IF NEW.outcome IS DISTINCT FROM OLD.outcome
           OR NEW.reason_code IS DISTINCT FROM OLD.reason_code
           OR NEW.recorded_at IS DISTINCT FROM OLD.recorded_at
           OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id THEN
            RAISE EXCEPTION '%', '""" + OUTCOME_IMMUTABLE_MESSAGE + """'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    "DROP TRIGGER IF EXISTS trg_forbid_outcome_change ON call_outcomes;",
    """
    CREATE TRIGGER trg_forbid_outcome_change
    BEFORE UPDATE ON call_outcomes
    FOR EACH ROW EXECUTE FUNCTION veda_forbid_outcome_change();
    """,
    """
    CREATE OR REPLACE FUNCTION veda_forbid_attempt_reassign() RETURNS trigger AS $$
    BEGIN
        IF NEW.task_id IS DISTINCT FROM OLD.task_id THEN
            RAISE EXCEPTION '%', '""" + ATTEMPT_REASSIGN_MESSAGE + """'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    "DROP TRIGGER IF EXISTS trg_forbid_attempt_reassign ON call_attempts;",
    """
    CREATE TRIGGER trg_forbid_attempt_reassign
    BEFORE UPDATE ON call_attempts
    FOR EACH ROW EXECUTE FUNCTION veda_forbid_attempt_reassign();
    """,
]

_SQLITE_TRIGGERS: List[str] = [
    "DROP TRIGGER IF EXISTS trg_forbid_outcome_change;",
    """
    CREATE TRIGGER trg_forbid_outcome_change
    BEFORE UPDATE ON call_outcomes
    WHEN NEW.outcome IS NOT OLD.outcome
      OR NEW.reason_code IS NOT OLD.reason_code
      OR NEW.recorded_at IS NOT OLD.recorded_at
      OR NEW.attempt_id IS NOT OLD.attempt_id
    BEGIN
        SELECT RAISE(ABORT, '""" + OUTCOME_IMMUTABLE_MESSAGE + """');
    END;
    """,
    "DROP TRIGGER IF EXISTS trg_forbid_attempt_reassign;",
    """
    CREATE TRIGGER trg_forbid_attempt_reassign
    BEFORE UPDATE ON call_attempts
    WHEN NEW.task_id IS NOT OLD.task_id
    BEGIN
        SELECT RAISE(ABORT, '""" + ATTEMPT_REASSIGN_MESSAGE + """');
    END;
    """,
]

_DROP_TRIGGER_SQL: List[str] = [
    "DROP TRIGGER IF EXISTS trg_forbid_outcome_change ON call_outcomes;",
    "DROP TRIGGER IF EXISTS trg_forbid_attempt_reassign ON call_attempts;",
    "DROP FUNCTION IF EXISTS veda_forbid_outcome_change();",
    "DROP FUNCTION IF EXISTS veda_forbid_attempt_reassign();",
]


def trigger_sql(dialect_name: str) -> List[str]:
    """The trigger statements for a dialect. Unknown dialects get none."""
    if dialect_name == "postgresql":
        return list(_POSTGRES_TRIGGERS)
    if dialect_name == "sqlite":
        return list(_SQLITE_TRIGGERS)
    return []


def create_tables(connection: Connection) -> None:
    """Create every table if it is not there yet."""
    Base.metadata.create_all(connection)


def create_triggers(connection: Connection) -> None:
    """Install the safety triggers for the connection's dialect."""
    for statement in trigger_sql(connection.dialect.name):
        connection.exec_driver_sql(statement)


def create_schema(connection: Connection) -> None:
    """Tables then triggers - the order the tests and the first migration use."""
    create_tables(connection)
    create_triggers(connection)


def drop_schema(connection: Connection) -> None:
    """Drop triggers first (SQLite needs the table to exist), then everything."""
    for statement in _DROP_TRIGGER_SQL:
        try:
            connection.exec_driver_sql(statement)
        except Exception:
            # PostgreSQL DROP TRIGGER ... ON <missing table> raises; that is fine
            # during a teardown, and the tables are dropped on the next line.
            continue
    Base.metadata.drop_all(connection)
