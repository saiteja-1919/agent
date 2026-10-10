"""The Veda server: accepts the Exotel stream and runs one voice pipeline per call.

Rules kept here:

* the stream token is checked BEFORE the WebSocket is accepted (P30);
* a stream without matching call details is refused, so a greeting can never be
  spoken with a blank name or a blank fee (P10, P11);
* the hard call limit and the silence limit are enforced by the server itself,
  not only by the dialer (P2);
* the recording is saved in a ``finally`` block, so a crash still leaves a
  transcript (P4, P32);
* ``/health`` never exposes the tunnel address or any secret (P30).

``handle_stream`` is written with injectable pieces (transport, pipeline, runner,
recorder) so the offline tests can drive the whole accept/refuse/save path
without FastAPI, websockets or real network calls.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from call_store import claim_pending
from config import (
    ConfigError,
    Settings,
    configure_console,
    ensure_runtime_dirs,
    load_settings,
)
from pipeline import (
    OPENER_GAP_SECS,
    SAMPLE_RATE,
    build_call,
    create_call_transport,
    load_prerendered_opener,
    prerendered_audio_frames,
)
from recorder import CallRecorder

STREAM_PATHS = (
    "/v1/exotel/stream",
    "/v1/exotel/stream/ws",
    "/ws",
    "/v1/twilio/stream",
    "/v1/twilio/stream/{token}",
)

#: How many calls are being handled right now (for the log line and for tests).
ACTIVE_STREAMS = 0


def _query(websocket: Any, name: str) -> str:
    params = getattr(websocket, "query_params", None)
    if params is None:
        return ""
    try:
        return str(params.get(name, "") or "")
    except Exception:  # pragma: no cover - defensive
        return ""


async def _close(websocket: Any, code: int = 1000, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception as exc:  # pragma: no cover - already closed
        logger.debug("closing the stream failed: {}", exc)


def _path_token(websocket: Any) -> str:
    """The token from the URL path (Twilio: its Stream url accepts no query
    string, so the shared secret travels as the last path piece instead)."""
    params = getattr(websocket, "path_params", None)
    if not params:
        return ""
    try:
        return str(params.get("token", "") or "")
    except Exception:  # pragma: no cover - defensive
        return ""


def token_is_valid(websocket: Any, settings: Settings) -> bool:
    """The shared secret is optional, but if it is set it must match.

    Exotel carries it as ``?token=``, Twilio as the final path piece.
    """
    if not settings.stream_token:
        return True
    return _query(websocket, "token") == settings.stream_token or _path_token(websocket) == settings.stream_token


def sample_rate_is_valid(websocket: Any, settings: Settings) -> bool:
    rate = _query(websocket, "sample-rate") or _query(websocket, "sample_rate")
    if not rate:
        return True
    try:
        return int(float(rate)) == SAMPLE_RATE
    except ValueError:
        return False


async def _queue_opener(assembly: Any, settings: Settings) -> str:
    """Speak the two opening lines, from the cache when it exists."""
    lines = list(assembly.opener_lines)
    if not lines:
        assembly.gate.release("no_opener")
        return "none"

    pcm = load_prerendered_opener(lines, settings)
    if pcm:
        duration = len(pcm) / (SAMPLE_RATE * 2)
        assembly.gate.arm(expect_utterances=1, timeout_secs=duration + 1.0)
        for frame in prerendered_audio_frames(pcm):
            await assembly.worker.queue_frame(frame)
        logger.info("opener played from the cache ({:.1f} s)", duration)
        return "prerendered"

    assembly.gate.arm(expect_utterances=len(lines))
    for index, frame in enumerate(assembly.opener_frames()):
        if index:
            await asyncio.sleep(OPENER_GAP_SECS)
        await assembly.worker.queue_frame(frame)
    return "live"


def _caller_info(call_data: Any) -> Dict[str, str]:
    """Which number is the parent is not settled yet, so keep both (P10)."""
    def value(*names: str) -> str:
        for name in names:
            try:
                found = getattr(call_data, name, None)
            except Exception:  # pragma: no cover - defensive
                found = None
            if found:
                return str(found)
        for name in names:
            try:
                found = call_data.get(name)  # type: ignore[union-attr]
            except Exception:
                found = None
            if found:
                return str(found)
        return ""

    return {
        "call_id": value("call_id"),
        "from": value("from_number", "from"),
        "to": value("to_number", "to"),
    }


async def handle_stream(
    websocket: Any,
    *,
    settings: Settings,
    make_transport: Optional[Callable] = None,
    make_pipeline: Optional[Callable] = None,
    runner_factory: Optional[Callable] = None,
    recorder_class: type = CallRecorder,
    runner_args_factory: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Handle one Exotel stream from start to the saved recording."""
    global ACTIVE_STREAMS
    started = time.time()
    result: Dict[str, Any] = {"status": "unknown", "call_ref": "", "transcript": None, "latency": None}

    if not token_is_valid(websocket, settings):
        logger.warning("refused a stream with a bad or missing token")
        await _close(websocket, 4401, "bad token")
        result["status"] = "refused_token"
        return result
    if not sample_rate_is_valid(websocket, settings):
        logger.warning("refused a stream that asked for a sample rate other than {}", SAMPLE_RATE)
        await _close(websocket, 4400, "unsupported sample-rate")
        result["status"] = "refused_sample_rate"
        return result

    await websocket.accept()

    transport = None
    recorder: Optional[CallRecorder] = None
    assembly: Any = None
    watchdog: Optional[asyncio.Task] = None
    counted = False  # only a started call may be subtracted again in finally
    try:
        if runner_args_factory is not None:
            runner_args = runner_args_factory(websocket)
        else:
            from pipecat.runner.types import WebSocketRunnerArguments

            runner_args = WebSocketRunnerArguments(websocket=websocket)
        try:
            transport = await (make_transport or create_call_transport)(runner_args, settings)
        except Exception as exc:
            logger.error("could not read the Exotel handshake: {}", exc)
            await _close(websocket, 1011, "handshake failed")
            result["status"] = "bad_handshake"
            return result

        caller = _caller_info(getattr(runner_args, "call_data", None))
        logger.info("stream started (call {} from {} to {})", caller["call_id"], caller["from"], caller["to"])

        details = claim_pending(caller["from"], caller["to"], settings.pending_dir)
        if details is None:
            logger.error(
                "refusing the stream: no pending call details match {} / {}. "
                "A call must always have its own details file (one file per call).",
                caller["from"],
                caller["to"],
            )
            await _close(websocket, 4404, "no matching call details")
            result["status"] = "refused_no_details"
            return result

        call_ref = str(details.get("call_ref") or "call")
        result["call_ref"] = call_ref
        recorder = recorder_class(
            call_ref,
            to=str(details.get("to") or caller["to"]),
            from_number=str(caller["from"]),
            student_name=str(details.get("student_name") or ""),
            parent_name=str(details.get("parent_name") or ""),
            school_name=str(details.get("school_name") or settings.school_name),
            created_at=started,
        )
        recorder.mark("stream_parsed", started)

        ACTIVE_STREAMS += 1
        counted = True
        assembly = (make_pipeline or build_call)(
            details=details, settings=settings, transport=transport, recorder=recorder
        )

        @transport.event_handler("on_client_connected")
        async def _on_connected(_transport, _client):  # pragma: no cover - real event
            recorder.mark("client_connected")
            how = await _queue_opener(assembly, settings)
            logger.info("opener queued ({})", how)

        @transport.event_handler("on_client_disconnected")
        async def _on_disconnected(_transport, _client):  # pragma: no cover - real event
            recorder.note_call_ended()
            logger.info("the parent hung up")
            try:
                await assembly.worker.queue_frame(_end_frame())
            except Exception as exc:
                logger.debug("could not queue the end frame: {}", exc)

        @assembly.worker.event_handler("on_pipeline_error")
        async def _on_pipeline_error(_worker, frame):  # pragma: no cover - real event
            logger.error("pipeline error: {}", getattr(frame, "error", frame))

        @assembly.worker.event_handler("on_pipeline_finished")
        async def _on_finished(_worker, _frame):  # pragma: no cover - real event
            recorder.note_call_ended()

        watchdog = asyncio.get_event_loop().create_task(_limit_call(assembly, recorder, settings))
        runner = runner_factory() if runner_factory is not None else _make_runner()
        await runner.run(assembly.worker)
        result["status"] = "finished" if recorder.status == "running" else recorder.status
    except asyncio.CancelledError:
        result["status"] = "cancelled"
        raise
    except Exception as exc:
        logger.exception("the call ended with an error: {}", exc)
        result["status"] = "error"
    finally:
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()
        if assembly is not None:
            assembly.hangup.cancel()
            if hasattr(assembly, "gate"):
                assembly.gate.release("server_cleanup")  # never leave the timer running
        if recorder is not None:
            saved = recorder.save(settings.transcripts_dir, status=str(result["status"]))
            result["transcript"] = saved.get("transcript")
            result["latency"] = saved.get("latency")
            result["call_ref"] = recorder.call_ref
            logger.info(
                "call finished ({}): {} turns, response latency {} s",
                result["status"],
                len(recorder.turns),
                recorder.response_latency_secs,
            )
        if transport is not None:
            try:
                await transport.cleanup()
            except Exception as exc:
                logger.debug("transport cleanup failed: {}", exc)
        if counted:
            ACTIVE_STREAMS = max(0, ACTIVE_STREAMS - 1)
    return result


