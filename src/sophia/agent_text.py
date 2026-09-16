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
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import clock, config, prompts, providers, safety
from .schemas import TOOL_SCHEMAS
from .tools import SophiaTools, ToolError

# A single turn should never need more than a handful of tool calls. If it
# does, something has gone wrong and looping forever would quietly drain
# the free tier.
MAX_TOOL_ROUNDS = 6

# How many completed exchanges to keep in full. Older ones are compacted,
# see _compact_history for why this matters so much on a free tier.
KEEP_RECENT_TURNS = 6

# Characters models reach for that either crash a Windows console or break
# the project's writing rule. Mapped to plain equivalents on the way out.
#
# This is not cosmetic. gpt-oss emitted a non breaking hyphen inside
# "check-up", which raised UnicodeEncodeError on cp1252 and killed the
# whole conversation. A voice agent would also hand these straight to the
# speech engine.
TEXT_REPLACEMENTS = {
    "‐": "-",  # hyphen
    "‑": "-",  # non breaking hyphen, the one that crashed the console
    "‒": "-",  # figure dash
    "–": "-",  # en dash
    "—": "-",  # em dash
    "―": "-",  # horizontal bar
    "−": "-",  # minus sign
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    " ": " ",  # non breaking space
}


# Positive claims only. "I can't find your record" must not match, and
# neither must describing an appointment the patient already had, which is
# why "you're booked in for Monday" is deliberately absent from the booking
# pattern: that is how Sophia describes an existing appointment.
_CLAIMS_VERIFIED = re.compile(
    r"\b("
    r"i'?ve (now )?(got you |)verified|i have (now )?(got you )?verified|"
    r"you(?:'re| are) (now )?verified|verified you|"
    r"(found|located|pulled up) your (record|details|file)|"
    r"confirmed your (identity|details)"
    r")"
)
_CLAIMS_BOOKED = re.compile(
    r"\b("
    r"i'?ve booked|i have booked|booked you in|"
    r"that'?s (all )?booked|that is (all )?booked|all booked|"
    r"your appointment is (now )?(booked|confirmed)"
    r")"
)


def plain_text(value: str) -> str:
    """
    Normalise anything Sophia is about to say.

    Keeps output printable on a Windows console, keeps the no dash writing
    rule, and keeps the speech engine from having to guess at typographic
    characters.
    """
    if not value:
        return value
    for fancy, plain in TEXT_REPLACEMENTS.items():
        value = value.replace(fancy, plain)
    return value


@dataclass
class TurnRecord:
    """What happened during one turn, for the eval suite and for debugging."""

    user: str
    reply: str
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    tool_results: list[tuple[str, dict]] = field(default_factory=list)
    latency_seconds: float = 0.0
    safety_level: str = safety.Level.ROUTINE.value
    escalated_without_model: bool = False
    # What the model tried to say, when it claimed something untrue and
    # was corrected before the caller heard it. Kept for the eval suite.
    corrected_claim: str | None = None
    # True when the medicine rule changed the reply before it was spoken.
    medication_rule_applied: bool = False

    @property
    def tools_used(self) -> list[str]:
        return [name for name, _ in self.tool_calls]

    def result_for(self, tool_name: str) -> dict | None:
        """
        The result of the first call to a given tool this turn.

        Kept on the record rather than read back out of the message
        history, because the history is compacted between turns and the
        tool messages are deliberately dropped.
        """
        for name, result in self.tool_results:
            if name == tool_name:
                return result
        return None


