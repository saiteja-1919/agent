"""Names and the school name, spoken correctly.

A name once came out of the voice as "Sai Raj" instead of "Sai Teja" (P27), so
every name is converted from English letters to Telugu script before the phone is
dialled, and the conversion is saved in a hand-editable file:

    name_pronunciations.json     { "sai teja": "సాయి తేజ", ... }

If the file is missing, corrupt, or Sarvam cannot answer, Veda falls back to the
spelling the office typed - a call is never blocked by this feature.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from loguru import logger

from config import NAME_CACHE_PATH
from speech_text import has_telugu, latin_letters

TRANSLITERATE_URL = "https://api.sarvam.ai/transliterate"
TARGET_LANGUAGE = "te-IN"
SOURCE_LANGUAGES = ("en-IN", "auto")
REQUEST_TIMEOUT_SECS = 8.0

#: poster(url, headers, payload, timeout) -> (status_code, body)
Poster = Callable[[str, Dict[str, str], Dict[str, Any], float], Tuple[int, Any]]


class NameSpeechAuthError(Exception):
    """Sarvam rejected the API key. The dialer must stop immediately."""


class NameSpeaker:
    def __init__(
        self,
        api_key: str,
        *,
        cache_path: Optional[Path] = None,
        enabled: bool = True,
        timeout: float = REQUEST_TIMEOUT_SECS,
        poster: Optional[Poster] = None,
    ) -> None:
        self.api_key = api_key or ""
        self.cache_path = Path(cache_path) if cache_path else NAME_CACHE_PATH
        self.enabled = bool(enabled)
        self.timeout = timeout
        self._poster = poster
        self.cache: Dict[str, str] = self._load_cache()
        self.changed = False

    # ------------------------------------------------------------------ public
    def speak(self, text: str, *, label: str = "name") -> str:
        """Return the Telugu spelling for ``text``; never raises for a live call."""
        typed = (text or "").strip()
        if not typed:
            return ""
        if not self.enabled:
            return typed
        if has_telugu(typed):
            return typed
        cached = self.cache.get(typed.lower())
        if cached:
            return cached
        if not self.api_key:
            logger.warning("{}: no Sarvam key, keeping the typed spelling: {}", label, typed)
            return typed
        converted = self._ask_sarvam(typed)
        if not converted:
            logger.warning("{}: keeping the typed spelling for {}", label, typed)
            return typed
        self.cache[typed.lower()] = converted
        self.changed = True
        self.save_cache()
        return converted

    def speak_all(self, values: Dict[str, str]) -> Dict[str, str]:
        """Convert several values at once, keeping the keys unchanged."""
        return {key: self.speak(value, label=key) for key, value in values.items()}

    # ------------------------------------------------------------------- cache
    def save_cache(self) -> None:
        if not self.changed:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + f".tmp{os.getpid()}")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.cache, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.cache_path)
            self.changed = False
        except OSError as exc:
            logger.warning("could not save the name file {}: {}", self.cache_path, exc)

    def _load_cache(self) -> Dict[str, str]:
        path = self.cache_path
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning("name file {} is unreadable ({}); starting a new one", path, exc)
            return {}
        if not isinstance(data, dict):
            logger.warning("name file {} does not contain a dictionary; ignoring it", path)
            return {}
        return {
            str(key).strip().lower(): str(value).strip()
            for key, value in data.items()
            if str(key).strip() and str(value).strip()
        }

    # ------------------------------------------------------------------ sarvam
    def _ask_sarvam(self, text: str) -> str:
        for source in SOURCE_LANGUAGES:
            payload = {
                "input": text,
                "source_language_code": source,
                "target_language_code": TARGET_LANGUAGE,
                "spoken_form": False,
            }
            status, body = self._post(payload)
            if status in (401, 403):
                raise NameSpeechAuthError(
                    "Sarvam refused the API key while transliterating a name. "
                    "Check SARVAM_API_KEY in .env and whether this key can use transliteration."
                )
            if status == 429:
                logger.warning("Sarvam rate-limited the name conversion; using the typed spelling")
                return ""
            if status != 200:
                logger.warning("Sarvam transliteration returned HTTP {} for {!r}", status, text)
                continue
            candidate = ""
            if isinstance(body, dict):
                candidate = str(body.get("transliterated_text") or "").strip()
            elif isinstance(body, str):
                candidate = body.strip()
            if _is_usable(candidate):
                return candidate
            if candidate:
                logger.warning(
                    "transliteration for {!r} was not usable ({}); trying the next source language",
                    text,
                    candidate,
                )
        return ""

    def _post(self, payload: Dict[str, Any]) -> Tuple[int, Any]:
        headers = {"api-subscription-key": self.api_key, "Content-Type": "application/json"}
        poster = self._poster or _httpx_poster
        try:
            return poster(TRANSLITERATE_URL, headers, payload, self.timeout)
        except NameSpeechAuthError:
            raise
        except Exception as exc:  # network problems must never break a call
            logger.warning("transliteration request failed ({}); using the typed spelling", exc)
            return 0, {}


def _is_usable(candidate: str) -> bool:
    """The answer must be Telugu and must not leak English letters (P27)."""
    if not candidate:
        return False
    if not has_telugu(candidate):
        return False
    return not latin_letters(candidate)


def _httpx_poster(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float) -> Tuple[int, Any]:
    import httpx

    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=payload)
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    return response.status_code, body


def convert_details_for_speech(
    values: Dict[str, str],
    api_key: str,
    *,
    cache_path: Optional[Path] = None,
    enabled: bool = True,
    poster: Optional[Poster] = None,
) -> Dict[str, str]:
    """Convert every value that a voice will read, with the shared cache file."""
    speaker = NameSpeaker(api_key, cache_path=cache_path, enabled=enabled, poster=poster)
    result = speaker.speak_all(values)
    speaker.save_cache()
    return result
