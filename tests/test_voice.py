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
from pathlib import Path
from datetime import date, time
from types import SimpleNamespace

import pytest

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
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

    def note_interrupted(self, reply):
        self.interrupted_replies = getattr(self, "interrupted_replies", []) + [reply]

    def say(self, text):
        self.heard.append(text)
        if self._fails:
            raise RuntimeError("the model fell over")
        return SimpleNamespace(
            reply=self._reply,
            tools_used=self._tools,
            safety_level=self._safety,
            latency_seconds=0.4,
            ends_call=getattr(self, "ends_call", False),
        )


def make_processor(agent):
    """
    A processor wired to collect what it pushes downstream.

    Pipecat processors normally push into a linked pipeline. For a unit
    test the push is replaced with a recorder.
    """
    events = []
    # A near zero silence window so tests do not wait 1.2 seconds per turn.
    processor = SophiaVoiceProcessor(agent, on_event=events.append, turn_end_silence=0.01)

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
        # Turns are answered after a short silence, in a background task.
        await processor.wait_idle()
    finally:
        parent.process_frame = original


async def feed_many(processor, frames):
    """Several frames in one event loop, as a real caller produces them."""
    parent = SophiaVoiceProcessor.__mro__[1]

    async def noop(self, frame, direction):
        return None

    original = parent.process_frame
    parent.process_frame = noop
    try:
        for frame in frames:
            await processor.process_frame(frame, FrameDirection.DOWNSTREAM)
        await processor.wait_idle()
    finally:
        parent.process_frame = original


def transcription(text):
    return TranscriptionFrame(text=text, user_id="caller", timestamp="now")


# ---------------------------------------------------------------------------
# Regressions from the first real phone tests
# ---------------------------------------------------------------------------


def test_a_sentence_split_by_a_pause_becomes_one_turn():
    """
    A real caller said "thirteenth April two thousand and", paused, then
    the rest. Answering each fragment on arrival made Sophia accept half a
    date of birth and move on. Fragments inside one turn are now joined.
    """
    agent = FakeAgent()
    processor, _, _ = make_processor(agent)

    asyncio.run(feed_many(processor, [
        VADUserStartedSpeakingFrame(),
        transcription("thirteenth April two thousand and"),
        transcription("four"),
        VADUserStoppedSpeakingFrame(),
    ]))

    assert agent.heard == ["thirteenth April two thousand and four"]


def test_speaking_again_before_the_silence_ends_keeps_it_one_turn():
    agent = FakeAgent()
    processor, _, _ = make_processor(agent)

    asyncio.run(feed_many(processor, [
        transcription("Can you tell me when the"),
        VADUserStartedSpeakingFrame(),
        transcription("dental office is open"),
        VADUserStoppedSpeakingFrame(),
    ]))

    assert agent.heard == ["Can you tell me when the dental office is open"]


def test_interim_words_arriving_hold_the_turn_open():
    agent = FakeAgent()
    processor, _, _ = make_processor(agent)

    asyncio.run(feed_many(processor, [
        transcription("my postcode is"),
        InterimTranscriptionFrame(text="W A", user_id="caller", timestamp="now"),
        transcription("W A 1 2 N F"),
    ]))

    assert agent.heard == ["my postcode is W A 1 2 N F"]


def test_turns_never_run_at_the_same_time():
    """
    The agent's database connection is shared across worker threads, which
    is only safe one turn at a time. Two turns overlapping would reintroduce
    the thread errors the first testers hit.
    """
    import threading
    import time as _time

    active = {"now": 0, "max": 0}
    guard = threading.Lock()

    class SlowAgent(FakeAgent):
        def say(self, text):
            with guard:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            _time.sleep(0.05)
            with guard:
                active["now"] -= 1
            return super().say(text)

    agent = SlowAgent()
    processor, _, _ = make_processor(agent)

    async def two_turns():
        parent = SophiaVoiceProcessor.__mro__[1]

        async def noop(self, frame, direction):
            return None

        original = parent.process_frame
        parent.process_frame = noop
        try:
            await processor.process_frame(transcription("first"), FrameDirection.DOWNSTREAM)
            await asyncio.sleep(0.03)
            await processor.process_frame(transcription("second"), FrameDirection.DOWNSTREAM)
            await asyncio.sleep(0.03)
            await processor.wait_idle()
        finally:
            parent.process_frame = original

    asyncio.run(two_turns())

    assert agent.heard == ["first", "second"]
    assert active["max"] == 1


