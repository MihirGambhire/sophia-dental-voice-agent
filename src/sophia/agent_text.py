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
# the free tier. Raised from 6 to 8 when a caller who gave every detail in
# one sentence and asked for the earliest appointment needed verify, the
# urgent check, the appointment type and the slot search in a single turn,
# and ran out with "I am having trouble with that".
MAX_TOOL_ROUNDS = 8

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
    # "A real person from our reception team answers the phone", said with
    # nothing behind it, to a caller asking whether a human would answer.
    r"|\breal person (?:from|on|in|at|will|would)\b"
    r"|\b(?:someone|a member of (?:the |our )?team|our (?:reception|team)) (?:will|would) (?:answer|pick up)\b"
    # "They will usually try calling you again later", a habit nobody wrote down.
    r"|\b(?:they|the team|we) (?:will |would )?(?:usually|normally|typically|always) (?:try|call|ring|give)\b"
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
    # A dash between words is a pause, and read as a hyphen it ran words
    # together: "is the swelling severe-for example". Between numbers it is
    # a range.
    value = re.sub(r"(\d)\s*[–—]\s*(\d)", r"\1 to \2", value)
    value = re.sub(r"\s*[–—]\s*", ", ", value)
    for fancy, plain in TEXT_REPLACEMENTS.items():
        value = value.replace(fancy, plain)
    # Stage directions a model sometimes writes, which a voice reads aloud:
    # a backup model ended a reply with "(Waiting for response)".
    value = _STAGE_DIRECTIONS.sub("", value)
    value = re.sub(r"[ 	]{2,}", " ", value).strip()
    # A reply once began ". Our reception staff...", from an empty first
    # part the model sent before the real one. Spoken, it is a stray pause.
    return re.sub(r"^[\s.,;:]+", "", value)


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
    r"|\bi have been (a patient|coming)\b"
    # A tester said "I want to cancel my appointment" and was asked whether
    # it was urgent and whether he had been a patient before. Only someone
    # already on file has an appointment to cancel, move or check.
    r"|\b(?:cancel|reschedule|move|change|rearrange|check|confirm)\b[^.?!]{0,25}\bmy\b[^.?!]{0,15}\bappointment"
    r"|\bi (?:have|'?ve got|already have) (?:an|a) (?:appointment|booking)\b"
    # "Are there any appointments booked in my name?"
    # Typed in a hurry as "appoinments", so the t is optional.
    r"|\bappoint?ments?\b[^.?!]{0,20}\b(?:in|under) my name\b"
    r"|\b(?:do|have) i (?:got |have )?(?:an|any) appoint?ments?\b"
    r"|\bwhen(?:'s| is) my (?:next )?appointment\b",
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


# Which details a reply asks for. Used so that "could you say it again?" is
# only ever said about something the caller has been asked for: a tester
# heard "I want to be sure I have your postcode exactly right, could you
# say it again?" before anyone had asked for his postcode.
_ASKS_FOR_DETAIL = {
    "postcode": re.compile(r"\bpostcode\b", re.IGNORECASE),
    "date of birth": re.compile(r"\bdate of birth\b|\bbirthday\b", re.IGNORECASE),
    "phone number": re.compile(r"\b(?:phone|contact|mobile) number\b", re.IGNORECASE),
    "full name": re.compile(r"\byour (?:full )?name\b|\bfirst name and surname\b", re.IGNORECASE),
}


# A bare "no" only means "I'm done" as the answer to "anything else?". A
# tester answered that with "no", heard goodbye, and the line stayed open.
_ASKED_ANYTHING_ELSE = re.compile(r"\banything else\b|\bhelp (?:you )?with anything\b", re.IGNORECASE)
_SHORT_DONE = re.compile(r"^\W*(?:no|nope|nah|not really|no thanks?|nothing|that'?s it)\W*$", re.IGNORECASE)


