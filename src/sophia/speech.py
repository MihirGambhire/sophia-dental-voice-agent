"""
Sophia's voice as a single audio file, for typed conversations.

A tester in a public place types, and Sophia still answers out loud. The
voice call streams audio through Pipecat; a typed chat has no pipeline, so
each reply is synthesised whole, with the same voice and the same way of
speaking chosen for calls.

Only Sophia's own latest reply is ever spoken, chosen on the server. An
endpoint that spoke whatever text the page sent would let anyone with the
link spend the free voice credits on anything they liked.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from . import config

# The Cartesia API version Pipecat 1.10's Cartesia service uses, so typed
# replies sound exactly like calls.
CARTESIA_VERSION = "2026-03-01"
SAMPLE_RATE = 24000


class SpeechError(RuntimeError):
    pass


def _cartesia(text: str) -> bytes:
    speech = config.SPEECH
    body = {
        "model_id": speech.cartesia_model,
        "transcript": text,
        "voice": {"mode": "id", "id": speech.cartesia_voice_id},
        "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
        "language": "en",
    }
    style = {}
    if speech.cartesia_speed:
        try:
            style["speed"] = min(1.5, max(0.6, float(speech.cartesia_speed)))
        except ValueError:
            pass
    if speech.cartesia_emotion:
        style["emotion"] = speech.cartesia_emotion
    if style:
        body["generation_config"] = style
    request = urllib.request.Request(
        "https://api.cartesia.ai/tts/bytes",
        data=json.dumps(body).encode(),
        headers={
            "X-API-Key": speech.cartesia_api_key,
            "Cartesia-Version": CARTESIA_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _deepgram(text: str) -> bytes:
    speech = config.SPEECH
    query = urllib.parse.urlencode(
        {"model": speech.tts_voice, "encoding": "linear16", "sample_rate": SAMPLE_RATE, "container": "wav"}
    )
    request = urllib.request.Request(
        f"https://api.deepgram.com/v1/speak?{query}",
        data=json.dumps({"text": text}).encode(),
        headers={"Authorization": f"Token {speech.deepgram_api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def synthesise_wav(text: str) -> bytes:
    """Sophia saying this text, as a WAV file, in whichever voice calls use."""
    if not text.strip():
        raise SpeechError("Nothing to say.")
    try:
        if config.SPEECH.uses_cartesia:
            return _cartesia(text)
        if config.SPEECH.deepgram_api_key:
            return _deepgram(text)
    except OSError as failure:  # urllib raises URLError and HTTPError, both OSErrors
        raise SpeechError(f"The voice service did not answer: {type(failure).__name__}") from failure
    raise SpeechError("No voice is configured.")