def test_a_failure_never_shows_the_caller_a_raw_exception():
    """The first testers saw a SQLite error message on screen."""
    agent = FakeAgent(fails=True)
    processor, _, events = make_processor(agent)

    asyncio.run(feed(processor, transcription("hello")))

    error = next(event for event in events if event["type"] == "error")
    assert "fell over" not in error["text"]
    assert "Error" not in error["text"]


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


# ---------------------------------------------------------------------------
# Interruptions, and telling them apart from Sophia's own echo
# ---------------------------------------------------------------------------

SAYING = "We are open Monday to Friday from eight until six, closed for lunch between one and two."


def speaking_processor(agent=None, clock=None):
    """A processor that believes Sophia is mid sentence, with interruption recorded."""
    agent = agent or FakeAgent()
    processor, pushed, events = make_processor(agent)
    interruptions = []

    async def record_interruption():
        interruptions.append(True)

    processor.broadcast_interruption = record_interruption
    if clock is not None:
        processor._now = lambda: clock["t"]
    processor.set_spoken_text(SAYING)
    return agent, processor, pushed, events, interruptions


def interim(text):
    return InterimTranscriptionFrame(text=text, user_id="caller", timestamp="now")


def test_the_caller_talking_over_sophia_stops_her():
    agent, processor, _, events, interruptions = speaking_processor()

    asyncio.run(feed_many(processor, [
        BotStartedSpeakingFrame(),
        interim("hang on can I"),
        transcription("hang on can I ask something else"),
        VADUserStoppedSpeakingFrame(),
    ]))

    assert interruptions == [True]
    assert any(event["type"] == "interrupted" for event in events)
    assert agent.heard == ["hang on can I ask something else"]


def test_her_own_echo_does_not_interrupt_her():
    """
    On a phone speaker her voice comes back through the microphone. If that
    counted as an interruption she would cut herself off mid sentence.
    """
    agent, processor, _, events, interruptions = speaking_processor()

    asyncio.run(feed_many(processor, [
        BotStartedSpeakingFrame(),
        interim("we are open Monday to Friday"),
        transcription("We are open Monday to Friday from eight until six"),
    ]))

    assert interruptions == []
    assert not any(event["type"] == "interrupted" for event in events)


def test_her_own_echo_never_becomes_a_caller_turn():
    """Otherwise she would answer her own sentence."""
    agent, processor, _, _, _ = speaking_processor()

    asyncio.run(feed_many(processor, [
        BotStartedSpeakingFrame(),
        transcription("closed for lunch between one and two"),
        BotStoppedSpeakingFrame(),
    ]))

    assert agent.heard == []


def test_echo_arriving_just_after_she_stops_is_still_ignored():
    clock = {"t": 100.0}
    agent, processor, _, _, _ = speaking_processor(clock=clock)

    async def run_it():
        parent = SophiaVoiceProcessor.__mro__[1]

        async def noop(self, frame, direction):
            return None

        original = parent.process_frame
        parent.process_frame = noop
        try:
            await processor.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
            await processor.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            clock["t"] += 1.5
            await processor.process_frame(transcription("eight until six"), FrameDirection.DOWNSTREAM)
            await processor.wait_idle()
        finally:
            parent.process_frame = original

    asyncio.run(run_it())
    assert agent.heard == []


