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
from typing import Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.deepgram.tts import DeepgramTTSService, DeepgramTTSSettings
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from . import config, db, prompts
from .agent_text import SophiaAgent


class SophiaVoiceProcessor(FrameProcessor):
    """
    The only new logic in the voice layer: speech in, speech out.

    Everything else is delegated to the agent that already exists.
    """

    def __init__(self, agent: SophiaAgent, on_event: Callable[[dict], None] | None = None):
        super().__init__()
        self.agent = agent
        self.on_event = on_event or (lambda event: None)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, TranscriptionFrame) or not (frame.text or "").strip():
            await self.push_frame(frame, direction)
            return

        said = frame.text.strip()
        self.on_event({"type": "caller", "text": said})

        # agent.say does blocking network calls. Running it on the event
        # loop would stall audio for every other participant in the
        # pipeline, including the cancellation of playback when the caller
        # interrupts, so it goes to a worker thread.
        try:
            turn = await asyncio.to_thread(self.agent.say, said)
        except Exception as failure:  # noqa: BLE001
            self.on_event({"type": "error", "text": str(failure)})
            await self.push_frame(
                TTSSpeakFrame(
                    "I am very sorry, something has gone wrong at my end. "
                    "Please ring the practice directly on "
                    f"{config.PRACTICE_PHONE}."
                )
            )
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


def build_pipeline(
    connection: SmallWebRTCConnection,
    on_event: Callable[[dict], None] | None = None,
) -> tuple[PipelineTask, SophiaAgent]:
    """
    Assemble one call.

    A fresh database connection and a fresh agent per call, so verification
    state can never leak from one caller to the next.
    """
    conn = db.connect()
    agent = SophiaAgent(conn)

    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )

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

    task = PipelineTask(pipeline)

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
        }

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
        connection = SmallWebRTCConnection()
        await connection.initialize(sdp=body.sdp, type=body.type)

        def record(event: dict) -> None:
            app.state.events.append(event)

        task, _agent = build_pipeline(connection, on_event=record)

        # The call runs for as long as the browser stays connected. It is
        # deliberately not awaited here, because this request has to return
        # the SDP answer before the browser can connect at all.
        asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))

        return connection.get_answer()

    web_dir = config.ROOT_DIR / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")

    return app


app = create_app()
