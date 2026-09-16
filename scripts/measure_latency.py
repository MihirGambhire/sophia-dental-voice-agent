"""
Measure how long a caller waits for Sophia to start answering.

    python scripts/measure_latency.py
    python scripts/measure_latency.py --calls 3 --server http://localhost:7861

Needs the voice server running. Each caller sentence is spoken by a second
Deepgram voice, streamed into a real call in real time exactly as a browser
microphone would send it, and the wait is timed from the caller's last word
to the first audio of Sophia's reply. Uses Deepgram credit and model quota.

WHAT THE NUMBER INCLUDES
========================
Everything a caller experiences: speech to text finalising, the deliberate
pause Sophia waits to be sure the caller has finished (TURN_END_SILENCE_SECS
in voice_app.py), every model request and tool call, and text to speech
producing its first audio. It excludes the network between a real caller
and the server, since this runs on the same machine.

The 999 case is reported apart. It never reaches the model, so it shows
what the voice layer costs on its own, and the difference from an ordinary
turn is what the model and tools add.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import websockets  # noqa: E402

from sophia import config, voice_app  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_BYTES = 640  # 20 ms of 16 bit mono audio, what the page sends
CALLER_VOICE = "aura-helios-en"  # a British male voice, so nobody mistakes it for Sophia
# Longer than the echo window, so a caller sentence is never judged against
# Sophia's last words.
QUIET_AFTER_REPLY_SECS = voice_app.ECHO_TAIL_SECS + 0.5

QUESTIONS = [
    "What are your opening hours?",
    "Is there any parking near the practice?",
    "Are you taking on new NHS patients at the moment?",
    "How much is an NHS check up?",
    "Do you do teeth whitening, and how much does it cost?",
]
EMERGENCY = "My face is really swollen and I can't breathe properly."


def synthesise(text: str) -> bytes:
    """The caller's sentence as raw 16 kHz PCM, the same format the page sends."""
    query = urllib.parse.urlencode(
        {"model": CALLER_VOICE, "encoding": "linear16", "sample_rate": SAMPLE_RATE, "container": "none"}
    )
    request = urllib.request.Request(
        f"https://api.deepgram.com/v1/speak?{query}",
        data=json.dumps({"text": text}).encode(),
        headers={
            "Authorization": f"Token {config.SPEECH.deepgram_api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        audio = response.read()
    return audio[: len(audio) - len(audio) % 2]


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))]


class Call:
    """One call over the WebSocket, with a microphone that is always sending."""

    def __init__(self, ws):
        self.ws = ws
        self.audio_times: list[float] = []
        self.pending: bytes = b""
        self.speech_ended: float | None = None

    async def receive(self):
        async for message in self.ws:
            if isinstance(message, bytes) and message:
                self.audio_times.append(time.monotonic())

    async def microphone(self):
        """Sends speech when there is some queued, silence otherwise, every 20 ms."""
        silence = bytes(CHUNK_BYTES)
        next_at = time.monotonic()
        while True:
            chunk, self.pending = self.pending[:CHUNK_BYTES], self.pending[CHUNK_BYTES:]
            await self.ws.send(chunk if chunk else silence)
            if chunk and not self.pending:
                self.speech_ended = time.monotonic()
            next_at += 0.02
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    async def wait_for_quiet(self, timeout: float = 90.0) -> None:
        """Until Sophia has said something and then been silent for a while."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            last = self.audio_times[-1] if self.audio_times else None
            if last is not None and time.monotonic() - last > QUIET_AFTER_REPLY_SECS:
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("Sophia never finished speaking")

    async def say(self, audio: bytes, timeout: float = 90.0) -> float:
        """Speak, then return seconds from the last word to Sophia's first audio."""
        heard_before = len(self.audio_times)
        self.speech_ended = None
        self.pending = audio
        deadline = time.monotonic() + len(audio) / (2 * SAMPLE_RATE) + timeout
        while time.monotonic() < deadline:
            if self.speech_ended and len(self.audio_times) > heard_before:
                first = next(t for t in self.audio_times[heard_before:])
                if first >= self.speech_ended:
                    return first - self.speech_ended
            await asyncio.sleep(0.01)
        raise TimeoutError("no reply")


async def one_call(server: str, audio: dict[str, bytes], questions: list[str]) -> list[dict]:
    session = f"latency-{uuid.uuid4().hex[:12]}"
    ws_url = server.replace("http", "ws", 1) + f"/ws?session={session}"
    results = []
    async with websockets.connect(ws_url, max_size=None) as ws:
        call = Call(ws)
        tasks = [asyncio.create_task(call.receive()), asyncio.create_task(call.microphone())]
        try:
            await call.wait_for_quiet()  # the greeting
            for text in questions:
                wait = await call.say(audio[text])
                results.append({"said": text, "wait": wait})
                await call.wait_for_quiet()
        finally:
            for task in tasks:
                task.cancel()

    # What the agent itself took on each turn, as recorded by the server.
    with urllib.request.urlopen(f"{server}/api/events?session={session}") as response:
        events = json.load(response)["events"]
    heard = [e for e in events if e.get("type") == "caller"]
    replies = [e for e in events if e.get("type") == "sophia"][1:]  # skip the greeting
    for result, caller, reply in zip(results, heard, replies):
        result["transcribed"] = caller.get("text", "")
        result["agent"] = reply.get("seconds")
        result["emergency"] = reply.get("safety") == "emergency_999"
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure caller to first audio latency.")
    parser.add_argument("--server", default="http://localhost:7861")
    parser.add_argument("--calls", type=int, default=2)
    args = parser.parse_args()

    if not config.SPEECH.deepgram_api_key:
        print("No DEEPGRAM_API_KEY in .env.")
        sys.exit(1)

    questions = QUESTIONS + [EMERGENCY]
    print(f"\nSynthesising {len(questions)} caller sentences with {CALLER_VOICE}...")
    audio = {text: synthesise(text) for text in questions}

    results: list[dict] = []
    for number in range(args.calls):
        print(f"Call {number + 1} of {args.calls}...")
        results += asyncio.run(one_call(args.server, audio, questions))

    print()
    for r in results:
        agent = f"{r['agent']:.2f}s" if isinstance(r.get("agent"), (int, float)) else "n/a"
        print(f"  {r['wait']:5.2f}s wait  (agent {agent:>6})  {r['said']}")
        if r.get("transcribed") and r["transcribed"].lower().rstrip(".?!") != r["said"].lower().rstrip(".?!"):
            print(f"         heard as: {r['transcribed']}")

    ordinary = [r["wait"] for r in results if not r.get("emergency")]
    emergency = [r["wait"] for r in results if r.get("emergency")]
    agent = [r["agent"] for r in results if not r.get("emergency") and isinstance(r.get("agent"), (int, float))]

    print("\nCaller's last word to Sophia's first audio")
    if ordinary:
        print(f"  ordinary turns: median {statistics.median(ordinary):.2f}s, p90 {percentile(ordinary, 0.9):.2f}s, n={len(ordinary)}")
    if agent:
        print(f"    of which model and tools: median {statistics.median(agent):.2f}s, p90 {percentile(agent, 0.9):.2f}s")
    if emergency:
        print(f"  999 turns, no model: median {statistics.median(emergency):.2f}s, n={len(emergency)}")
    print(f"  includes a deliberate {voice_app.TURN_END_SILENCE_SECS}s pause to be sure the caller has finished\n")


if __name__ == "__main__":
    main()
