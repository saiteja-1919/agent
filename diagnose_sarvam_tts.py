"""Listen to Veda's voice without making a phone call.

    python diagnose_sarvam_tts.py --text "హలో సర్, నేను Veda." --voice shubh

It asks Sarvam to speak the text exactly as the phone pipeline would (8 kHz,
linear16, 16-bit mono), writes a WAV file you can play, and prints how long the
audio took to start. Use it to compare voices, pace and temperature by ear.

This is a diagnostic, not part of a call. Nothing here changes how Veda talks.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import wave
from pathlib import Path
from typing import List, Optional

from config import ConfigError, Settings, configure_console, load_settings
from pipeline import SAMPLE_RATE
from sarvam_tts import SarvamTTS

DEFAULT_TEXT = "Hello sir, నేను Veda ని, మీ child school fees గురించి call చేస్తున్నాను."


def write_wav(path: Path, audio: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(audio)


async def synthesize(settings: Settings, text: str, voice: str, pace: float, temperature: float) -> bytes:
    first = {"at": 0.0}
    started = time.time()
    tts = SarvamTTS(
        api_key=settings.sarvam_api_key,
        model=settings.sarvam_tts_model,
        language_code=settings.sarvam_stt_language,
        speaker=voice,
        sample_rate=SAMPLE_RATE,
        pace=pace,
        temperature=temperature,
        emit_frames=False,
        on_audio=lambda _chunk: first.update(at=first["at"] or time.time()),
    )
    try:
        audio = await tts.synthesize(text, timeout=40.0)
    finally:
        await tts.stop()
    if first["at"]:
        print(f"first audio after {first['at'] - started:.2f} s")
    return audio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Listen to Veda's voice (no phone call).")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="the text to speak")
    parser.add_argument("--voice", default="", help="Sarvam voice; defaults to SARVAM_TTS_VOICE in .env")
    parser.add_argument("--pace", type=float, default=0.0, help="0.5 to 2.0; defaults to .env")
    parser.add_argument("--temperature", type=float, default=0.0, help="0.01 to 2.0; defaults to .env")
    parser.add_argument("--out", default="transcripts/voice_probe.wav", help="where to write the WAV file")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(require_secrets=False, require_school=False)
    except ConfigError as exc:
        print(str(exc))
        return 2
    if not settings.sarvam_api_key:
        print("SARVAM_API_KEY is missing in .env.")
        return 2
    voice = args.voice or settings.sarvam_tts_voice
    pace = args.pace or settings.sarvam_tts_pace
    temperature = args.temperature or settings.sarvam_tts_temperature
    print(f"Voice {voice}, pace {pace}, temperature {temperature}, {SAMPLE_RATE} Hz")
    audio = asyncio.run(synthesize(settings, args.text, voice, pace, temperature))
    if not audio:
        print("Sarvam returned no audio. Check the model and the voice name.")
        return 1
    out = Path(args.out)
    write_wav(out, audio, SAMPLE_RATE)
    print(f"{len(audio) / (SAMPLE_RATE * 2):.2f} s of audio written to {out}")
    print("Play it and listen. Change --voice, --pace or --temperature to compare.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
