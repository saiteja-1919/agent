"""Deduplication: the same child must not be reminded twice for the same fee.

Three different kinds of "same" matter here, and they need three different keys:

* **The same file.** A SHA-256 of the bytes catches "I clicked import twice" or
  "I uploaded yesterday's copy again". Enforced by a unique index on
  ``import_batches.file_checksum``.
* **The same row.** The same student name and parent number inside one file is a
  duplicate row (handled while validating).
* **The same reminder.** A business key of school + child identity + period is
  what makes re-importing the same month safe: the second import finds the
  existing task instead of creating a second one, and the unique index on
  ``reminder_tasks.business_key`` is the backstop.

The identity key is a hash rather than the raw name and number, so a business key
can be stored, logged and printed without containing a parent's name or phone
number.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Optional

#: How much the identity hash keeps: collisions are effectively impossible, and
#: the key stays readable in a database browser.
HASH_PREFIX_LENGTH = 12

_WHITESPACE_RE = re.compile(r"\s+")


def file_checksum(path: Path) -> str:
    """SHA-256 of the file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalise_name(name: str) -> str:
    """Case- and spacing-insensitive form of a name, for identity comparison."""
    return _WHITESPACE_RE.sub(" ", (name or "").strip()).lower()


def identity_hash(student_name: str, parent_phone: str) -> str:
    """A stable, non-reversible identity for one child."""
    payload = f"{normalise_name(student_name)}|{parent_phone or ''}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:HASH_PREFIX_LENGTH]


def period_key(due_date: Optional[date], fallback: Optional[str] = None) -> str:
    """The reminder period, normally the month the fee was due."""
    if due_date is not None:
        return f"{due_date.year:04d}-{due_date.month:02d}"
    return fallback or "unknown"


def business_key(*, school_id: int, student_name: str, parent_phone: str, period: str) -> str:
    """The unique key of one reminder task (deterministic, safe to log)."""
    return f"{school_id}:{period}:fee:{identity_hash(student_name, parent_phone)}"


def describe_identity(student_name: str, parent_phone: str) -> str:
    """Human-readable identity for a report - masked, never the real number."""
    from app.domain.policies import mask_name, mask_phone

    return f"{mask_name(student_name)} / {mask_phone(parent_phone)}"
