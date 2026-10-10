"""Pending call details: one file per call, claimed atomically.

One shared file let two overlapping calls swap details, and a missing file once
produced a greeting with a blank name and a blank fee (P10). Rules kept here:

* one file per call, written atomically (temp file + rename);
* a stream may only use details whose phone number matches the call, and only
  when exactly one call is pending if there is no match (P11);
* claiming is atomic, so two streams can never claim the same call;
* a dialer removes only its own file when its call was not answered (P12).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import PENDING_DIR
from speech_text import (
    MAX_NAME_LEN,
    MAX_NOTES_LEN,
    MAX_SCHOOL_LEN,
    clean_name,
    clean_notes,
    clean_school_name,
)

STALE_AFTER_SECS = 15 * 60
_NON_DIGIT_RE = re.compile(r"\D+")

REQUIRED_FIELDS = ("call_ref", "to", "student_name", "amount_words", "school_name")


def phone_tail(number: str, digits: int = 10) -> str:
    """Trailing digits of a phone number, which is how calls are matched."""
    only_digits = _NON_DIGIT_RE.sub("", number or "")
    return only_digits[-digits:] if len(only_digits) >= digits else only_digits


def new_call_ref() -> str:
    return uuid.uuid4().hex[:12]


def _pending_dir(directory: Optional[Path]) -> Path:
    return Path(directory) if directory is not None else PENDING_DIR


def purge_stale(directory: Optional[Path] = None, max_age_secs: int = STALE_AFTER_SECS) -> int:
    """Remove pending files older than ``max_age_secs``. Never touches fresh ones."""
    folder = _pending_dir(directory)
    removed = 0
    if not folder.exists():
        return removed
    now = time.time()
    for path in folder.glob("*.json"):
        try:
            if now - path.stat().st_mtime > max_age_secs:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def save_pending(details: Dict[str, Any], directory: Optional[Path] = None) -> Path:
    """Write one pending call. Fails loudly, because a silent failure means a call
    with blank details (P10)."""
    folder = _pending_dir(directory)
    folder.mkdir(parents=True, exist_ok=True)
    purge_stale(folder)
    call_ref = str(details.get("call_ref") or new_call_ref())
    payload = dict(details)
    payload["call_ref"] = call_ref
    payload["created_at"] = payload.get("created_at") or time.time()
    payload["created_at_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(payload["created_at"]))
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(payload["created_at"]))
    path = folder / f"{stamp}_{call_ref}.json"
    _atomic_write_json(path, payload)
    return path


def load_pending(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def list_pending(directory: Optional[Path] = None) -> List[Path]:
    folder = _pending_dir(directory)
    if not folder.exists():
        return []
    return sorted(folder.glob("*.json"))


def validate_details(details: Dict[str, Any]) -> List[str]:
    """Everything that must be true before a call may be dialled (P17)."""
    problems: List[str] = []
    for name in REQUIRED_FIELDS:
        if not str(details.get(name) or "").strip():
            problems.append(f"{name} is missing")
    if not phone_tail(str(details.get("to") or "")):
        problems.append("to is not a phone number")
    amount_words = str(details.get("amount_words") or "").strip().lower()
    if amount_words and not any(ch.isalpha() for ch in amount_words):
        problems.append("amount_words must be words, not digits")
    if len(str(details.get("student_name") or "")) > MAX_NAME_LEN:
        problems.append("student_name is too long")
    if len(str(details.get("school_name") or "")) > MAX_SCHOOL_LEN:
        problems.append("school_name is too long")
    if len(str(details.get("student_notes") or "")) > MAX_NOTES_LEN:
        problems.append("student_notes is too long")
    greeting = details.get("greeting_lines")
    if not isinstance(greeting, list) or len(greeting) != 2 or not all(str(line).strip() for line in greeting):
        problems.append("greeting_lines must hold the two opening lines")
    return problems


def _matches_caller(details: Dict[str, Any], number: str) -> bool:
    tail = phone_tail(number)
    if not tail:
        return False
    return tail in {phone_tail(str(details.get("to") or "")), phone_tail(str(details.get("from") or ""))}


def claim_pending(
    call_from: str = "",
    call_to: str = "",
    directory: Optional[Path] = None,
    *,
    purge: bool = True,
) -> Optional[Dict[str, Any]]:
    """Claim the details for an incoming stream.

    Matches the last ten digits of the phone number against both ``from`` and
    ``to``, because we do not yet know which one is the parent on an outbound
    call. Falls back to the only pending call when exactly one exists, and
    refuses (returns ``None``) when several exist and none matches (P11).
    """
    folder = _pending_dir(directory)
    if purge:
        purge_stale(folder)
    candidates = list_pending(folder)

    matched: Optional[Path] = None
    for path in candidates:
        details = load_pending(path)
        if details and (_matches_caller(details, call_from) or _matches_caller(details, call_to)):
            matched = path
            break
    if matched is None:
        if len(candidates) == 1:
            matched = candidates[0]
        else:
            return None

    # Atomically claim it: os.replace is a single step on Windows and POSIX, so
    # two streams can never both win the same file.
    claimed = matched.with_suffix(".claimed")
    try:
        os.replace(matched, claimed)
    except OSError:
        return None
    details = load_pending(claimed)
    if details is None:
        return None
    try:
        claimed.unlink()
    except OSError:
        pass
    return details


def discard_pending(call_ref: str, directory: Optional[Path] = None) -> bool:
    """Remove this dialer's own pending file (used when a call is not answered)."""
    folder = _pending_dir(directory)
    if not folder.exists():
        return False
    wanted = f"_{call_ref}.json"
    removed = False
    for path in folder.glob("*.json"):
        if path.name.endswith(wanted):
            try:
                path.unlink()
                removed = True
            except OSError:
                continue
    return removed
