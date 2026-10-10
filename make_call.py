"""Veda's dialer: one command places one real phone call.

    python make_call.py --to 98xxxxxxxx --name "Sai Teja" --amount 5000

What it does, in order: validate the input, convert the names to Telugu script,
write this call's own details file, start the local server (or use a running
one), dial through Exotel, follow the call status, then find THIS call's
transcript and print the result.

Exit codes
    0  the call finished and a transcript was saved
    1  Exotel or the local server reported an error
    2  the input or the configuration is wrong (nothing was dialled)
    3  nobody answered, or no conversation was recorded
    4  the local server stopped
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import xml.etree.ElementTree as ET
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from call_store import discard_pending, new_call_ref, save_pending, validate_details
from config import (
    APP_DIR,
    ConfigError,
    PLACEHOLDER_SCHOOL_NAMES,
    Settings,
    configure_console,
    ensure_runtime_dirs,
    load_settings,
)
from name_speech import NameSpeaker, NameSpeechAuthError
from sarvam_llm import build_system_prompt, check_prompt_inputs
from speech_text import (
    TELUGU_DIGITS,
    amount_to_words,
    clean_name,
    clean_notes,
    clean_school_name,
    parse_amount,
)

HEALTH_TIMEOUT_SECS = 25.0
STATUS_POLL_SECS = 5.0
TRANSCRIPT_GRACE_SECS = 25.0
TERMINAL_STATUSES = {"completed", "failed", "busy", "no-answer", "canceled", "cancelled"}
FAILED_STATUSES = {"failed", "busy", "no-answer", "canceled", "cancelled"}


class InputError(Exception):
    """Something the office member typed is wrong."""


@dataclass
class CallPlan:
    call_ref: str
    details: Dict[str, Any]
    pending_path: Path


# ------------------------------------------------------------------ validation
def normalise_phone(raw: str) -> str:
    """Accept the ways a school office writes a parent's number."""
    if raw is None or not str(raw).strip():
        raise InputError("the phone number is missing")
    text = str(raw).strip()
    for telugu, ascii_digit in TELUGU_DIGITS.items():
        text = text.replace(telugu, ascii_digit)
    digits = "".join(ch for ch in text if ch.isdigit())
    plus = text.strip().startswith("+")
    if len(digits) < 8:
        raise InputError(f"{raw!r} is not a phone number")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10:
        if digits[0] not in "6789":
            raise InputError(
                f"{raw!r} does not look like an Indian mobile number "
                "(ten digits starting with 6, 7, 8 or 9)"
            )
        return f"+91{digits}"
    if plus or len(digits) >= 11:
        return f"+{digits}"
    raise InputError(f"{raw!r} is not a phone number")


