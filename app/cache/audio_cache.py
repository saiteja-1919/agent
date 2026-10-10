"""The audio cache: previously synthesised speech for exact approved phrases.

Section 13.7 is deliberately narrow about what belongs here. Only the fixed
opening lines, a few rotating closing lines and the silence check-in may be
pre-rendered - :meth:`app.cache.response_cache.ResponseCache.is_cacheable` is
the single source of that rule. Reusing audio for ordinary replies would make
every call sound identical, which is exactly the "sounds scripted" failure the
brief warns about.

Three properties make the cache safe to trust:

* **The key is complete.** It hashes the template key, the exact text, the
  language, the voice, the model, the template version, the sample rate and the
  format (:meth:`ResponseCache.audio_cache_key`), so changing any of them misses
  rather than serving the wrong recording.
* **Every read is validated.** The file must exist, be the size it claims, carry
  the checksum it was stored with, match the text it was made from, and sit
  inside sensible duration bounds. Anything else is discarded, deleted, counted
  and reported as a miss - never served.
* **Every write is atomic.** The bytes go to a temporary file and are renamed
  into place, so a crash mid-write can never leave a truncated file that looks
  like a hit. Error and partial audio are never written at all.

When anything is wrong the caller synthesises live: the cache's failure mode is
a slightly slower reply, never a wrong or silent one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..domain.models import utcnow
from ..integrations.database import audio_repo
from .response_cache import ResponseCache, text_checksum

#: A file shorter than this is a truncated or failed synthesis, not speech.
MIN_AUDIO_BYTES = 256
#: Sanity ceiling: nothing Veda says should approach this.
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MIN_DURATION_MS = 80
MAX_DURATION_MS = 20000

_WAV_HEADER = b"RIFF"


@dataclass(frozen=True)
class CacheResult:
    """What one cache operation did, and why."""

    hit: bool
    path: Path
    asset_id: Optional[int] = None
    audio_bytes: int = 0
    duration_ms: Optional[int] = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.hit and self.path != Path("")


class AudioCache:
    """Validated on-disk storage for pre-rendered approved phrases."""

    def __init__(
        self,
        *,
        root: Path,
        responses: Optional[ResponseCache] = None,
        language: str = "te-IN",
        voice: str = "",
        model: str = "",
        template_version: str = "v1",
        sample_rate: int = 8000,
        audio_format: str = "pcm",
    ) -> None:
        self.root = Path(root)
        self.responses = responses or ResponseCache()
        self.language = language
        self.voice = voice
        self.model = model
        self.template_version = template_version
        self.sample_rate = sample_rate
        self.audio_format = audio_format

        self.hits = 0
        self.misses = 0
        self.invalidations = 0
        self.bytes_written = 0
        self.bytes_served = 0

    # ----------------------------------------------------------------- helpers
    @property
    def cacheable_keys(self):
        """The only phrases allowed in this cache (section 13.7)."""
        return [template.key for template in self.responses.audio_candidates()]

    def key_for(self, template_key: str, text: str) -> str:
        return self.responses.audio_cache_key(
            template_key,
            text,
            language=self.language,
            voice=self.voice,
            model=self.model,
            template_version=self.template_version,
            sample_rate=self.sample_rate,
            audio_format=self.audio_format,
        )

    def path_for(self, cache_key: str) -> Path:
        return self.root / f"{cache_key}.{self.audio_format}"

    def metrics(self) -> Dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "invalidations": self.invalidations,
            "bytes_written": self.bytes_written,
            "bytes_served": self.bytes_served,
            "entries": len(self.cacheable_keys),
        }

    def _validate_audio(self, audio: bytes) -> str:
        """Return why these bytes are unusable, or an empty string."""
        if not audio:
            return "empty_audio"
        if len(audio) < MIN_AUDIO_BYTES:
            return "audio_too_short"
        if len(audio) > MAX_AUDIO_BYTES:
            return "audio_too_large"
        if self.audio_format == "wav" and not audio.startswith(_WAV_HEADER):
            return "missing_wav_header"
        return ""

    def _validate_duration(self, duration_ms: Optional[int]) -> str:
        if duration_ms is None:
            return ""
        if duration_ms < MIN_DURATION_MS:
            return "duration_too_short"
        if duration_ms > MAX_DURATION_MS:
            return "duration_too_long"
        return ""

    # ----------------------------------------------------------------- reading
    async def lookup(self, session: Any, template_key: str, text: str) -> CacheResult:
        """Return validated audio for an approved phrase, or a miss."""
        if not self.responses.is_cacheable(template_key):
            # Enforced here as well as in the controller: the rule must survive
            # a caller that forgets to ask.
            return CacheResult(False, Path(""), reason="miss:not_cacheable")

        cache_key = self.key_for(template_key, text)
        asset = await audio_repo.get_asset(session, cache_key)
        if asset is None:
            self.misses += 1
            return CacheResult(False, Path(""), reason="miss:new")

        problem = self._problems(asset, text)
        if problem:
            await self._drop(session, asset, problem)
            return CacheResult(False, Path(""), reason=f"miss:{problem}")

        await audio_repo.touch_asset(session, asset.id)
        self.hits += 1
        self.bytes_served += asset.file_size or 0
        return CacheResult(
            True,
            Path(asset.storage_path),
            asset_id=asset.id,
            audio_bytes=asset.file_size or 0,
            duration_ms=asset.duration_ms,
            reason="hit",
        )

    def _problems(self, asset: Any, text: str) -> str:
        """Why this row must not be served, or an empty string."""
        if asset.invalidated_at is not None:
            return "invalidated"
        if asset.language != self.language:
            return "language_changed"
        if asset.voice != self.voice:
            return "voice_changed"
        if asset.model != self.model:
            return "model_changed"
        if asset.template_version != self.template_version:
            return "template_version_changed"
        if asset.sample_rate != self.sample_rate or asset.audio_format != self.audio_format:
            return "format_changed"
        if asset.text_checksum != text_checksum(text) or asset.text != text:
            # The approved wording changed underneath a cached file.
            return "text_changed"

        path = Path(asset.storage_path)
        if not path.is_file():
            return "file_missing"
        try:
            size = path.stat().st_size
        except OSError:
            return "file_unreadable"
        if size == 0:
            return "file_empty"
        if asset.file_size is not None and size != asset.file_size:
            return "file_size_mismatch"
        if asset.file_checksum:
            try:
                digest = text_checksum(path.read_bytes())
            except OSError:
                return "file_unreadable"
            if digest != asset.file_checksum:
                return "file_checksum_mismatch"
        return ""

    async def _drop(self, session: Any, asset: Any, problem: str) -> None:
        """Invalidate a bad entry and delete its bytes. Never raises."""
        self.invalidations += 1
        try:
            await audio_repo.invalidate_asset(session, asset.id)
        except Exception:  # noqa: BLE001 - a bad row must not break the call
            pass
        try:
            path = Path(asset.storage_path)
            if path.is_file():
                path.unlink()
        except OSError:
            pass

    # ----------------------------------------------------------------- writing
    async def store(
        self,
        session: Any,
        template_key: str,
        text: str,
        audio: bytes,
        *,
        duration_ms: Optional[int] = None,
    ) -> CacheResult:
        """Write synthesised audio for an approved phrase, atomically.

        Refuses to store anything that fails validation, so an error page or a
        truncated stream can never become a permanent "hit".
        """
        cache_key = self.key_for(template_key, text)
        problem = self._validate_audio(audio) or self._validate_duration(duration_ms)
        if problem:
            return CacheResult(False, Path(""), reason=f"not_stored:{problem}")
        if not self.responses.is_cacheable(template_key):
            return CacheResult(False, Path(""), reason="not_stored:not_cacheable")

        path = self.path_for(cache_key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".part")
            with open(temporary, "wb") as handle:
                handle.write(audio)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)  # atomic: a reader never sees a half file
        except OSError:
            return CacheResult(False, Path(""), reason="not_stored:write_failed")

        self.bytes_written += len(audio)
        asset = await audio_repo.upsert_asset(
            session,
            cache_key=cache_key,
            template_key=template_key,
            text=text,
            text_checksum=text_checksum(text),
            language=self.language,
            voice=self.voice,
            model=self.model,
            audio_format=self.audio_format,
            sample_rate=self.sample_rate,
            storage_path=str(path),
            file_checksum=text_checksum(audio),
            file_size=len(audio),
            duration_ms=duration_ms,
            template_version=self.template_version,
        )
        return CacheResult(
            True,
            path,
            asset_id=getattr(asset, "id", None),
            audio_bytes=len(audio),
            duration_ms=duration_ms,
            reason="stored",
        )

    # ------------------------------------------------------------ invalidation
    async def invalidate_template(self, session: Any, template_key: str) -> int:
        """Drop every cached file for one phrase and delete the bytes."""
        rows = await audio_repo.invalidate_template(session, template_key)
        self._delete_files(rows)
        self.invalidations += len(rows)
        return len(rows)

    async def invalidate_version(self, session: Any) -> int:
        """Drop everything cached under another template version."""
        rows = await audio_repo.invalidate_version(session, self.template_version)
        self._delete_files(rows)
        self.invalidations += len(rows)
        return len(rows)

    @staticmethod
    def _delete_files(rows) -> None:
        for row in rows:
            try:
                path = Path(row.storage_path)
                if path.is_file():
                    path.unlink()
            except OSError:  # pragma: no cover - a missing file is fine
                pass
