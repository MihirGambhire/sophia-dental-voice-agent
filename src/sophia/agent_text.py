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

from . import clock, config, outbound, prompts, providers, safety
from .schemas import OUTBOUND_TOOL_SCHEMAS, TOOL_SCHEMAS
from .tools import SophiaTools, ToolError

# A single turn should never need more than a handful of tool calls. If it
# does, something has gone wrong and looping forever would quietly drain
# the free tier.
MAX_TOOL_ROUNDS = 6

# How many completed exchanges to keep as what was said. Older ones are
# dropped, see _compact_history for why this matters on a free tier.
KEEP_RECENT_TURNS = 12
# How many of those keep their tool calls and results too, so Sophia still
# knows the fee or the practice fact she looked up a moment ago.
KEEP_TOOL_RESULTS_TURNS = 3

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


# A reply saying what is or is not available. On a tester's call, Sophia
# said "nothing between 2 and 5 tomorrow" and "no lunchtime appointments"
# without searching at all, when the afternoons were almost empty.
_CLAIMS_AVAILABILITY = re.compile(
    r"\b(?:no|not any|don't have any|do not have any|haven't got any|nothing|none)\b"
    r"[^.?!]{0,40}\b(?:appointments?|slots?|availability|times?|openings?)\b"
    r"|\b(?:only|limited to)\b[^.?!]{0,40}\b\d{1,2}(?::\d\d)?\s?(?:a\.?m\.?|p\.?m\.?)"
    r"|\bfully booked\b"
)
_CHECKS_AVAILABILITY = {"find_available_slots", "get_urgent_slots_today", "get_patient_status", "check_cancellation"}

# A reply stating what the practice offers or is. The same call had "We do
# offer Invisalign", which is nowhere in the data, and "we are a private
# practice", when it is NHS and private, both without any lookup.
_CLAIMS_PRACTICE_FACT = re.compile(
    r"\bwe (?:do |also |don't |do not |can't |cannot |can )?(?:offer|provide|accept)\b"
    r"|\bwe(?: are|'re) (?:an? |only )?(?:nhs|private|mixed)\b"
    r"|\bour (?:address|phone number|email|website) (?:is|are)\b"
)
_CHECKS_PRACTICE_FACT = {"search_practice_info", "get_fee", "recommend_appointment_type"}

_CHECK_FIRST = "CHECK FIRST."


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
    # Stage directions a model sometimes writes, which a voice reads aloud:
    # a backup model ended a reply with "(Waiting for response)".
    value = _STAGE_DIRECTIONS.sub("", value)
    return re.sub(r"[ 	]{2,}", " ", value).strip()


_STAGE_DIRECTIONS = re.compile(
    r"\s*[\(\[](?:waiting|pause|pauses|silence|laughs?|sighs?|end of (?:call|response))[^\)\]]*[\)\]]",
    re.IGNORECASE,
)


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
    # A reply that claimed availability or a practice fact without looking
    # it up, and was sent back to the model to check before being spoken.
    unchecked_claim: str | None = None
    # Which models answered this turn, as "provider:model", when the client
    # can fall back between several. Empty for a single fixed model.
    models: list[str] = field(default_factory=list)
    # True when the caller has finished and Sophia has said goodbye, so the
    # voice layer hangs up once she has finished speaking.
    ends_call: bool = False

    @property
    def tools_used(self) -> list[str]:
        return [name for name, _ in self.tool_calls]

    def result_for(self, tool_name: str) -> dict | None:
        """
        The result of the first call to a given tool this turn.

        Kept on the record rather than read back out of the message
        history, because the history is compacted between turns and the
        older tool messages are dropped.
        """
        for name, result in self.tool_results:
            if name == tool_name:
                return result
        return None


