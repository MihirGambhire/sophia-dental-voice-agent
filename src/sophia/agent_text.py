"""
The text version of Sophia.

This is the whole agent apart from the voice. Days 4 and 5 swap the
console for speech, but the conversation loop, the tools and every rule
stay exactly as they are here. Keeping the agent testable in text first
means the voice work is only ever about audio, never about logic.

The loop is the standard one: send the conversation, if the model asks
for tools then run them, feed the results back, repeat until it produces
something to say.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from . import clock, config, prompts
from .schemas import TOOL_SCHEMAS
from .tools import SophiaTools, ToolError

# A single turn should never need more than a handful of tool calls. If it
# does, something has gone wrong and looping forever would quietly drain
# the free tier.
MAX_TOOL_ROUNDS = 6


@dataclass
class TurnRecord:
    """What happened during one turn, for the eval suite and for debugging."""

    user: str
    reply: str
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    latency_seconds: float = 0.0

    @property
    def tools_used(self) -> list[str]:
        return [name for name, _ in self.tool_calls]


class SophiaAgent:
    """One conversation with one caller."""

    def __init__(self, conn, now: datetime | None = None, client=None):
        self.conn = conn
        self.tools = SophiaTools(conn, now=now)
        self.now = now or clock.now()
        self.client = client or self._build_client()
        self.history: list[TurnRecord] = []

        self.messages: list[dict] = [
            {"role": "system", "content": prompts.system_prompt(self.now)},
            {"role": "assistant", "content": prompts.GREETING},
        ]

    # -- model access -----------------------------------------------------

    @staticmethod
    def _build_client():
        """
        Create the model client from config.

        Groq is the default because voice needs fast inference. The Gemini
        path is wired into config so it can be switched with one value,
        but it is not implemented yet and says so rather than failing in
        a confusing way later.
        """
        if config.LLM.provider == "gemini":
            raise NotImplementedError(
                "The Gemini fallback is configured but not implemented yet. "
                "Set LLM_PROVIDER=groq in your .env."
            )

        if not config.LLM.groq_api_key:
            raise RuntimeError(
                "No GROQ_API_KEY found. Copy .env.example to .env and add a key "
                "from https://console.groq.com, which is free and needs no card."
            )

        from groq import Groq

        return Groq(api_key=config.LLM.groq_api_key)

    def _complete(self):
        return self.client.chat.completions.create(
            model=config.LLM.model,
            messages=self.messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.3,  # low, because this job rewards consistency
            max_completion_tokens=500,
        )

    # -- tool dispatch ----------------------------------------------------

    def dispatch(self, name: str, arguments: dict) -> dict:
        """
        Run one tool call.

        A refused or failed tool is not an exception as far as the model
        is concerned. It comes back as a normal result with an error
        message, so the model can apologise or take a different route,
        rather than the call falling over.
        """
        method = getattr(self.tools, name, None)
        if method is None or name.startswith("_"):
            return {"error": f"There is no tool called {name}."}

        try:
            return method(**arguments)
        except ToolError as refusal:
            return {"error": str(refusal)}
        except TypeError as bad_arguments:
            return {"error": f"Those arguments do not fit that tool: {bad_arguments}"}

    # -- the conversation loop --------------------------------------------

    def say(self, text: str) -> TurnRecord:
        """Send one thing the caller said, and get Sophia's reply."""
        started = clock.now()
        self.messages.append({"role": "user", "content": text})
        record = TurnRecord(user=text, reply="")

        for _ in range(MAX_TOOL_ROUNDS):
            response = self._complete()
            message = response.choices[0].message

            if not getattr(message, "tool_calls", None):
                record.reply = message.content or ""
                self.messages.append({"role": "assistant", "content": record.reply})
                break

            # The assistant turn that requested the tools has to go back
            # into the history exactly as it came, or the model loses the
            # thread of what it asked for.
            self.messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )

            for call in message.tool_calls:
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}

                result = self.dispatch(call.function.name, arguments)
                record.tool_calls.append((call.function.name, arguments))

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.function.name,
                        "content": json.dumps(result, default=str),
                    }
                )
        else:
            record.reply = (
                "I am sorry, I am having trouble with that. "
                "Let me take a message and the team will call you back."
            )
            self.messages.append({"role": "assistant", "content": record.reply})

        record.latency_seconds = (clock.now() - started).total_seconds()
        self.history.append(record)
        return record
