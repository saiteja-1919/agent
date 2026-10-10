"""Sarvam speech and language integration.

The existing adapters in this repository are reused, not replaced: `sarvam_tts.py`
and `sarvam_stt.py` are Pipecat processors with the connection handling, retry,
digit-safety and Telugu details already worked out, and `sarvam_llm.py` already
owns the system prompt. This module adds the three things Stage 1 needs around
them:

* a **plain streaming chat client** for the conversation controller, because the
  controller decides *what* to say and then needs the text, without a pipeline;
* **factories** that build the STT/TTS processors from validated settings so no
  credential is ever passed around by hand;
* **timeouts and error categories** with the verified endpoint contract.

Verified against the installed `pipecat-ai==1.12.0` (not assumed): the Sarvam LLM
is OpenAI-compatible, its base URL is ``https://api.sarvam.ai/v1`` for
``sarvam-105b-conversations`` (``/v2`` for other models), the API key travels as
both the bearer token and the ``api-subscription-key`` header, and the speech
socket model ids take a ``-realtime`` suffix.

The model ids, the exact streaming behaviour and the audio format can only be
*confirmed* by a live call; that is stated in the delivery report rather than
claimed here.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional, Sequence

import httpx
from loguru import logger

from app.integrations.errors import ErrorCategory, ProviderError, classify_exception, classify_status

#: Verified from the installed package: only this model uses the ``/v1`` base.
V1_MODELS = frozenset({"sarvam-105b-conversations"})
DEFAULT_BASE_URL = "https://api.sarvam.ai"
DEFAULT_STREAM_BASE_PATH = "v1"


def resolve_base_url(model: str, override: str = "") -> str:
    """The base URL for a model, matching the installed client's rule."""
    if override:
        return override.rstrip("/")
    version = DEFAULT_STREAM_BASE_PATH if model in V1_MODELS else "v2"
    return f"{DEFAULT_BASE_URL}/{version}"


@dataclass
class LLMReply:
    """A generated reply plus what it cost, with honest gaps when unmeasured."""

    text: str = ""
    first_token_ms: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    error: Optional[ProviderError] = None
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.error is None

    def sanitized(self) -> Dict[str, Any]:
        return {
            # Length only: the reply text itself is conversation content.
            "chars": len(self.text),
            "first_token_ms": self.first_token_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cancelled": self.cancelled,
            "error": self.error.sanitized() if self.error else None,
        }