# Whether the caller is new to the practice, from their own words. A tester
# answered "nope" to "have you been a patient before?" and the model then
# tried the existing patient check five times. A short yes or no only counts
# as an answer when Sophia has just asked that question.
_ASKED_IF_BEEN_BEFORE = re.compile(
    r"\b(been|registered|seen)\b[^?]*\b(before|with us|here)\b|\b(new|existing) patient\b[^?]*\?",
    re.IGNORECASE,
)
_SHORT_NO = re.compile(r"^\W*(no|nope|nah|never|not yet|no i haven'?t|i haven'?t)\b", re.IGNORECASE)
_SHORT_YES = re.compile(r"^\W*(yes|yeah|yep|yup|i have|i am|i'?m registered)\b", re.IGNORECASE)
_SAYS_NEW = re.compile(
    r"\b(i'?ve never been|i have never been|never been (to|here|there|a patient)|first time (calling|here|with you)"
    r"|i'?m (a )?new( patient)?\b|i am (a )?new( patient)?\b|not (yet )?a patient)",
    re.IGNORECASE,
)
_SAYS_EXISTING = re.compile(
    r"\b(i'?m|i am) (an existing|already a|a registered) patient\b|\bi'?ve been (a patient|coming)\b"
    r"|\bi have been (a patient|coming)\b",
    re.IGNORECASE,
)


# The caller's name, from their own words. A tester opened with "hi sophia
# my name is mihir gambhire" and was asked "what is your name?" a few turns
# later, while booking. "I'm" and "it's" only count after Sophia asked for a
# name, because "I'm new" and "it's my tooth" are not names.
_NAME_ALWAYS = re.compile(
    r"\b(?:my name is|my name's|name is|you'?re speaking (?:to|with))\s+(.+)"
    # "this is" only as a greeting, since "this is really painful" is not a name
    r"|^\W*(?:(?:hi|hello|hiya|good (?:morning|afternoon))\W+)?(?:sophia\W+)?this is\s+(.+)",
    re.IGNORECASE,
)
_NAME_WHEN_ASKED = re.compile(r"^\W*(?:(?:it'?s|it is|i'?m|i am)\s+)?(.+)", re.IGNORECASE)
_ASKED_FOR_NAME = re.compile(r"\b(your (full )?name|who am i speaking|who'?s calling|first name and surname)\b", re.IGNORECASE)
_NOT_A_NAME = {
    "and", "i", "im", "i'm", "calling", "from", "here", "speaking", "please", "the", "a", "an", "new",
    "patient", "so", "but", "just", "wanted", "want", "would", "need", "looking", "ringing", "sophia",
    "hi", "hello", "yes", "no", "sure", "ok", "okay", "thanks", "thank", "as", "mentioned", "above", "it",
    "is", "my", "to", "about", "for", "with", "not", "sorry", "um", "uh", "er", "well", "again",
    "it's", "its", "i've", "i'd", "i'll", "really", "very",
}


def name_the_caller_gave(said: str, last_reply: str) -> str | None:
    """A name the caller has just told Sophia, if any, title cased."""
    match = _NAME_ALWAYS.search(said)
    if match is None and _ASKED_FOR_NAME.search(last_reply or ""):
        match = _NAME_WHEN_ASKED.search(said)
    if match is None:
        return None
    # Filler before the name ("as I mentioned, it is Mihir") is skipped only
    # in an answer to "what is your name?". After "this is", a word that
    # cannot be a name means it was never an introduction.
    answering = match.re is _NAME_WHEN_ASKED
    # "mihir gambhire, I'd like a check up": the name ends at the comma. In an
    # answer, the name is in the last part: "as I mentioned above, it is Mihir".
    parts = [p for p in re.split(r"[,.!?;]", match.group(match.lastindex or 1)) if p.strip()]
    if not parts:
        return None
    words = []
    for word in re.findall(r"[A-Za-z][A-Za-z'\-]*", parts[-1] if answering else parts[0]):
        if word.lower() in _NOT_A_NAME:
            if words or not answering:
                break
            continue
        words.append(word)
        if len(words) == 3:
            break
    if not words:
        return None
    return " ".join(w[:1].upper() + w[1:].lower() for w in words)