def test_the_same_words_later_are_heard_as_the_caller():
    """Outside the echo window, repeating her phrase is a genuine question."""
    clock = {"t": 100.0}
    agent, processor, _, _, _ = speaking_processor(clock=clock)

    async def run_it():
        parent = SophiaVoiceProcessor.__mro__[1]

        async def noop(self, frame, direction):
            return None

        original = parent.process_frame
        parent.process_frame = noop
        try:
            await processor.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
            await processor.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            clock["t"] += 10
            await processor.process_frame(transcription("closed for lunch between one and two"), FrameDirection.DOWNSTREAM)
            await processor.wait_idle()
        finally:
            parent.process_frame = original

    asyncio.run(run_it())
    assert agent.heard == ["closed for lunch between one and two"]


def test_a_single_noise_word_does_not_cut_her_off():
    agent, processor, _, _, interruptions = speaking_processor()

    asyncio.run(feed_many(processor, [BotStartedSpeakingFrame(), interim("mm")]))

    assert interruptions == []


@pytest.mark.parametrize("word", ["stop", "wait", "sorry", "actually"])
def test_a_single_cut_in_word_does_stop_her(word):
    agent, processor, _, _, interruptions = speaking_processor()

    asyncio.run(feed_many(processor, [BotStartedSpeakingFrame(), interim(word)]))

    assert interruptions == [True]


def test_speech_while_she_is_silent_is_not_an_interruption():
    agent, processor, _, _, interruptions = speaking_processor()

    asyncio.run(feed_many(processor, [
        BotStoppedSpeakingFrame(),
        transcription("I would like a completely different appointment"),
    ]))

    assert interruptions == []


def test_the_agent_is_told_which_reply_was_cut_off():
    agent, processor, _, _, _ = speaking_processor()

    asyncio.run(feed_many(processor, [
        BotStartedSpeakingFrame(),
        transcription("sorry which dentist was that"),
    ]))

    assert agent.interrupted_replies == [SAYING]


