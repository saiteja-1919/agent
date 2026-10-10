"""Veda configuration: the single source of truth for every setting.

Everything Veda can be tuned with lives in the ``.env`` file next to this file.
``.env`` is loaded with ``override=True`` so a stale PowerShell ``$env:``
variable can never quietly win over the file.

``Settings.from_env()`` is a pure function of a mapping, which is what the
offline tests use; ``load_settings()`` is the real entry point.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
ENV_PATH = APP_DIR / ".env"
TRANSCRIPTS_DIR = APP_DIR / "transcripts"
PENDING_DIR = APP_DIR / ".pending_calls"
NAME_CACHE_PATH = APP_DIR / "name_pronunciations.json"
OPENER_CACHE_DIR = APP_DIR / "opener_cache"

#: WebSocket paths the server answers. The StreamUrl in .env must use one of them.
STREAM_PATHS = ("/v1/exotel/stream", "/v1/exotel/stream/ws", "/ws")

#: The school name must never be left at a placeholder (P16: the school was
#: once spoken as "the school").
PLACEHOLDER_SCHOOL_NAMES = frozenset(
    {"the school", "school", "your school", "our school", "your school name", "xxx"}
)

#: Sarvam Bulbul v3 voices (from Sarvam's text-to-speech WebSocket reference).
BULBUL_V3_VOICES = frozenset(
    {
        "shubh", "aditya", "ritu", "priya", "neha", "rahul", "pooja", "rohan", "simran",
        "kavya", "amit", "dev", "ishita", "shreya", "ratan", "varun", "manan", "sumit",
        "roopa", "kabir", "aayan", "ashutosh", "advait", "anand", "tanya", "tarun",
        "sunny", "mani", "gokul", "vijay", "shruti", "suhani", "mohit", "kavitha",
        "rehan", "soham", "rupali",
    }
)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: Every key Veda reads. Anything else in the shell is ignored, so a stray
#: variable from another tool can never change how Veda behaves.
ENV_KEYS = frozenset(
    {
        "EXOTEL_ACCOUNT_SID", "EXOTEL_API_KEY", "EXOTEL_API_TOKEN", "EXOTEL_CALLER_ID",
        "EXOTEL_API_BASE", "EXOTEL_STREAM_URL", "CALL_PROVIDER",
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_CALLER_ID", "TWILIO_API_BASE",
        "SARVAM_API_KEY", "SARVAM_STT_MODEL", "SARVAM_STT_LANGUAGE", "SARVAM_LLM_MODEL",
        "SARVAM_TTS_MODEL", "SARVAM_TTS_VOICE", "SARVAM_TTS_PACE", "SARVAM_TTS_TEMPERATURE",
        "SCHOOL_NAME", "LLM_TEMPERATURE", "LLM_MAX_TOKENS",
        "VAD_STOP_SECS", "VAD_START_SECS", "VAD_MIN_VOLUME", "USER_TURN_STOP_TIMEOUT_SECS",
        "PIPECAT_HEARTBEAT_SECS", "HOST", "PORT", "CALL_TIMEOUT_SECS", "IDLE_TIMEOUT_SECS",
        "STREAM_TOKEN", "OPENER_GATE", "OPENER_GATE_MAX_SECS", "DIGIT_FILTER",
        "AGENT_HANGUP", "HANGUP_MARKER", "HANGUP_GRACE_SECS", "OPENER_PRERENDER",
    }
)

#: VEDA_ prefixed names win over the generic ones, in case another program on
#: the same computer already sets HOST or PORT.
ENV_ALIASES = {"VEDA_HOST": "HOST", "VEDA_PORT": "PORT"}


def filter_environment(source: Mapping[str, str]) -> Dict[str, str]:
    """Keep only Veda's own keys, and let the VEDA_ aliases win."""
    env = {key: value for key, value in source.items() if key in ENV_KEYS or key in ENV_ALIASES}
    for alias, key in ENV_ALIASES.items():
        if alias in env:
            env[key] = env[alias]
    return env


class ConfigError(Exception):
    """Raised when the configuration cannot be used. Message lists every problem."""


