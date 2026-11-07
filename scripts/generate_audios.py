#!/usr/bin/env python3
"""
generate_audios.py — Generate all survey audio files via MT+TTS API.

For each question/system prompt:
  1. POST English text to /translation_speech_synthesis  →  WAV audio
  2. POST English text to /translate                     →  Twi transcript
  3. Save WAV to audios/{name}.wav
  4. Save transcripts to audios/transcripts.json

Usage:
    python scripts/generate_audios.py [--voice female|male] [--only Q1 Q3A SYS_001]
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
AUDIOS_DIR  = PROJECT_DIR / "audios"
API_BASE    = os.getenv("ASR_MT_API_URL", "http://localhost:8080").rstrip("/docs").rstrip("/")

MTTTS_URL   = f"{API_BASE}/translation_speech_synthesis"
TRANSLATE_URL = f"{API_BASE}/translate"

# ── All 17 audio scripts ──────────────────────────────────────────────────────
SCRIPTS: dict[str, str] = {

    # ── System ────────────────────────────────────────────────────────────────
    "SYS_001": (
        "Hello, this is a call from AgriCo. "
        "You recently attended one of our field days — thank you so much for that. "
        "We have just a few quick questions for you, it will take about five minutes. "
        "Is now a good time? Press 1 for yes, or press 2 to be called back at a better time."
    ),
    "SYS_002": (
        "No problem at all. We will call you back at a better time. Thank you, and goodbye."
    ),
    "SYS_003": (
        "I am sorry, I did not quite catch that. Please try again."
    ),
    "SYS_004": (
        "Thank you."
    ),
    "SYS_005": (
        "Thank you so much. Your feedback helps us serve farmers better. "
        "We will be in touch about new products and field days in your area. Goodbye."
    ),

    # ── Section 1: SeedX Seed ─────────────────────────────────────────────────
    "Q1": (
        "Did you buy SeedX maize seed after the field day? "
        "Press 1 for yes, or press 2 for no."
    ),
    "Q2": (
        "Have you planted it yet? "
        "Press 1 if you have already planted. "
        "Press 2 if you are planning to plant this season. "
        "Press 3 if you are waiting for next season."
    ),
    "Q2A": (
        "How many bags or acres did you plant with SeedX? Please say the number."
    ),
    "Q3": (
        "What was the main reason you did not buy SeedX? "
        "Press 1 if it was too expensive. "
        "Press 2 if you could not find it near you. "
        "Press 3 if you were not ready to try a new seed. "
        "Press 4 if you bought a different seed instead. "
        "Press 5 for something else."
    ),
    "Q3A": (
        "Which seed did you buy? Please say the name of the seed."
    ),

    # ── Section 2: Fertilizer ─────────────────────────────────────────────────
    "Q4": (
        "Since the field day, have you bought fertilizer for this season? "
        "Press 1 for yes, or press 2 for no."
    ),
    "Q5": (
        "What fertilizer did you buy? Please say the name."
    ),
    "Q6": (
        "Where did you buy it from? Which agro shop or dealer? Please say the name."
    ),

    # ── Section 3: Field day impact ───────────────────────────────────────────
    "Q7": (
        "After attending the field day, did you tell any other farmers about AgriCo or SeedX? "
        "Press 1 for yes, or press 2 for no."
    ),
    "Q8": (
        "Is there anything from the field day that you remember most — "
        "something that really stuck with you? Please share what you remember."
    ),

    # ── Section 4: Forward intent ─────────────────────────────────────────────
    "Q9": (
        "Are you planning to use any AgriCo products next season? "
        "Press 1 for yes, definitely. "
        "Press 2 for probably yes. "
        "Press 3 if you are not sure. "
        "Press 4 for probably not. "
        "Press 5 for no."
    ),
    "Q10": (
        "What would help you most as a farmer right now? "
        "Press 1 for better access to inputs. "
        "Press 2 for better prices. "
        "Press 3 for more knowledge about how to farm better."
    ),
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def synthesize(client: httpx.Client, name: str, text: str, voice: str) -> bytes:
    resp = client.post(
        MTTTS_URL,
        json={"text": text, "voice": voice, "source_lang_name": "English",
              "maintainEnglishNumbers": True},
        timeout=60.0,
    )
    resp.raise_for_status()
    mt_s  = resp.headers.get("x-mt-time", "?")
    tts_s = resp.headers.get("x-tts-time", "?")
    print(f"    MT: {mt_s}s  TTS: {tts_s}s  size: {len(resp.content)//1024}KB")
    return resp.content


def translate(client: httpx.Client, text: str) -> str:
    resp = client.post(
        TRANSLATE_URL,
        json={"text": text, "source_lang_name": "English"},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("translated_text") or data.get("translation") or ""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate survey audio files")
    parser.add_argument("--voice", choices=["female", "male"], default="female")
    parser.add_argument("--only", nargs="+", metavar="NAME",
                        help="Generate only these files (e.g. --only Q1 SYS_001)")
    args = parser.parse_args()

    AUDIOS_DIR.mkdir(exist_ok=True)

    targets = {k: v for k, v in SCRIPTS.items()
               if not args.only or k in args.only}

    if not targets:
        print(f"No matching scripts for: {args.only}")
        sys.exit(1)

    print(f"Generating {len(targets)} audio file(s)  voice={args.voice}  api={API_BASE}")
    print()

    transcripts: dict = {}
    transcript_path = AUDIOS_DIR / "transcripts.json"
    if transcript_path.exists():
        transcripts = json.loads(transcript_path.read_text())

    failed = []

    with httpx.Client() as client:
        for name, english_text in targets.items():
            out_path = AUDIOS_DIR / f"{name}.wav"
            print(f"[{name}]  {english_text[:60]}{'…' if len(english_text)>60 else ''}")

            try:
                # 1. Synthesize
                audio = synthesize(client, name, english_text, args.voice)

                # 2. Translate for transcript
                twi_text = translate(client, english_text)

                # 3. Save WAV
                out_path.write_bytes(audio)
                print(f"    → {out_path.name}")

                # 4. Record transcript
                transcripts[name] = {
                    "english": english_text,
                    "twi":     twi_text,
                    "voice":   args.voice,
                    "file":    f"{name}.wav",
                }

            except Exception as e:
                print(f"    ERROR: {e}")
                failed.append(name)

            time.sleep(0.3)   # small gap between requests
            print()

    # Save transcripts
    transcript_path.write_text(json.dumps(transcripts, indent=2, ensure_ascii=False))
    print(f"Transcripts saved → {transcript_path}")

    if failed:
        print(f"\nFailed: {failed}")
        sys.exit(1)
    else:
        print(f"\nDone. {len(targets)} file(s) written to {AUDIOS_DIR}/")
        print("Run './scripts/server.sh restart' to reload.")


if __name__ == "__main__":
    main()