def test_the_greeting_is_registered_so_its_echo_is_ignored():
    """
    The greeting is spoken from outside the processor. If it were not
    registered, its echo would be the first thing the caller "said".
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "sophia" / "voice_app.py").read_text(encoding="utf-8")
    assert "sophia.set_spoken_text(agent.greeting)" in source


def test_the_page_no_longer_mutes_the_microphone_while_she_speaks():
    from sophia import config

    page = (config.ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")
    assert "ctx.currentTime < playHead" not in page


# ---------------------------------------------------------------------------
# Choosing the voice
# ---------------------------------------------------------------------------


def _speech(**overrides):
    from sophia.config import SpeechSettings

    return SpeechSettings(**{"deepgram_api_key": "dg", "tts_provider": "deepgram", "cartesia_api_key": "",
                             "extra_cartesia_keys": (), "cartesia_voice_id": "", **overrides})


def test_cartesia_speaks_when_it_is_chosen_and_configured(monkeypatch):
    from pipecat.services.cartesia.tts import CartesiaTTSService
    from sophia import voice_app

    monkeypatch.setattr(voice_app.config, "SPEECH",
                        _speech(tts_provider="cartesia", cartesia_api_key="ck", cartesia_voice_id="voice-1"))
    assert isinstance(voice_app.build_tts(), CartesiaTTSService)


def test_a_half_configured_cartesia_falls_back_to_deepgram_rather_than_silence(monkeypatch):
    from pipecat.services.deepgram.tts import DeepgramTTSService
    from sophia import voice_app

    monkeypatch.setattr(voice_app.config, "SPEECH", _speech(tts_provider="cartesia", cartesia_api_key="ck"))
    assert isinstance(voice_app.build_tts(), DeepgramTTSService)


def test_the_cartesia_key_never_appears_in_a_repr():
    assert "secret-cartesia" not in repr(_speech(cartesia_api_key="secret-cartesia"))


def test_the_chosen_way_of_speaking_reaches_cartesia(monkeypatch):
    from sophia import voice_app

    monkeypatch.setattr(voice_app.config, "SPEECH", _speech(
        tts_provider="cartesia", cartesia_api_key="ck", cartesia_voice_id="voice-1",
        cartesia_speed="0.9", cartesia_emotion="calm"))
    style = voice_app.build_tts()._settings.generation_config
    assert style.speed == 0.9 and style.emotion == "calm"


def test_a_speed_outside_cartesias_range_is_clamped(monkeypatch):
    from sophia import voice_app

    monkeypatch.setattr(voice_app.config, "SPEECH", _speech(
        tts_provider="cartesia", cartesia_api_key="ck", cartesia_voice_id="voice-1", cartesia_speed="3"))
    assert voice_app.build_tts()._settings.generation_config.speed == 1.5


# ---------------------------------------------------------------------------
# Typing during a call
# ---------------------------------------------------------------------------


def test_a_typed_message_is_answered_out_loud_straight_away():
    """A tester in public types; Sophia still speaks the reply on the call."""
    from sophia.web_audio import TypedTextFrame

    agent = FakeAgent(reply="Have you been a patient here before?")
    processor, pushed, events = make_processor(agent)
    processor.turn_end_silence = 60  # a typed message must not wait for silence

    asyncio.run(feed(processor, TypedTextFrame(text="I'd like a check up")))

    assert agent.heard == ["I'd like a check up"]
    spoken = [frame.text for frame, _ in pushed if isinstance(frame, TTSSpeakFrame)]
    assert spoken == ["Have you been a patient here before?"]
    assert events[0] == {"type": "caller", "text": "I'd like a check up"}


def test_typing_while_she_talks_interrupts_her():
    from sophia.web_audio import TypedTextFrame

    agent, processor, _, _, interruptions = speaking_processor()
    asyncio.run(feed_many(processor, [BotStartedSpeakingFrame(), TypedTextFrame(text="ok")]))

    assert interruptions == [True]
    assert agent.heard == ["ok"]


def test_words_just_spoken_are_answered_with_the_typed_message():
    """Half said, then typed: one turn, not two answers talking over each other."""
    from sophia.web_audio import TypedTextFrame

    agent = FakeAgent()
    processor, _, _ = make_processor(agent)
    processor.turn_end_silence = 60

    asyncio.run(feed_many(processor, [
        TranscriptionFrame(text="my name is", user_id="caller", timestamp="now"),
        TypedTextFrame(text="Margaret Hollis"),
    ]))

    assert agent.heard == ["my name is Margaret Hollis"]


# ---------------------------------------------------------------------------
# When the main voice runs out of credit
# ---------------------------------------------------------------------------


def _with_voices(processor, monkeypatch, keys=("k1",)):
    """Cartesia voices for the given keys, then a Deepgram voice."""
    from sophia import voice_app

    monkeypatch.setattr(voice_app, "_cartesia_resting_until", {})
    voices = [object() for _ in keys] + [object()]
    processor.voices = voices
    processor.voice_keys = dict(zip(voices, keys))
    return voices


def test_running_out_of_voice_credit_switches_to_the_backup_and_repeats_the_line(monkeypatch):
    """Cartesia's free credits ran out in a morning, and every call went silent."""
    from pipecat.frames.frames import ErrorFrame, ManuallySwitchServiceFrame
    from sophia import voice_app

    processor, pushed, _ = make_processor(FakeAgent())
    voices = _with_voices(processor, monkeypatch)
    processor.set_spoken_text("Hello, you're through to the practice.")

    error = ErrorFrame(error="{'context_id': 'abc', 'error_code': 'quota_exceeded', 'status_code': 402}")
    asyncio.run(feed(processor, error, FrameDirection.UPSTREAM))
    asyncio.run(feed(processor, error, FrameDirection.UPSTREAM))

    switches = [f for f, _ in pushed if isinstance(f, ManuallySwitchServiceFrame)]
    repeats = [f.text for f, _ in pushed if isinstance(f, TTSSpeakFrame)]
    assert len(switches) == 1 and switches[0].service is voices[1]
    assert repeats == ["Hello, you're through to the practice."]
    assert "k1" in voice_app._cartesia_resting_until