def build_details(
    args: argparse.Namespace,
    settings: Settings,
    spoken: Dict[str, str],
    *,
    call_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Everything one call needs. No demo values, nothing hard-coded (P15)."""
    school = clean_school_name(args.school_name or settings.school_name)
    if not school or school.strip().lower() in PLACEHOLDER_SCHOOL_NAMES:
        raise InputError(
            "the school name is missing. Set SCHOOL_NAME in .env (or pass --school-name); "
            "Veda refuses to call without the real school name."
        )
    student = clean_name(args.name)
    if not student:
        raise InputError("the student name is missing")
    parent = clean_name(args.parent_name or "")
    amount = parse_amount(args.amount)
    notes = clean_notes(args.student_notes or "")
    due = clean_notes(args.due_date or "")

    school_spoken = spoken.get("school_name") or school
    student_spoken = spoken.get("student_name") or student
    parent_spoken = spoken.get("parent_name") or parent

    line_one = f"Hello sir, నేను Veda, {school_spoken} నుండి మాట్లాడుతున్నాను."
    if parent_spoken:
        line_two = f"నేను {student_spoken} parent {parent_spoken} గారితో మాట్లాడుతున్నానా?"
    else:
        line_two = f"నేను {student_spoken} parent గారితో మాట్లాడుతున్నానా?"

    details: Dict[str, Any] = {
        "call_ref": call_ref or new_call_ref(),
        "created_at": time.time(),
        "to": normalise_phone(args.to),
        "student_name": student,
        "student_name_spoken": student_spoken,
        "parent_name": parent,
        "parent_name_spoken": parent_spoken,
        "school_name": school,
        "school_name_spoken": school_spoken,
        "amount": str(amount),
        "amount_words": amount_to_words(amount),
        "due_date": due,
        "student_notes": notes,
        "greeting_lines": [line_one, line_two],
    }
    return details


def _number_tail(value: Any) -> str:
    """Last digits of any phone number, never raising.

    The ExoPhone is often a landline (for example 0404xxxxxxx), which is not a
    valid mobile number - comparing through ``normalise_phone`` here refused
    every single call before it was dialled.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def ensure_callable(details: Dict[str, Any], settings: Settings) -> None:
    """Refuse to dial while anything is missing or unsafe."""
    problems = validate_details(details)
    problems += check_prompt_inputs(details, settings)
    parent_tail = _number_tail(details.get("to"))
    caller_tails = {_number_tail(settings.exotel_caller_id), _number_tail(settings.twilio_caller_id)}
    caller_tails.discard("")
    if parent_tail and parent_tail in caller_tails:
        problems.append("the parent's number is the same as your own calling number")
    if problems:
        raise InputError("; ".join(sorted(set(problems))))


# ------------------------------------------------------------------- planning
def plan_call(args: argparse.Namespace, settings: Settings) -> CallPlan:
    speaker = NameSpeaker(
        settings.sarvam_api_key,
        cache_path=settings.name_cache_path,
        enabled=not args.no_name_conversion,
    )
    spoken = {
        "student_name": speaker.speak(clean_name(args.name), label="student name"),
        "school_name": speaker.speak(
            clean_school_name(args.school_name or settings.school_name), label="school name"
        ),
    }
    if args.parent_name:
        spoken["parent_name"] = speaker.speak(clean_name(args.parent_name), label="parent name")
    speaker.save_cache()

    for label in ("student_name", "parent_name", "school_name"):
        typed = clean_name(
            args.name
            if label == "student_name"
            else args.parent_name
            if label == "parent_name"
            else (args.school_name or settings.school_name)
        )
        if typed and spoken.get(label):
            print(f"Name for speech: {typed} -> {spoken[label]}")

    details = build_details(args, settings, spoken)
    ensure_callable(details, settings)
    # Building the prompt here proves every placeholder can be filled before we dial.
    build_system_prompt(details, settings)
    return CallPlan(call_ref=str(details["call_ref"]), details=details, pending_path=Path())


def required_secrets(settings: Settings) -> List[str]:
    missing = []
    if settings.call_provider == "twilio":
        own = (
            ("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
            ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
            ("TWILIO_CALLER_ID", settings.twilio_caller_id),
        )
    else:
        own = (
            ("EXOTEL_ACCOUNT_SID", settings.exotel_account_sid),
            ("EXOTEL_API_KEY", settings.exotel_api_key),
            ("EXOTEL_API_TOKEN", settings.exotel_api_token),
            ("EXOTEL_CALLER_ID", settings.exotel_caller_id),
        )
    for name, value in own:
        if not value:
            missing.append(name)
    for name, value in (
        ("EXOTEL_STREAM_URL", settings.exotel_stream_url),
        ("SARVAM_API_KEY", settings.sarvam_api_key),
    ):
        if not value:
            missing.append(name)
    return missing


# --------------------------------------------------------------------- server
def health_ok(settings: Settings, timeout: float = 2.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"http://{settings.host}:{settings.port}/health")
        return response.status_code == 200 and bool(response.json().get("ok"))
    except Exception:
        return False


def start_server(settings: Settings, *, wait_secs: float = HEALTH_TIMEOUT_SECS) -> Optional[subprocess.Popen]:
    """Start ``server.py`` as a child process and wait until it answers."""
    if health_ok(settings):
        print(f"Using the server that is already running on {settings.host}:{settings.port}.")
        return None
    print(f"Starting the local server on {settings.host}:{settings.port} ...")
    child = subprocess.Popen(
        [sys.executable, str(APP_DIR / "server.py")],
        cwd=str(APP_DIR),
    )
    deadline = time.monotonic() + wait_secs
    while time.monotonic() < deadline:
        if child.poll() is not None:
            print("The local server stopped as soon as it started. Check the messages above.")
            return child
        if health_ok(settings):
            return child
        time.sleep(0.5)
    print("The local server did not answer within the time limit.")
    return child


def stop_server(child: Optional[subprocess.Popen]) -> None:
    if child is None:
        return
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()


# ---------------------------------------------------------------------- exotel
def dial(settings: Settings, details: Dict[str, Any]) -> Dict[str, Any]:
    """Ask the telephony provider to call the parent and connect the audio."""
    if settings.call_provider == "twilio":
        return _dial_twilio(settings, details)
    url = f"{settings.exotel_api_base}/v1/Accounts/{settings.exotel_account_sid}/Calls/connect"
    payload = {
        "From": details["to"],
        "CallerId": settings.exotel_caller_id,
        "StreamUrl": settings.exotel_stream_url,
        "StreamType": "bidirectional",
        "TimeLimit": str(int(settings.call_timeout_secs)),
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            url,
            data=payload,
            auth=(settings.exotel_api_key, settings.exotel_api_token),
        )
    body: Any
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:400]}
        raise RuntimeError(
            f"Exotel refused the call (HTTP {response.status_code}): "
            f"{json.dumps(body, ensure_ascii=False)[:400]}"
        )

    try:
        body = response.json()
    except ValueError:
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise RuntimeError(
                f"Exotel returned an unreadable response: {response.text[:500]}"
            ) from exc

        call_element = root.find(".//Call")
        if call_element is None:
            raise RuntimeError(
                f"Exotel returned XML without a Call element: {response.text[:500]}"
            )

        call = {
            child.tag.split("}")[-1]: (child.text or "").strip()
            for child in call_element
        }
        if not call.get("Sid"):
            raise RuntimeError(
                f"Exotel returned XML without a Sid: {response.text[:500]}"
            )
        return call

    call = body.get("Call") if isinstance(body, dict) else None
    if not isinstance(call, dict) or not call.get("Sid"):
        raise RuntimeError(f"Exotel did not return a call id: {body}")
    return call