def _end_frame():
    from pipecat.frames.frames import EndFrame

    return EndFrame()


def _make_runner():
    from pipecat.workers.runner import WorkerRunner

    return WorkerRunner(handle_sigint=False)


async def _limit_call(assembly: Any, recorder: CallRecorder, settings: Settings) -> None:
    """Hard limit for one call, enforced by the server (P2)."""
    try:
        await asyncio.sleep(max(30, int(settings.call_timeout_secs)))
        recorder.status = "timeout"
        recorder.note_call_ended()
        logger.warning("the call reached the {} s limit and is being ended", settings.call_timeout_secs)
        await assembly.worker.cancel(reason="call timeout")
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning("could not end the call at the time limit: {}", exc)


def create_app(
    settings: Settings,
    *,
    make_transport: Optional[Callable] = None,
    make_pipeline: Optional[Callable] = None,
    runner_factory: Optional[Callable] = None,
) -> Any:
    """Build the FastAPI app (used by ``python server.py``)."""
    from fastapi import FastAPI, WebSocket

    app = FastAPI(title="Veda", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        # Only what is needed to say "the server is up". No address, no secrets.
        return {"ok": True, "service": "veda", "active_calls": ACTIVE_STREAMS}

    async def stream(websocket: WebSocket) -> None:
        await handle_stream(
            websocket,
            settings=settings,
            make_transport=make_transport,
            make_pipeline=make_pipeline,
            runner_factory=runner_factory,
        )

    for path in STREAM_PATHS:
        app.add_api_websocket_route(path, stream)

    return app


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level: <7} | {message}")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc))
        return 2
    ensure_runtime_dirs(settings)
    logger.info("Veda is listening on {}:{}", settings.host, settings.port)
    for key, value in settings.safe_summary().items():
        logger.debug("{} = {}", key, value)

    import uvicorn

    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