def test_an_empty_account_hands_the_call_to_the_next_account_before_deepgram(monkeypatch):
    from pipecat.frames.frames import ErrorFrame, ManuallySwitchServiceFrame
    from sophia import voice_app

    processor, pushed, _ = make_processor(FakeAgent())
    voices = _with_voices(processor, monkeypatch, keys=("k1", "k2"))
    first = ErrorFrame(error="{'context_id': 'a', 'status_code': 402}")
    first.processor = voices[0]
    asyncio.run(feed(processor, first, FrameDirection.UPSTREAM))
    # The account just left says so again: no second switch.
    again = ErrorFrame(error="{'context_id': 'a', 'status_code': 402}")
    again.processor = voices[0]
    asyncio.run(feed(processor, again, FrameDirection.UPSTREAM))

    switches = [f.service for f, _ in pushed if isinstance(f, ManuallySwitchServiceFrame)]
    assert switches == [voices[1]]
    assert set(voice_app._cartesia_resting_until) == {"k1"}

    # Later the second account runs out too, and Deepgram takes over.
    processor._switched_at = float("-inf")
    later = ErrorFrame(error="{'context_id': 'b', 'status_code': 402}")
    later.processor = voices[1]
    asyncio.run(feed(processor, later, FrameDirection.UPSTREAM))
    switches = [f.service for f, _ in pushed if isinstance(f, ManuallySwitchServiceFrame)]
    assert switches == [voices[1], voices[2]]
    assert set(voice_app._cartesia_resting_until) == {"k1", "k2"}


def test_other_errors_do_not_switch_voices(monkeypatch):
    from pipecat.frames.frames import ErrorFrame, ManuallySwitchServiceFrame

    processor, pushed, _ = make_processor(FakeAgent())
    _with_voices(processor, monkeypatch)
    asyncio.run(feed(processor, ErrorFrame(error="Deepgram connection reset"), FrameDirection.UPSTREAM))
    assert not any(isinstance(f, ManuallySwitchServiceFrame) for f, _ in pushed)


def test_later_calls_start_on_the_backup_voice_while_credit_is_out(monkeypatch):
    from pipecat.services.deepgram.tts import DeepgramTTSService
    from sophia import voice_app

    monkeypatch.setattr(voice_app.config, "SPEECH", _speech(
        tts_provider="cartesia", cartesia_api_key="ck", extra_cartesia_keys=(), cartesia_voice_id="voice-1"))
    monkeypatch.setattr(voice_app, "_cartesia_resting_until", {"ck": float("inf")})
    assert isinstance(voice_app.build_tts(), DeepgramTTSService)


def test_later_calls_skip_an_empty_account_and_use_the_next(monkeypatch):
    from sophia import voice_app

    monkeypatch.setattr(voice_app.config, "SPEECH", _speech(
        tts_provider="cartesia", cartesia_api_key="ck1", extra_cartesia_keys=("", "ck2", "ck3"),
        cartesia_voice_id="voice-1"))
    monkeypatch.setattr(voice_app, "_cartesia_resting_until", {"ck1": float("inf")})
    assert voice_app.usable_cartesia_keys() == ["ck2", "ck3"]
    assert "ck2" not in repr(voice_app.config.SPEECH)


def test_a_refused_connection_at_the_start_switches_without_repeating(monkeypatch):
    """Nothing had been said yet, and repeating made the greeting play twice."""
    from pipecat.frames.frames import ErrorFrame, ManuallySwitchServiceFrame

    processor, pushed, _ = make_processor(FakeAgent())
    _with_voices(processor, monkeypatch)
    processor.set_spoken_text("Hello, you're through to the practice.")

    error = ErrorFrame(error="Unknown error occurred: server rejected WebSocket connection: HTTP 402")
    asyncio.run(feed(processor, error, FrameDirection.UPSTREAM))

    assert any(isinstance(f, ManuallySwitchServiceFrame) for f, _ in pushed)
    assert not any(isinstance(f, TTSSpeakFrame) for f, _ in pushed)


