"""
The voice layer: a real phone call, in a browser.

The important thing about this file is how little it contains. Every rule
Sophia follows, every tool she can call, the safety screen, the whole
conversation loop: none of it is here. It all lives in agent_text.py and
below, unchanged. This file only turns speech into text, hands it to the
agent that already worked, and turns the answer back into speech.

That was the point of building the text agent first. A bug in a voice
demo is miserable to diagnose, because you cannot tell whether the agent
misunderstood, the transcription was wrong, or the audio never arrived.
Keeping the logic somewhere it can be tested in silence means anything
that goes wrong in here is an audio problem, which narrows it enormously.

    browser mic
        -> WebSocket, raw 16 kHz PCM   (web_audio.py; WebRTC kept for local use)
        -> voice activity detection
        -> Deepgram speech to text, UK English
        -> SophiaVoiceProcessor  (turn taking, interruptions, calls the agent)
        -> Deepgram text to speech, British voice
        -> WebSocket
        -> browser speaker

Each tester gets their own database and transcript, see sessions.py.

No phone number anywhere, which is what keeps this free.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
import re
import time
from typing import Callable

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipecat.audio.vad.silero import SileroVADAnalyzer
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    ManuallySwitchServiceFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.service_switcher import ServiceSwitcher, ServiceSwitcherStrategyManual
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.deepgram.tts import DeepgramTTSService, DeepgramTTSSettings
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from . import clock, config, db, demo, outbound, prompts, providers, sessions, web_audio
from .agent_text import SophiaAgent


# How long the caller must be silent before their turn is treated as over.
#
# The first version answered every transcript fragment the moment it
# arrived. Speech to text finalises on short pauses, so a real caller
# saying "thirteenth April two thousand and... four" produced two turns,
# and Sophia accepted the half date and moved on to asking for the
# postcode. "Can you tell me when the" got its own reply before the rest of
# the sentence arrived. People pause mid sentence, especially over dates
# and postcodes, so this errs long.
TURN_END_SILENCE_SECS = 1.2

FAILURE_REPLY = (
    "I am very sorry, something has gone wrong at my end. "
    f"Please ring the practice directly on {config.PRACTICE_PHONE}."
)


# Interruptions.
#
# Until now the browser muted the microphone while Sophia spoke, so a phone
# speaker could not feed her own voice back into her. That also meant
# nobody could talk over her, which every tester tried within a minute,
# because that is how people behave on the phone.
#
# The microphone now stays open. The risk is echo: her voice coming out of
# a speaker and back in through the microphone looks exactly like a caller
# interrupting, and she would cut herself off mid sentence. Browser echo
# cancellation helps but differs between phones, so it is not relied on.
# Instead, anything transcribed while she is speaking, or shortly after, is
# compared with the words she is actually saying. If it is her words, it is
# her echo and is ignored.
#
# A real interruption also has to be more than a noise: at least two words
# that are not hers, or one of the short words people use to cut in.
ECHO_TAIL_SECS = 2.5
INTERRUPT_MIN_WORDS = 2
INTERRUPT_WORDS = frozenset(
    {"stop", "wait", "sorry", "hang", "hold", "no", "excuse", "pardon", "actually", "sophia"}
)


# Sophia's voice, when Cartesia's free credits run out.
#
# The free tier's 20,000 monthly credits went in one morning of samples and
# testing, and from then every call was silent: Cartesia answered each
# request with HTTP 402. Now the first out of credit error switches the call
# to the next voice and repeats what failed, and later calls leave that
# key out until this long has passed. With several Cartesia keys the next
# voice is the next account; Deepgram comes last.
VOICE_OUT_OF_CREDIT_REST_SECS = 6 * 3600
# Out of credit keys, and when each may be tried again. Held in memory only.
_cartesia_resting_until: dict[str, float] = {}
# Cartesia voices opened per call. Each opens a connection as the call
# starts, in parallel, so two covers a key running out mid call without
# every call connecting to every account.
CARTESIA_VOICES_PER_CALL = 2
# Errors that do not say which voice failed, arriving this soon after a
# switch, belong to the voice just left.
SWITCH_SETTLE_SECS = 2.0


def _is_out_of_voice_credit(error) -> bool:
    text = str(error).lower()
    return "402" in text or "quota_exceeded" in text or "insufficient credits" in text


def usable_cartesia_keys() -> list[str]:
    """The Cartesia keys not known to be out of credit, in order."""
    if not config.SPEECH.uses_cartesia:
        return []
    now = time.monotonic()
    return [key for key in config.SPEECH.cartesia_keys if _cartesia_resting_until.get(key, 0.0) <= now]


def cartesia_available() -> bool:
    return bool(usable_cartesia_keys())


def _mark_cartesia_out_of_credit(key: str) -> None:
    _cartesia_resting_until[key] = time.monotonic() + VOICE_OUT_OF_CREDIT_REST_SECS


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


class SophiaVoiceProcessor(FrameProcessor):
    """
    Speech in, speech out, one complete caller turn at a time.

    Everything Sophia decides is delegated to the agent that already
    exists. This class decides when the caller has finished speaking, when
    the caller is genuinely talking over Sophia rather than hearing her
    echo, and makes sure turns reach the agent strictly one after another.
    """

    def __init__(
        self,
        agent: SophiaAgent,
        on_event: Callable[[dict], None] | None = None,
        turn_end_silence: float = TURN_END_SILENCE_SECS,
        voices: list[FrameProcessor] | None = None,
        voice_keys: dict[FrameProcessor, str] | None = None,
    ):
        super().__init__()
        self.agent = agent
        self.on_event = on_event or (lambda event: None)
        self.turn_end_silence = turn_end_silence
        # The voices behind the switch, in the order to use them, and the
        # Cartesia key each one speaks with. Deepgram, last, has no key.
        self.voices = list(voices or [])
        self.voice_keys = dict(voice_keys or {})
        self._active_voice = 0
        self._switched_at = float("-inf")

        self._pending: list[str] = []
        self._caller_speaking = False
        self._flush_task: asyncio.Task | None = None
        self._turn_tasks: set[asyncio.Task] = set()
        # One turn at a time. The agent's database connection is shared
        # across worker threads, which is only safe if turns never overlap.
        self._turn_lock = asyncio.Lock()

        # What Sophia is saying, so her own echo can be recognised.
        self._bot_speaking = False
        self._bot_text = ""
        self._echo_until = 0.0

    # Overridable in tests, so the echo window does not depend on real time.
    def _now(self) -> float:
        return time.monotonic()

    def set_spoken_text(self, text: str) -> None:
        """
        Register what Sophia is about to say.

        Every reply passes through here, and so must anything spoken from
        elsewhere, like the greeting. Without it, her own greeting echoing
        back through a phone speaker would be taken as the caller speaking.
        """
        self._bot_text = text or ""
        # Held open until she stops speaking, which normally resets it to a
        # short tail. Capped, so a reply that never produces audio cannot
        # leave every later caller sentence being checked against it.
        self._echo_until = self._now() + 30.0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, ErrorFrame) and _is_out_of_voice_credit(frame.error):
            # Only an error about a particular sentence means that sentence
            # went unheard. A connection refused at the start of the call
            # comes before anything was said, and repeating then made the
            # greeting play twice.
            await self._voice_out_of_credit(frame, repeat="context_id" in str(frame.error))
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            # Her last words are still travelling: out of the speaker, into
            # the microphone, through the network and speech to text.
            self._echo_until = self._now() + ECHO_TAIL_SECS
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            # Inside the echo window, voice activity alone could be Sophia
            # herself, so only transcribed words decide whether the caller
            # is talking.
            if not self._in_echo_window():
                self._caller_speaking = True
                self._cancel_flush()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._caller_speaking = False
            if self._pending:
                self._schedule_flush()
        elif isinstance(frame, InterimTranscriptionFrame):
            text = (frame.text or "").strip()
            if text and not self._is_echo(text):
                # Words are still arriving, so the turn is not over.
                self._cancel_flush()
                if self._bot_speaking and self._is_barge_in(text):
                    await self._interrupt(text)
        elif isinstance(frame, web_audio.TypedTextFrame):
            await self._typed(frame.text)
            return
        elif isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                if self._is_echo(text):
                    logger.debug(f"Ignoring Sophia's own echo: {text!r}")
                    return
                if self._bot_speaking and self._is_barge_in(text):
                    await self._interrupt(text)
                self._pending.append(text)
                if not self._caller_speaking:
                    self._schedule_flush()
                return

        await self.push_frame(frame, direction)

    async def _voice_out_of_credit(self, frame: ErrorFrame, repeat: bool = True) -> None:
        """The voice speaking is out of credit: carry on in the next one."""
        if not self.voices:
            return
        if frame.processor in self.voices:
            failed = self.voices.index(frame.processor)
        elif self._now() - self._switched_at < SWITCH_SETTLE_SECS:
            # Unattributed, and a switch has only just happened: this is the
            # voice already left saying so again, not the new one failing.
            return
        else:
            failed = self._active_voice
        key = self.voice_keys.get(self.voices[failed])
        if key:
            _mark_cartesia_out_of_credit(key)
        if failed != self._active_voice or failed == len(self.voices) - 1:
            return
        self._active_voice = failed + 1
        self._switched_at = self._now()
        logger.warning(f"Voice {failed + 1} is out of credit; switching this call to voice {failed + 2}")
        await self.push_frame(ManuallySwitchServiceFrame(service=self.voices[self._active_voice]))
        # A sentence that failed was never heard, so it is said again.
        if repeat and self._bot_text:
            await self.push_frame(TTSSpeakFrame(self._bot_text))

    async def _typed(self, text: str) -> None:
        """
        The caller typed instead of speaking.

        A typed message is complete when it arrives, so it does not wait for
        silence. It stops Sophia if she is talking, as speaking would, and
        anything the caller had just said aloud is answered together with it.
        """
        if self._bot_speaking:
            await self._interrupt(text)
        self._cancel_flush()
        said = " ".join([*self._pending, text]).strip()
        self._pending.clear()
        task = asyncio.create_task(self._respond(said))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    # -- telling a real interruption from echo ----------------------------

    def _in_echo_window(self) -> bool:
        return self._bot_speaking or self._now() < self._echo_until

    def _is_echo(self, text: str) -> bool:
        """
        True if this transcript is Sophia's own voice coming back.

        Short fragments must be entirely her words. Longer ones only mostly,
        because speech to text will not reproduce her sentence exactly.
        Outside the window nothing is echo, so a caller repeating a phrase
        she used a minute ago is still heard.
        """
        if not self._in_echo_window():
            return False
        said = _words(text)
        if not said:
            return True
        hers = set(_words(self._bot_text))
        if not hers:
            return False
        overlap = sum(word in hers for word in said) / len(said)
        return overlap == 1.0 if len(said) <= 2 else overlap >= 0.7

    @staticmethod
    def _is_barge_in(text: str) -> bool:
        words = _words(text)
        return len(words) >= INTERRUPT_MIN_WORDS or any(w in INTERRUPT_WORDS for w in words)

    async def _interrupt(self, heard: str) -> None:
        """Stop Sophia mid sentence because the caller is talking."""
        if not self._bot_speaking:
            return
        self._bot_speaking = False
        self._echo_until = self._now() + ECHO_TAIL_SECS
        interrupted_reply = self._bot_text
        logger.info(f"Caller interrupted Sophia: {heard!r}")
        self.on_event({"type": "interrupted", "text": heard})

        # Clears her queued speech in the text to speech service and the
        # output transport, and the serializer tells the browser to stop
        # playing what it already has.
        await self.broadcast_interruption()

        # The model believes it said the whole reply. Telling it otherwise
        # stops it assuming the caller heard, say, a fee they never heard.
        task = asyncio.create_task(self._note_interrupted(interrupted_reply))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _note_interrupted(self, reply: str) -> None:
        async with self._turn_lock:
            self.agent.note_interrupted(reply)

    # -- deciding when the caller has finished ----------------------------

    def _cancel_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None

    def _schedule_flush(self) -> None:
        self._cancel_flush()
        self._flush_task = asyncio.create_task(self._flush_after_silence())

    async def _flush_after_silence(self) -> None:
        await asyncio.sleep(self.turn_end_silence)
        if not self._pending:
            return
        said = " ".join(self._pending)
        self._pending.clear()
        # Detached from the flush task, so a caller who starts speaking
        # again cannot cancel a turn that is already being answered.
        task = asyncio.create_task(self._respond(said))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    # -- answering --------------------------------------------------------

    async def _respond(self, said: str) -> None:
        async with self._turn_lock:
            self.on_event({"type": "caller", "text": said})

            # agent.say makes blocking network calls. On the event loop it
            # would stall the audio, so it runs in a worker thread.
            try:
                turn = await asyncio.to_thread(self.agent.say, said)
            except Exception:  # noqa: BLE001
                # Logged in full for whoever reads the server output. The
                # caller gets a sentence, never an exception message: a raw
                # SQLite error was shown on screen to the first testers.
                logger.exception("Sophia failed to answer a caller turn")
                self.on_event(
                    {"type": "error", "text": "Something went wrong on our side."}
                )
                self.set_spoken_text(FAILURE_REPLY)
                await self.push_frame(TTSSpeakFrame(FAILURE_REPLY))
                return

            self.on_event(
                {
                    "type": "sophia",
                    "text": turn.reply,
                    "tools": turn.tools_used,
                    "safety": turn.safety_level,
                    "seconds": round(turn.latency_seconds, 2),
                }
            )
            self.set_spoken_text(turn.reply)
            await self.push_frame(TTSSpeakFrame(turn.reply))

    async def wait_idle(self) -> None:
        """Wait for any pending or running turn. Used by the tests."""
        if self._flush_task:
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        while self._turn_tasks:
            await asyncio.gather(*list(self._turn_tasks), return_exceptions=True)

    async def cleanup(self) -> None:
        self._cancel_flush()
        for task in list(self._turn_tasks):
            task.cancel()
        await super().cleanup()


def build_tts():
    """
    The voice Sophia speaks with: Cartesia when configured, otherwise Deepgram.

    Falling back rather than failing, so a missing Cartesia key or voice id
    leaves the demo working on the old voice instead of silent.
    """
    keys = usable_cartesia_keys()
    if keys:
        return _cartesia_tts(keys[0])
    if config.SPEECH.uses_cartesia:
        logger.warning("Cartesia ran out of credit recently; using Deepgram's voice")
    elif config.SPEECH.tts_provider == "cartesia":
        logger.warning("TTS_PROVIDER is cartesia but the key or voice id is missing; using Deepgram")
    return _deepgram_tts()


def _cartesia_tts(api_key: str):
    from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig

    speech = config.SPEECH
    try:
        speed = min(1.5, max(0.6, float(speech.cartesia_speed))) if speech.cartesia_speed else None
    except ValueError:
        logger.warning(f"CARTESIA_SPEED {speech.cartesia_speed!r} is not a number; using the default")
        speed = None
    style = GenerationConfig(speed=speed, emotion=speech.cartesia_emotion or None)
    return CartesiaTTSService(
        api_key=api_key,
        settings=CartesiaTTSService.Settings(
            voice=speech.cartesia_voice_id,
            model=speech.cartesia_model,
            generation_config=style if (style.speed or style.emotion) else None,
        ),
    )


def _deepgram_tts():
    speech = config.SPEECH
    speed = None
    # Only Aura-2 voices accept a speed; an older voice is left at its own pace.
    if speech.tts_speed and speech.tts_voice.startswith("aura-2-"):
        try:
            speed = min(1.5, max(0.7, float(speech.tts_speed)))
        except ValueError:
            logger.warning(f"DEEPGRAM_TTS_SPEED {speech.tts_speed!r} is not a number; using the default")
    return DeepgramTTSService(
        api_key=speech.deepgram_api_key,
        settings=DeepgramTTSSettings(voice=speech.tts_voice, speed=speed),
    )


def build_pipeline(
    transport,
    on_event: Callable[[dict], None] | None = None,
    sample_rate: int | None = None,
    reminder_id: int | None = None,
    db_path=None,
) -> tuple[PipelineTask, SophiaAgent]:
    """
    Assemble one call around whichever transport carries the audio.

    The pipeline is identical for WebRTC and for the WebSocket transport.
    Only the pipe the audio travels down differs, which is the same
    separation that kept every rule out of this file in the first place.

    A fresh database connection and a fresh agent per call, so verification
    state can never leak from one caller to the next. db_path is the
    tester's own database, see sessions.py.
    """
    # Opened here on the event loop thread, used from worker threads by
    # agent.say. SophiaVoiceProcessor's lock keeps it to one turn at a time.
    conn = db.connect(db_path, shared_across_threads=True)

    # An outbound reminder call when a reminder is given: Sophia speaks
    # first as if she had rung the patient.
    reminder = outbound.load(conn, reminder_id) if reminder_id is not None else None
    if reminder is not None:
        outbound.mark_started(conn, reminder)
    agent = SophiaAgent(conn, reminder=reminder)

    stt = DeepgramSTTService(
        api_key=config.SPEECH.deepgram_api_key,
        settings=DeepgramSTTSettings(
            model=config.SPEECH.stt_model,
            # UK English matters more here than anywhere else in the
            # project. Postcodes, surnames and "twenty past nine" are all
            # transcribed badly by a US English model.
            language=config.SPEECH.stt_language,
            smart_format=True,
            punctuate=True,
            interim_results=True,
        ),
    )

    voices: list = []
    voice_keys: dict = {}
    keys = usable_cartesia_keys()[:CARTESIA_VOICES_PER_CALL]
    if keys:
        # Every voice sits behind a switch, so a call whose voice runs out
        # of credit carries on in the next: another Cartesia account, then
        # Deepgram.
        for key in keys:
            voice = _cartesia_tts(key)
            voices.append(voice)
            voice_keys[voice] = key
        voices.append(_deepgram_tts())
        tts = ServiceSwitcher(services=voices, strategy_type=ServiceSwitcherStrategyManual)
    else:
        tts = build_tts()

    sophia = SophiaVoiceProcessor(agent, on_event=on_event, voices=voices, voice_keys=voice_keys)

    pipeline = Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=SileroVADAnalyzer()),
            stt,
            sophia,
            tts,
            transport.output(),
        ]
    )

    params = (
        PipelineParams(audio_in_sample_rate=sample_rate, audio_out_sample_rate=sample_rate)
        if sample_rate
        else None
    )
    task = PipelineTask(pipeline, params=params)

    @transport.event_handler("on_client_connected")
    async def _greet(_transport, _client):
        """Sophia speaks first, as a receptionist does, or as the caller on an outbound call."""
        if on_event:
            on_event({"type": "sophia", "text": agent.greeting, "tools": []})
        sophia.set_spoken_text(agent.greeting)
        await task.queue_frames([TTSSpeakFrame(agent.greeting)])

    @transport.event_handler("on_client_disconnected")
    async def _hang_up(_transport, _client):
        conn.close()
        await task.queue_frames([EndFrame()])

    return task, agent


# ---------------------------------------------------------------------------
# The web server
# ---------------------------------------------------------------------------


class Offer(BaseModel):
    sdp: str
    type: str


def _turn_preference(url: str) -> int:
    """
    Sort key putting TURN over TCP and TLS ahead of TURN over UDP.

    aiortc only ever uses the FIRST turn: URL it finds, unlike a browser,
    which tries them all. Metered list plain UDP on port 80 first. On the
    office network this was developed on, outbound UDP to the relay is
    blocked, so every allocation silently timed out after five seconds and
    the server never had a relay address of its own. Probed directly:

        turn  :80   udp   no relay, 5.0s timeout
        turn  :80   tcp   relay in 0.1s
        turn  :443  udp   no relay, 5.0s timeout
        turns :443  tcp   relay in 0.1s

    TLS on 443 goes first because it looks like ordinary HTTPS and survives
    the most restrictive firewalls. It costs a little latency on open
    networks, which is a fair price for the call connecting at all.
    """
    if url.startswith("turns:"):
        return 0
    if url.startswith("turn:") and "transport=tcp" in url:
        return 1
    if url.startswith("turn:"):
        return 2
    return 3


def _aiortc_ice_servers() -> list:
    """
    The same ICE list as the browser gets, in aiortc's shape and order.

    WebRTC is imported only here and in the offer route. It is kept for
    calls on the same machine, but the page uses the WebSocket, and leaving
    the WebRTC libraries out keeps the deployed server small enough for a
    free hosting instance.

    STUN entries are kept as they are. All TURN URLs that share credentials
    are merged into one entry sorted by _turn_preference, because aiortc
    picks the first TURN URL across the whole list.
    """
    from aiortc import RTCIceServer

    stun: list = []
    turn_urls: list[str] = []
    username = credential = None

    for entry in config.ice_servers():
        urls = entry["urls"]
        urls = [urls] if isinstance(urls, str) else list(urls)
        relay = [url for url in urls if url.startswith(("turn:", "turns:"))]
        if relay:
            turn_urls.extend(relay)
            username = username or entry.get("username")
            credential = credential or entry.get("credential")
        else:
            stun.append(RTCIceServer(urls=urls))

    servers = list(stun)
    if turn_urls:
        servers.insert(
            0,
            RTCIceServer(
                urls=sorted(dict.fromkeys(turn_urls), key=_turn_preference),
                username=username,
                credential=credential,
            ),
        )
    return servers


def _practice_clock(now=None) -> dict:
    """The practice's local time and whether it is open, as the page shows it."""
    now = now or clock.now()
    if clock.is_within_opening_hours(now):
        status = "open"
    elif clock.is_open_day(now.date()) and now.strftime("%H:%M") < config.OPENING_TIME:
        status = f"closed, opens at {config.OPENING_TIME}"
    elif clock.is_open_day(now.date()) and config.LUNCH_START <= now.strftime("%H:%M") < config.LUNCH_END:
        status = f"closed for lunch until {config.LUNCH_END}"
    else:
        status = "closed"
    return {"practice_time": now.strftime("%H:%M"), "practice_status": status}


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.sessions.clear_leftovers()
        # Build the shared model client now, so the first caller after a
        # restart is not the one kept waiting while it loads certificates.
        for key in config.LLM.gemini_keys:
            await asyncio.to_thread(providers._shared_genai_client, key)
        yield

    app = FastAPI(title="Sophia", lifespan=lifespan)
    app.state.sessions = sessions.SessionStore()

    @app.get("/")
    async def index():
        # no-cache: the page and the server change together. A browser that
        # kept an older page sent no session id after sessions were added,
        # and its transcript would have stayed empty.
        return FileResponse(
            config.ROOT_DIR / "web" / "index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "practice": config.PRACTICE_NAME,
            "disclaimer": config.DISCLAIMER,
            "provider": config.LLM.provider,
            "model": config.LLM.model,
            "model_chain": [f"{provider}:{model}" for provider, model in config.LLM.chain()],
            "models_resting": providers.HEALTH.snapshot(),
            "voice": config.SPEECH.voice_label,
            "speech_configured": bool(config.SPEECH.deepgram_api_key),
            "turn_configured": config.turn_configured(),
            # Testers in India called at 12:20, found urgent appointments
            # "not released yet", and took it for a bug: it was 7:50am in
            # Warrington. The page shows the practice's own clock.
            **_practice_clock(),
        }

    @app.get("/api/ice")
    async def ice():
        """
        How the browser should connect.

        Served rather than hardcoded in the page, so the two ends of the
        call cannot end up using different ICE servers.
        """
        return {"iceServers": config.ice_servers()}

    @app.get("/api/reminders")
    async def reminders(session: str | None = None):
        """The outbound call list, for the practice side of the demo page."""
        store = app.state.sessions
        conn = db.connect(store.database(store.get(session)))
        try:
            return {"calls": outbound.pending(conn)}
        finally:
            conn.close()

    @app.get("/api/demo-patients")
    async def demo_patients(session: str | None = None):
        """The fake records a tester can call as, including any they registered."""
        store = app.state.sessions
        conn = db.connect(store.database(store.get(session)))
        try:
            return {"patients": demo.patient_cards(conn)}
        finally:
            conn.close()

    @app.get("/api/practice-info")
    async def practice_info(session: str | None = None):
        """The facts and fees Sophia works from, for testers to check her against."""
        store = app.state.sessions
        conn = db.connect(store.database(store.get(session)))
        try:
            return demo.practice_card(conn)
        finally:
            conn.close()

    @app.post("/api/reset")
    async def reset(session: str | None = None):
        """Put this tester's patients and appointments back as they started."""
        store = app.state.sessions
        store.reset(store.get(session))
        return {"status": "reset"}

    @app.get("/api/events")
    async def events(after: int = 0, session: str | None = None):
        """
        Everything said so far, for the live transcript in the browser.

        Polled rather than streamed. A websocket would be tidier, but this
        is a demo panel beside the call, not the call itself, and polling
        cannot fail in a way that affects the audio.
        """
        # Only a known session has a transcript. An unknown id is not given
        # a session here, or every poll from a stale page would create one.
        known = app.state.sessions.find(session)
        events = known.events if known else []
        return {"events": events[after:], "total": len(events)}

    @app.post("/api/offer")
    async def offer(body: Offer, session: str | None = None):
        if not config.SPEECH.deepgram_api_key:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "No DEEPGRAM_API_KEY in .env, so there is no speech layer."
                },
            )

        store = app.state.sessions
        caller = store.get(session)
        caller.events.clear()

        from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
        from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

        # Without TURN here, the server offers only candidates on its own
        # network, which a caller anywhere else cannot reach. The call
        # then fails in ICE checking and the browser reports "Call ended".
        connection = SmallWebRTCConnection(ice_servers=_aiortc_ice_servers())
        await connection.initialize(sdp=body.sdp, type=body.type)

        def record(event: dict) -> None:
            store.record(caller, event)

        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        task, _agent = build_pipeline(transport, on_event=record, db_path=store.database(caller))

        # The call runs for as long as the browser stays connected. It is
        # deliberately not awaited here, because this request has to return
        # the SDP answer before the browser can connect at all.
        asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))

        return connection.get_answer()

    @app.websocket("/ws")
    async def call_over_websocket(
        websocket: WebSocket, reminder: int | None = None, session: str | None = None
    ):
        """
        A call carried entirely over one WebSocket.

        This is the path the page uses. It rides the same HTTPS connection
        that already delivered the page, so if the caller could load the
        site, the call connects. See web_audio.py for why this replaced
        WebRTC as the default.
        """
        await websocket.accept()

        if not config.SPEECH.deepgram_api_key:
            await websocket.send_text(
                json.dumps({"type": "error", "text": "No DEEPGRAM_API_KEY in .env."})
            )
            await websocket.close()
            return

        store = app.state.sessions
        caller = store.get(session)
        caller.events.clear()

        def record(event: dict) -> None:
            store.record(caller, event)

        transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=web_audio.SAMPLE_RATE,
                audio_out_sample_rate=web_audio.SAMPLE_RATE,
                serializer=web_audio.RawPCMSerializer(),
                # The browser plays whatever arrives, so the server paces
                # audio in real time rather than dumping a whole sentence at
                # once. That keeps an interruption able to cut it off.
                add_wav_header=False,
            ),
        )
        task, _agent = build_pipeline(
            transport,
            on_event=record,
            sample_rate=web_audio.SAMPLE_RATE,
            reminder_id=reminder,
            db_path=store.database(caller),
        )

        # Unlike the WebRTC offer, this request IS the call, so it is
        # awaited: the handler returns when the caller hangs up.
        await PipelineRunner(handle_sigint=False).run(task)

    web_dir = config.ROOT_DIR / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")

    return app


app = create_app()
