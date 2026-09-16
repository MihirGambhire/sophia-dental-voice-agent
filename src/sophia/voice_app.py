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
        -> WebRTC transport
        -> voice activity detection
        -> Deepgram speech to text, UK English
        -> SophiaVoiceProcessor  (calls the existing agent)
        -> Deepgram text to speech, British voice
        -> WebRTC transport
        -> browser speaker

No phone number anywhere, which is what keeps this free.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipecat.audio.vad.silero import SileroVADAnalyzer
from loguru import logger
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.deepgram.tts import DeepgramTTSService, DeepgramTTSSettings
from pipecat.transports.base_transport import TransportParams
from aiortc import RTCIceServer
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from . import config, db, prompts, web_audio
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


class SophiaVoiceProcessor(FrameProcessor):
    """
    Speech in, speech out, one complete caller turn at a time.

    Everything Sophia decides is delegated to the agent that already
    exists. This class only decides when the caller has finished speaking,
    and makes sure turns reach the agent strictly one after another.
    """

    def __init__(
        self,
        agent: SophiaAgent,
        on_event: Callable[[dict], None] | None = None,
        turn_end_silence: float = TURN_END_SILENCE_SECS,
    ):
        super().__init__()
        self.agent = agent
        self.on_event = on_event or (lambda event: None)
        self.turn_end_silence = turn_end_silence

        self._pending: list[str] = []
        self._caller_speaking = False
        self._flush_task: asyncio.Task | None = None
        self._turn_tasks: set[asyncio.Task] = set()
        # One turn at a time. The agent's database connection is shared
        # across worker threads, which is only safe if turns never overlap.
        self._turn_lock = asyncio.Lock()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            # Still talking: whatever was heard so far is not a full turn.
            self._caller_speaking = True
            self._cancel_flush()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._caller_speaking = False
            if self._pending:
                self._schedule_flush()
        elif isinstance(frame, InterimTranscriptionFrame):
            # Words are still arriving, which is the same signal.
            self._cancel_flush()
        elif isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                self._pending.append(text)
                if not self._caller_speaking:
                    self._schedule_flush()
                return

        await self.push_frame(frame, direction)

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


def build_pipeline(
    transport,
    on_event: Callable[[dict], None] | None = None,
    sample_rate: int | None = None,
) -> tuple[PipelineTask, SophiaAgent]:
    """
    Assemble one call around whichever transport carries the audio.

    The pipeline is identical for WebRTC and for the WebSocket transport.
    Only the pipe the audio travels down differs, which is the same
    separation that kept every rule out of this file in the first place.

    A fresh database connection and a fresh agent per call, so verification
    state can never leak from one caller to the next.
    """
    # Opened here on the event loop thread, used from worker threads by
    # agent.say. SophiaVoiceProcessor's lock keeps it to one turn at a time.
    conn = db.connect(shared_across_threads=True)
    agent = SophiaAgent(conn)

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

    tts = DeepgramTTSService(
        api_key=config.SPEECH.deepgram_api_key,
        settings=DeepgramTTSSettings(voice=config.SPEECH.tts_voice),
    )

    sophia = SophiaVoiceProcessor(agent, on_event=on_event)

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
        """Sophia speaks first, as a receptionist does."""
        if on_event:
            on_event({"type": "sophia", "text": prompts.GREETING, "tools": []})
        await task.queue_frames([TTSSpeakFrame(prompts.GREETING)])

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


def _aiortc_ice_servers() -> list[RTCIceServer]:
    """
    The same ICE list as the browser gets, in aiortc's shape and order.

    STUN entries are kept as they are. All TURN URLs that share credentials
    are merged into one entry sorted by _turn_preference, because aiortc
    picks the first TURN URL across the whole list.
    """
    stun: list[RTCIceServer] = []
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


def create_app() -> FastAPI:
    app = FastAPI(title="Sophia")
    app.state.events = []

    @app.get("/")
    async def index():
        return FileResponse(config.ROOT_DIR / "web" / "index.html")

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "practice": config.PRACTICE_NAME,
            "disclaimer": config.DISCLAIMER,
            "provider": config.LLM.provider,
            "model": config.LLM.model,
            "voice": config.SPEECH.tts_voice,
            "speech_configured": bool(config.SPEECH.deepgram_api_key),
            "turn_configured": config.turn_configured(),
        }

    @app.get("/api/ice")
    async def ice():
        """
        How the browser should connect.

        Served rather than hardcoded in the page, so the two ends of the
        call cannot end up using different ICE servers.
        """
        return {"iceServers": config.ice_servers()}

    @app.get("/api/events")
    async def events(after: int = 0):
        """
        Everything said so far, for the live transcript in the browser.

        Polled rather than streamed. A websocket would be tidier, but this
        is a demo panel beside the call, not the call itself, and polling
        cannot fail in a way that affects the audio.
        """
        return {"events": app.state.events[after:], "total": len(app.state.events)}

    @app.post("/api/offer")
    async def offer(body: Offer):
        if not config.SPEECH.deepgram_api_key:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "No DEEPGRAM_API_KEY in .env, so there is no speech layer."
                },
            )

        app.state.events = []

        # Without TURN here, the server offers only candidates on its own
        # network, which a caller anywhere else cannot reach. The call
        # then fails in ICE checking and the browser reports "Call ended".
        connection = SmallWebRTCConnection(ice_servers=_aiortc_ice_servers())
        await connection.initialize(sdp=body.sdp, type=body.type)

        def record(event: dict) -> None:
            app.state.events.append(event)

        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        task, _agent = build_pipeline(transport, on_event=record)

        # The call runs for as long as the browser stays connected. It is
        # deliberately not awaited here, because this request has to return
        # the SDP answer before the browser can connect at all.
        asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))

        return connection.get_answer()

    @app.websocket("/ws")
    async def call_over_websocket(websocket: WebSocket):
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

        app.state.events = []

        def record(event: dict) -> None:
            app.state.events.append(event)

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
            transport, on_event=record, sample_rate=web_audio.SAMPLE_RATE
        )

        # Unlike the WebRTC offer, this request IS the call, so it is
        # awaited: the handler returns when the caller hangs up.
        await PipelineRunner(handle_sigint=False).run(task)

    web_dir = config.ROOT_DIR / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")

    return app


app = create_app()
