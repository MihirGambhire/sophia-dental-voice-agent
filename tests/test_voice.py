"""
Tests for the voice layer.

There is deliberately very little to test here, and that is the point.
The voice layer only turns speech into text, hands it to the agent that
was already tested in silence, and turns the answer back into speech. All
the judgement lives below it.

What these check is the seam: that a transcript reaches the agent, that
the reply comes back as something the speech engine will say, that
unrelated frames are not swallowed, and that a failure mid call ends in
Sophia saying something sensible rather than silence.

No microphone, no network, no Deepgram key needed.
"""

import asyncio
from datetime import date, time
from types import SimpleNamespace

import pytest

from pipecat.frames.frames import Frame, TextFrame, TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection

from sophia import clock
from sophia.voice_app import SophiaVoiceProcessor


class FakeAgent:
    """Stands in for SophiaAgent, recording what it was asked."""

    def __init__(self, reply="Of course.", tools=None, safety="routine", fails=False):
        self.heard = []
        self._reply = reply
        self._tools = tools or []
        self._safety = safety
        self._fails = fails

    def say(self, text):
        self.heard.append(text)
        if self._fails:
            raise RuntimeError("the model fell over")
        return SimpleNamespace(
            reply=self._reply,
            tools_used=self._tools,
            safety_level=self._safety,
            latency_seconds=0.4,
        )


def make_processor(agent):
    """
    A processor wired to collect what it pushes downstream.

    Pipecat processors normally push into a linked pipeline. For a unit
    test the push is replaced with a recorder.
    """
    events = []
    processor = SophiaVoiceProcessor(agent, on_event=events.append)

    pushed = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    processor.push_frame = capture
    return processor, pushed, events


async def feed(processor, frame, direction=FrameDirection.DOWNSTREAM):
    """
    Send one frame in, bypassing the base class bookkeeping.

    FrameProcessor.process_frame expects to belong to a started pipeline,
    which is far more machinery than this needs. The parent method is
    stubbed for the duration of the call.

    Note the signature: patching the class replaces an unbound function,
    so the stub has to accept self. Getting that wrong is what the first
    version of this helper did.
    """
    parent = SophiaVoiceProcessor.__mro__[1]

    async def noop(self, frame, direction):
        return None

    original = parent.process_frame
    parent.process_frame = noop
    try:
        await processor.process_frame(frame, direction)
    finally:
        parent.process_frame = original


def transcription(text):
    return TranscriptionFrame(text=text, user_id="caller", timestamp="now")


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def test_what_the_caller_says_reaches_the_agent():
    agent = FakeAgent(reply="Certainly, what is your name?")
    processor, pushed, _ = make_processor(agent)

    asyncio.run(feed(processor, transcription("I'd like to book a check up")))

    assert agent.heard == ["I'd like to book a check up"]


def test_the_reply_is_pushed_as_something_to_speak():
    agent = FakeAgent(reply="Certainly, what is your name?")
    processor, pushed, _ = make_processor(agent)

    asyncio.run(feed(processor, transcription("hello")))

    spoken = [f for f, _ in pushed if isinstance(f, TTSSpeakFrame)]
    assert len(spoken) == 1
    assert spoken[0].text == "Certainly, what is your name?"


def test_surrounding_whitespace_is_trimmed_before_the_agent_sees_it():
    agent = FakeAgent()
    processor, _, _ = make_processor(agent)

    asyncio.run(feed(processor, transcription("   hello there  ")))

    assert agent.heard == ["hello there"]


# ---------------------------------------------------------------------------
# Frames that are not speech must not be swallowed
# ---------------------------------------------------------------------------


def test_an_unrelated_frame_is_passed_through_untouched():
    """
    Swallowing frames breaks the pipeline in ways that are very hard to
    diagnose from an audio problem.
    """
    agent = FakeAgent()
    processor, pushed, _ = make_processor(agent)

    frame = TextFrame(text="not a transcript")
    asyncio.run(feed(processor, frame))

    assert agent.heard == []
    assert pushed[0][0] is frame


