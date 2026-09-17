"""
Hear Cartesia's British voices say Sophia's own lines, to choose one by ear.

    python scripts/voice_samples.py
    python scripts/voice_samples.py --gender masculine --limit 4
    python scripts/voice_samples.py --voice-id <id> --styles

Needs CARTESIA_API_KEY in .env. Writes one WAV per voice to
data/voice_samples/ (not committed) and prints each voice's id, which goes
in .env as CARTESIA_VOICE_ID once one is chosen.

A voice is chosen by listening, not from a description. The Deepgram
placeholder on day one was described as friendly and turned out to be
American, which undid everything else about a Warrington practice.
"""

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sophia import config, prompts  # noqa: E402

API = "https://api.cartesia.ai"
# The version Pipecat 1.10's Cartesia service is written against, so the
# samples come from the same API the calls will use.
VERSION = "2026-03-01"

SAMPLE_TEXT = (
    prompts.GREETING
    + " Thank you, Margaret. I can see you on Tuesday the twenty ninth of September at"
    " twenty past nine with Dr Helen Whitfield. That's an NHS check up, and the charge"
    " is twenty seven pounds ninety. Shall I book that for you?"
)


def _request(method: str, path: str, body: dict | None = None) -> bytes:
    request = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "X-API-Key": config.SPEECH.cartesia_api_key,
            "Cartesia-Version": VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def british_voices(gender: str | None, limit: int) -> list[dict]:
    query = {"language": "en-GB", "limit": 100}
    if gender:
        query["gender"] = gender
    voices = json.loads(_request("GET", "/voices?" + urllib.parse.urlencode(query)))["data"]
    return voices[:limit]


# Ways of speaking to compare for one voice: (file name, speed, emotion).
# Speed runs 0.6 to 1.5 and emotion is one of Cartesia's named tones.
STYLES = [
    ("1-as-is", None, None),
    ("2-calm", None, "calm"),
    ("3-calm-slower", 0.9, "calm"),
    ("4-warm-content", None, "content"),
    ("5-sympathetic-slower", 0.9, "sympathetic"),
    ("6-confident-brisker", 1.1, "confident"),
]


def synthesise(voice_id: str, model: str, speed: float | None = None, emotion: str | None = None) -> bytes:
    body = {
        "model_id": model,
        "transcript": SAMPLE_TEXT,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 24000},
        "language": "en",
    }
    generation = {key: value for key, value in (("speed", speed), ("emotion", emotion)) if value is not None}
    if generation:
        body["generation_config"] = generation
    return _request("POST", "/tts/bytes", body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate samples of Cartesia's British voices.")
    parser.add_argument("--gender", choices=["feminine", "masculine", "gender_neutral"], default="feminine")
    parser.add_argument("--limit", type=int, default=6, help="how many voices to sample")
    parser.add_argument("--voice-id", help="sample one voice in several ways of speaking")
    parser.add_argument("--styles", action="store_true", help="with --voice-id, try each style")
    args = parser.parse_args()

    if not config.SPEECH.cartesia_api_key:
        print("No CARTESIA_API_KEY in .env. Sign up at https://play.cartesia.ai and add one.")
        sys.exit(1)

    out = ROOT / "data" / "voice_samples"
    out.mkdir(parents=True, exist_ok=True)

    if args.voice_id:
        styles = STYLES if args.styles else STYLES[:1]
        print(f"\nSampling voice {args.voice_id} in {len(styles)} styles\n")
        for name, speed, emotion in styles:
            path = out / f"style-{name}.wav"
            path.write_bytes(synthesise(args.voice_id, config.SPEECH.cartesia_model, speed, emotion))
            print(f"  {name:<24} speed={speed or 1.0}  emotion={emotion or 'none'}  {path.relative_to(ROOT)}")
        print("\nPut the chosen values in .env as CARTESIA_SPEED and CARTESIA_EMOTION.\n")
        return

    voices = british_voices(args.gender, args.limit)
    if not voices:
        print("No British voices returned for that filter.")
        sys.exit(1)

    print(f"\nSampling {len(voices)} British {args.gender} voices with {config.SPEECH.cartesia_model}\n")
    for voice in voices:
        slug = re.sub(r"[^a-z0-9]+", "-", voice["name"].lower()).strip("-")
        path = out / f"{slug}.wav"
        path.write_bytes(synthesise(voice["id"], config.SPEECH.cartesia_model))
        print(f"  {voice['name']}")
        print(f"    id:   {voice['id']}")
        print(f"    note: {voice.get('description', '').strip()[:110]}")
        print(f"    file: {path.relative_to(ROOT)}\n")
    print("Put the chosen id in .env as CARTESIA_VOICE_ID, and TTS_PROVIDER=cartesia.\n")


if __name__ == "__main__":
    main()
