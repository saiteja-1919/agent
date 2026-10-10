"""Stage 1 configuration: validated, bounded, and never holding real secrets.

This does not replace ``config.py``. The existing module already validates the
provider and voice settings against a real ``.env`` and its tests depend on
that behaviour, so it is reused as-is through :attr:`Stage1Settings.base`.
What is added here is everything Stage 1 introduces: the database, the
dialing-safety switches, the timeouts, the cache/retention paths and the
transcript-refusal rule.

Two rules from the brief are enforced here rather than trusted to a caller:

* ``STORE_TRANSCRIPTS`` defaults to false and cannot be turned on by a single
  flag. Enabling it requires a second, explicit acknowledgement variable, so it
  can never be switched on by accident or by copying a line from a document.
* ``DATABASE_URL`` accepts only the async drivers that actually work on this
  platform. Appendix A pitfall 19 is a Windows-specific trap: psycopg 3's async
  mode does not run on the default Windows event loop, so the URL is rejected
  rather than allowed to fail later inside a live call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from config import APP_DIR, ENV_PATH, ConfigError, Settings
from dotenv import load_dotenv

#: Only these SQLAlchemy async drivers are allowed. ``postgresql+asyncpg`` is the
#: production driver; ``sqlite+aiosqlite`` exists so the offline test suite can
#: exercise the real repositories without a database server.
ALLOWED_DATABASE_SCHEMES = ("postgresql+asyncpg://", "sqlite+aiosqlite://")

DEFAULT_DATABASE_URL = "postgresql+asyncpg://veda:veda@127.0.0.1:5432/veda"

#: Every key this module reads. Kept separate from ``config.ENV_KEYS`` so the
#: existing module's behaviour and tests stay untouched.
STAGE1_ENV_KEYS = frozenset(
    {
        "DATABASE_URL", "VEDA_DATABASE_URL",
        "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_STATEMENT_TIMEOUT_MS",
        "MAX_ATTEMPTS_PER_TASK", "MAX_SIMULTANEOUS_CALLS",
        "RING_TIMEOUT_SECS", "PARENT_SILENCE_SECS", "SILENCE_PROMPT_WAIT_SECS",
        "STT_FINALIZE_TIMEOUT_SECS", "LLM_TIMEOUT_SECS", "TTS_TIMEOUT_SECS",
        "PROVIDER_TIMEOUT_SECS", "OPENER_PAUSE_SECS",
        "STORE_TRANSCRIPTS", "TRANSCRIPT_STORAGE_ACKNOWLEDGED", "TRANSCRIPT_RETENTION_DAYS",
        "AUDIO_CACHE_DIR", "IMPORT_DIR", "EXPORT_DIR", "DATA_DIR",
        "EXOTEL_CALLBACK_TOKEN", "VEDA_API_TOKEN", "ENABLE_OPERATIONAL_API",
        "MAX_STREAM_CHUNK_BYTES", "MAX_RESPONSE_CHARS", "MAX_IMPORT_ROWS",
        "MAX_IMPORT_FILE_BYTES",
        "OUTCOME_RETENTION_DAYS", "METRIC_RETENTION_DAYS", "LOG_RETENTION_DAYS",
        "LOG_LEVEL", "LOG_JSON", "DRY_RUN_DEFAULT",
        "APP_ORIGIN", "TIMEZONE",
        "SCHOOL_NAME_TELUGU", "SCHOOL_ID", "DEFAULT_CURRENCY",
        "LLM_MAX_CONTEXT_CHARS", "PROMPT_VERSION",
    }
)


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int(
    env: Mapping[str, str], key: str, default: int, low: int, high: int, errors: List[str]
) -> int:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        errors.append(f"{key} must be a whole number, got {raw!r}")
        return default
    if not (low <= value <= high):
        errors.append(f"{key} must be between {low} and {high}, got {value}")
        return default
    return value


def _float(
    env: Mapping[str, str], key: str, default: float, low: float, high: float, errors: List[str]
) -> float:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        errors.append(f"{key} must be a number, got {raw!r}")
        return default
    if not (low <= value <= high):
        errors.append(f"{key} must be between {low} and {high}, got {value}")
        return default
    return value


def _text(env: Mapping[str, str], key: str, default: str = "") -> str:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip()


def _path(env: Mapping[str, str], key: str, default: Path) -> Path:
    raw = _text(env, key)
    return Path(raw).expanduser() if raw else default


@dataclass(frozen=True)
class Stage1Settings:
    """Validated Stage 1 settings, plus the existing provider/voice settings."""

    base: Settings = field(default_factory=Settings)

    # Database
    database_url: str = DEFAULT_DATABASE_URL
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 15000

    # Dialing policy
    max_attempts_per_task: int = 1
    max_simultaneous_calls: int = 2
    dry_run_default: bool = True

    # Timeouts (seconds)
    ring_timeout_secs: int = 45
    parent_silence_secs: float = 8.0
    silence_prompt_wait_secs: float = 6.0
    stt_finalize_timeout_secs: float = 2.0
    llm_timeout_secs: float = 15.0
    tts_timeout_secs: float = 15.0
    provider_timeout_secs: float = 20.0
    opener_pause_secs: float = 0.7

    # Privacy
    store_transcripts: bool = False
    transcript_retention_days: int = 7

    # Paths
    data_dir: Path = APP_DIR / "data"
    audio_cache_dir: Path = APP_DIR / "data" / "audio_cache"
    import_dir: Path = APP_DIR / "data" / "imports"
    export_dir: Path = APP_DIR / "data" / "exports"

    # Interface protection
    exotel_callback_token: str = ""
    api_token: str = ""
    enable_operational_api: bool = False
    app_origin: str = ""

    # Limits
    max_stream_chunk_bytes: int = 3200
    max_response_chars: int = 400
    max_import_rows: int = 5000
    max_import_file_bytes: int = 5 * 1024 * 1024
    llm_max_context_chars: int = 4000

    # Retention
    outcome_retention_days: int = 730
    metric_retention_days: int = 730
    log_retention_days: int = 30

    # Operations
    log_level: str = "INFO"
    log_json: bool = False
    timezone: str = "Asia/Kolkata"
    school_id: int = 1
    school_name_telugu: str = ""
    default_currency: str = "INR"
    prompt_version: str = "v1"

    # ------------------------------------------------------------------ helpers
    @property
    def school_name(self) -> str:
        """The configured school name (from the existing settings, unchanged)."""
        return self.base.school_name

    @property
    def spoken_school_name(self) -> str:
        """Telugu spelling if provided, otherwise the typed name.

        The controller still runs the typed name through the name converter, so
        leaving SCHOOL_NAME_TELUGU empty is a valid configuration.
        """
        return self.school_name_telugu or self.base.school_name

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def database_driver(self) -> str:
        return self.database_url.split("://", 1)[0]

    @property
    def stream_token(self) -> str:
        return self.base.stream_token

    def redacted_database_url(self) -> str:
        """``postgresql+asyncpg://veda:***@127.0.0.1:5432/veda`` - log safe."""
        if "://" not in self.database_url:
            return "invalid"
        scheme, rest = self.database_url.split("://", 1)
        if "@" not in rest:
            return f"{scheme}://{rest}"
        credentials, host = rest.rsplit("@", 1)
        user = credentials.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"

    def safe_summary(self) -> Dict[str, Any]:
        """Log-safe settings view: no secrets, no tokens, no passwords."""
        summary = dict(self.base.safe_summary())
        summary.update(
            {
                "database": self.redacted_database_url(),
                "database_driver": self.database_driver,
                "max_attempts_per_task": self.max_attempts_per_task,
                "max_simultaneous_calls": self.max_simultaneous_calls,
                "dry_run_default": self.dry_run_default,
                "ring_timeout_secs": self.ring_timeout_secs,
                "parent_silence_secs": self.parent_silence_secs,
                "opener_pause_secs": self.opener_pause_secs,
                "store_transcripts": self.store_transcripts,
                "exotel_callback_token": "set" if self.exotel_callback_token else "missing",
                "api_token": "set" if self.api_token else "missing",
                "enable_operational_api": self.enable_operational_api,
                "audio_cache_dir": str(self.audio_cache_dir),
                "export_dir": str(self.export_dir),
                "log_level": self.log_level,
                "timezone": self.timezone,
                "prompt_version": self.prompt_version,
                "school_name": self.base.school_name or "missing",
                "school_name_telugu": "set" if self.school_name_telugu else "missing",
            }
        )
        return summary

    def ensure_directories(self) -> None:
        """Create the folders Veda writes into. Never raises for a live call."""
        for path in (
            self.data_dir,
            self.audio_cache_dir,
            self.import_dir,
            self.export_dir,
            self.base.transcripts_dir,
            self.base.pending_dir,
        ):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError):
                # ValueError covers an unrepresentable path (embedded NUL and
                # friends). A bad folder must never stop a call being handled.
                pass

    # ------------------------------------------------------------------ building
    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        base: Optional[Settings] = None,
        require_secrets: bool = False,
        require_school: bool = False,
    ) -> "Stage1Settings":
        """Validate a plain mapping. Raises :class:`ConfigError` listing every problem."""
        errors: List[str] = []
        base = base or Settings.from_env(
            env, require_secrets=require_secrets, require_school=require_school
        )

        database_url = _text(env, "VEDA_DATABASE_URL") or _text(env, "DATABASE_URL", DEFAULT_DATABASE_URL)
        if not database_url.startswith(ALLOWED_DATABASE_SCHEMES):
            errors.append(
                "DATABASE_URL must start with postgresql+asyncpg:// (or "
                "sqlite+aiosqlite:// for offline tests only). psycopg 3's async driver "
                "does not work on the default Windows event loop, and a synchronous "
                f"driver would block the audio pipeline. Got: {database_url.split('://')[0]}://..."
            )

        store_transcripts = _bool(env, "STORE_TRANSCRIPTS", False)
        if store_transcripts and not _bool(env, "TRANSCRIPT_STORAGE_ACKNOWLEDGED", False):
            errors.append(
                "STORE_TRANSCRIPTS=true is refused. Stage 1 stores outcomes and metrics "
                "only; keeping transcripts needs a separate, explicit decision with a "
                "stated purpose, retention period and access control. Set "
                "TRANSCRIPT_STORAGE_ACKNOWLEDGED=true as well only after that decision is "
                "recorded in writing."
            )

        settings = cls(
            base=base,
            database_url=database_url,
            db_pool_size=_int(env, "DB_POOL_SIZE", 5, 1, 50, errors),
            db_max_overflow=_int(env, "DB_MAX_OVERFLOW", 5, 0, 50, errors),
            db_statement_timeout_ms=_int(env, "DB_STATEMENT_TIMEOUT_MS", 15000, 1000, 120000, errors),
            max_attempts_per_task=_int(env, "MAX_ATTEMPTS_PER_TASK", 1, 1, 3, errors),
            max_simultaneous_calls=_int(env, "MAX_SIMULTANEOUS_CALLS", 2, 1, 20, errors),
            dry_run_default=_bool(env, "DRY_RUN_DEFAULT", True),
            ring_timeout_secs=_int(env, "RING_TIMEOUT_SECS", 45, 5, 180, errors),
            parent_silence_secs=_float(env, "PARENT_SILENCE_SECS", 8.0, 1.0, 60.0, errors),
            silence_prompt_wait_secs=_float(env, "SILENCE_PROMPT_WAIT_SECS", 6.0, 1.0, 60.0, errors),
            stt_finalize_timeout_secs=_float(env, "STT_FINALIZE_TIMEOUT_SECS", 2.0, 0.2, 20.0, errors),
            llm_timeout_secs=_float(env, "LLM_TIMEOUT_SECS", 15.0, 1.0, 120.0, errors),
            tts_timeout_secs=_float(env, "TTS_TIMEOUT_SECS", 15.0, 1.0, 120.0, errors),
            provider_timeout_secs=_float(env, "PROVIDER_TIMEOUT_SECS", 20.0, 1.0, 120.0, errors),
            opener_pause_secs=_float(env, "OPENER_PAUSE_SECS", 0.7, 0.0, 5.0, errors),
            store_transcripts=store_transcripts,
            transcript_retention_days=_int(env, "TRANSCRIPT_RETENTION_DAYS", 7, 1, 90, errors),
            data_dir=_path(env, "DATA_DIR", APP_DIR / "data"),
            audio_cache_dir=_path(env, "AUDIO_CACHE_DIR", APP_DIR / "data" / "audio_cache"),
            import_dir=_path(env, "IMPORT_DIR", APP_DIR / "data" / "imports"),
            export_dir=_path(env, "EXPORT_DIR", APP_DIR / "data" / "exports"),
            exotel_callback_token=_text(env, "EXOTEL_CALLBACK_TOKEN"),
            api_token=_text(env, "VEDA_API_TOKEN"),
            enable_operational_api=_bool(env, "ENABLE_OPERATIONAL_API", False),
            app_origin=_text(env, "APP_ORIGIN"),
            max_stream_chunk_bytes=_int(env, "MAX_STREAM_CHUNK_BYTES", 3200, 320, 64000, errors),
            max_response_chars=_int(env, "MAX_RESPONSE_CHARS", 400, 80, 4000, errors),
            max_import_rows=_int(env, "MAX_IMPORT_ROWS", 5000, 1, 100000, errors),
            max_import_file_bytes=_int(
                env, "MAX_IMPORT_FILE_BYTES", 5 * 1024 * 1024, 1024, 50 * 1024 * 1024, errors
            ),
            llm_max_context_chars=_int(env, "LLM_MAX_CONTEXT_CHARS", 4000, 500, 20000, errors),
            outcome_retention_days=_int(env, "OUTCOME_RETENTION_DAYS", 730, 30, 3650, errors),
            metric_retention_days=_int(env, "METRIC_RETENTION_DAYS", 730, 30, 3650, errors),
            log_retention_days=_int(env, "LOG_RETENTION_DAYS", 30, 1, 3650, errors),
            log_level=(_text(env, "LOG_LEVEL", "INFO").upper() or "INFO"),
            log_json=_bool(env, "LOG_JSON", False),
            timezone=_text(env, "TIMEZONE", "Asia/Kolkata"),
            school_id=_int(env, "SCHOOL_ID", 1, 1, 1000, errors),
            school_name_telugu=_text(env, "SCHOOL_NAME_TELUGU"),
            default_currency=_text(env, "DEFAULT_CURRENCY", "INR").upper(),
            prompt_version=_text(env, "PROMPT_VERSION", "v1"),
        )

        if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            errors.append(
                f"LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL, got {settings.log_level!r}"
            )
        if len(settings.default_currency) != 3:
            errors.append(
                f"DEFAULT_CURRENCY must be a three-letter code such as INR, got {settings.default_currency!r}"
            )
        if settings.max_attempts_per_task != 1:
            errors.append(
                f"MAX_ATTEMPTS_PER_TASK is {settings.max_attempts_per_task}; Stage 1 allows 1. "
                "Any retry policy needs an explicit owner decision, because a redial may "
                "interrupt a parent who was already called."
            )
        if settings.store_transcripts and settings.transcript_retention_days > 30:
            errors.append(
                "TRANSCRIPT_RETENTION_DAYS must be 30 or fewer when transcript storage is "
                f"enabled, got {settings.transcript_retention_days}"
            )
        if settings.is_sqlite and settings.store_transcripts:
            errors.append("Transcript storage is not supported with the SQLite test database.")

        if errors:
            raise ConfigError("Configuration problems:\n  - " + "\n  - ".join(errors))
        return settings

    def with_overrides(self, **kwargs: Any) -> "Stage1Settings":
        """A copy with specific fields replaced - used by the CLI and tests."""
        return replace(self, **kwargs)


def load_stage1_settings(
    env: Optional[Mapping[str, str]] = None,
    *,
    env_path: Optional[Path] = ENV_PATH,
    require_secrets: bool = False,
    require_school: bool = False,
) -> Stage1Settings:
    """The real entry point: read ``.env`` (it wins over the shell), then validate.

    ``.env`` is loaded with ``override=True`` for the same reason ``config.py``
    does it: a stale ``$env:`` variable left over in PowerShell must never beat
    the file.
    """
    if env is None:
        if env_path is not None:
            load_dotenv(dotenv_path=env_path, override=True)
        env = {key: str(value) for key, value in os.environ.items()}
    return Stage1Settings.from_env(env, require_secrets=require_secrets, require_school=require_school)