def test_an_empty_transcript_does_not_wake_the_agent():
    """
    Deepgram emits empty finals. Sending those to the model would waste a
    request and produce a confused answer to nothing.
    """
    agent = FakeAgent()
    processor, pushed, _ = make_processor(agent)

    asyncio.run(feed(processor, transcription("   ")))

    assert agent.heard == []
    assert not any(isinstance(f, TTSSpeakFrame) for f, _ in pushed)


def test_direction_is_preserved_for_passed_through_frames():
    agent = FakeAgent()
    processor, pushed, _ = make_processor(agent)

    frame = TextFrame(text="upstream thing")
    asyncio.run(feed(processor, frame, FrameDirection.UPSTREAM))

    assert pushed[0][1] is FrameDirection.UPSTREAM


# ---------------------------------------------------------------------------
# Failure has to be audible, not silent
# ---------------------------------------------------------------------------


def test_a_failure_mid_call_still_says_something_useful():
    """
    A caller cannot see a stack trace. Silence on a phone call is the
    worst possible outcome, so a failure ends with a spoken apology and
    the practice's real number.
    """
    agent = FakeAgent(fails=True)
    processor, pushed, events = make_processor(agent)

    asyncio.run(feed(processor, transcription("hello")))

    spoken = [f for f, _ in pushed if isinstance(f, TTSSpeakFrame)]
    assert len(spoken) == 1
    assert "01925 630221" in spoken[0].text
    assert "sorry" in spoken[0].text.lower()


def test_a_failure_is_recorded_for_the_transcript_panel():
    agent = FakeAgent(fails=True)
    processor, _, events = make_processor(agent)

    asyncio.run(feed(processor, transcription("hello")))

    assert any(event["type"] == "error" for event in events)


# ---------------------------------------------------------------------------
# What the live transcript panel is told
# ---------------------------------------------------------------------------


def test_both_sides_of_the_conversation_are_reported():
    agent = FakeAgent(reply="Certainly.")
    processor, _, events = make_processor(agent)

    asyncio.run(feed(processor, transcription("hello")))

    kinds = [event["type"] for event in events]
    assert kinds == ["caller", "sophia"]
    assert events[0]["text"] == "hello"
    assert events[1]["text"] == "Certainly."


def test_the_tools_used_are_reported_so_a_demo_can_show_them():
    agent = FakeAgent(tools=["verify_patient", "find_available_slots"])
    processor, _, events = make_processor(agent)

    asyncio.run(feed(processor, transcription("Margaret Hollis")))

    assert events[1]["tools"] == ["verify_patient", "find_available_slots"]


def test_an_emergency_is_flagged_to_the_panel():
    """The browser colours these red, so the escalation is visible in a demo."""
    agent = FakeAgent(reply="Please ring 999.", safety="emergency_999")
    processor, _, events = make_processor(agent)

    asyncio.run(feed(processor, transcription("I can't breathe")))

    assert events[1]["safety"] == "emergency_999"


def test_latency_is_reported_for_each_turn():
    agent = FakeAgent()
    processor, _, events = make_processor(agent)

    asyncio.run(feed(processor, transcription("hello")))

    assert events[1]["seconds"] == 0.4


# ---------------------------------------------------------------------------
# Configuration that would be embarrassing to get wrong
# ---------------------------------------------------------------------------


def test_the_voice_is_a_british_one():
    """
    An American receptionist answering a Warrington dental practice undoes
    every other detail in the project. The Day 1 placeholder was
    aura-2-thalia-en, which is American.
    """
    from sophia import config

    british = {
        "aura-2-pandora-en",
        "aura-athena-en",
        "aura-2-draco-en",
        "aura-helios-en",
    }
    assert config.SPEECH.tts_voice in british, (
        f"{config.SPEECH.tts_voice} is not one of Deepgram's British voices"
    )


def test_speech_to_text_is_configured_for_uk_english():
    """Postcodes and surnames are transcribed badly by a US English model."""
    from sophia import config

    assert config.SPEECH.stt_language == "en-GB"


def test_the_web_page_exists_and_carries_the_disclaimer():
    from sophia import config

    page = (config.ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")
    assert "Unofficial concept demo" in page
    assert "999" in page
    assert "AI assistant" in page
