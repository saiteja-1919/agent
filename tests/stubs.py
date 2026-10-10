"""Stand-ins for Pipecat, FastAPI, websockets and aiohttp.

These exist only so the offline test suite can run without the heavy libraries
and without a single network call. They mimic signatures, never behaviour: a
live phone call is the only real proof that the pipeline works.

The stand-in ``TranscriptionFrame`` keeps its REQUIRED ``timestamp`` field on
purpose - building one without a timestamp is exactly the bug that silenced a
real call after the parent's first sentence (P1), so a test must be able to
reproduce it.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


# ------------------------------------------------------------------- frames
class Frame:
    name = "Frame"


class DataFrame(Frame):
    name = "DataFrame"


class ControlFrame(Frame):
    name = "ControlFrame"


class SystemFrame(Frame):
    name = "SystemFrame"


@dataclass
class TextFrame(DataFrame):
    text: str = ""


@dataclass
class InputAudioRawFrame(Frame):
    audio: bytes = b""
    sample_rate: int = 8000
    num_channels: int = 1


@dataclass
class OutputAudioRawFrame(Frame):
    audio: bytes = b""
    sample_rate: int = 8000
    num_channels: int = 1


@dataclass
class TTSAudioRawFrame(OutputAudioRawFrame):
    pass


@dataclass
class TranscriptionFrame(TextFrame):
    user_id: str = ""
    timestamp: Optional[str] = None
    language: Any = None
    finalized: bool = False

    def __post_init__(self) -> None:
        if self.timestamp is None:
            raise TypeError("TranscriptionFrame requires a timestamp (this broke a real call - P1)")


@dataclass
class InterimTranscriptionFrame(TextFrame):
    user_id: str = ""
    timestamp: Optional[str] = None


@dataclass
class LLMTextFrame(TextFrame):
    pass


@dataclass
class TTSSpeakFrame(DataFrame):
    text: str = ""


class StartFrame(Frame):
    pass


class EndFrame(Frame):
    pass


class CancelFrame(Frame):
    pass


class StopFrame(Frame):
    pass


class InterruptionFrame(SystemFrame):
    pass


class UserStartedSpeakingFrame(SystemFrame):
    pass


class UserStoppedSpeakingFrame(SystemFrame):
    pass


class BotStartedSpeakingFrame(SystemFrame):
    pass


class BotStoppedSpeakingFrame(SystemFrame):
    pass


class TTSStartedFrame(ControlFrame):
    pass


class TTSStoppedFrame(ControlFrame):
    pass


class LLMFullResponseStartFrame(ControlFrame):
    pass


class LLMFullResponseEndFrame(ControlFrame):
    pass


class ErrorFrame(SystemFrame):
    error: str = ""

    def __init__(self, error: str = "", **kwargs: Any) -> None:
        self.error = error


class MetricsFrame(ControlFrame):
    pass


class FrameDirection:
    DOWNSTREAM = "downstream"
    UPSTREAM = "upstream"


# ---------------------------------------------------------- frame processors
class FrameProcessor:
    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = dict(kwargs)
        self.pushed: List[Any] = []
        self.errors: List[str] = []
        self._tasks: List[asyncio.Task] = []

    async def process_frame(self, frame: Frame, direction: Any = FrameDirection.DOWNSTREAM) -> None:
        return None

    async def push_frame(self, frame: Frame, direction: Any = FrameDirection.DOWNSTREAM) -> None:
        self.pushed.append(frame)

    def create_task(self, coroutine, name: str | None = None) -> asyncio.Task:
        task = asyncio.ensure_future(coroutine)
        self._tasks.append(task)
        return task

    async def push_error(self, error_msg: str, exception: Exception | None = None, **kwargs: Any) -> None:
        self.errors.append(error_msg)

    def pushed_types(self) -> List[str]:
        return [type(frame).__name__ for frame in self.pushed]


# -------------------------------------------------------------------- misc
class VADParams:
    def __init__(self, confidence: float = 0.7, start_secs: float = 0.2, stop_secs: float = 0.2,
                 min_volume: float = 0.6) -> None:
        self.confidence = confidence
        self.start_secs = start_secs
        self.stop_secs = stop_secs
        self.min_volume = min_volume


class SileroVADAnalyzer:
    def __init__(self, *, sample_rate: int | None = None, params: VADParams | None = None) -> None:
        self.sample_rate = sample_rate
        self.params = params or VADParams()


class Pipeline:
    def __init__(self, processors: List[Any] | None = None) -> None:
        self.processors = list(processors or [])


class PipelineParams:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)


class PipelineWorker:
    def __init__(self, pipeline: Pipeline, **kwargs: Any) -> None:
        self.pipeline = pipeline
        self.kwargs = dict(kwargs)
        self.queued: List[Any] = []
        self.cancelled = False
        self.handlers: Dict[str, Any] = {}
        self.finished = False

    async def queue_frame(self, frame: Frame, direction: Any = None) -> None:
        self.queued.append(frame)

    async def cancel(self, reason: str | None = None) -> None:
        self.cancelled = True

    async def end(self, reason: str | None = None) -> None:
        self.finished = True

    def event_handler(self, name: str):
        def decorator(handler):
            self.handlers[name] = handler
            return handler

        return decorator


class WorkerRunner:
    """Runs nothing: the offline tests inject their own runner."""

    def __init__(self, *, handle_sigint: bool = True, **kwargs: Any) -> None:
        self.handle_sigint = handle_sigint
        self.kwargs = dict(kwargs)

    async def run(self, worker: Any = None, *, auto_end: bool = True) -> None:
        if worker is not None and hasattr(worker, "_stub_run"):
            await worker._stub_run()


class LLMContext:
    def __init__(self, messages: List[Any] | None = None, **kwargs: Any) -> None:
        self.messages = messages if messages is not None else []
        self.kwargs = dict(kwargs)


class LLMUserAggregatorParams:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)


class _FakeAggregator(FrameProcessor):
    def __init__(self, role: str) -> None:
        super().__init__()
        self.role = role


class LLMContextAggregatorPair:
    def __init__(self, context: LLMContext, *, user_params: Any = None, assistant_params: Any = None,
                 **kwargs: Any) -> None:
        self.context = context
        self.user_params = user_params or LLMUserAggregatorParams()
        self.assistant_params = assistant_params
        self._user = _FakeAggregator("user")
        self._assistant = _FakeAggregator("assistant")

    def user(self) -> _FakeAggregator:
        return self._user

    def assistant(self) -> _FakeAggregator:
        return self._assistant


class _StubLLMSettings:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)


class SarvamLLMService:
    Settings = _StubLLMSettings

    def __init__(self, *, api_key: str = "", settings: Any = None, **kwargs: Any) -> None:
        self.api_key = api_key
        self.settings = settings
        self.kwargs = dict(kwargs)


class FastAPIWebsocketParams:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)

    def __getattr__(self, item: str) -> Any:  # pragma: no cover - defensive
        raise AttributeError(item)


@dataclass
class _CallData:
    call_id: str = ""
    from_number: str = ""
    to_number: str = ""


@dataclass
class WebSocketRunnerArguments:
    websocket: Any = None
    transport_type: str | None = None
    call_data: Any = None


async def create_transport(runner_args: Any, transport_params: dict) -> Any:
    """The real one parses the handshake; here it just reports the call data."""
    runner_args.call_data = _CallData()
    return types.SimpleNamespace(name="transport")


# ------------------------------------------------------------------ fastapi
class _Route:
    def __init__(self, path: str, handler: Any) -> None:
        self.path = path
        self.handler = handler


class FastAPI:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)
        self.routes: List[_Route] = []
        self.ws_routes: List[_Route] = []

    def get(self, path: str):
        def decorator(handler):
            self.routes.append(_Route(path, handler))
            return handler

        return decorator

    def add_api_websocket_route(self, path: str, handler: Any) -> None:
        self.ws_routes.append(_Route(path, handler))


class WebSocket:
    def __init__(self, query: str = "") -> None:
        self.query_params: Dict[str, str] = {}
        for part in query.split("&"):
            if "=" in part:
                key, value = part.split("=", 1)
                self.query_params[key] = value
        self.accepted = False
        self.closed: List[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append(code)


# ----------------------------------------------------------------- network
async def _offline_connect(*args: Any, **kwargs: Any):
    raise RuntimeError("the offline tests never open a real socket")


class FakeConnection:
    """A WebSocket that only knows how to hand out prepared messages."""

    def __init__(self, messages: List[Any] | None = None, send_error: Exception | None = None) -> None:
        self.messages = list(messages or [])
        self.sent: List[str] = []
        self.closed = False
        self.send_error = send_error

    async def send(self, payload: Any) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        item = self.messages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def json_sent(self) -> List[Any]:
        import json

        return [json.loads(payload) for payload in self.sent]


class Connector:
    """Hands out the prepared connections in order and records the calls."""

    def __init__(self, connections: List[FakeConnection] | None = None) -> None:
        self.connections = list(connections or [])
        self.calls: List[Any] = []

    async def __call__(self, url: str, **kwargs: Any) -> FakeConnection:
        self.calls.append({"url": url, "kwargs": kwargs})
        if not self.connections:
            raise RuntimeError("no more fake connections")
        return self.connections.pop(0)


class StopAfter:
    """A sleep() replacement that ends a retry loop by cancelling it."""

    def __init__(self, times: int = 1) -> None:
        self.times = times
        self.delays: List[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self.times:
            raise asyncio.CancelledError()


_INSTALLED = False


def install() -> None:
    """Put the stand-ins in place. Safe to call from every test module."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    _module("pipecat")
    _module(
        "pipecat.frames",
    )
    _module(
        "pipecat.frames.frames",
        Frame=Frame,
        DataFrame=DataFrame,
        ControlFrame=ControlFrame,
        SystemFrame=SystemFrame,
        TextFrame=TextFrame,
        InputAudioRawFrame=InputAudioRawFrame,
        OutputAudioRawFrame=OutputAudioRawFrame,
        TTSAudioRawFrame=TTSAudioRawFrame,
        TranscriptionFrame=TranscriptionFrame,
        InterimTranscriptionFrame=InterimTranscriptionFrame,
        LLMTextFrame=LLMTextFrame,
        TTSSpeakFrame=TTSSpeakFrame,
        StartFrame=StartFrame,
        EndFrame=EndFrame,
        CancelFrame=CancelFrame,
        StopFrame=StopFrame,
        InterruptionFrame=InterruptionFrame,
        UserStartedSpeakingFrame=UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame=UserStoppedSpeakingFrame,
        BotStartedSpeakingFrame=BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame=BotStoppedSpeakingFrame,
        TTSStartedFrame=TTSStartedFrame,
        TTSStoppedFrame=TTSStoppedFrame,
        LLMFullResponseStartFrame=LLMFullResponseStartFrame,
        LLMFullResponseEndFrame=LLMFullResponseEndFrame,
        LLMFullResponseEndFrameAlias=LLMFullResponseEndFrame,
        ErrorFrame=ErrorFrame,
        MetricsFrame=MetricsFrame,
    )
    _module("pipecat.processors")
    _module(
        "pipecat.processors.frame_processor",
        FrameProcessor=FrameProcessor,
        FrameDirection=FrameDirection,
    )
    _module("pipecat.utils")
    _module(
        "pipecat.utils.time",
        time_now_iso8601=lambda: "2026-10-06T10:00:00+00:00",
    )
    _module("pipecat.audio")
    _module("pipecat.audio.vad")
    _module("pipecat.audio.vad.silero", SileroVADAnalyzer=SileroVADAnalyzer)
    _module("pipecat.audio.vad.vad_analyzer", VADParams=VADParams, VADAnalyzer=SileroVADAnalyzer)
    _module("pipecat.pipeline")
    _module("pipecat.pipeline.pipeline", Pipeline=Pipeline)
    _module(
        "pipecat.pipeline.worker",
        PipelineParams=PipelineParams,
        PipelineWorker=PipelineWorker,
        PipelineTask=PipelineWorker,
    )
    _module("pipecat.pipeline.runner", PipelineRunner=WorkerRunner)
    _module("pipecat.workers")
    _module("pipecat.workers.runner", WorkerRunner=WorkerRunner)
    _module("pipecat.processors.aggregators")
    _module("pipecat.processors.aggregators.llm_context", LLMContext=LLMContext)
    _module(
        "pipecat.processors.aggregators.llm_response_universal",
        LLMContextAggregatorPair=LLMContextAggregatorPair,
        LLMUserAggregatorParams=LLMUserAggregatorParams,
    )
    _module("pipecat.services")
    _module("pipecat.services.sarvam")
    _module("pipecat.services.sarvam.llm", SarvamLLMService=SarvamLLMService)
    _module("pipecat.transports")
    _module("pipecat.transports.websocket")
    _module(
        "pipecat.transports.websocket.fastapi",
        FastAPIWebsocketParams=FastAPIWebsocketParams,
    )
    _module("pipecat.runner")
    _module(
        "pipecat.runner.utils",
        create_transport=create_transport,
    )
    _module("pipecat.runner.types", WebSocketRunnerArguments=WebSocketRunnerArguments, CallData=_CallData)

    _module("fastapi", FastAPI=FastAPI, WebSocket=WebSocket)
    _module("websockets", connect=_offline_connect)
    _module("aiohttp")