# The end of a call. A tester said goodbye and the line stayed open until he
# pressed hang up. Both halves are needed: the caller saying they are done,
# and Sophia saying goodbye. "No thanks" to "shall I book that?" is not the
# end of a call, and Sophia saying "have a lovely day" to a caller who is
# still asking something must not cut them off.
_CALLER_DONE = re.compile(
    r"\b(?:that'?s (?:all|it|everything)|that is (?:all|it)|nothing else|no(?:pe)?,? thank(?:s| you)"
    r"|no(?:pe)?,? i'?m (?:good|fine|ok(?:ay)?)|i'?m all (?:good|set)|all good|no more"
    r"|(?:good)?bye|cheers|that will be all|that'?ll be all|i'?m done)\b",
    re.IGNORECASE,
)
_SOPHIA_FAREWELL = re.compile(
    r"\b(?:goodbye|bye(?: now| for now)?|have a (?:lovely|good|great|nice|wonderful) "
    r"(?:day|afternoon|evening|morning|weekend)|take care)\b",
    re.IGNORECASE,
)


def call_is_over(said: str, reply: str) -> bool:
    """True when the caller is done and Sophia has said goodbye."""
    return bool(_CALLER_DONE.search(said or "") and _SOPHIA_FAREWELL.search(reply or ""))


def said_new_or_existing(said: str, last_reply: str) -> bool | None:
    """True if the caller says they are new, False if they have been before."""
    if _SAYS_NEW.search(said):
        return True
    if _SAYS_EXISTING.search(said):
        return False
    if _ASKED_IF_BEEN_BEFORE.search(last_reply or ""):
        if _SHORT_NO.search(said):
            return True
        if _SHORT_YES.search(said):
            return False
    return None


