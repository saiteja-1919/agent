"""Beginner terminal self-check for the Veda WebSocket path."""

from __future__ import annotations

import asyncio
import sys

PROBE_PORT = 8007


async def probe_health(port: int) -> tuple[bool, str]:
    import httpx

    url = f"http://127.0.0.1:{port}/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        if resp.status_code == 200 and resp.json().get("status") == "ok":
            return True, f"http {port} /health -> 200 ok"
        return False, f"http {port} /health -> status {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"http {port} failed: {exc}"


async def probe_websocket(port: int) -> tuple[bool, str]:
    import websockets

    url = f"ws://127.0.0.1:{port}/ws-test"
    try:
        async with websockets.connect(url) as ws:
            await ws.send("veda_probe")
            pong = await ws.recv()
        if pong == "veda_probe":
            return True, f"ws {port} /ws-test -> echo ok"
        return False, f"ws {port} /ws-test -> unexpected reply: {pong!r}"
    except Exception as exc:  # noqa: BLE001
        return False, f"ws {port} failed: {exc}"


async def main() -> int:
    print(f"probing 127.0.0.1:{PROBE_PORT}")
    http_ok, http_msg = await probe_health(PROBE_PORT)
    ws_ok, ws_msg = await probe_websocket(PROBE_PORT)

    print("")
    print(f"[HTTP]  {http_msg}")
    print(f"[WS]    {ws_msg}")
    print("")

    if http_ok and ws_ok:
        print("Local check PASSED: the diagnostic server accepts HTTP + WebSocket.")
        return 0

    print("Local check FAILED: something is wrong with the server or port.")
    if not http_ok:
        print("  - health endpoint did not answer as expected")
    if not ws_ok:
        print("  - WebSocket upgrade was rejected or could not connect")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
