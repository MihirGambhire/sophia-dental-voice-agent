"""
Tests for the WebSocket audio wire format.

The format is deliberately trivial, raw 16 kHz PCM both ways, so these are
short. The ones that matter are the edges: a malformed message must not
become noise, and an interruption must reach the browser as something that
stops playback.
"""

import asyncio
import json

from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TextFrame,
)

from sophia.web_audio import CHANNELS, SAMPLE_RATE, RawPCMSerializer


def run(coroutine):
    return asyncio.run(coroutine)


def test_microphone_bytes_become_an_input_audio_frame():
    frame = run(RawPCMSerializer().deserialize(bytes(640)))
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.sample_rate == SAMPLE_RATE == 16000
    assert frame.num_channels == CHANNELS == 1
    assert len(frame.audio) == 640


def test_an_odd_byte_count_is_trimmed_rather_than_shifting_every_sample():
    """
    One stray byte would misalign every following 16 bit sample, which
    sounds like loud static rather than a caller.
    """
    frame = run(RawPCMSerializer().deserialize(bytes(641)))
    assert len(frame.audio) == 640


def test_empty_and_single_byte_messages_are_ignored():
    serializer = RawPCMSerializer()
    assert run(serializer.deserialize(b"")) is None
    assert run(serializer.deserialize(b"\x00")) is None


def test_text_from_the_browser_is_not_mistaken_for_audio():
    assert run(RawPCMSerializer().deserialize("hello")) is None


def test_sophias_voice_is_sent_as_raw_bytes():
    audio = bytes(range(256)) * 4
    frame = OutputAudioRawFrame(audio=audio, sample_rate=16000, num_channels=1)
    assert run(RawPCMSerializer().serialize(frame)) == audio


def test_an_interruption_tells_the_browser_to_stop_playback():
    """Queued audio would otherwise keep playing after she has been cut off."""
    payload = run(RawPCMSerializer().serialize(InterruptionFrame()))
    assert json.loads(payload) == {"type": "clear"}


def test_other_frames_are_not_sent_to_the_browser():
    assert run(RawPCMSerializer().serialize(TextFrame(text="internal"))) is None


def test_the_page_uses_the_websocket_not_webrtc():
    """
    WebRTC connected only from the machine running the server. The page
    must use the WebSocket, which rides the same connection as the page.
    """
    from sophia import config

    page = (config.ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")
    assert "new WebSocket(" in page
    assert "RTCPeerConnection" not in page


def test_the_app_exposes_the_websocket_route():
    from sophia.voice_app import app

    assert "/ws" in [route.path for route in app.routes]


# ---------------------------------------------------------------------------
# Typing during a call
# ---------------------------------------------------------------------------


def test_a_typed_message_becomes_a_typed_text_frame():
    import asyncio
    import json

    from sophia.web_audio import RawPCMSerializer, TypedTextFrame

    frame = asyncio.run(RawPCMSerializer().deserialize(json.dumps({"type": "text", "text": "  I'd  like a check up "})))
    assert isinstance(frame, TypedTextFrame)
    assert frame.text == "I'd like a check up"


def test_empty_unknown_or_broken_text_messages_are_ignored():
    import asyncio
    import json

    from sophia.web_audio import RawPCMSerializer

    serializer = RawPCMSerializer()
    for message in (json.dumps({"type": "text", "text": "   "}), json.dumps({"type": "other"}), "not json", "[1, 2]"):
        assert asyncio.run(serializer.deserialize(message)) is None


def test_a_very_long_typed_message_is_cut_short():
    import asyncio
    import json

    from sophia.web_audio import MAX_TYPED_LENGTH, RawPCMSerializer

    frame = asyncio.run(RawPCMSerializer().deserialize(json.dumps({"type": "text", "text": "a" * 5000})))
    assert len(frame.text) == MAX_TYPED_LENGTH