# ---------------------------------------------------------------------- twilio
def build_twiml(settings: Settings, details: Dict[str, Any]) -> str:
    """What Twilio does with the call: stream the audio to our server.

    Twilio's Stream url accepts NO query string (its official docs), so the
    shared secret must already be inside EXOTEL_STREAM_URL as the last path
    piece. The phone numbers travel as <Parameter> entries; Twilio sends them
    back in the stream's first message, which is how the server finds this
    call's details file.
    """
    from html import escape

    url = escape(settings.exotel_stream_url, quote=True)
    caller = escape(settings.twilio_caller_id, quote=True)
    parent = escape(str(details.get("to") or ""), quote=True)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><Stream url=\"{url}\">"
        f"<Parameter name=\"from_number\" value=\"{caller}\"/>"
        f"<Parameter name=\"to_number\" value=\"{parent}\"/>"
        "</Stream></Connect></Response>"
    )


def _dial_twilio(settings: Settings, details: Dict[str, Any]) -> Dict[str, Any]:
    """Ask Twilio to call the parent (POST Calls.json, Basic auth, inline TwiML)."""
    url = (
        f"{settings.twilio_api_base}/2010-04-01/Accounts/"
        f"{settings.twilio_account_sid}/Calls.json"
    )
    payload = {
        "To": details["to"],
        "From": settings.twilio_caller_id,
        "Twiml": build_twiml(settings, details),
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            url,
            data=payload,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
        )
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:400]}
    if response.status_code >= 400:
        raise RuntimeError(
            f"Twilio refused the call (HTTP {response.status_code}): "
            f"{json.dumps(body, ensure_ascii=False)[:400]}"
        )
    sid: Any = None
    if isinstance(body, dict):
        sid = body.get("sid") or body.get("Sid")
        if not sid and isinstance(body.get("Call"), dict):
            sid = body["Call"].get("Sid")
    if not sid:
        raise RuntimeError(f"Twilio did not return a call id: {body}")
    return {"Sid": str(sid)}


