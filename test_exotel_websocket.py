"""Standalone WebSocket Diagnostic Tool for Veda and Exotel Streaming.

Purpose:
Determines whether Exotel (or any external client) attempts to establish a
WebSocket connection with this machine when an outbound call is made.

Endpoints exposed:
- GET  /health          -> Diagnostic health check
- GET  /events          -> In-memory ring buffer of recorded diagnostic events
- WS   /ws-test         -> Diagnostic WebSocket endpoint
- WS   /v1/exotel/stream -> Alias matching Veda's standard Exotel route

Usage:
    python test_exotel_websocket.py [--host 127.0.0.1] [--port 8001]
    python test_exotel_websocket.py --test-client [--port 8001]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("veda.ws_diagnostic")

# Bounded in-memory event log (keeps last 200 events)
MAX_EVENTS = 200
event_history: Deque[Dict[str, Any]] = deque(maxlen=MAX_EVENTS)


def record_event(category: str, details: Dict[str, Any]) -> None:
    """Store an event with UTC timestamp in the bounded history."""
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "timestamp": ts,
        "category": category,
        **details,
    }
    event_history.append(entry)


def mask_sensitive_query(query_string: str) -> str:
    """Mask any tokens or keys in query strings before logging."""
    if not query_string:
        return ""
    parts = []
    for pair in query_string.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if any(s in k.lower() for s in ("token", "key", "secret", "auth")):
                parts.append(f"{k}=[MASKED_{len(v)}chars]")
            else:
                parts.append(pair)
        else:
            parts.append(pair)
    return "&".join(parts)


# ---------------------------------------------------------------------------
# FastAPI Application Factory
# ---------------------------------------------------------------------------
def create_diagnostic_app() -> FastAPI:
    app = FastAPI(title="Veda WebSocket Diagnostic Server", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        """Verify diagnostic HTTP reachability."""
        record_event("http_health_check", {"status": "ok"})
        return {
            "status": "ok",
            "service": "veda_websocket_diagnostic",
            "server_time_utc": datetime.now(timezone.utc).isoformat(),
            "recorded_events_count": len(event_history),
        }

    @app.get("/events")
    async def get_events(limit: int = 50) -> Dict[str, Any]:
        """Inspect recorded diagnostic events."""
        events_list = list(event_history)
        selected = events_list[-max(1, min(limit, MAX_EVENTS)):]
        return {
            "total_events_recorded": len(event_history),
            "returned_count": len(selected),
            "events": selected,
        }

    async def handle_websocket_diagnostic(websocket: WebSocket, route_name: str) -> None:
        client_host = websocket.client.host if websocket.client else "unknown"
        client_port = websocket.client.port if websocket.client else 0
        raw_query = str(websocket.scope.get("query_string", b"").decode("utf-8", "ignore"))
        masked_query = mask_sensitive_query(raw_query)

        # Extract headers safely (mask Authorization or token headers if present)
        sanitized_headers = {}
        for k, v in websocket.headers.items():
            if any(s in k.lower() for s in ("auth", "token", "cookie")):
                sanitized_headers[k] = "[MASKED]"
            else:
                sanitized_headers[k] = v

        attempt_info = {
            "route": route_name,
            "client": f"{client_host}:{client_port}",
            "query": masked_query,
            "user_agent": sanitized_headers.get("user-agent", "unknown"),
        }

        logger.info("=" * 65)
        logger.info(">>> INCOMING WEBSOCKET ATTEMPT on %s", route_name)
        logger.info("    Client: %s:%s", client_host, client_port)
        logger.info("    User-Agent: %s", attempt_info["user_agent"])
        if masked_query:
            logger.info("    Query params: %s", masked_query)
        record_event("ws_connection_attempt", attempt_info)

        # Accept connection
        try:
            await websocket.accept()
            logger.info(">>> RESULT: WebSocket HANDSHAKE ACCEPTED on %s", route_name)
            record_event("ws_accepted", {"route": route_name, "client": f"{client_host}:{client_port}"})
        except Exception as exc:
            logger.error(">>> ERROR: Failed to accept WebSocket handshake on %s: %s", route_name, exc)
            record_event(
                "ws_handshake_failed",
                {"route": route_name, "error": str(exc)},
            )
            return

        message_count = 0
        total_binary_bytes = 0

        # Receive loop
        try:
            while True:
                msg = await websocket.receive()
                msg_type = msg.get("type", "")

                if msg_type == "websocket.disconnect":
                    code = msg.get("code", 1000)
                    logger.info("<<< CLIENT DISCONNECTED gracefully on %s (code=%s)", route_name, code)
                    record_event(
                        "ws_client_disconnect",
                        {
                            "route": route_name,
                            "close_code": code,
                            "total_messages": message_count,
                            "total_binary_bytes": total_binary_bytes,
                        },
                    )
                    break

                message_count += 1

                # Text Frame (JSON metadata, Exotel control frames, start/stop events)
                if "text" in msg and msg["text"] is not None:
                    text_data = msg["text"]
                    preview = text_data[:150] + ("..." if len(text_data) > 150 else "")
                    logger.info("    [TEXT #%d] on %s (%d chars): %s", message_count, route_name, len(text_data), preview)
                    record_event(
                        "ws_text_message",
                        {
                            "route": route_name,
                            "message_number": message_count,
                            "char_count": len(text_data),
                            "preview": preview,
                        },
                    )

                # Binary Frame (raw linear16 PCM audio chunks from Exotel / caller)
                elif "bytes" in msg and msg["bytes"] is not None:
                    byte_len = len(msg["bytes"])
                    total_binary_bytes += byte_len
                    # Log binary arrivals periodically or per frame
                    if message_count <= 5 or message_count % 20 == 0:
                        logger.info(
                            "    [AUDIO/BINARY #%d] on %s: %d bytes (total audio: %d bytes)",
                            message_count,
                            route_name,
                            byte_len,
                            total_binary_bytes,
                        )
                    record_event(
                        "ws_binary_message",
                        {
                            "route": route_name,
                            "message_number": message_count,
                            "byte_count": byte_len,
                            "cumulative_bytes": total_binary_bytes,
                        },
                    )

        except WebSocketDisconnect as exc:
            logger.info("<<< WebSocket CLOSED by peer on %s (code=%s)", route_name, exc.code)
            record_event(
                "ws_disconnect_exception",
                {
                    "route": route_name,
                    "close_code": exc.code,
                    "total_messages": message_count,
                    "total_binary_bytes": total_binary_bytes,
                },
            )
        except Exception as exc:
            logger.exception("<<< EXCEPTION during WebSocket session on %s: %s", route_name, exc)
            record_event(
                "ws_session_error",
                {
                    "route": route_name,
                    "error": str(exc),
                    "total_messages": message_count,
                },
            )
        finally:
            logger.info(
                ">>> SESSION COMPLETE on %s. Messages: %d | Audio bytes: %d",
                route_name,
                message_count,
                total_binary_bytes,
            )
            logger.info("=" * 65)

    # Listen on both the user-specified /ws-test and Veda's standard /v1/exotel/stream
    @app.websocket("/ws-test")
    async def ws_test_endpoint(websocket: WebSocket) -> None:
        await handle_websocket_diagnostic(websocket, "/ws-test")

    @app.websocket("/v1/exotel/stream")
    async def ws_v1_stream_endpoint(websocket: WebSocket) -> None:
        await handle_websocket_diagnostic(websocket, "/v1/exotel/stream")

    return app


# ---------------------------------------------------------------------------
# Local Client Verification Test
# ---------------------------------------------------------------------------
async def run_local_client_test(host: str, port: int) -> int:
    """Connect a local test client to verify that this diagnostic server accepts WebSockets."""
    import httpx
    import websockets

    http_url = f"http://{host}:{port}/health"
    ws_url = f"ws://{host}:{port}/ws-test"

    print(f"\n[1/2] Checking HTTP health endpoint: {http_url}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(http_url)
            if resp.status_code == 200:
                print(f"      [OK] HTTP health answered 200: {resp.json()}")
            else:
                print(f"      [FAIL] HTTP returned {resp.status_code}")
                return 1
    except Exception as exc:
        print(f"      [FAIL] Could not reach HTTP endpoint ({exc})")
        print("      Make sure the diagnostic server is running first.")
        return 1

    print(f"\n[2/2] Testing local WebSocket handshake: {ws_url}")
    try:
        async with websockets.connect(ws_url) as ws:
            print("      [OK] WebSocket connection established successfully!")
            test_payload = json.dumps({"event": "diagnostic_probe", "sender": "local_test_client"})
            await ws.send(test_payload)
            print("      [OK] Sent test JSON message.")
            await asyncio.sleep(0.5)
            await ws.close()
            print("      [OK] WebSocket closed cleanly.")
    except Exception as exc:
        print(f"      [FAIL] WebSocket connection failed: {exc}")
        return 1

    print("\nLocal test passed: The diagnostic server is working correctly on this machine.\n")
    return 0


# ---------------------------------------------------------------------------
# Main CLI Entry Point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="test_exotel_websocket.py",
        description="Standalone WebSocket diagnostic tool for Veda and Exotel.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8001, help="Port to bind (default: 8001)")
    parser.add_argument(
        "--test-client",
        action="store_true",
        help="Run a local probe client against the running diagnostic server, then exit.",
    )

    args = parser.parse_args()

    if args.test_client:
        return asyncio.run(run_local_client_test(args.host, args.port))

    app = create_diagnostic_app()

    logger.info("Starting Veda WebSocket Diagnostic Server on http://%s:%d", args.host, args.port)
    logger.info("Endpoints active:")
    logger.info("  - GET  http://%s:%d/health", args.host, args.port)
    logger.info("  - GET  http://%s:%d/events", args.host, args.port)
    logger.info("  - WS   ws://%s:%d/ws-test", args.host, args.port)
    logger.info("  - WS   ws://%s:%d/v1/exotel/stream", args.host, args.port)
    logger.info("Ready for connections. Press CTRL+C to stop.")

    record_event("server_startup", {"host": args.host, "port": args.port})

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