# What the team does when a call back goes unanswered is written nowhere, and
# a tester was told "they will usually try calling you back again later"
# twice, once straight after the lookup that said Sophia cannot know. A
# lookup is not enough here, so the sentence itself is replaced.
_CALL_BACK_GUESS = re.compile(
    r"[^.?!]*\b(?:they|the team|we)\b[^.?!]{0,30}\b(?:usually|normally|typically|always|generally)\b"
    r"[^.?!]{0,40}\b(?:try|call|ring|phone)[^.?!]*[.?!]?",
    re.IGNORECASE,
)
CALL_BACK_ANSWER = (
    "I can't say exactly when they will call or what happens if they miss you, "
    "but you can always ring the practice on 01925 630221 during opening hours."
)


def replace_call_back_guess(reply: str) -> str | None:
    """The reply with a guessed call back habit replaced, or None if there is none."""
    if not _CALL_BACK_GUESS.search(reply or ""):
        return None
    # Sentence by sentence: the first guess becomes the known answer, any
    # others go. The answer itself would match the pattern, so it is added
    # after matching rather than matched against.
    sentences = re.findall(r"[^.?!]+[.?!]*", reply)
    kept, answered = [], False
    for sentence in sentences:
        if _CALL_BACK_GUESS.search(sentence):
            if not answered:
                kept.append(CALL_BACK_ANSWER)
                answered = True
            continue
        kept.append(sentence.strip())
    return " ".join(part for part in kept if part)


# Sophia telling a caller when to ring for an urgent appointment, in any of
# the ways the model words it: "ring from 8am", "ring from eight in the morning".
_GAVE_URGENT_ADVICE = re.compile(
    r"\b(?:ring|call)\b[^.?!]{0,50}\b(?:8\s?am|8\s?o'?clock|8:00|eight)\b", re.IGNORECASE
)


# A caller asking to be seen today, in their own words. A tester said "if
# possible I would like it seen today" with no symptom the safety screen
# knows, and was booked Monday's check up without being told when urgent
# appointments open. "Not urgent" must not count.
_WANTS_TODAY = re.compile(
    r"\b(?:seen|seeing|see me|see someone|be seen|appointment|come in|get in)\b[^.?!]{0,30}"
    r"\b(?:today|this morning|this afternoon|tonight|as soon as possible|asap|straight away|right away)\b"
    r"|\b(?:urgent|emergency) appointment\b|\bneeds? (?:to be seen|seeing) today\b",
    re.IGNORECASE,
)
_NOT_URGENT = re.compile(
    r"\bnot\b[^.?!]{0,15}\b(?:urgent|today)\b|\bno rush\b"
    # "Do I have an appointment today?" is about their own booking.
    r"|\b(?:do i have|have i got|is my|my)\b[^.?!]{0,20}\bappointments?\b",
    re.IGNORECASE,
)
_ASKED_IF_TODAY = re.compile(r"\b(?:seeing|seen) today\b|\burgent\b", re.IGNORECASE)


# A caller asking to book. A tester opened with "I want to book the earliest
# available appointment", gave his details, and was offered a message; he
# had to ask again. Cancelling or moving an appointment is not a booking.
_WANTS_BOOKING = re.compile(
    r"\b(?:book|make|get|arrange)\b[^.?!]{0,30}\b(?:appointment|check ?up|slot)\b"
    r"|\b(?:earliest|next|first)\b[^.?!]{0,15}\b(?:available|appointment|slot)\b",
    re.IGNORECASE,
)
_CANCEL_OR_MOVE = re.compile(r"\b(?:cancel|move|reschedule|change)\b", re.IGNORECASE)


def wants_to_be_seen_today(said: str, last_reply: str) -> bool:
    """True if the caller asks to be seen today, or says yes when asked."""
    if _NOT_URGENT.search(said or ""):
        return False
    if _WANTS_TODAY.search(said or ""):
        return True
    return bool(_ASKED_IF_TODAY.search(last_reply or "") and _SHORT_YES.search(said or ""))


# A caller actually asking about ringing or the out of hours help, when it
# may be said again. "My phone number is ..." is not one: a first version
# matched "number" there and let the whole advice through a second time.
_ASKS_ABOUT_ADVICE = re.compile(
    r"\bwhat(?:'s| is) (?:the|that) number\b|\bwhich number\b|\bwhen (?:can|should|do|could) i (?:ring|call)\b"
    r"|\bwhat time (?:can|should|do|could) i\b|\bout[- ]of[- ]hours\b|\b111\b"
    r"|\b(?:say|repeat) (?:that|it) again\b|\bsorry,? what\b",
    re.IGNORECASE,
)


