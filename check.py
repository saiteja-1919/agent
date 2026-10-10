"""Pre-flight checks: run this before the first real call.

    python check.py

It checks the configuration, the Sarvam connections (speech recognition, voice,
transliteration), and whether the Cloudflare tunnel is reachable from outside.

It cannot prove that a phone call will work: only a real call can. Exotel's API
credentials, for example, are only proven by dialling. Anything that cannot be
checked here is printed as a warning, never silently as "fine".
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import List, Tuple

import httpx

from config import (
    ConfigError,
    Settings,
    configure_console,
    load_settings,
)
from name_speech import NameSpeaker, NameSpeechAuthError
from speech_text import has_telugu, latin_letters

RESULT_OK = "ok"
RESULT_WARN = "warn"
RESULT_FAIL = "fail"

PROBE_NAME = "Sai Teja"
PROBE_PHRASE = "హలో సర్, ఇది ఒక test message."
PROBE_TIMEOUT = 25.0


def line(state: str, message: str) -> None:
    print(f"[{state:^4}] {message}")


def check_configuration() -> Tuple[List[Tuple[str, str]], Settings]:
    results: List[Tuple[str, str]] = []
    try:
        settings = load_settings()
    except ConfigError as exc:
        results.append((RESULT_FAIL, str(exc).replace("\n", "\n       ")))
        return results, Settings()
    results.append((RESULT_OK, f"school name: {settings.school_name}"))
    if settings.call_provider == "twilio":
        results.append((RESULT_OK, f"call provider: twilio ({settings.twilio_api_base})"))
    else:
        results.append((RESULT_OK, f"call provider: exotel ({settings.exotel_api_base})"))
    results.append((RESULT_OK, f"stream path: {settings.stream_path}"))
    results.append((RESULT_OK, f"server listens on {settings.host}:{settings.port}"))
    results.append(
        (
            RESULT_OK,
            f"voice: {settings.sarvam_tts_voice}, pace {settings.sarvam_tts_pace}, "
            f"temperature {settings.sarvam_tts_temperature}",
        )
    )
    results.append(
        (
            RESULT_OK,
            f"turn taking: start {settings.vad_start_secs} s, stop {settings.vad_stop_secs} s, "
            f"volume {settings.vad_min_volume}, turn timeout {settings.user_turn_stop_timeout_secs} s",
        )
    )
    results.append(
        (
            RESULT_OK,
            f"limits: call {settings.call_timeout_secs} s, silence {settings.idle_timeout_secs} s",
        )
    )
    if settings.stream_token:
        results.append((RESULT_OK, "the stream token is set and matches the stream URL"))
    else:
        results.append(
            (
                RESULT_WARN,
                "STREAM_TOKEN is empty: anyone who learns your tunnel address can open a stream. "
                "Set it before the school uses this for real.",
            )
        )
    if settings.school_name == "":
        results.append((RESULT_FAIL, "SCHOOL_NAME is empty"))
    return results, settings


def check_transliteration(settings: Settings) -> List[Tuple[str, str]]:
    if not settings.sarvam_api_key:
        return [(RESULT_FAIL, "SARVAM_API_KEY is missing, cannot check names")]
    speaker = NameSpeaker(settings.sarvam_api_key, cache_path=settings.name_cache_path)
    try:
        spoken = speaker.speak(PROBE_NAME, label="check name")
    except NameSpeechAuthError as exc:
        return [(RESULT_FAIL, f"transliteration: {exc}")]
    if spoken == PROBE_NAME or not has_telugu(spoken) or latin_letters(spoken):
        return [
            (
                RESULT_FAIL,
                f"transliteration returned something unusable ({spoken!r}). "
                "Names will fall back to the typed spelling. Check that this key can use transliteration.",
            )
        ]
    results = [(RESULT_OK, f"transliteration: {PROBE_NAME} -> {spoken}")]
    if spoken != PROBE_NAME:
        results.append((RESULT_OK, "the answer is Telugu script and saved in name_pronunciations.json"))
    return results


async def check_tts(settings: Settings) -> List[Tuple[str, str]]:
    from pipeline import SAMPLE_RATE
    from sarvam_tts import SarvamTTS

    first_audio = {"at": 0.0}
    started = time.time()
    tts = SarvamTTS(
        api_key=settings.sarvam_api_key,
        model=settings.sarvam_tts_model,
        language_code=settings.sarvam_stt_language,
        speaker=settings.sarvam_tts_voice,
        sample_rate=SAMPLE_RATE,
        pace=settings.sarvam_tts_pace,
        temperature=settings.sarvam_tts_temperature,
        emit_frames=False,
        on_audio=lambda _chunk: first_audio.update(at=first_audio["at"] or time.time()),
    )
    try:
        audio = await tts.synthesize(PROBE_PHRASE, timeout=PROBE_TIMEOUT)
    except Exception as exc:
        await tts.stop()
        return [(RESULT_FAIL, f"voice: could not speak a test phrase ({exc})")]
    await tts.stop()
    if not audio:
        return [
            (
                RESULT_FAIL,
                "voice: Sarvam returned no audio. Check SARVAM_TTS_MODEL and SARVAM_TTS_VOICE "
                "(bulbul:v3 voices are lower case, for example shubh).",
            )
        ]
    seconds = len(audio) / (SAMPLE_RATE * 2)
    delay = (first_audio["at"] - started) if first_audio["at"] else 0.0
    results = [
        (RESULT_OK, f"voice: {seconds:.1f} s of audio for one short sentence"),
        (RESULT_OK, f"voice: first audio after {delay:.2f} s"),
    ]
    if tts.failures:
        results.append((RESULT_WARN, f"voice: {tts.failures} error(s) while speaking"))
    return results


async def check_stt(settings: Settings) -> List[Tuple[str, str]]:
    from pipeline import SAMPLE_RATE
    from sarvam_stt import SarvamRealtimeSTT

    stt = SarvamRealtimeSTT(
        api_key=settings.sarvam_api_key,
        model=settings.sarvam_stt_realtime_model,
        language_code=settings.sarvam_stt_language,
        sample_rate=SAMPLE_RATE,
    )
    stt.start()
    try:
        for _ in range(40):
            if stt.connected:
                break
            await asyncio.sleep(0.25)
        connected = stt.connected
    finally:
        await stt.stop()
    if not connected:
        return [
            (
                RESULT_FAIL,
                "speech recognition: could not open the Sarvam realtime socket. "
                "Check the API key and SARVAM_STT_MODEL (the realtime socket needs "
                "saaras:v3-realtime).",
            )
        ]
    return [(RESULT_OK, "speech recognition: the realtime socket opened and closed cleanly")]


def check_tunnel(settings: Settings) -> List[Tuple[str, str]]:
    url = settings.public_health_url
    if not url:
        return [(RESULT_FAIL, "EXOTEL_STREAM_URL has no public host, so nothing can reach this computer")]
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url)
        if response.status_code == 200 and response.json().get("ok"):
            return [(RESULT_OK, f"tunnel: {url} answers, so Exotel can reach this computer")]
        return [
            (
                RESULT_FAIL,
                f"tunnel: {url} answered HTTP {response.status_code}. If the tunnel was restarted, "
                "its address changed - update EXOTEL_STREAM_URL.",
            )
        ]
    except Exception as exc:
        return [
            (
                RESULT_FAIL,
                f"tunnel: {url} could not be reached ({exc}). Is cloudflared running, and does "
                "EXOTEL_STREAM_URL match the address it printed?",
            )
        ]


def check_exotel_reachable(settings: Settings) -> List[Tuple[str, str]]:
    results: List[Tuple[str, str]] = []
    label = "Twilio" if settings.call_provider == "twilio" else "Exotel"
    base = settings.twilio_api_base if settings.call_provider == "twilio" else settings.exotel_api_base
    try:
        with httpx.Client(timeout=15.0) as client:
            client.get(base)
        results.append((RESULT_OK, f"{label}: {base} is reachable"))
    except Exception as exc:
        results.append((RESULT_FAIL, f"{label}: {base} is not reachable ({exc})"))
    results.append(
        (
            RESULT_WARN,
            f"{label} credentials and the calling number can only be proven by placing a real call "
            "(and the number must be mapped to your account).",
        )
    )
    return results


async def run_checks(settings: Settings) -> List[Tuple[str, str]]:
    results: List[Tuple[str, str]] = []
    results += check_transliteration(settings) if settings.sarvam_api_key else []
    if settings.sarvam_api_key:
        results += await check_tts(settings)
        results += await check_stt(settings)
    results += check_tunnel(settings)
    results += check_exotel_reachable(settings)
    return results


def main(argv: List[str] | None = None) -> int:
    configure_console()
    print("Veda pre-flight check")
    print("")
    results, settings = check_configuration()
    if not any(state == RESULT_FAIL for state, _ in results):
        results += asyncio.run(run_checks(settings))
    print("Configuration")
    for state, message in results:
        line(state, message)
    failures = sum(1 for state, _ in results if state == RESULT_FAIL)
    warnings = sum(1 for state, _ in results if state == RESULT_WARN)
    print("")
    print(f"{failures} problem(s), {warnings} warning(s).")
    if failures:
        print("Fix the problems above before making a call.")
        return 1
    print("Everything that can be checked without a live call looks ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
