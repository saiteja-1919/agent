"""Replay Exotel's side of the stream handshake, without a phone call.

Exotel's documented sequence (Connect Voice AI / Voicebot applet protocol):
  1. open the WebSocket to EXOTEL_STREAM_URL once the call is answered
  2. send {"event": "connected"}
  3. send {"event": "start", "start": {...stream_sid, call_sid, account_sid,
     from, to, media_format...}}
  4. then media/dtmf events; "stop" when the stream ends

This client sends exactly that against:
  * the local server (default): ws://127.0.0.1:8000<same path>?<same token>
  * the real tunnel (--tunnel):  the wss:// address from settings, so
    cloudflared is exercised too

A matching pending details file is written first, so the server can claim it
exactly like it would during a real call. Watch the server log: success looks like
  stream started (call ...) -> opener queued (...) -> call finished (...).

Usage:
    python _debug_exotel_client.py                # local, 12 s of silence audio
    python _debug_exotel_client.py --hold 30      # stay connected 30 s
    python _debug_exotel_client.py --tunnel       # go through cloudflared
    python _debug_exotel_client.py --tunnel --hold 30
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
import uuid
from typing import Any, Dict
from urllib.parse import urlparse, urlunparse

from config import ConfigError, configure_console, ensure_runtime_dirs, load_settings

SAMPLE_RATE = 8000          # Exotel stream format used by pipeline.py
FRAME_MS = 20               # media chunk period
SILENCE = b"\x00" * (SAMPLE_RATE * 2 * FRAME_MS // 1000)   # one silent chunk


def build_pending(settings: Any) -> Dict[str, Any]:
    """A pending call this client's start frame will match (claim_pending)."""
    from call_store import save_pending

    details: Dict[str, Any] = {
        "call_ref": uuid.uuid4().hex[:12],
        "to": "+919999999999",
        "student_name": "Debug Student",
        "student_name_spoken": "Debug Student",
        "parent_name": "debug parent",
        "parent_name_spoken": "debug parent",
        "school_name": settings.school_name or "debug school",
        "school_name_spoken": settings.school_name or "debug school",
        "amount": "1",
        "amount_words": "one rupee",
        "due_date": "",
        "student_notes": "",
        "greeting_lines": [
            "Hello sir, this is a handshake debug line one.",
            "Is this line two reaching you?",
        ],
    }
    return details


def local_url(settings: Any) -> str:
    """The same path+token as EXOTEL_STREAM_URL, but pointed at the local server."""
    parsed = urlparse(settings.exotel_stream_url)
    query = f"token={settings.stream_token}" if settings.stream_token else ""
    return urlunparse(("ws", f"{settings.host}:{settings.port}", parsed.path, "", query, ""))


def mask(url: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.split("@", 1)[-1]
    query = ""
    if parsed.query:
        parts = []
        for pair in parsed.query.split("&"):
            key, _, value = pair.partition("=")
            if key.lstrip("?").lower() == "token":
                parts.append(f"token=[{len(value)} chars]")
            else:
                parts.append(pair)
        query = "?" + "&".join(parts)
    return f"{parsed.scheme}://{netloc}{parsed.path}{query}"


async def run(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(require_secrets=False, require_school=False)
    except ConfigError as exc:
        print(f"Configuration problem: {exc}")
        return 2
    ensure_runtime_dirs(settings)

    from call_store import list_pending, load_pending, purge_stale, save_pending

    purge_stale(settings.pending_dir)
    # Drop leftovers of earlier debug runs only (never a real call's pending file).
    for path in list_pending(settings.pending_dir):
        data = load_pending(path)
        if data and str(data.get("to") or "") == "+919999999999":
            try:
                path.unlink()
            except OSError:
                pass
    details = build_pending(settings)
    pending_path = save_pending(details, settings.pending_dir)
    print(f"Pending details written: {pending_path.name}")
    print(f"  to={details['to']}  call_ref={details['call_ref']}")

    url = mask(settings.exotel_stream_url if args.tunnel else local_url(settings))
    target = settings.exotel_stream_url if args.tunnel else local_url(settings)
    print(f"Connecting ({'tunnel' if args.tunnel else 'local'}): {url}")

    import websockets

    start_frame = {
        "event": "start",
        "sequenceNumber": "1",
        "start": {
            "streamSid": "MSdebug" + uuid.uuid4().hex[:8],
            "stream_sid": "MSdebug" + uuid.uuid4().hex[:8],
            "callSid": "Cdebug" + uuid.uuid4().hex[:8],
            "call_sid": "Cdebug" + uuid.uuid4().hex[:8],
            "account_sid": "ACdebug00000000000000000000000000",
            "accountSid": "ACdebug00000000000000000000000000",
            "from": settings.exotel_caller_id or "+910000000000",
            "to": details["to"],
            "custom_parameters": {},
            "media_format": {
                "encoding": "audio/x-l16",
                "sample_rate": SAMPLE_RATE,
                "sample_width": 2,
                "channels": 1,
            },
        },
        "custom_parameters": {},
    }

    try:
        async with websockets.connect(
            target,
            open_timeout=10,
            max_size=None,
            ping_interval=10,
            ping_timeout=10,
        ) as ws:
            print("WebSocket accepted by the server. Sending 'connected' + 'start' ...")
            await ws.send(json.dumps({"event": "connected", "protocol": "voicebot-applet.v1"}))
            await ws.send(json.dumps(start_frame))

            deadline = time.monotonic() + args.hold
            chunk = 0
            sent_bytes = 0
            while time.monotonic() < deadline:
                # Exotel media events: base64 linear16 silence, one every 20 ms.
                payload = base64.b64encode(SILENCE).decode("ascii")
                await ws.send(
                    json.dumps(
                        {
                            "event": "media",
                            "media": {
                                "chunk": str(chunk),
                                "timestamp": str(chunk * FRAME_MS),
                                "payload": payload,
                            },
                        }
                    )
                )
                chunk += 1
                sent_bytes += len(SILENCE)
                await asyncio.sleep(FRAME_MS / 1000)
                # Drain whatever the server (pipeline audio back) sends us.
                try:
                    while True:
                        reply = await asyncio.wait_for(ws.recv(), timeout=0.001)
                        if isinstance(reply, bytes):
                            if chunk <= 3:
                                print(f"  <- server sent {len(reply)} audio bytes")
                        elif chunk <= 5:
                            print(f"  <- server text: {str(reply)[:160]}")
                except (asyncio.TimeoutError, TimeoutError):
                    pass

            print(f"Done: {chunk} media frames ({sent_bytes} audio bytes) sent, sending 'stop'.")
            await ws.send(json.dumps({"event": "stop"}))
            await asyncio.sleep(0.5)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        print("If the local run failed, the problem is inside this machine. "
              "If --tunnel failed but local worked, the problem is cloudflared/the tunnel URL.")
        return 1

    print("OK: the server accepted the handshake sequence and kept the stream open.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay Exotel's stream handshake for debugging.")
    parser.add_argument("--tunnel", action="store_true", help="use EXOTEL_STREAM_URL (through cloudflared)")
    parser.add_argument("--hold", type=float, default=12.0, help="seconds to stream silence (default 12)")
    return parser


def main() -> int:
    configure_console()
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