def _without_advice(reply: str) -> str:
    """The reply without sentences repeating when to ring or the out of hours help."""
    kept = []
    for sentence in re.findall(r"[^.?!]+[.?!]*", reply or ""):
        repeats = (
            _GAVE_URGENT_ADVICE.search(sentence)
            or config.OUT_OF_HOURS_DENTAL_NUMBER in sentence
            or re.search(r"\bno urgent (?:slots|appointments)\b", sentence, re.IGNORECASE)
        )
        if not repeats and sentence.strip():
            kept.append(sentence.strip())
    return " ".join(kept)


# The model's own working, printed instead of a reply. On one replay Sophia
# "said": "Caller situation: severe tooth pain ... get_urgent_slots_today
# returned no slots (released: false) ... What should I say to Mihir? 1. 2.
# 3." A voice would have read all of it out.
_LEAKED_NOTES = re.compile(
    r"\b(?:verify_patient|register_new_patient|get_patient_status|recommend_appointment_type"
    r"|find_available_slots|get_urgent_slots_today|book_appointment|book_urgent_slot"
    r"|check_cancellation|cancel_appointment|reschedule_appointment|search_practice_info"
    r"|get_fee|take_message|record_call_outcome)\b"
    r"|\bCALL STATE\b|\bSTAGE \d|\bcaller situation\b|\bwhat should i say\b"
    r"|\b(?:released|verified|booked|found): (?:true|false)\b|\bAI assistant on the phone for\b",
    re.IGNORECASE,
)


def leaked_notes(reply: str) -> bool:
    """True if a reply contains the model's own notes rather than words for the caller."""
    return bool(_LEAKED_NOTES.search(reply or ""))


# Yes to "would you like me to book it?", and not a yes with a but.
_YES_TO_OFFER = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|go ahead|book it|that'?s fine|that works|perfect|great)\b"
    r"(?![^.?!]*\b(?:but|different|another|other|later|earlier|afternoon)\b)",
    re.IGNORECASE,
)


def _before_last_question(reply: str, sentence: str) -> str:
    """
    Put a sentence in before the reply's closing question, or at the end.

    When the reply is asking the caller to repeat something, the sentence
    goes first instead: "Sorry, I did not catch that number. [advice]
    Could you say it again?" left "it" pointing at the wrong thing.
    """
    parts = re.findall(r"[^.?!]+[.?!]*", reply or "")
    if (reply or "").lstrip().lower().startswith("sorry") or (parts and " again" in parts[-1].lower()):
        return f"{sentence} {(reply or '').strip()}".strip()
    if parts and parts[-1].strip().endswith("?"):
        return " ".join([*(p.strip() for p in parts[:-1]), sentence, parts[-1].strip()]).strip()
    return f"{(reply or '').strip()} {sentence}".strip()


def call_is_over(said: str, reply: str, last_reply: str = "") -> bool:
    """True when the caller is done and Sophia has said goodbye, or she has ended it."""
    # An outright "goodbye" closing her reply ends the call on its own. A
    # tester gave his number for a message, Sophia thanked him and said
    # "Goodbye", and the line stayed open because he had not said he was
    # done. Once she has said goodbye, an open line only confuses.
    if _ENDS_WITH_GOODBYE.search((reply or "").strip()):
        return True
    done = bool(_CALLER_DONE.search(said or "")) or bool(
        _SHORT_DONE.match(said or "") and _ASKED_ANYTHING_ELSE.search(last_reply or "")
    )
    return done and bool(_SOPHIA_FAREWELL.search(reply or ""))