class SophiaAgent:
    """One conversation with one caller."""

    def __init__(self, conn, now: datetime | None = None, client=None):
        self.conn = conn
        self.tools = SophiaTools(conn, now=now)
        # Lets the tools check identifiers against what the caller really
        # said, rather than trusting the model's arguments.
        self.tools.caller_heard = []
        self.now = now or clock.now()
        self.client = client or self._build_client()
        self.history: list[TurnRecord] = []
        self.last_screening: safety.Screening | None = None

        self.messages: list[dict] = [
            {"role": "system", "content": prompts.system_prompt(self.now)},
            {"role": "assistant", "content": prompts.GREETING},
        ]

    # -- model access -----------------------------------------------------

    @staticmethod
    def _build_client():
        """
        Create the model client for whichever provider is configured.

        Groq or Gemini, switched by LLM_PROVIDER in .env. Both are
        presented through the same interface, see providers.py, so nothing
        in this file depends on which one is in use.
        """
        return providers.build_client()

    def _complete(self):
        """
        One request to the model.

        reasoning_effort is set low on purpose. The free tier is capped on
        tokens per minute rather than requests, and reasoning tokens count
        towards it. At high effort a single turn spent over 300 completion
        tokens thinking about a question whose answer comes from a database
        lookup anyway.
        """
        # The state block goes last, immediately before the model answers,
        # so it is the freshest thing in context rather than something
        # buried behind several turns of transcript.
        messages = [*self.messages, {"role": "system", "content": self._state_block()}]

        request = dict(
            model=config.LLM.model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.3,  # low, because this job rewards consistency
            max_completion_tokens=500,
        )
        if config.LLM.provider == "groq" and config.LLM.reasoning_effort:
            request["reasoning_effort"] = config.LLM.reasoning_effort
        return self.client.chat.completions.create(**request)

    def _state_block(self) -> str:
        """
        A few lines telling the model what has already happened on this call.

        This exists because of a real failure. The first version simply
        deleted old tool results to save tokens, and the model promptly
        forgot it had already verified the caller. It then re-verified on
        every single turn, and re-searched for slots it had just been
        given. Each of those is another round trip resending the whole
        prompt, so one turn could burn the entire 8000 token minute and
        the call would stall for the best part of a minute.

        Carrying forward the conclusions instead of the transcript costs
        about forty tokens and removes the need for any of that.
        """
        lines = ["CALL STATE, carried forward. Do not call tools to rediscover this."]

        if self.tools.verified_patient_id:
            lines.append(f"Verified caller: {self.tools.verified_name}. Do not verify again.")
        else:
            lines.append("Caller NOT verified yet.")

        if self.tools.offered_slots:
            lines.append("Times already offered, book one of these by its ref:")
            for index, slot in enumerate(self.tools.offered_slots, start=1):
                lines.append(f"  {index}. {slot['when']}  ref={slot['slot_ref']}")

        if self.tools.offered_urgent:
            lines.append("Urgent appointments offered today:")
            for slot in self.tools.offered_urgent:
                lines.append(f"  {slot['when']} id={slot['urgent_slot_id']}")

        if self.last_screening and self.last_screening.level is safety.Level.URGENT:
            lines.append(
                "The caller has described a same day problem. Offer today's urgent "
                "appointments, not a routine check up."
            )

        if self.tools.bookings_made:
            lines.append("Already booked on this call:")
            for booking in self.tools.bookings_made:
                lines.append(f"  {booking['when']}, {booking['type']}, {booking['fee']}")
        else:
            lines.append(
                "Nothing has been booked yet on this call. Do not tell the caller "
                "anything is booked until book_appointment has returned booked: true."
            )

        return "\n".join(lines)

    def _compact_history(self) -> None:
        """
        Trim the transcript between turns, keeping the conclusions.

        Every turn resends the whole conversation, so an unbounded history
        grows quadratically in tokens. Tool call and tool result messages
        are dropped, because the state block above carries what they
        established, and the plain text of recent exchanges is kept.

        This only ever runs between turns, never mid sequence, because a
        tool result message must stay immediately after the assistant
        message that asked for it or the API rejects the conversation.
        """
        system, greeting = self.messages[0], self.messages[1]
        body = self.messages[2:]

        plain = [
            message
            for message in body
            if message["role"] in ("user", "assistant")
            and not message.get("tool_calls")
            and message.get("content")
        ]

        # Keep two messages per exchange: what the caller said, what Sophia said.
        keep = plain[-(KEEP_RECENT_TURNS * 2) :]
        self.messages = [system, greeting, *keep]

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

        # Screen BEFORE the model sees it. A caller who cannot breathe
        # gets the 999 instruction from deterministic code, without
        # waiting on a model to decide, and without any chance of it
        # deciding otherwise.
        self.tools.caller_heard.append(text)
        screening = safety.screen(text)
        if screening.level is not safety.Level.ROUTINE:
            self.last_screening = screening

        self.messages.append({"role": "user", "content": text})
        record = TurnRecord(user=text, reply="", safety_level=screening.level.value)

        if screening.blocks_booking:
            record.reply = screening.say
            record.escalated_without_model = True
            record.latency_seconds = (clock.now() - started).total_seconds()
            self.messages.append({"role": "assistant", "content": record.reply})
            self.history.append(record)
            self._compact_history()
            return record

        for _ in range(MAX_TOOL_ROUNDS):
            response = self._complete()
            message = response.choices[0].message

            if not getattr(message, "tool_calls", None):
                record.reply = plain_text(message.content or "")
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
                record.tool_results.append((call.function.name, result))

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

        corrected = self._correct_false_claims(record.reply)
        if corrected is not None:
            record.corrected_claim = record.reply
            record.reply = corrected
            self.messages[-1] = {"role": "assistant", "content": corrected}

        enforced = safety.enforce_medication_rule(
            record.reply, caller_asked=safety.asks_about_medication(text)
        )
        if enforced is not None:
            record.medication_rule_applied = True
            record.reply = enforced
            self.messages[-1] = {"role": "assistant", "content": enforced}

        record.latency_seconds = (clock.now() - started).total_seconds()
        self.history.append(record)
        self._compact_history()
        return record

    def note_interrupted(self, reply: str) -> None:
        """
        Record that the caller cut this reply off before it finished.

        The model otherwise assumes every word it produced was heard, and
        may later refer to a fee or a time the caller never got to hear.
        Matched by content because further turns may already have been
        added after it.
        """
        marker = " [The caller interrupted before this finished, so they may not have heard all of it.]"
        for message in reversed(self.messages):
            if message.get("role") == "assistant" and message.get("content") == reply:
                message["content"] = reply + marker
                return

    def _correct_false_claims(self, reply: str) -> str | None:
        """
        Replace a reply that claims something the call state says did not happen.

        Two real failures prompted this. Earlier, Sophia told a caller
        "I have booked you in" when nothing had been written. Later, on a
        live voice call, she said "I've got you verified now" when no
        identity check had run at all, for details that matched no
        patient. Both times the system prompt already forbade exactly
        that, in capitals.

        The tools were never at risk: nothing is revealed or changed
        without a real verification, because that is enforced in Python.
        But a caller told they are verified, or booked, believes it. So
        the claim itself is checked against the call state here, and a
        false one never reaches the caller. Returns the replacement, or
        None when the reply is consistent with what actually happened.
        """
        text = (reply or "").lower()

        if self.tools.verified_patient_id is None and _CLAIMS_VERIFIED.search(text):
            return (
                "I haven't been able to confirm your details yet. Could you give me "
                "your full name, your date of birth, and your postcode, one at a time?"
            )

        # Only inside a booking conversation, meaning times have been
        # offered. Outside one, "your appointment is confirmed for Monday"
        # is Sophia truthfully describing a booking the patient already had.
        in_booking_flow = bool(self.tools.offered_slots or self.tools.offered_urgent)
        if in_booking_flow and not self.tools.bookings_made and _CLAIMS_BOOKED.search(text):
            return (
                "Before I confirm anything, I need to make that booking properly. "
                "Which of the times I offered would you like?"
            )

        return None
