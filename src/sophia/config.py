"""
Central configuration for Sophia.

Everything that might change (keys, model names, file paths, business
constants) lives here so nothing else in the codebase has to guess.
Values come from environment variables, with sensible defaults, so the
data layer and the policy tests run without any API key at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load .env if it exists. It is never committed, see .gitignore.
load_dotenv()

# Repository root: this file is at <root>/src/sophia/config.py
ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
SEED_DIR = DATA_DIR / "seed"
PRACTICE_INFO_DIR = DATA_DIR / "practice_info"


def _env(name: str, default: str) -> str:
    """Read an environment variable, falling back to a default."""
    value = os.getenv(name)
    return value if value not in (None, "") else default


# ---------------------------------------------------------------------------
# Practice identity
# ---------------------------------------------------------------------------

PRACTICE_NAME = "Museum Street Dental Practice"
PRACTICE_PHONE = "01925 630221"
PRACTICE_ADDRESS = "11-13 Museum Street, Warrington, Cheshire, WA1 1JA"
AGENT_NAME = "Sophia"

DISCLAIMER = (
    "Unofficial concept demo, not affiliated with Museum Street Dental Practice."
)

# Escalation numbers, see CLAUDE.md section 6.4.
EMERGENCY_NUMBER = "999"
NHS_URGENT_NUMBER = "111"
OUT_OF_HOURS_DENTAL_NUMBER = "0161 476 9651"


# ---------------------------------------------------------------------------
# Time and opening hours
# ---------------------------------------------------------------------------

TIMEZONE_NAME = _env("TIMEZONE", "Europe/London")
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

# Monday to Friday only. 0 = Monday, 6 = Sunday.
OPEN_WEEKDAYS = (0, 1, 2, 3, 4)
OPENING_TIME = "08:00"
CLOSING_TIME = "18:00"
LUNCH_START = "13:00"
LUNCH_END = "14:00"

# Urgent same day appointments are released by phone from this time.
# The website is inconsistent (one line says 8am, another says 9am to 5pm).
# We use 8am and say so in the README.
URGENT_RELEASE_TIME = "08:00"
URGENT_SLOTS_PER_DAY = 6

# How far ahead find_available_slots will look by default.
DEFAULT_SEARCH_DAYS = 21
# How many days of urgent slots the seeder creates.
URGENT_SLOT_HORIZON_DAYS = 21


# ---------------------------------------------------------------------------
# Business rule constants (the reasoning lives in policies.py)
# ---------------------------------------------------------------------------

# A patient who has not attended for longer than this is no longer registered,
# and is treated as a new patient for fee purposes.
REGISTRATION_LAPSE_YEARS = 3

# Cancelling with less than this much notice is a Short Notice Cancellation.
SHORT_NOTICE_HOURS = 24

# Attendance thresholds are counted over a rolling window of this length.
ATTENDANCE_WINDOW_MONTHS = 24

# A patient is "new" for the attendance policy if they have been with the
# practice for less than this long. This is our documented assumption, the
# published policy does not define it. See README limitations.
NEW_PATIENT_MONTHS = 12

# Discharge thresholds, see CLAUDE.md section 6.7.
# One failure to attend is weighted as two short notice cancellations.
FTA_WEIGHT_IN_SNC = 2
EXISTING_PATIENT_FTA_LIMIT = 2
NEW_PATIENT_FTA_LIMIT = 1
EXISTING_PATIENT_SNC_LIMIT = 3
NEW_PATIENT_SNC_LIMIT = 2


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

DB_PATH = ROOT_DIR / _env("SOPHIA_DB_PATH", "data/sophia.db")
CHROMA_DIR = DATA_DIR / "chroma"
CHROMA_COLLECTION = "practice_info"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMSettings:
    """
    Which language model Sophia talks through.

    The key fields carry repr=False. A dataclass puts every field in its
    repr by default, so any traceback that touched this object printed
    both API keys in full. That happened once, into a test failure, which
    is exactly how a key ends up in a log file or a screenshot.
    """

    # Gemini by default. Groq answers a single request faster, but its
    # free tier allows 8000 tokens per minute and a tool calling turn
    # resends the prompt and every schema on each round trip, so a real
    # conversation stalls for the best part of a minute. Measured, not
    # assumed: see docs/ENGINEERING_LOG.md entry 16.
    provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "gemini"))
    groq_api_key: str = field(
        default_factory=lambda: _env("GROQ_API_KEY", ""), repr=False
    )
    # Checked against the live /models endpoint on 16 September 2026.
    # llama-3.3-70b-versatile was in Groq's docs but has been retired from
    # the API, so it is not usable. gpt-oss-120b follows instructions well
    # and calls tools reliably; gpt-oss-20b is roughly 250ms faster per
    # turn if latency ever matters more than accuracy.
    groq_model: str = field(
        default_factory=lambda: _env("GROQ_MODEL", "openai/gpt-oss-120b")
    )
    # Low keeps completion tokens down, which matters because the free
    # tier is capped on tokens per minute, not on requests.
    reasoning_effort: str = field(
        default_factory=lambda: _env("GROQ_REASONING_EFFORT", "low")
    )
    gemini_api_key: str = field(
        default_factory=lambda: _env("GEMINI_API_KEY", ""), repr=False
    )
    # Probed against the account on 16 September 2026. The larger flash
    # models were returning 503 "high demand" and gemini-3.6-flash took
    # 32 seconds for a single tool call. flash-lite answered in under a
    # second with tool calling intact, which is what a voice agent needs.
    gemini_model: str = field(
        default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.5-flash-lite")
    )

    @property
    def model(self) -> str:
        return self.gemini_model if self.provider == "gemini" else self.groq_model

    @property
    def api_key(self) -> str:
        return self.gemini_api_key if self.provider == "gemini" else self.groq_api_key


@dataclass(frozen=True)
class SpeechSettings:
    """Speech to text and text to speech. Only needed from Day 4."""

    deepgram_api_key: str = field(
        default_factory=lambda: _env("DEEPGRAM_API_KEY", ""), repr=False
    )
    stt_model: str = field(default_factory=lambda: _env("DEEPGRAM_STT_MODEL", "nova-3"))
    stt_language: str = field(
        default_factory=lambda: _env("DEEPGRAM_STT_LANGUAGE", "en-GB")
    )
    # Checked against the Deepgram models endpoint on 16 September 2026.
    # The Day 1 placeholder was aura-2-thalia-en, which is an AMERICAN
    # voice. An American receptionist answering a Warrington dental
    # practice is the kind of detail that undoes everything else.
    #
    # Deepgram lists four British voices. athena is the one chosen after
    # actually listening to them: Deepgram describes it as smooth, calm
    # and professional, which is what a receptionist sounds like. pandora
    # is the newer aura-2 generation but reads breathier, which is wrong
    # for someone taking a medical booking.
    tts_voice: str = field(
        default_factory=lambda: _env("DEEPGRAM_TTS_VOICE", "aura-athena-en")
    )


LLM = LLMSettings()
SPEECH = SpeechSettings()


# ---------------------------------------------------------------------------
# WebRTC connectivity
# ---------------------------------------------------------------------------
#
# A STUN server is enough when both ends can reach each other directly,
# which is true when the browser and the server are on the same machine.
# It is NOT enough over the internet.
#
# This was a real failure. Sharing a Cloudflare tunnel link worked
# perfectly on the machine running the server and failed for everyone
# else with "Call ended". The tunnel carries HTTP only. WebRTC audio is
# peer to peer UDP that never goes through it, so with the server behind
# one home router and the caller behind another, there is no route and
# ICE dies in the checking state.
#
# TURN fixes it by relaying the media through a public server. Both ends
# have to be told about it, so the browser fetches this same list from
# /api/ice rather than keeping its own copy that could drift.

# There is deliberately no default TURN server.
#
# The obvious candidate was Open Relay's old anonymous endpoint,
# openrelay.metered.ca with the credentials openrelayproject. It was tried
# and it is dead: the hostname no longer resolves, and the service now
# requires a free account. Shipping a default that silently fails is worse
# than shipping none, because the call still breaks and the configuration
# looks correct.
#
# So TURN is opt in. Without it, calls work between a browser and a server
# on the same machine and nowhere else. With it, they work across the
# internet. /api/health reports which of those you have.
TURN_URLS = tuple(
    url.strip() for url in _env("TURN_URLS", "").split(",") if url.strip()
)
TURN_USERNAME = _env("TURN_USERNAME", "")
TURN_CREDENTIAL = _env("TURN_CREDENTIAL", "")

STUN_URLS = tuple(
    url.strip()
    for url in _env(
        "STUN_URLS", "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302"
    ).split(",")
    if url.strip()
)


# Metered issue short lived TURN credentials from a REST endpoint rather
# than handing out fixed ones, so the app name and API key are what get
# configured, and the actual username and password are fetched per call.
METERED_APP_NAME = _env("METERED_APP_NAME", "")
METERED_API_KEY = _env("METERED_API_KEY", "")

# Credentials last hours, not seconds, so refetching on every single call
# would just be a slow way to get the same answer.
_METERED_CACHE: dict[str, object] = {"servers": None, "fetched_at": 0.0}
_METERED_CACHE_SECONDS = 1800


def _metered_ice_servers() -> list[dict]:
    """
    Fetch relay credentials from Metered, or return nothing if unavailable.

    Never raises. A failure here must degrade to a call that works locally,
    not to a server that will not start.
    """
    if not (METERED_APP_NAME and METERED_API_KEY):
        return []

    import json
    import time
    import urllib.request

    cached = _METERED_CACHE["servers"]
    if cached and (time.time() - float(_METERED_CACHE["fetched_at"])) < _METERED_CACHE_SECONDS:
        return list(cached)  # type: ignore[arg-type]

    url = (
        f"https://{METERED_APP_NAME}.metered.live/api/v1/turn/credentials"
        f"?apiKey={METERED_API_KEY}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            servers = json.load(response)
        if not isinstance(servers, list) or not servers:
            return []
        _METERED_CACHE["servers"] = servers
        _METERED_CACHE["fetched_at"] = time.time()
        return servers
    except Exception:  # noqa: BLE001
        return list(cached) if cached else []  # type: ignore[arg-type]


def ice_servers() -> list[dict]:
    """
    The ICE servers both ends should use, in the browser's own format.

    Served to the page from /api/ice so the two sides can never disagree
    about how to connect.

    Order of preference: explicit TURN_URLS if set, otherwise Metered if
    configured, otherwise STUN alone, which only works when the browser
    and the server are on the same machine.
    """
    servers: list[dict] = [{"urls": list(STUN_URLS)}]

    if TURN_URLS:
        servers.append(
            {
                "urls": list(TURN_URLS),
                "username": TURN_USERNAME,
                "credential": TURN_CREDENTIAL,
            }
        )
        return servers

    servers.extend(_metered_ice_servers())
    return servers


def turn_configured() -> bool:
    """True if any relay is available, which is what remote calls need."""
    if TURN_URLS:
        return True
    return bool(_metered_ice_servers())

LOG_LEVEL = _env("LOG_LEVEL", "INFO")