def call_status(settings: Settings, call_sid: str) -> str:
    if settings.call_provider == "twilio":
        url = (
            f"{settings.twilio_api_base}/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Calls/{call_sid}.json"
        )
        auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    else:
        url = f"{settings.exotel_api_base}/v1/Accounts/{settings.exotel_account_sid}/Calls/{call_sid}.json"
        auth = (settings.exotel_api_key, settings.exotel_api_token)
    with httpx.Client(timeout=20.0) as client:
        response = client.get(url, auth=auth)
    response.raise_for_status()
    body = response.json()
    if settings.call_provider == "twilio":
        # Twilio answers {"sid": ..., "status": "queued" | ... | "completed"}
        if isinstance(body, dict):
            return str(body.get("status") or body.get("Status") or "")
        return ""
    call = body.get("Call") if isinstance(body, dict) else None
    if isinstance(call, dict):
        return str(call.get("Status") or "")
    return ""


def wait_for_call(settings: Settings, call_sid: str) -> str:
    deadline = time.monotonic() + int(settings.call_timeout_secs) + 120
    last = ""
    while time.monotonic() < deadline:
        try:
            status = call_status(settings, call_sid)
        except Exception as exc:
            print(f"  (could not read the call status: {exc})")
            status = last
        if status != last:
            print(f"  call status: {status or 'unknown'}")
            last = status
        if status in TERMINAL_STATUSES:
            return status
        time.sleep(STATUS_POLL_SECS)
    print("  the call did not reach a final status in time.")
    return last