class SophiaAgent:
    """One conversation with one caller."""

    def __init__(self, conn, now: datetime | None = None, client=None, reminder=None):
        self.conn = conn
        self.tools = SophiaTools(conn, now=now)
        # Lets the tools check identifiers against what the caller really
        # said, rather than trusting the model's arguments.
        self.tools.caller_heard = []
        self.now = now or clock.now()
        self.client = client or self._build_client()
        self.history: list[TurnRecord] = []
        self.last_screening: safety.Screening | None = None

        # An outbound reminder call, if Sophia placed this call. The model
        # is only ever told the patient's name; see outbound.py for why.
        self.reminder = reminder
        self.tools.reminder = reminder
        self.tool_schemas = list(TOOL_SCHEMAS)
        if reminder is not None:
            self.tools.expected_patient_id = reminder.patient_id
            self.tool_schemas += OUTBOUND_TOOL_SCHEMAS
            self.greeting = outbound.greeting(reminder)
        else:
            self.greeting = prompts.GREETING

        self.messages: list[dict] = [
            {"role": "system", "content": prompts.system_prompt(self.now)},
            {"role": "assistant", "content": self.greeting},
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
            tools=self.tool_schemas,
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
        stage = self._call_stage()
        if stage:
            lines.append(stage)

        # A tester said "my face is really swollen", correctly screened as a
        # same day problem, and Sophia then repeated "call 999 or go to A and
        # E" in the next ten replies, including one about parking. Real red
        # flags are caught by the safety screen before the model every time.
        if any("999" in turn.reply for turn in self.history if not turn.escalated_without_model):
            lines.append(
                "You have already mentioned 999 on this call. Do not repeat it unless the caller "
                "describes a new red flag. Answer what they ask."
            )

        if self.reminder is not None:
            lines.append(outbound.call_context(self.reminder))

        if self.tools.verified_patient_id in self.tools.registered_patient_ids:
            lines.append(
                f"New patient registered on this call: {self.tools.verified_name}. "
                "Never been seen, private. Do not verify or register again."
            )
        elif self.tools.verified_patient_id:
            lines.append(f"Verified caller: {self.tools.verified_name}. Do not verify again.")
        else:
            lines.append("Caller NOT verified yet.")
            if self.tools.caller_name:
                lines.append(
                    f"The caller already said their name: {self.tools.caller_name}. Never ask for it "
                    f'again. When taking details, confirm it instead: "Just to confirm, your name is '
                    f'{self.tools.caller_name}?" and pass it to the tools.'
                )

        if self.tools.offered_slots:
            lines.append("Times already offered, book one of these by its ref:")
            for index, slot in enumerate(self.tools.offered_slots, start=1):
                lines.append(f"  {index}. {slot['when']}  ref={slot['slot_ref']}")

        if self.tools.offered_urgent:
            lines.append("Urgent appointments offered today:")
            for slot in self.tools.offered_urgent:
                lines.append(f"  {slot['when']} id={slot['urgent_slot_id']}")


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

    def _call_stage(self) -> str:
        """
        Where the call is in the order every call follows, and the next step.

        Testers found conversations wandering: details asked for before
        anyone knew whether the caller was new, and new or existing never
        asked at all. The order is in the prompt, but the step the call has
        reached is worked out here from what has actually happened, so the
        model is told it every turn rather than left to remember it.
        """
        if self.reminder is not None:
            return ""  # a call Sophia placed follows its own context
        screening = self.last_screening
        if screening is not None and screening.level is safety.Level.EMERGENCY:
            return (
                "STAGE: EMERGENCY. 999 advice has been given. Do not book or take details "
                "unless they say it is not an emergency; if asked, repeat the advice."
            )
        tools = self.tools
        if tools.verified_patient_id:
            # A tester was asked "have you been a patient before?" again after
            # registering, and for an appointment date when they had none.
            kind = "new patient registered" if tools.verified_patient_id in tools.registered_patient_ids else "existing patient verified"
            upcoming = tools.conn.execute(
                "SELECT COUNT(*) FROM appointments WHERE patient_id = ? AND status = 'booked' AND start_time > ?",
                (tools.verified_patient_id, clock.to_db(self.now)),
            ).fetchone()[0]
            booked = f"{upcoming} upcoming appointment{'s' if upcoming != 1 else ''}" if upcoming else "no upcoming appointments"
            carry_on = ""
            if tools.waiting_booking is None and tools.offered_urgent and not tools.bookings_made:
                carry_on = " They asked for an urgent appointment today: book with book_urgent_slot, not a check up."
            if tools.waiting_booking is not None:
                carry_on = (
                    f" They already asked for a booking: call {tools.waiting_booking['tool']} with "
                    f"{json.dumps(tools.waiting_booking['arguments'])} now."
                )
            return (
                f"STAGE 4: {kind}, {tools.verified_name}, with {booked}. Do not ask whether they "
                "have been here before or for their details again. Only this patient's records "
                "can be discussed on this call. Carry on with what they asked for earlier in the "
                f"call rather than asking how you can help.{carry_on}"
            )
        urgency = (
            "They described a same day problem, so offer today's urgent appointments, not a routine check up. "
            if screening is not None and screening.level is safety.Level.URGENT
            else "If they want an appointment and have not said why, first ask whether it needs seeing today. "
        )
        if tools.caller_said_new is True:
            identify = (
                "The caller has said they are NEW to the practice: take name, date of birth, "
                "postcode and phone, then register_new_patient. Never use verify_patient for them. "
                "Anything else they tell you meanwhile, such as NHS or private or a preferred "
                "day or time, acknowledge briefly and use it later; answer questions they ask."
                + (
                    " This is a demo: pass the postcode and phone number exactly as they said them, "
                    "whatever the format, and never ask for a UK one."
                    if config.DEMO_RELAXED_DETAILS else ""
                )
            )
        elif tools.caller_said_new is False:
            identify = "The caller has said they have been here before: name, date of birth, postcode, verify_patient."
        else:
            identify = (
                "Then ask whether they have been a patient here before, unless they said. Existing: "
                "name, date of birth, postcode, verify_patient. New: name, date of birth, postcode, "
                "phone, register_new_patient."
            )
        return (
            "STAGE 1 to 3: caller not identified. General questions need no details. "
            f"For appointments or records: {urgency}{identify}"
        )

    def _compact_history(self) -> None:
        """
        Trim the transcript between turns, keeping the conclusions.

        Every turn resends the whole conversation, so an unbounded history
        grows quadratically in tokens. The last few exchanges are kept
        whole, tool calls and results included. Older ones keep only what
        was said, and the oldest are dropped.

        The first version dropped every tool result after its own turn, on
        the grounds that the state block carried what they established. It
        carries identity, slots and bookings, but not a fee or a practice
        fact, so a tester found Sophia forgetting within two or three
        turns what she had just looked up. That rule was written for Groq's
        8000 tokens a minute; Gemini, the model now in use, allows far more.

        This only ever runs between turns, never mid sequence, because a
        tool result message must stay immediately after the assistant
        message that asked for it or the API rejects the conversation.
        Exchanges are therefore cut at the caller's messages, never inside.
        """
        system, greeting = self.messages[0], self.messages[1]

        exchanges: list[list[dict]] = []
        for message in self.messages[2:]:
            if message["role"] == "user" or not exchanges:
                exchanges.append([])
            exchanges[-1].append(message)
        exchanges = exchanges[-KEEP_RECENT_TURNS:]

        kept: list[dict] = []
        whole_from = len(exchanges) - KEEP_TOOL_RESULTS_TURNS
        for index, exchange in enumerate(exchanges):
            if index >= whole_from:
                kept.extend(exchange)
            else:
                # Two messages per exchange: what the caller said, what Sophia said.
                kept.extend(
                    message for message in exchange
                    if message["role"] in ("user", "assistant")
                    and not message.get("tool_calls")
                    and message.get("content")
                )
        self.messages = [system, greeting, *kept]

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
        last_reply = self.history[-1].reply if self.history else self.greeting
        new_or_existing = said_new_or_existing(text, last_reply)
        if new_or_existing is not None:
            self.tools.caller_said_new = new_or_existing
        name = name_the_caller_gave(text, last_reply)
        # A fuller name replaces a first name, never the other way round.
        if name and len(name.split()) >= len((self.tools.caller_name or "").split()):
            self.tools.caller_name = name
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

        rechecked = False
        for _ in range(MAX_TOOL_ROUNDS):
            response = self._complete()
            served_by = getattr(self.client, "served_by", None)
            if isinstance(served_by, str) and served_by not in record.models:
                record.models.append(served_by)
            message = response.choices[0].message

            if not getattr(message, "tool_calls", None):
                record.reply = plain_text(message.content or "")
                unchecked = self._unchecked_claim(record)
                if unchecked and not rechecked:
                    # Sent back once, to look it up, rather than spoken.
                    rechecked = True
                    record.unchecked_claim = record.reply
                    self.messages.append({"role": "system", "content": unchecked})
                    continue
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

        # The one off instruction to look something up is not part of the
        # conversation and should not be resent on every later turn.
        self.messages = [
            m for m in self.messages
            if not (m.get("role") == "system" and str(m.get("content", "")).startswith(_CHECK_FIRST))
        ]

        if self._unchecked_claim(record):
            # Checked once and still not looked up: better silence on the
            # point than a confident guess.
            record.corrected_claim = record.reply
            record.reply = (
                "I don't want to tell you the wrong thing there, so let me check. "
                "Could you tell me again exactly what you'd like to know?"
            )
            self.messages[-1] = {"role": "assistant", "content": record.reply}

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

        record.ends_call = call_is_over(text, record.reply)
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

    def _unchecked_claim(self, record: TurnRecord) -> str | None:
        """
        An instruction to look something up, if this reply claims what it has not checked.

        Only lookups made during this turn count. A fact looked up three
        turns ago may have been about something else, and the model has
        shown it will stretch an old answer to cover a new question.
        """
        text = (record.reply or "").lower()
        used = set(record.tools_used)
        if _CLAIMS_AVAILABILITY.search(text) and not used & _CHECKS_AVAILABILITY:
            return (
                f"{_CHECK_FIRST} Your reply said what is or is not available without searching. "
                "Call find_available_slots now, with after_time and before_time for a time of "
                "day, and answer only from what it returns."
            )
        if _CLAIMS_PRACTICE_FACT.search(text) and not used & _CHECKS_PRACTICE_FACT:
            return (
                f"{_CHECK_FIRST} Your reply stated a fact about the practice without looking it "
                "up. Call search_practice_info now and answer only from what it returns. If it "
                "finds nothing, say you do not have that information and offer a message."
            )
        return None

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