@pytest.mark.parametrize(
    "hour, minute, expected",
    [(7, 50, "closed, opens at 08:00"), (10, 0, "open"), (13, 30, "closed for lunch until 14:00"), (19, 0, "closed")],
)
def test_the_page_shows_the_practice_clock(hour, minute, expected):
    """Testers in India took "urgent slots not released yet" for a bug at 7:50am UK time."""
    from sophia import voice_app

    shown = voice_app._practice_clock(clock.combine(date(2026, 9, 21), time(hour, minute)))
    assert shown == {"practice_time": f"{hour:02d}:{minute:02d}", "practice_status": expected}


def test_pandora_speaks_at_the_chosen_speed(monkeypatch):
    from pipecat.services.deepgram.tts import DeepgramTTSService
    from sophia import voice_app

    monkeypatch.setattr(voice_app.config, "SPEECH", _speech(tts_voice="aura-2-pandora-en", tts_speed="0.9"))
    tts = voice_app.build_tts()
    assert isinstance(tts, DeepgramTTSService)
    assert tts._settings.voice == "aura-2-pandora-en"
    assert tts._settings.speed == 0.9


def test_an_older_voice_gets_no_speed_it_cannot_use(monkeypatch):
    from sophia import voice_app

    monkeypatch.setattr(voice_app.config, "SPEECH", _speech(tts_voice="aura-athena-en", tts_speed="0.9"))
    assert voice_app.build_tts()._settings.speed is None


# ---------------------------------------------------------------------------
# Hanging up after goodbye
# ---------------------------------------------------------------------------


def _goodbye_processor(monkeypatch):
    from sophia import voice_app
    from sophia.web_audio import TypedTextFrame

    monkeypatch.setattr(voice_app, "HANG_UP_DELAY_SECS", 0.01)
    agent = FakeAgent(reply="Thank you for calling, goodbye.")
    agent.ends_call = True
    processor, pushed, events = make_processor(agent)
    return processor, pushed, events, TypedTextFrame


def test_the_call_ends_once_sophia_has_said_goodbye(monkeypatch):
    """A tester said goodbye and the line stayed open until he pressed hang up."""
    from pipecat.frames.frames import EndTaskFrame

    processor, pushed, events, Typed = _goodbye_processor(monkeypatch)

    async def call():
        await feed(processor, Typed(text="no that's all, thanks"))
        await feed(processor, BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)

    asyncio.run(call())
    ends = [(f, d) for f, d in pushed if isinstance(f, EndTaskFrame)]
    assert ends and ends[0][1] is FrameDirection.UPSTREAM
    assert events[-1]["type"] == "ended"


def test_speaking_after_the_goodbye_keeps_the_call_open(monkeypatch):
    from pipecat.frames.frames import EndTaskFrame
    from sophia import voice_app

    processor, pushed, _, Typed = _goodbye_processor(monkeypatch)
    monkeypatch.setattr(voice_app, "HANG_UP_DELAY_SECS", 0.2)

    async def call():
        await feed(processor, Typed(text="no that's all, thanks"))
        await feed(processor, BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        processor.agent.ends_call = False
        await feed(processor, Typed(text="oh wait, one more thing"))
        await asyncio.sleep(0.3)

    asyncio.run(call())
    assert not any(isinstance(f, EndTaskFrame) for f, _ in pushed)


def test_an_ordinary_reply_does_not_end_the_call(monkeypatch):
    from pipecat.frames.frames import EndTaskFrame
    from sophia import voice_app
    from sophia.web_audio import TypedTextFrame

    monkeypatch.setattr(voice_app, "HANG_UP_DELAY_SECS", 0.01)
    processor, pushed, _ = make_processor(FakeAgent(reply="Is there anything else?"))

    async def call():
        await feed(processor, TypedTextFrame(text="book it please"))
        await feed(processor, BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)

    asyncio.run(call())
    assert not any(isinstance(f, EndTaskFrame) for f, _ in pushed)
