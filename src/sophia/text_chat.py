"""
Talking to Sophia by typing, for testers who cannot speak aloud.

The tester types; Sophia still answers out loud, see speech.py.

A tester in a public place could not try the demo at all. Typed messages
go to exactly the same agent a voice call uses: the same safety screen,
tools, verification and call stages, and the same transcript on the page.
Only speech to text and text to speech are left out, so anything found
here is the agent's behaviour, not the audio's.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from . import db, outbound
from .agent_text import SophiaAgent, TurnRecord

# Shown if the agent fails, the same wording a voice caller hears.
FAILURE_REPLY = "I am very sorry, something has gone wrong at my end. Please ring the practice directly."

MAX_MESSAGE_LENGTH = 500


def sophia_event(turn: TurnRecord) -> dict:
    """One of Sophia's replies as the page's transcript shows it, voice or typed."""
    return {
        "type": "sophia",
        "text": turn.reply,
        "tools": turn.tools_used,
        "safety": turn.safety_level,
        "seconds": round(turn.latency_seconds, 2),
    }


class TextChat:
    """One typed conversation, with its own database connection and agent."""

    def __init__(self, db_path, reminder_id: int | None = None, agent_factory=None):
        # Shared across threads because each turn runs in a worker thread,
        # the same as a voice call; the lock keeps turns one at a time.
        self.conn = db.connect(db_path, shared_across_threads=True)
        reminder = outbound.load(self.conn, reminder_id) if reminder_id is not None else None
        if reminder is not None:
            outbound.mark_started(self.conn, reminder)
        self.agent = (agent_factory or SophiaAgent)(self.conn, reminder=reminder)
        self._lock = asyncio.Lock()
        self.closed = False
        # What Sophia last said, the only text the voice endpoint will speak.
        self.last_reply = self.agent.greeting

    @property
    def greeting(self) -> str:
        return self.agent.greeting

    async def say(self, text: str) -> dict:
        """Send what the tester typed, and return Sophia's reply as a transcript event."""
        async with self._lock:
            try:
                turn = await asyncio.to_thread(self.agent.say, text)
            except Exception:  # noqa: BLE001
                logger.exception("Sophia failed to answer a typed message")
                self.last_reply = FAILURE_REPLY
                return {"type": "sophia", "text": FAILURE_REPLY, "tools": [], "error": True}
            self.last_reply = turn.reply
            return sophia_event(turn)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.conn.close()