# "Goodbye" in the last sentence, which is not a question. "Before we say
# goodbye, could I take your number?" does not end a call.
_ENDS_WITH_GOODBYE = re.compile(r"\b(?:goodbye|bye(?: now| for now)?)\b[^.?!]*[.!]?\s*$", re.IGNORECASE)


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

    def _note_details_asked_for(self, reply: str) -> None:
        """Remember which details Sophia has asked the caller for."""
        for label, pattern in _ASKS_FOR_DETAIL.items():
            if pattern.search(reply or ""):
                self.tools.details_asked_for.add(label)

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
            if (tools.caller_has_same_day_problem()
                    and not tools.urgent_bookable_now() and not tools.urgent_advice_given):
                carry_on = f' They have a same day problem and urgent appointments cannot be booked now. Tell them, once: "{tools.urgent_advice()}"'
            if tools.booking_requested and not tools.bookings_made and tools.waiting_booking is None:
                carry_on += (
                    " They asked to book the earliest available appointment: call recommend_appointment_type "
                    "and find_available_slots now and offer the earliest. Do not offer a message instead."
                )
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
        same_day = (screening is not None and screening.level is safety.Level.URGENT) or tools.urgent_requested
        if same_day:
            if tools.urgent_bookable_now():
                urgency = "They described a same day problem, so offer today's urgent appointments, not a routine check up. "
            elif tools.urgent_advice_given:
                urgency = (
                    "They have a same day problem. You have already told them when to ring for an urgent "
                    "appointment and the out of hours number: do not say it again unless they ask. "
                )
            else:
                urgency = (
                    "They described a same day problem, but urgent appointments cannot be booked right now. "
                    f'Tell them, once: "{tools.urgent_advice()}" '
                )
        else:
            urgency = (
                "If they want to book an appointment and have not said why, first ask whether it needs "
                "seeing today. Not for cancelling, moving or checking one. "
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
        # Held until this turn is over. The name is in the caller's latest
        # words, so the model has it; asking "just to confirm, your name is
        # Mihir Gambhire?" straight after he said it sounded like she had not
        # listened. Confirming is for a name given earlier in the call.
        if wants_to_be_seen_today(text, last_reply):
            self.tools.urgent_requested = True
        if _WANTS_BOOKING.search(text) and not _CANCEL_OR_MOVE.search(text):
            self.tools.booking_requested = True
        advice_given_before = self.tools.urgent_advice_given
        name = name_the_caller_gave(text, last_reply)
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
        leak_rechecked = False
        for _ in range(MAX_TOOL_ROUNDS):
            response = self._complete()
            served_by = getattr(self.client, "served_by", None)
            if isinstance(served_by, str) and served_by not in record.models:
                record.models.append(served_by)
            message = response.choices[0].message

            if not getattr(message, "tool_calls", None):
                record.reply = plain_text(message.content or "")
                if leaked_notes(record.reply):
                    if not leak_rechecked:
                        # Its own working, not a reply: sent back once.
                        leak_rechecked = True
                        self.messages.append({"role": "system", "content": (
                            f"{_CHECK_FIRST} Your last reply contained your own notes and instructions. "
                            "Reply only with the words Sophia says to the caller, nothing else."
                        )})
                        continue
                    # Notes again: no further retries, it is dealt with below.
                    self.messages.append({"role": "assistant", "content": record.reply})
                    break
                unchecked = self._unchecked_claim(record)
                if unchecked and not rechecked:
                    # Sent back once, to look it up, rather than spoken.
                    rechecked = True
                    record.unchecked_claim = record.reply
                    self.messages.append({"role": "system", "content": unchecked})
                    continue
                # Its own once per call, so an earlier look it up retry in
                # the same turn does not use it up.
                missed = self._missed_booking(record)
                if missed:
                    self.messages.append({"role": "system", "content": missed})
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

        if leaked_notes(record.reply):
            # Still notes after being sent back. None of it is spoken; the
            # booking offer below, or a plain request to repeat, takes over.
            record.corrected_claim = record.reply
            record.reply = ""
            self.messages[-1] = {"role": "assistant", "content": "Sorry, could you say that again?"}

        if self._unchecked_claim(record):
            # Checked once and still not looked up: better silence on the
            # point than a confident guess. A guessed call back habit has a
            # known answer, which beats asking the caller to repeat themselves.
            record.corrected_claim = record.reply
            record.reply = (
                replace_call_back_guess(record.reply)
                or self._without_unchecked_sentences(record)
                or (
                    "I don't want to tell you the wrong thing there, so let me check. "
                    "Could you tell me again exactly what you'd like to know?"
                )
            )
            self.messages[-1] = {"role": "assistant", "content": record.reply}

        corrected = self._correct_false_claims(record.reply)
        if corrected is None:
            corrected = replace_call_back_guess(record.reply)
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

        record.ends_call = call_is_over(text, record.reply, last_reply)
        # A fuller name replaces a first name, never the other way round.
        if name and len(name.split()) >= len((self.tools.caller_name or "").split()):
            self.tools.caller_name = name
        self._note_details_asked_for(record.reply)
        # A yes to the slot the code offered is kept, even if the model
        # answered it with something else. A replay said "sure" to "would
        # you like me to book it?" and was offered a message instead.
        kept = self._keep_accepted_offer(text, last_reply, record)
        if kept:
            record.reply = kept
            self.messages[-1] = {"role": "assistant", "content": record.reply}
        # The booking the caller asked for, offered by the code when the
        # model would not. See _offer_earliest.
        offer = self._offer_earliest(record)
        if offer:
            record.reply = offer
            self.messages[-1] = {"role": "assistant", "content": record.reply}
        if not (record.reply or "").strip():
            record.reply = "Sorry, could you say that again?"
            self.messages[-1] = {"role": "assistant", "content": record.reply}
        # Decided on the words actually spoken: a goodbye replaced by a
        # booking or an offer must not hang up on the caller.
        record.ends_call = call_is_over(text, record.reply, last_reply)
        # Said once, it stays said. A tester heard the whole advice twice in
        # a row, the second time in the model's own words, despite being told
        # not to repeat it. Unless the caller asked about it, it comes out.
        if advice_given_before and not _ASKS_ABOUT_ADVICE.search(text):
            trimmed = _without_advice(record.reply)
            if trimmed and trimmed != record.reply:
                record.reply = trimmed
                self.messages[-1] = {"role": "assistant", "content": record.reply}
        # The lookup said when urgent appointments open and the reply left it
        # out: a tester heard "we don't have any urgent appointments right
        # now" and nothing about ringing at 8am on Monday. Said by the code.
        if (self.tools.caller_has_same_day_problem() and not self.tools.urgent_bookable_now()
                and not self.tools.urgent_advice_given and not _GAVE_URGENT_ADVICE.search(record.reply or "")):
            record.reply = _before_last_question(record.reply, self.tools.urgent_advice())
            self.messages[-1] = {"role": "assistant", "content": record.reply}
        # Said once is enough: a replay had the whole urgent advice twice.
        if _GAVE_URGENT_ADVICE.search(record.reply or ""):
            self.tools.urgent_advice_given = True
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

    def _without_unchecked_sentences(self, record: TurnRecord) -> str | None:
        """
        The reply with only its unchecked claims removed, if a question is left.

        A tester gave his name and heard "I don't want to tell you the wrong
        thing there, could you tell me again exactly what you'd like to
        know?", because one sentence of the reply guessed at availability.
        The rest of it, thanking him and asking for his date of birth, was
        thrown away with it. Only the guess needs to go.
        """
        used = set(record.tools_used)
        kept = []
        for sentence in re.findall(r"[^.?!]+[.?!]*", record.reply or ""):
            lowered = sentence.lower()
            guessed = (
                (_CLAIMS_AVAILABILITY.search(lowered) and not used & _CHECKS_AVAILABILITY)
                or (_CLAIMS_PRACTICE_FACT.search(lowered) and not used & _CHECKS_PRACTICE_FACT)
            )
            if not guessed and sentence.strip():
                kept.append(sentence.strip())
        remaining = " ".join(kept)
        # Only worth saying if it still moves the call on.
        return remaining if "?" in remaining else None

    def _keep_accepted_offer(self, said: str, last_reply: str, record: TurnRecord) -> str | None:
        """Book the slot the code offered, if the caller said yes and the model did not book it."""
        tools = self.tools
        offer = tools.code_offer
        if offer is None or tools.bookings_made or "book" in " ".join(record.tools_used):
            return None
        if "Would you like me to book it?" not in (last_reply or "") or not _YES_TO_OFFER.search(said or ""):
            return None
        tools.code_offer = None
        try:
            result = tools.book_appointment(offer["slot_ref"], offer["appointment_type"])
        except ToolError:
            return None
        record.tool_calls.append(("book_appointment", dict(offer)))
        record.tool_results.append(("book_appointment", result))
        if not result.get("booked"):
            return None
        return f"{result['say']} Is there anything else I can help you with today?"

    def _offer_earliest(self, record: TurnRecord) -> str | None:
        """
        The reply with the earliest routine slot offered, when the caller asked to book and nothing was offered.

        A tester in pain on a Saturday opened with "I want to book the
        earliest available appointment", gave every detail, and was asked
        "is there anything else I can help you with?". Told in the call
        state, then sent back once, the model still offered nothing, on
        replay after replay: once urgent care was ruled out it lost the
        booking. So the code offers it, with the same rules the tools use,
        and the caller's yes books it as usual.
        """
        tools = self.tools
        if record.ends_call:
            return None  # a goodbye is not the place to start a booking
        if not (tools.booking_requested and tools.verified_patient_id) or tools.bookings_made:
            return None
        if tools.offered_slots or tools.offered_urgent or tools.waiting_booking is not None:
            return None
        if tools.urgent_bookable_now() or re.search(r"\bbook\b[^.?!]*\?", record.reply or "", re.IGNORECASE):
            return None
        try:
            kind = tools.recommend_appointment_type("check_up")
            if not kind.get("appointment_type"):
                return None
            found = tools.find_available_slots(kind["appointment_type"])
        except ToolError:
            return None
        if not found.get("slots"):
            return None
        slot = found["slots"][0]
        tools.code_offer = {"slot_ref": slot["slot_ref"], "appointment_type": kind["appointment_type"]}
        record.tool_calls += [("recommend_appointment_type", {"purpose": "check_up"}),
                              ("find_available_slots", {"appointment_type": kind["appointment_type"]})]
        record.tool_results += [("recommend_appointment_type", kind), ("find_available_slots", found)]
        # Keep what was said, less a closing "anything else?", which the
        # offer replaces.
        kept = [s.strip() for s in re.findall(r"[^.?!]+[.?!]*", record.reply or "")
                if s.strip() and not re.search(r"\banything else\b", s, re.IGNORECASE)]
        offer = (
            f"The earliest routine appointment I have is {slot['when']}, for a "
            f"{kind['name'].lower()} at {kind['fee']}. Would you like me to book it?"
        )
        return " ".join([*kept, offer])

    def _missed_booking(self, record: TurnRecord) -> str | None:
        """
        An instruction to offer the booking the caller asked for, if the reply offers a message instead.

        A tester asked for the earliest appointment in his first sentence,
        gave his details, and was offered a message; he had to ask again.
        The call state already said what he wanted, and the model still
        chose a message, so the reply is sent back once.
        """
        tools = self.tools
        if not (tools.booking_requested and tools.verified_patient_id) or tools.bookings_made:
            return None
        if tools.offered_slots or tools.offered_urgent or tools.waiting_booking is not None:
            return None
        if set(record.tools_used) & {"find_available_slots", "book_appointment", "book_urgent_slot"}:
            return None
        # Once per call. A replay's reply offered nothing at all, just
        # "anything else I can help with?", so any reply counts, not only one
        # offering a message; and a caller who then changes their mind is
        # not pushed twice.
        if tools.booking_nudged:
            return None
        # Already offering to book, which is what was wanted: a replay's good
        # reply ("would you like me to book a routine appointment for when we
        # reopen?") was sent back anyway, and the retry came back worse.
        if re.search(r"\bbook\b[^.?!]*\?", record.reply or "", re.IGNORECASE):
            return None
        tools.booking_nudged = True
        return (
            f"{_CHECK_FIRST} The caller asked to book the earliest available appointment and is now "
            "identified. Do not offer a message or ask what else they need. Call "
            "recommend_appointment_type with purpose check_up, then find_available_slots, and offer "
            "the earliest routine slot now. Urgent advice already given is not a reason to skip it."
        )

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