class SarvamLLMClient:
    """OpenAI-compatible streaming chat against Sarvam.

    Never logs a prompt or a reply: the prompt contains a student's name and the
    reply is conversation content (section 17.8).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "sarvam-105b-conversations",
        base_url: str = "",
        timeout_secs: float = 15.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = resolve_base_url(model, base_url)
        self.timeout_secs = timeout_secs
        self._transport = transport
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    def _headers(self) -> Dict[str, str]:
        # Both headers are sent because the installed client does exactly that.
        return {
            "Authorization": f"Bearer {self.api_key}",
            "api-subscription-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def _payload(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        stream: bool,
    ) -> Dict[str, Any]:
        return {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "stream": stream,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    async def stream_reply(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = 0.6,
        max_tokens: int = 300,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[str]:
        """Yield reply text chunks as they arrive.

        The caller cancels by flipping ``cancelled`` (a parent interrupting or
        hanging up); the loop checks it between chunks so no more work is done
        than is needed. A transport failure ends the stream quietly: section 14.4
        says not to expose a provider error to the parent.
        """
        if not self.configured:
            logger.warning("Sarvam LLM is not configured; no reply can be generated")
            return

        timeout = httpx.Timeout(self.timeout_secs, connect=min(5.0, self.timeout_secs))
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=timeout,
                transport=self._transport,
                headers=self._headers(),
            ) as client:
                async with client.stream(
                    "POST",
                    "/chat/completions",
                    json=self._payload(
                        messages, temperature=temperature, max_tokens=max_tokens, stream=True
                    ),
                ) as response:
                    if response.status_code >= 400:
                        error = ProviderError(
                            category=classify_status(response.status_code),
                            message="Sarvam refused the generation request",
                            provider="sarvam",
                            http_status=response.status_code,
                        )
                        logger.warning("sarvam llm: {}", error)
                        return
                    async for chunk in _iter_sse_text(response):
                        if cancelled is not None and cancelled():
                            logger.debug("sarvam llm: generation cancelled")
                            return
                        if chunk:
                            yield chunk
        except Exception as exc:
            category, _ = classify_exception(exc, request_sent=True)
            logger.warning(
                "sarvam llm: {}", ProviderError(category=category, provider="sarvam", message=str(type(exc).__name__))
            )
            return

    async def complete_reply(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = 0.6,
        max_tokens: int = 300,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> LLMReply:
        """Collect a whole reply, measuring the first-token latency.

        One HTTP request, one accumulator: this is the path used when the caller
        wants text first and will chunk it for speech itself (section 14.2).
        """
        if not self.configured:
            return LLMReply(
                error=ProviderError(
                    category=ErrorCategory.CONFIGURATION,
                    message="Sarvam LLM is not configured",
                    provider="sarvam",
                )
            )

        started = time.monotonic()
        reply = LLMReply()
        timeout = httpx.Timeout(self.timeout_secs, connect=min(5.0, self.timeout_secs))
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=timeout,
                transport=self._transport,
                headers=self._headers(),
            ) as client:
                async with client.stream(
                    "POST",
                    "/chat/completions",
                    json=self._payload(
                        messages, temperature=temperature, max_tokens=max_tokens, stream=True
                    ),
                ) as response:
                    if response.status_code >= 400:
                        reply.error = ProviderError(
                            category=classify_status(response.status_code),
                            message="Sarvam refused the generation request",
                            provider="sarvam",
                            http_status=response.status_code,
                        )
                        return reply
                    pieces: List[str] = []
                    async for text, usage in _iter_sse(response):
                        if cancelled is not None and cancelled():
                            reply.cancelled = True
                            break
                        if text:
                            if reply.first_token_ms is None:
                                reply.first_token_ms = int((time.monotonic() - started) * 1000)
                            pieces.append(text)
                        if usage:
                            reply.prompt_tokens = usage.get("prompt_tokens", reply.prompt_tokens)
                            reply.completion_tokens = usage.get(
                                "completion_tokens", reply.completion_tokens
                            )
                    reply.text = "".join(pieces).strip()
        except Exception as exc:
            category, retryable = classify_exception(exc, request_sent=True)
            reply.error = ProviderError(
                category=category,
                message=f"the generation request failed ({type(exc).__name__})",
                provider="sarvam",
                retryable=retryable,
            )
            logger.warning("sarvam llm: {}", reply.error)
        return reply


async def _iter_sse(response: httpx.Response) -> AsyncIterator[tuple]:
    """Yield ``(text, usage)`` from an OpenAI-style server-sent event stream."""
    async for line in response.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            if data == "[DONE]":
                return
            continue
        try:
            parsed = json.loads(data)
        except ValueError:
            logger.debug("sarvam llm: skipped an unreadable stream chunk")
            continue
        if not isinstance(parsed, dict):
            continue
        usage = parsed.get("usage")
        choices = parsed.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta") or {}
            if isinstance(delta, dict):
                text = delta.get("content") or ""
            if not text:
                text = choices[0].get("text") or ""
        if text or usage:
            yield text, usage if isinstance(usage, dict) else None


async def _iter_sse_text(response: httpx.Response) -> AsyncIterator[str]:
    async for text, _usage in _iter_sse(response):
        if text:
            yield text


def build_tts_processor(settings, **overrides: Any):
    """Build the existing streaming TTS processor from validated settings."""
    from sarvam_tts import SarvamTTS

    options: Dict[str, Any] = {
        "api_key": settings.base.sarvam_api_key,
        "model": settings.base.sarvam_tts_model,
        "language_code": settings.base.sarvam_stt_language or "te-IN",
        "speaker": settings.base.sarvam_tts_voice,
        "pace": settings.base.sarvam_tts_pace,
        "temperature": settings.base.sarvam_tts_temperature,
        "connect_timeout_secs": min(10.0, settings.tts_timeout_secs),
        "flush_timeout_secs": settings.tts_timeout_secs,
    }
    options.update(overrides)
    return SarvamTTS(**options)


def build_stt_processor(settings, **overrides: Any):
    """Build the existing realtime STT processor from validated settings."""
    from sarvam_stt import SarvamRealtimeSTT

    options: Dict[str, Any] = {
        "api_key": settings.base.sarvam_api_key,
        "model": settings.base.sarvam_stt_realtime_model,
        "language_code": settings.base.sarvam_stt_language or "te-IN",
        "sample_rate": _sample_rate_from_stream_url(settings.base.exotel_stream_url),
    }
    options.update(overrides)
    return SarvamRealtimeSTT(**options)


def _sample_rate_from_stream_url(stream_url: str) -> int:
    """The stream's sample rate, defaulting to the verified 8 kHz."""
    from urllib.parse import parse_qs, urlparse

    try:
        query = parse_qs(urlparse(stream_url or "").query)
    except Exception:  # pragma: no cover - defensive
        return 8000
    for key in ("sample-rate", "sample_rate"):
        values = query.get(key)
        if values:
            try:
                return int(values[0])
            except (TypeError, ValueError):
                return 8000
    return 8000


async def render_pcm(settings, text: str, *, timeout: Optional[float] = None, connector=None) -> bytes:
    """Synthesise one phrase to raw PCM, for the audio cache and the opener.

    Reuses the existing processor's offline synthesis path; it is never used
    inside a live call because it reads the socket directly.
    """
    processor = build_tts_processor(settings, connector=connector)
    try:
        return await processor.synthesize(text, timeout=timeout or settings.tts_timeout_secs)
    finally:
        stopper = getattr(processor, "stop", None)
        if stopper is not None:
            try:
                await stopper()
            except Exception:  # pragma: no cover - cleanup only
                pass


def normalise_transcript(text: str) -> str:
    """Tidy a final transcript without changing its meaning.

    Collapses whitespace and drops control characters. It deliberately does *not*
    translate or transliterate: the transcript is only ever used in memory for one
    turn and never stored (STORE_TRANSCRIPTS=false).
    """
    if not text:
        return ""
    cleaned = "".join(
        char for char in text if char == "\n" or char == "\t" or ord(char) >= 32
    )
    return " ".join(cleaned.split())