def _bool(value: Optional[str], default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Every knob Veda reads. Built by :func:`load_settings` or ``from_env``."""

    # Exotel
    exotel_account_sid: str = ""
    exotel_api_key: str = ""
    exotel_api_token: str = ""
    exotel_caller_id: str = ""
    exotel_api_base: str = "https://api.exotel.com"
    exotel_stream_url: str = ""

    # Which company places the calls: "exotel" (default) or "twilio".
    call_provider: str = "exotel"

    # Twilio (used only when call_provider is "twilio")
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_caller_id: str = ""
    twilio_api_base: str = "https://api.twilio.com"

    # Sarvam
    sarvam_api_key: str = ""
    sarvam_stt_model: str = "saaras:v3"
    sarvam_stt_language: str = "te-IN"
    sarvam_llm_model: str = "sarvam-105b-conversations"
    sarvam_tts_model: str = "bulbul:v3"

    # School
    school_name: str = ""

    # Voice and replies
    sarvam_tts_voice: str = "shubh"
    sarvam_tts_pace: float = 1.15
    sarvam_tts_temperature: float = 0.6
    llm_temperature: float = 0.6
    llm_max_tokens: int = 300

    # Turn taking
    vad_stop_secs: float = 0.35
    vad_start_secs: float = 0.2
    vad_min_volume: float = 0.6
    user_turn_stop_timeout_secs: float = 0.5
    pipecat_heartbeat_secs: float = 1.0

    # Safety
    host: str = "127.0.0.1"
    port: int = 8000
    call_timeout_secs: int = 600
    idle_timeout_secs: int = 60
    stream_token: str = ""

    # Opener, digit filter and hanging up
    opener_gate: bool = True
    opener_gate_max_secs: float = 12.0
    digit_filter: bool = True
    agent_hangup: bool = True
    hangup_marker: str = "[END]"
    hangup_grace_secs: float = 0.8
    opener_prerender: bool = False

    # Where things are written
    app_dir: Path = APP_DIR

    # ---------------------------------------------------------------- helpers
    @property
    def transcripts_dir(self) -> Path:
        return self.app_dir / "transcripts"

    @property
    def pending_dir(self) -> Path:
        return self.app_dir / ".pending_calls"

    @property
    def name_cache_path(self) -> Path:
        return self.app_dir / "name_pronunciations.json"

    @property
    def opener_cache_dir(self) -> Path:
        return self.app_dir / "opener_cache"

    @property
    def stream_path(self) -> str:
        return urlparse(self.exotel_stream_url).path

    @property
    def sarvam_stt_realtime_model(self) -> str:
        """Sarvam's realtime socket wants the ``-realtime`` suffix."""
        model = self.sarvam_stt_model.strip() or "saaras:v3"
        return model if model.endswith("-realtime") else f"{model}-realtime"

    @property
    def public_health_url(self) -> str:
        """Best-effort https form of the tunnel for ``check.py`` (never logged)."""
        parsed = urlparse(self.exotel_stream_url)
        if not parsed.netloc:
            return ""
        scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
        return f"{scheme}://{parsed.netloc}/health"

    def safe_summary(self) -> Dict[str, Any]:
        """Settings with every secret masked - safe to print or log."""
        def masked(value: str) -> str:
            return "set" if value else "missing"

        return {
            "exotel_api_base": self.exotel_api_base,
            "exotel_account_sid": masked(self.exotel_account_sid),
            "exotel_api_key": masked(self.exotel_api_key),
            "exotel_api_token": masked(self.exotel_api_token),
            "exotel_caller_id": self.exotel_caller_id or "missing",
            "exotel_stream_url": self.exotel_stream_url or "missing",
            "call_provider": self.call_provider,
            "twilio_account_sid": masked(self.twilio_account_sid),
            "twilio_auth_token": masked(self.twilio_auth_token),
            "twilio_caller_id": self.twilio_caller_id or "missing",
            "twilio_api_base": self.twilio_api_base,
            "sarvam_api_key": masked(self.sarvam_api_key),
            "sarvam_stt_model": self.sarvam_stt_realtime_model,
            "sarvam_stt_language": self.sarvam_stt_language,
            "sarvam_llm_model": self.sarvam_llm_model,
            "sarvam_tts_model": self.sarvam_tts_model,
            "sarvam_tts_voice": self.sarvam_tts_voice,
            "sarvam_tts_pace": self.sarvam_tts_pace,
            "sarvam_tts_temperature": self.sarvam_tts_temperature,
            "llm_temperature": self.llm_temperature,
            "llm_max_tokens": self.llm_max_tokens,
            "school_name": self.school_name or "missing",
            "host": self.host,
            "port": self.port,
            "call_timeout_secs": self.call_timeout_secs,
            "idle_timeout_secs": self.idle_timeout_secs,
            "vad_start_secs": self.vad_start_secs,
            "vad_stop_secs": self.vad_stop_secs,
            "vad_min_volume": self.vad_min_volume,
            "user_turn_stop_timeout_secs": self.user_turn_stop_timeout_secs,
            "pipecat_heartbeat_secs": self.pipecat_heartbeat_secs,
            "stream_token": masked(self.stream_token),
            "opener_gate": self.opener_gate,
            "digit_filter": self.digit_filter,
            "agent_hangup": self.agent_hangup,
            "opener_prerender": self.opener_prerender,
        }

    # ---------------------------------------------------------------- building
    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        require_secrets: bool = True,
        require_school: bool = True,
    ) -> "Settings":
        """Validate a plain mapping (``os.environ`` or a test dictionary)."""
        env = filter_environment(env)
        errors: List[str] = []

        def text(key: str, default: str = "") -> str:
            value = env.get(key)
            return default if value is None or str(value).strip() == "" else str(value).strip()

        def number(key: str, default: float, low: float, high: float) -> float:
            raw = text(key, "")
            if raw == "":
                return default
            try:
                value = float(raw)
            except ValueError:
                errors.append(f"{key} must be a number, got {raw!r}")
                return default
            if not (low <= value <= high):
                errors.append(f"{key} must be between {low} and {high}, got {raw!r}")
                return default
            return value

        def integer(key: str, default: int, low: int, high: Optional[int] = None) -> int:
            raw = text(key, "")
            if raw == "":
                return default
            try:
                value = int(float(raw))
            except ValueError:
                errors.append(f"{key} must be a whole number, got {raw!r}")
                return default
            if value < low:
                errors.append(f"{key} must be at least {low}, got {raw!r}")
                return default
            if high is not None and value > high:
                errors.append(f"{key} must be at most {high}, got {raw!r}")
                return default
            return value

        settings = cls(
            exotel_account_sid=text("EXOTEL_ACCOUNT_SID"),
            exotel_api_key=text("EXOTEL_API_KEY"),
            exotel_api_token=text("EXOTEL_API_TOKEN"),
            exotel_caller_id=text("EXOTEL_CALLER_ID"),
            exotel_api_base=text("EXOTEL_API_BASE", "https://api.exotel.com").rstrip("/"),
            exotel_stream_url=text("EXOTEL_STREAM_URL"),
            call_provider=text("CALL_PROVIDER", "exotel").lower(),
            twilio_account_sid=text("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=text("TWILIO_AUTH_TOKEN"),
            twilio_caller_id=text("TWILIO_CALLER_ID"),
            twilio_api_base=text("TWILIO_API_BASE", "https://api.twilio.com").rstrip("/"),
            sarvam_api_key=text("SARVAM_API_KEY"),
            sarvam_stt_model=text("SARVAM_STT_MODEL", "saaras:v3"),
            sarvam_stt_language=text("SARVAM_STT_LANGUAGE", "te-IN"),
            sarvam_llm_model=text("SARVAM_LLM_MODEL", "sarvam-105b-conversations"),
            sarvam_tts_model=text("SARVAM_TTS_MODEL", "bulbul:v3"),
            school_name=text("SCHOOL_NAME"),
            sarvam_tts_voice=text("SARVAM_TTS_VOICE", "shubh").lower(),
            sarvam_tts_pace=number("SARVAM_TTS_PACE", 1.15, 0.5, 2.0),
            sarvam_tts_temperature=number("SARVAM_TTS_TEMPERATURE", 0.6, 0.01, 2.0),
            llm_temperature=number("LLM_TEMPERATURE", 0.6, 0.0, 1.5),
            llm_max_tokens=integer("LLM_MAX_TOKENS", 300, 60, 2000),
            vad_stop_secs=number("VAD_STOP_SECS", 0.35, 0.3, 1.5),
            vad_start_secs=number("VAD_START_SECS", 0.2, 0.05, 1.0),
            vad_min_volume=number("VAD_MIN_VOLUME", 0.6, 0.0, 1.0),
            user_turn_stop_timeout_secs=number("USER_TURN_STOP_TIMEOUT_SECS", 0.5, 0.3, 3.0),
            pipecat_heartbeat_secs=number("PIPECAT_HEARTBEAT_SECS", 1.0, 0.2, 30.0),
            host=text("HOST", "127.0.0.1"),
            port=integer("PORT", 8000, 1, 65535),
            call_timeout_secs=integer("CALL_TIMEOUT_SECS", 600, 30),
            stream_token=text("STREAM_TOKEN"),
            opener_gate=_bool(env.get("OPENER_GATE"), True),
            opener_gate_max_secs=number("OPENER_GATE_MAX_SECS", 12.0, 2.0, 60.0),
            digit_filter=_bool(env.get("DIGIT_FILTER"), True),
            agent_hangup=_bool(env.get("AGENT_HANGUP"), True),
            hangup_marker=text("HANGUP_MARKER", "[END]"),
            hangup_grace_secs=number("HANGUP_GRACE_SECS", 0.8, 0.0, 10.0),
            opener_prerender=_bool(env.get("OPENER_PRERENDER"), False),
        )

        raw_idle = text("IDLE_TIMEOUT_SECS", "")
        idle = settings.idle_timeout_secs
        if raw_idle != "":
            try:
                idle = int(float(raw_idle))
            except ValueError:
                errors.append(f"IDLE_TIMEOUT_SECS must be a whole number, got {raw_idle!r}")
                idle = 60
        if idle != 0 and idle < 10:
            errors.append(f"IDLE_TIMEOUT_SECS must be 0 (never) or at least 10, got {idle!r}")
        object.__setattr__(settings, "idle_timeout_secs", idle)

        # ------------------------------------------------------------ cross checks
        if settings.sarvam_tts_model.startswith("bulbul:v3") and settings.sarvam_tts_voice not in BULBUL_V3_VOICES:
            errors.append(
                "SARVAM_TTS_VOICE must be a bulbul:v3 voice in lower case "
                f"(for example shubh, ritu, kavya); got {settings.sarvam_tts_voice!r}"
            )
        if settings.call_provider not in ("exotel", "twilio"):
            errors.append(f"CALL_PROVIDER must be exotel or twilio, got {settings.call_provider!r}")
        errors.extend(
            _stream_url_errors(settings.exotel_stream_url, settings.stream_token, settings.call_provider)
        )
        if settings.call_provider == "twilio":
            errors.extend(_twilio_caller_id_errors(settings.twilio_caller_id, required=require_secrets))
        else:
            errors.extend(_caller_id_errors(settings.exotel_caller_id, required=require_secrets))
        if settings.stream_token and not _TOKEN_RE.match(settings.stream_token):
            errors.append("STREAM_TOKEN may only use letters, digits, - and _")
        elif settings.stream_token and len(settings.stream_token) < 12:
            errors.append("STREAM_TOKEN must be at least 12 characters long")

        if require_school:
            errors.extend(_school_name_errors(settings.school_name))

        if require_secrets:
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
            for key, value in own + (("SARVAM_API_KEY", settings.sarvam_api_key),):
                if not value:
                    errors.append(f"{key} is missing")
            if not settings.exotel_stream_url:
                errors.append("EXOTEL_STREAM_URL is missing")

        if errors:
            raise ConfigError("Configuration problems:\n  - " + "\n  - ".join(errors))
        return settings


def _school_name_errors(school_name: str) -> List[str]:
    if not school_name or school_name.strip().lower() in PLACEHOLDER_SCHOOL_NAMES:
        return [
            "SCHOOL_NAME is missing or is still a placeholder. Set the real school "
            "name in .env before calling (option --school-name overrides it)."
        ]
    if len(school_name) > 100:
        return ["SCHOOL_NAME must be at most 100 characters"]
    return []


def _caller_id_errors(caller_id: str, *, required: bool) -> List[str]:
    if not caller_id:
        return ["EXOTEL_CALLER_ID is missing"] if required else []
    digits = caller_id.lstrip("+").replace(" ", "").replace("-", "")
    if not digits.isdigit() or not (8 <= len(digits) <= 15):
        return [
            "EXOTEL_CALLER_ID must be your ExoPhone as digits, for example 0404xxxxxxx "
            f"or +91404xxxxxxx; got {caller_id!r}"
        ]
    return []


def _twilio_caller_id_errors(caller_id: str, *, required: bool) -> List[str]:
    if not caller_id:
        return ["TWILIO_CALLER_ID is missing"] if required else []
    if not re.match(r"^\+[1-9]\d{7,14}$", caller_id):
        return [
            "TWILIO_CALLER_ID must be one of your Twilio numbers in international "
            "form, for example +15551234567 (Twilio has no Indian numbers, and calls "
            f"to India must come from a non-Indian number); got {caller_id!r}"
        ]
    return []


def _stream_url_errors(stream_url: str, stream_token: str, provider: str = "exotel") -> List[str]:
    """Every StreamUrl mistake we have already made (P29) is caught here."""
    if not stream_url:
        return []  # reported as missing by the secrets check
    errors: List[str] = []
    if stream_url.count("://") != 1 or "://https://" in stream_url or "://wss://" in stream_url:
        errors.append(
            f"EXOTEL_STREAM_URL has a doubled scheme; it must look like "
            f"wss://your-public-host/v1/exotel/stream (got {stream_url!r})"
        )
    parsed = urlparse(stream_url)
    if parsed.scheme not in {"ws", "wss"}:
        errors.append(f"EXOTEL_STREAM_URL must start with ws:// or wss:// (got {stream_url!r})")
    if not parsed.netloc or not parsed.path:
        errors.append(f"EXOTEL_STREAM_URL must include a public host and the path (got {stream_url!r})")
    elif provider == "twilio":
        # Twilio's <Stream url> does NOT accept a query string, so the secret
        # travels in the path instead of ?token= (verified in Twilio's docs).
        if parsed.path != "/v1/twilio/stream" and not parsed.path.startswith("/v1/twilio/stream/"):
            errors.append(
                "With CALL_PROVIDER=twilio the EXOTEL_STREAM_URL path must be "
                f"/v1/twilio/stream (got {parsed.path!r})"
            )
        if parsed.query:
            errors.append(
                "Twilio does not accept a query string in the Stream URL; put the secret in "
                "the path instead: wss://your-public-host/v1/twilio/stream/<the token>"
            )
        elif stream_token and not parsed.path.endswith("/" + stream_token):
            errors.append(
                "STREAM_TOKEN is set, so EXOTEL_STREAM_URL must end with "
                f"/<the same value> (got {parsed.path!r})"
            )
        elif not stream_token and parsed.path != "/v1/twilio/stream":
            errors.append(
                "EXOTEL_STREAM_URL must be exactly wss://your-public-host/v1/twilio/stream "
                f"when STREAM_TOKEN is empty (got {parsed.path!r})"
            )
    elif not any(parsed.path.endswith(p) for p in STREAM_PATHS):
        errors.append(
            "EXOTEL_STREAM_URL path must be /v1/exotel/stream "
            "(the server also answers /v1/exotel/stream/ws and /ws); "
            f"got {parsed.path!r}"
        )
    query = parse_qs(parsed.query)
    if provider == "exotel":
        sample_rates = query.get("sample-rate", []) + query.get("sample_rate", [])
        for rate in sample_rates:
            if str(rate).strip() != "8000":
                errors.append(
                    f"If the stream URL contains sample-rate it must be 8000, got {rate!r} "
                    "(use 16000 only after changing the pipeline sample rates too)"
                )
        if stream_token:
            tokens = query.get("token", [])
            if stream_token not in tokens:
                errors.append(
                    "STREAM_TOKEN is set, so EXOTEL_STREAM_URL must end with "
                    f"?token=<the same value> (token query value found: {tokens or 'none'})"
                )
    if len(stream_url) > 600:
        errors.append(f"EXOTEL_STREAM_URL must be shorter than 600 characters, got {len(stream_url)}")
    return errors


def load_settings(
    *,
    require_secrets: bool = True,
    require_school: bool = True,
    env_path: Optional[Path] = ENV_PATH,
) -> Settings:
    """Load ``.env`` (it wins over the shell) and validate everything."""
    if env_path is not None:
        load_dotenv(dotenv_path=env_path, override=True)
    env = {key: str(value) for key, value in os.environ.items()}
    return Settings.from_env(env, require_secrets=require_secrets, require_school=require_school)


def shell_environment(env_path: Optional[Path] = ENV_PATH) -> Dict[str, str]:
    """The real environment, filtered to Veda's own keys (used by check.py)."""
    if env_path is not None:
        load_dotenv(dotenv_path=env_path, override=True)
    return filter_environment({key: str(value) for key, value in os.environ.items()})


def configure_console() -> None:
    """Let Telugu text be printed on a Windows console instead of crashing (P31)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            continue


def ensure_runtime_dirs(settings: Settings) -> None:
    """Create the folders Veda writes into. Never raises for a live call."""
    for path in (settings.transcripts_dir, settings.pending_dir):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