def find_transcript(settings: Settings, call_ref: str, since: float) -> Optional[Path]:
    folder = settings.transcripts_dir
    if not folder.exists():
        return None
    candidates = sorted(
        (path for path in folder.glob("call_*.json") if not path.name.endswith("_latency.json")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if path.stat().st_mtime < since - 5:
            break
        if call_ref and call_ref in path.name:
            return path
    # Second chance: only if exactly one new transcript exists (never guess among
    # several - that is how calls once got mixed up, P13).
    fresh: List[Path] = [path for path in candidates if path.stat().st_mtime >= since - 5]
    if len(fresh) == 1:
        return fresh[0]
    return None


def summarise(transcript_path: Path) -> None:
    try:
        data = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Transcript saved at {transcript_path} (could not be read: {exc})")
        return
    latency_path = transcript_path.with_name(transcript_path.stem + "_latency.json")
    latency = None
    try:
        latency = json.loads(latency_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    print("")
    print("Call finished. What was recorded:")
    for turn in data.get("turns", []):
        who = "Veda" if turn.get("role") == "assistant" else "Parent"
        flags = []
        if turn.get("interrupted"):
            flags.append("interrupted")
        if turn.get("no_reply"):
            flags.append("no reply")
        if turn.get("first_audio_delay_secs") is not None:
            flags.append(f"answered after {turn['first_audio_delay_secs']} s")
        suffix = f"   [{', '.join(flags)}]" if flags else ""
        print(f"  {who}: {turn.get('text', '')}{suffix}")
    if latency:
        print("")
        print(f"  response latency: {latency.get('response_latency_secs')} s")
        marks = latency.get("marks", {})
        for name in ("opener_queued", "first_tts_audio", "user_final", "llm_first_token", "hangup_requested"):
            if name in marks:
                print(f"  {name}: {marks[name].get('since_start_secs')} s after the stream was parsed")
    print("")
    print(f"  transcript: {transcript_path}")
    print(f"  latency:    {latency_path}")


# ------------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_call.py",
        description="Call one parent about a pending school fee.",
    )
    parser.add_argument("--to", required=True, help="the parent's phone number")
    parser.add_argument("--name", required=True, help="the student's name")
    parser.add_argument("--amount", required=True, help="the pending amount, for example 5000")
    parser.add_argument("--parent-name", default="", help="the parent's name (optional)")
    parser.add_argument("--due-date", default="", help="the due date on record (optional)")
    parser.add_argument("--student-notes", default="", help="extra student information (optional)")
    parser.add_argument("--school-name", default="", help="the school name; defaults to SCHOOL_NAME in .env")
    parser.add_argument("--no-start-server", action="store_true", help="use a server that is already running")
    parser.add_argument("--check-names", action="store_true", help="show how the names will be spoken, then stop")
    parser.add_argument(
        "--no-name-conversion",
        action="store_true",
        help="do not convert names to Telugu script with Sarvam",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(require_secrets=False, require_school=False)
    except ConfigError as exc:
        print(str(exc))
        return 2

    try:
        plan = plan_call(args, settings)
    except NameSpeechAuthError as exc:
        print(f"Sarvam refused the API key: {exc}")
        return 2
    except InputError as exc:
        print(f"Nothing was dialled: {exc}")
        return 2
    except ValueError as exc:
        print(f"Nothing was dialled: {exc}")
        return 2

    if args.check_names:
        print("")
        print("The opening lines would be:")
        for line in plan.details["greeting_lines"]:
            print(f"  {line}")
        print("")
        print("Nothing was dialled (--check-names).")
        return 0

    missing = required_secrets(settings)
    if missing:
        print("Nothing was dialled. These settings are missing in .env: " + ", ".join(missing))
        return 2
    if not args.to:
        print("Nothing was dialled: the parent's number is missing.")
        return 2

    ensure_runtime_dirs(settings)
    plan.pending_path = save_pending(plan.details, settings.pending_dir)
    print(f"Call details saved: {plan.pending_path.name}")
    print(f"Amount for speech: {plan.details['amount_words']}")

    if settings.opener_prerender:
        from pipeline import prerender_opener

        print("Pre-rendering the opening lines ...")
        try:
            asyncio.run(
                prerender_opener(plan.details["greeting_lines"], settings, cache_dir=settings.opener_cache_dir)
            )
        except Exception as exc:  # never block a call because of the cache
            print(f"  (pre-rendering failed, the live opener will be used: {exc})")

    child: Optional[subprocess.Popen] = None
    dialled = False  # a pending file must never outlive a call that was never made
    try:
        if args.no_start_server:
            if not health_ok(settings):
                print(f"No server is answering on {settings.host}:{settings.port}. Start server.py first.")
                return 4
        else:
            child = start_server(settings)
            if not health_ok(settings):
                return 4

        started = time.time()
        try:
            call = dial(settings, plan.details)
        except Exception as exc:
            print(f"Exotel error: {exc}")
            discard_pending(plan.call_ref, settings.pending_dir)
            return 1
        call_sid = str(call.get("Sid"))
        dialled = True
        print(f"Calling {plan.details['to']} (call {call_sid}) ...")

        status = wait_for_call(settings, call_sid)
        if status in FAILED_STATUSES:
            print(f"The call was not answered: {status}.")
            discard_pending(plan.call_ref, settings.pending_dir)
            return 3
        if status != "completed":
            print(f"The call ended with the status {status or 'unknown'}.")
            discard_pending(plan.call_ref, settings.pending_dir)
            return 1

        deadline = time.monotonic() + TRANSCRIPT_GRACE_SECS
        transcript: Optional[Path] = None
        while time.monotonic() < deadline:
            transcript = find_transcript(settings, plan.call_ref, started)
            if transcript is not None:
                break
            time.sleep(1.0)
        if transcript is None:
            print("The call finished but no transcript was recorded for it.")
            discard_pending(plan.call_ref, settings.pending_dir)
            return 3
        summarise(transcript)
        discard_pending(plan.call_ref, settings.pending_dir)
        return 0
    finally:
        if not dialled:
            discard_pending(plan.call_ref, settings.pending_dir)
        if child is not None:
            stop_server(child)


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
