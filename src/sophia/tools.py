"""
The tools Sophia can call, and the guard rails around them.

This is the only way the language model can touch the practice's data.
Two principles run through the whole file:

  1. **Nothing is revealed or changed before the caller is verified.**
     Verification state lives here, on the session, not in the
     conversation. A model cannot talk its way past it, because the
     check is an `if` statement in Python, not an instruction in a
     prompt.

  2. **Every tool re-validates its own inputs.** A slot the model offers
     might have been taken since. A type code it invents might not
     exist. An appointment it names might belong to someone else. Each
     of those is checked again at the moment of writing, not trusted
     from earlier in the conversation.

Every method returns a plain dictionary, ready to be handed back to the
model as a tool result.
"""

from __future__ import annotations

import json

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import clock, config, dashboard, knowledge, policies, safety
from .policies import PatientFacts

# Availability is searched on this grid. Ten minutes keeps the number of
# candidate slots manageable while still offering times that sound
# natural to say out loud, like twenty past nine.
SLOT_GRID_MINUTES = 10

# Which real clinician roles can deliver each appointment type's role.
ROLE_GROUPS: dict[str, tuple[str, ...]] = {
    "dentist": ("principal_dentist", "associate_dentist"),
    "endodontist": ("endodontist",),
    "hygiene_therapist": ("hygiene_therapist",),
    "dentist_or_therapist": (
        "principal_dentist",
        "associate_dentist",
        "hygiene_therapist",
    ),
}


class ToolError(Exception):
    """Raised when a tool is asked to do something it must not do."""


@dataclass
class Slot:
    """One bookable moment. Not stored, computed on demand."""

    clinician_id: int
    clinician_name: str
    start: datetime
    duration_min: int

    @property
    def ref(self) -> str:
        """
        A compact reference the model can pass back to book_appointment.

        Encoding the clinician and the exact start time means a booking
        request carries everything needed to re-check availability, and
        nothing has to be held in server side session state.
        """
        return f"{self.clinician_id}@{self.start.strftime('%Y-%m-%dT%H:%M')}"

    def spoken(self) -> str:
        return f"{clock.spoken_datetime(self.start)} with {self.clinician_name}"

    def as_dict(self) -> dict:
        return {
            "slot_ref": self.ref,
            "when": self.spoken(),
            "date": self.start.strftime(clock.DB_DATE_FORMAT),
            "time": self.start.strftime("%H:%M"),
            "clinician": self.clinician_name,
        }


def _normalise_name(value: str) -> str:
    """Collapse whitespace and case so "  mary  smith " matches "Mary Smith"."""
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _normalise_postcode(value: str) -> str:
    """Postcodes are compared without spaces or case. "wa1 1qn" matches "WA11QN"."""
    return re.sub(r"\s+", "", (value or "")).casefold()


def _format_postcode(compact: str) -> str:
    """WA11QN becomes WA1 1QN: the inward part is always the last three characters."""
    compact = compact.upper()
    if not _UK_POSTCODE.match(compact):
        return compact  # a made up demo postcode, kept as said
    return f"{compact[:-3]} {compact[-3:]}"


def _normalise_dob(value: str) -> str | None:
    """
    Accept the several ways a caller might give a date of birth.

    A voice pipeline will transcribe "the third of March 1985" in more
    than one shape, so we accept the common written forms and normalise
    to the stored format. Anything unrecognised returns None, which the
    caller treats as a failed match rather than guessing.
    """
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime(clock.DB_DATE_FORMAT)
        except ValueError:
            continue

    # Spoken forms. Deepgram rendered "twelfth of March, nineteen fifty
    # eight" as "March 12. 1958.", with the month first and a full stop in
    # the middle, which none of the formats above accept. Ordinals, "of"
    # and punctuation are stripped before trying either order.
    spoken = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", value.lower())
    spoken = re.sub(r"\bof\b|[.,]", " ", spoken)
    spoken = re.sub(r"\s+", " ", spoken).strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(spoken, fmt).strftime(clock.DB_DATE_FORMAT)
        except ValueError:
            continue
    return None


_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")


def _dob_readings(dob: str, normalised: str, utterances: list[str] | None) -> set[str]:
    """
    Every date of birth the caller could have meant.

    Speech to text wrote "sixth of December nineteen forty nine" as
    "12/06/1949", month first, despite the UK English model. Read the UK way
    that is 12 June, and a real patient failed verification over voice. A
    numeric date with both parts twelve or under cannot be read one way with
    certainty, so when the caller's own words contain one, and it is the
    date the model passed, both readings are tried. Name and postcode must
    still match, and to exactly one record, so this adds no way in for
    someone who does not already know the patient's details.
    """
    readings = {normalised}
    for text in [dob, *(utterances or [])]:
        for first, second, year in _NUMERIC_DATE.findall(text or ""):
            a, b = int(first), int(second)
            if a > 12 or b > 12 or a == b:
                continue
            both = set()
            for day, month in ((a, b), (b, a)):
                try:
                    both.add(date(int(year), month, day).strftime(clock.DB_DATE_FORMAT))
                except ValueError:
                    pass
            if normalised in both:
                readings |= both
    return readings


# What a model passes when it does not actually have a value yet.
_PLACEHOLDERS = {"", "unknown", "none", "n/a", "na", "not provided", "not given", "null", "?"}

# A deliberately loose UK postcode shape, checked with spaces removed, so
# "W A 1 2 N F" from speech to text still passes.
_UK_POSTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$")

# What counts as a postcode for a new patient when demo details are relaxed.
_DEMO_POSTCODE = re.compile(r"^[A-Z0-9]{2,8}$")

# How alike a spoken name must be to the record once date of birth and
# postcode already match exactly. Speech to text heard "Hollis" as
# "Hollies", which is 0.97 alike; an unrelated name scores far lower.
_NAME_SIMILARITY = 0.85


def _name_similarity(said: str, recorded: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, _normalise_name(said), _normalise_name(recorded)).ratio()


def _parse_hhmm(value: str | None):
    """
    Read a time of day like "14:00", "2pm" or "2:30 pm". None if absent or unreadable.

    Unreadable is treated as no filter rather than an error, because the
    worst outcome of ignoring it is offering times outside the window,
    which Sophia will read out and the caller can decline.
    """
    text = (value or "").strip().lower().replace(".", "")
    if not text:
        return None
    for fmt in ("%H:%M", "%H", "%I%p", "%I %p", "%I:%M%p", "%I:%M %p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance, small inputs only."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (char_a != char_b),
            ))
        previous = current
    return previous[-1]


_NUMBER_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def _spoken_characters(utterances: list[str]) -> str:
    """
    Everything the caller said, reduced to the characters a postcode uses.

    "W one two n f" becomes "W12NF", so a postcode read out letter by
    letter or digit by word can be found in it.
    """
    words = re.findall(r"[a-z0-9]+", " ".join(utterances).lower())
    return "".join(_NUMBER_WORDS.get(word, word) for word in words).upper()


def _postcode_was_said(postcode: str, utterances: list[str]) -> bool:
    """
    True if the caller actually said this postcode, allowing one character
    of speech to text error.

    The model was caught passing a postcode the caller had never given: on
    one run "UNKNOWN", on another "placeholder", and on a third WA1 1LZ, a
    plausible postcode it simply made up. The last one produced "I cannot
    find a record" for a real patient who had not reached that question
    yet. Checking against the caller's own words catches all three.
    """
    target = _normalise_postcode(postcode).upper()
    heard = _spoken_characters(utterances)
    if not target or not heard:
        return False
    if target in heard:
        return True
    for size in (len(target) - 1, len(target), len(target) + 1):
        for start in range(0, max(0, len(heard) - size) + 1):
            if _edit_distance(target, heard[start:start + size]) <= 1:
                return True
    return False


_FUNDING_WORDS = {
    "private": re.compile(r"\bprivate(ly)?\b|\bpay (for it )?myself\b", re.IGNORECASE),
    "nhs": re.compile(r"\bn\.?\s?h\.?\s?s\b", re.IGNORECASE),
}


def _funding_was_asked_for(funding: str, utterances: list[str]) -> bool:
    """True if the caller said anything asking for this kind of funding."""
    pattern = _FUNDING_WORDS.get(funding)
    return bool(pattern) and any(pattern.search(said or "") for said in utterances)


def _latest_funding_request(utterances: list[str] | None) -> str | None:
    """
    NHS or private, whichever the caller asked for most recently, if either.

    Used for urgent bookings, where the fee depends on it and there is no
    separate step to choose. A mention within a couple of words after "not",
    "no" or "n't" does not count, so "not private, NHS please" means NHS and
    "I haven't got an NHS dentist" asks for nothing.
    """
    if not utterances:
        return None
    text = " | ".join(utterances)
    found: list[tuple[int, str]] = []
    for funding, pattern in _FUNDING_WORDS.items():
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 24):match.start()].lower()
            # "not private", "I don't want NHS", "I haven't got an NHS dentist"
            if re.search(r"(?:\bnot|\bno|n't)(?:\s+[a-z]+){0,2}\s+$", before):
                continue
            found.append((match.start(), funding))
    return max(found)[1] if found else None


_UK_PHONE = re.compile(r"^0\d{10}$")

# New registrations for a child go to the team, see register_new_patient.
_MINIMUM_AGE_TO_REGISTER = 16
_MAXIMUM_PLAUSIBLE_AGE = 120

# One patient per call. A tester's call registered one person, was offered
# a check on "Sarah Smith" for her husband, then began verifying "Margaret
# Wilson". Anyone with someone else's details could have checked their
# appointments. Anyone else becomes a message for the team.
ONE_PATIENT_PER_CALL = (
    "I can only help with one person's records on a call, and that's you on this one. "
    "If it's about someone else, I can take a message and the team will call them."
)


def _normalise_phone(value: str, relaxed: bool = False) -> str | None:
    """
    A UK number as eleven digits starting with 0, or None if it is not one.

    Relaxed, for demo testers giving made up numbers, any six to fifteen
    digits will do.
    """
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("44") and len(digits) == 12:
        digits = "0" + digits[2:]
    if relaxed:
        return digits if 6 <= len(digits) <= 15 else None
    return digits if _UK_PHONE.match(digits) else None


def _spoken_digits(utterances: list[str]) -> str:
    """
    Every digit the caller said, in order, however they said it.

    People read phone numbers as "oh seven seven double oh", so number
    words, "oh", and "double" or "triple" are all turned into digits.
    """
    words = re.findall(r"[a-z]+|\d+", " ".join(utterances).lower())
    digits: list[str] = []
    repeat = 1
    for word in words:
        if word in ("double", "triple"):
            repeat = 2 if word == "double" else 3
            continue
        if word.isdigit():
            digits.append(word[0] * repeat + word[1:])
        elif word in _NUMBER_WORDS:
            digits.append(_NUMBER_WORDS[word] * repeat)
        repeat = 1
    return "".join(digits)


def _phone_was_said(phone: str, utterances: list[str]) -> bool:
    """True if the caller said this number, with or without the +44 or leading 0."""
    core = phone[1:] if phone.startswith("0") else phone
    return core in _spoken_digits(utterances)


def _letters(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if ch.isalpha())


def _fuzzy_contains(haystack: str, needle: str, max_errors: int) -> bool:
    """True if needle appears in haystack with at most max_errors edits."""
    if needle in haystack:
        return True
    if max_errors == 0:
        return False
    size = len(needle)
    for width in (size - 1, size, size + 1):
        if width <= 0:
            continue
        for start in range(0, max(0, len(haystack) - width) + 1):
            if _edit_distance(needle, haystack[start:start + width]) <= max_errors:
                return True
    return False


def _name_was_said(full_name: str, utterances: list[str]) -> bool:
    """
    True if every part of the name was said, allowing speech to text spellings.

    Checked for the same reason as postcodes: a model with nothing to go on
    fills the gap with something plausible, and a record created under an
    invented name is worse than no record.

    Matched against everything the caller said run together as letters, so
    a name spelled out letter by letter is found, and so is one speech to
    text split or merged: a real call heard "Eileen Ashworth" as "Isai Lee
    Nashworth", which contains "ashworth" whole and "aileen", one letter
    from "eileen". Longer name parts tolerate more errors.
    """
    heard = _letters(" ".join(utterances))
    for part in re.split(r"[\s'-]+", full_name or ""):
        part = _letters(part)
        if len(part) < 2:
            continue
        allowed = 0 if len(part) < 4 else 1 if len(part) < 8 else 2
        if not _fuzzy_contains(heard, part, allowed):
            return False
    return True


# Said before a name, never part of it. Stored with the name they would make
# a later duplicate check miss "Priya Sharma" against "Mrs Priya Sharma".
_TITLES = {"mr", "mrs", "ms", "miss", "mx", "dr", "doctor", "sir", "madam"}


def _clean_name(value: str) -> str:
    """
    "  mrs mary-jane o'neil " becomes "Mary-Jane O'Neil".

    Only a name given all in one case is recapitalised, so "McDonald" as
    said is left alone.
    """
    parts = re.sub(r"\s+", " ", (value or "").strip()).split(" ")
    while len(parts) > 1 and parts[0].casefold().strip(".") in _TITLES:
        parts.pop(0)

    def capitalise(word: str) -> str:
        if word != word.lower() and word != word.upper():
            return word
        return re.sub(r"(^|[-'])(\w)", lambda m: m.group(1) + m.group(2).upper(), word.lower())

    return " ".join(capitalise(part) for part in parts if part)


def _name_is_plausible(name: str) -> bool:
    """Letters, spaces, hyphens and apostrophes, and not absurdly long."""
    return 0 < len(name) <= 60 and all(ch.isalpha() or ch in " '-." for ch in name)


class SophiaTools:
    """
    One conversation's worth of tool access.

    A new instance per call, so verification never leaks between callers.
    """

    def __init__(self, conn, now: datetime | None = None):
        self.conn = conn
        self._fixed_now = now
        self.verified_patient_id: int | None = None

        # What has happened so far on this call. The agent turns this into
        # a short state block for the model, so it does not have to
        # rediscover by calling the same tools again every turn.
        self.verified_name: str | None = None
        # What the caller has actually said on this call, filled in by the
        # agent. None means the tools are being used directly, by a test or
        # a script, where there is no caller whose words to check against.
        self.caller_heard: list[str] | None = None
        # Records created on this call for callers new to the practice.
        self.registered_patient_ids: list[int] = []
        # Set on an outbound call: the only patient who may be verified, and
        # the queued reminder the call belongs to. See outbound.py.
        self.expected_patient_id: int | None = None
        self.reminder = None
        self.offered_slots: list[dict] = []
        self.offered_urgent: list[dict] = []
        self.bookings_made: list[dict] = []
        # The message taken on this call, if any. Later additions join it.
        self.message_id: int | None = None
        # Cancellations made on this call, for the call summary.
        self.cancellations_made: list[str] = []
        # Whether Sophia has already told a caller with a same day problem
        # when to ring for an urgent appointment. Set by the agent.
        self.urgent_advice_given = False
        # True once today's urgent appointments have been looked up: the
        # caller asked to be seen today, whatever words they used for why.
        self.urgent_requested = False
        # True once the caller has asked to book, read from their words by
        # the agent, so the request survives the identity questions.
        self.booking_requested = False
        # Whether the reply was already sent back once to offer that booking.
        self.booking_nudged = False
        # The slot the code itself offered, so a yes to it can be kept.
        self.code_offer: dict | None = None
        # Whether an unclear answer on a reminder call has been checked once.
        self._wrong_person_checked = False
        # True once the caller has said they are new to the practice, False
        # once they have said they have been before, None until either.
        # Set by the agent from the caller's own words.
        self.caller_said_new: bool | None = None
        # The name the caller gave, if they have, set by the agent.
        self.caller_name: str | None = None
        # Details Sophia has asked this caller for, set by the agent.
        self.details_asked_for: set[str] = set()
        # A booking the model tried before the caller was identified, so it
        # can be finished straight after, instead of "how can I help?".
        self.waiting_booking: dict | None = None

    # -- helpers ----------------------------------------------------------

    @property
    def now(self) -> datetime:
        """Current time, or a pinned time when a test needs one."""
        return self._fixed_now or clock.now()

    def _require_verified(self, patient_id: int | None = None) -> int:
        """
        Refuse to continue unless the caller has been verified.

        This is the single choke point that protects every piece of
        patient data. It is intentionally boring.
        """
        if self.verified_patient_id is None:
            raise ToolError(
                "The caller has not been verified yet. Ask for their full name, "
                "date of birth and postcode, then call verify_patient."
            )
        if patient_id is not None and patient_id != self.verified_patient_id:
            raise ToolError(
                "That record belongs to a different patient than the one verified."
            )
        return self.verified_patient_id

    def _patient_row(self, patient_id: int):
        row = self.conn.execute(
            "SELECT * FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if row is None:
            raise ToolError("No such patient.")
        return row

    def _facts(self, patient_id: int) -> PatientFacts:
        return PatientFacts.from_row(self._patient_row(patient_id))

    def _events(self, patient_id: int) -> list[tuple[str, date]]:
        return [
            (
                row["event_type"],
                datetime.strptime(row["event_date"], clock.DB_DATE_FORMAT).date(),
            )
            for row in self.conn.execute(
                "SELECT event_type, event_date FROM attendance_events WHERE patient_id = ?",
                (patient_id,),
            )
        ]

    def _type_row(self, type_code: str):
        row = self.conn.execute(
            "SELECT * FROM appointment_types WHERE code = ?", (type_code,)
        ).fetchone()
        if row is None:
            # The model guessed "PV-EXAM" for a fee question and got nothing
            # back to correct itself with, so the caller heard no price.
            known = ", ".join(
                f"{r['code']} ({r['name']})"
                for r in self.conn.execute("SELECT code, name FROM appointment_types ORDER BY code")
            )
            raise ToolError(f"There is no appointment type called {type_code}. The types are: {known}.")
        return row

    # -- 1. verification --------------------------------------------------

    def verify_patient(self, full_name: str, dob: str, postcode: str) -> dict:
        """
        Confirm who is calling, using all three identifiers.

        All three must match. A partial match is reported as a failure
        with no hint about which part was wrong, because telling a caller
        "the postcode is right but the date of birth is wrong" would
        confirm that the postcode belongs to a real patient.

        Two problems found on live voice calls shaped the rest of this.

        The model called this with only a name and date of birth, before
        asking for the postcode, and the caller heard "I cannot find a
        record". A missing detail is now reported as missing, naming which
        one, which leaks nothing because it only describes what the caller
        has not said yet.

        Speech to text heard "Hollis" as "Hollies", so an exact name match
        failed for a real patient giving correct details. Date of birth and
        postcode must still match exactly. The name only has to be very
        close, and only when a single record is that close. Twins share a
        date of birth and usually a postcode, so if two records at the same
        address are both near the spoken name, nothing is verified.
        """
        missing = [
            label
            for label, value in (
                ("full name", full_name),
                ("date of birth", dob),
                ("postcode", postcode),
            )
            if (value or "").strip().lower() in _PLACEHOLDERS
        ]
        if missing:
            return {
                "verified": False,
                "reason": "missing_details",
                "missing": missing,
                "say": self._ask_for(missing),
            }

        # A tester said "this is Margaret speaking", gave the right date of
        # birth and postcode, and was told no record matched: nobody asked
        # for her surname. A first name alone is asked to be completed
        # before any lookup, so the answer says nothing about the records.
        if len(_clean_name(full_name).split()) < 2:
            first = _clean_name(full_name).strip().title()
            return {
                "verified": False,
                "reason": "missing_details",
                "missing": ["surname"],
                "say": f"Could I take your surname as well{', ' + first if first else ''}?",
            }

        normalised_dob = _normalise_dob(dob)
        if normalised_dob is None:
            return {
                "verified": False,
                "reason": "date_not_understood",
                "say": "Sorry, I did not catch that date of birth. Could you give it to me again, starting with the day?",
            }

        # Someone is already confirmed on this call. The same person again is
        # fine; anyone else is refused before any lookup, so the refusal says
        # nothing about whether that other person is a patient.
        if self.verified_patient_id is not None:
            current = self._patient_row(self.verified_patient_id)
            if not (
                _name_similarity(full_name, current["full_name"]) >= _NAME_SIMILARITY
                and current["dob"] in _dob_readings(dob, normalised_dob, self.caller_heard)
            ):
                return {"verified": False, "reason": "one_patient_per_call", "say": ONE_PATIENT_PER_CALL}
            return {
                "verified": True,
                "patient_id": current["id"],
                "first_name": current["full_name"].split()[0],
                "say": "You are already confirmed on this call.",
            }

        # A tester said "nope" to "have you been a patient before?", and the
        # model still ran the existing patient check five times, asking for
        # the postcode again and again, before it registered him. A caller
        # who has said they are new is sent to registration instead.
        if self.caller_said_new and self.expected_patient_id is None:
            return {
                "verified": False,
                "reason": "caller_is_new",
                "use_instead": "register_new_patient",
                "say": "As you are new to us, I will set you up as a new patient.",
            }

        if self.caller_heard is not None and not _postcode_was_said(postcode, self.caller_heard):
            return {
                "verified": False,
                "reason": "missing_details",
                "missing": ["postcode"],
                "say": self._ask_again_or_first("postcode"),
            }

        # On a call Sophia placed, the model already knows the patient's name,
        # so it passes the right one whatever the caller said. A voice test
        # showed it: speech to text heard "Isai Lee Nashworth" and the model
        # sent "Eileen Ashworth". That leaves the date of birth and postcode
        # doing all the work. So here the name must be one the caller said.
        if (
            self.expected_patient_id is not None
            and self.caller_heard is not None
            and not _name_was_said(full_name, self.caller_heard)
        ):
            return {
                "verified": False,
                "reason": "missing_details",
                "missing": ["full name"],
                "say": "Could you tell me your full name, please?",
            }

        spoken_postcode = _normalise_postcode(postcode).upper()
        if not _UK_POSTCODE.match(spoken_postcode) and config.DEMO_RELAXED_DETAILS:
            # Testers give made up postcodes as new patients. No record on
            # file has one, so this is simply no match, which offers the new
            # patient route, rather than asking again letter by letter.
            return self._no_match()
        if not _UK_POSTCODE.match(spoken_postcode):
            return {
                "verified": False,
                "reason": "postcode_not_understood",
                "say": (
                    "Sorry, I did not catch that as a UK postcode. Could you say it "
                    "again, letter by letter? For example, W A 1, 1 J A."
                ),
            }

        readings = sorted(_dob_readings(dob, normalised_dob, self.caller_heard))
        rows = self.conn.execute(
            f"SELECT * FROM patients WHERE dob IN ({', '.join('?' for _ in readings)})",
            readings,
        ).fetchall()
        same_address = [
            row for row in rows if _normalise_postcode(row["postcode"]).upper() == spoken_postcode
        ]

        exact = [
            row for row in same_address
            if _normalise_name(row["full_name"]) == _normalise_name(full_name)
        ]
        close = [
            row for row in same_address
            if _name_similarity(full_name, row["full_name"]) >= _NAME_SIMILARITY
        ]
        match = exact[0] if len(exact) == 1 else (close[0] if len(close) == 1 else None)

        if match is None:
            # Speech to text heard "W A 1, 2 N F" as "W one two n f", losing
            # one letter. Allow a single character of postcode error, but
            # only when the date of birth is exact, the name is close, and
            # exactly one record fits. Two fuzzy details out of three is not
            # enough on its own terms, so the uniqueness rule stays.
            near = [
                row for row in rows
                if _edit_distance(_normalise_postcode(row["postcode"]).upper(), spoken_postcode) <= 1
                and _name_similarity(full_name, row["full_name"]) >= _NAME_SIMILARITY
            ]
            if len(near) == 1:
                match = near[0]

        # On an outbound call, only the patient Sophia rang counts. Someone
        # else in the household passing the identity check with their own
        # details must not unlock the patient's appointment. Refused with
        # the same answer as a mismatch, so nothing is revealed either way.
        if (
            match is not None
            and self.expected_patient_id is not None
            and match["id"] != self.expected_patient_id
        ):
            match = None

        if match is not None:
            self.verified_patient_id = match["id"]
            self.verified_name = match["full_name"]
            return {
                "verified": True,
                "patient_id": match["id"],
                "first_name": match["full_name"].split()[0],
                "say": "Thank you, I have found your record.",
                **self._carry_on(),
            }

        return self._no_match()

    def _no_match(self) -> dict:
        # Deliberately identical response whether the patient does not
        # exist or the details did not line up. It offers the new patient
        # route to everyone, which says nothing about which case this is.
        self.verified_patient_id = None
        return {
            "verified": False,
            "reason": "no_match",
            "say": (
                "I am sorry, I cannot find a record matching those details. "
                "Have you been to the practice before? If you are new, I can set "
                "you up as a new patient. Otherwise I can take a message for the team."
            ),
        }

    def _ask_for(self, missing: list[str]) -> str:
        """
        Ask for missing details, confirming a name the caller already gave.

        The model passed "unknown" for a name the caller had said at the
        start of the call, and the caller was asked for it again.
        """
        if "full name" in missing and self.caller_name:
            rest = [label for label in missing if label != "full name"]
            if len(self.caller_name.split()) < 2:
                ask = f"Could I take your surname as well, {self.caller_name}?"
            else:
                ask = f"Just to confirm, your name is {self.caller_name}?"
            if rest:
                ask += f" And could you tell me your {' and '.join(rest)}, please?"
            return ask
        return f"Could you tell me your {' and '.join(missing)}, please?"

    def _ask_again_or_first(self, label: str) -> str:
        """
        Ask for a detail the model passed but the caller never said.

        Either the caller gave it and it was misheard, or the model made it
        up before asking. Only the first deserves "again".
        """
        if label in self.details_asked_for:
            letters = ", letter by letter" if label == "postcode" else ""
            return f"Sorry, I want to be sure I have your {label} exactly right. Could you say it again{letters}?"
        return f"Could you tell me your {label}, please?"

    def _urgent_only_on_the_day(self) -> dict:
        """
        Why an urgent appointment type cannot be searched or booked like a routine one.

        A replay on a Saturday booked an "emergency consultation" for
        Monday at 8am as if it were a check up. Urgent care comes only from
        the day's limited urgent appointments, released at 8am, which is
        the whole point of the 8am release.
        """
        if self.urgent_bookable_now():
            return {"slots": [], "reason": "use_todays_urgent_slots",
                    "say": "Urgent appointments come from today's urgent slots. Let me check what is left today."}
        return {
            "slots": [],
            "reason": "urgent_only_on_the_day",
            "for_the_model": "For the earliest routine appointment instead, use recommend_appointment_type with purpose check_up.",
            "say": (
                "Urgent appointments can only be booked on the day itself. "
                + ("" if self.urgent_advice_given else self.urgent_advice())
            ).strip(),
        }

    def next_urgent_release(self) -> str:
        """
        When the caller can next ring for an urgent appointment, as Sophia says it.

        "8am tomorrow" was wrong every Friday, and a closed day gave no
        time at all, so a tester in severe pain on a Saturday was booked a
        routine check up and never told when urgent appointments open.
        """
        today = self.now.date()
        if clock.is_open_day(today) and not clock.urgent_slots_released(self.now):
            return "8am this morning"
        day = clock.next_open_day(today + timedelta(days=1))
        if day == today + timedelta(days=1):
            return f"8am tomorrow, {clock.spoken_date(day)}"
        return f"8am on {clock.spoken_date(day)}"

    def urgent_bookable_now(self) -> bool:
        """True if one of today's urgent appointments can still be booked."""
        if not clock.urgent_slots_released(self.now):
            return False
        rows = self.conn.execute(
            "SELECT slot_time FROM urgent_slots WHERE slot_date = ? AND status = 'available'",
            (self.now.strftime(clock.DB_DATE_FORMAT),),
        ).fetchall()
        return any(clock.combine(self.now.date(), clock.parse_time(r["slot_time"])) > self.now for r in rows)

    def caller_has_same_day_problem(self) -> bool:
        """
        True if the caller wants to be seen today.

        Either something they said is a same day problem by the safety
        screen, or they asked to be seen today and urgent appointments were
        looked up. A tester said "I would like it seen today" with no
        symptom the screen knows, and was never told when to ring.
        """
        return self.urgent_requested or any(
            safety.screen(said).level is safety.Level.URGENT for said in (self.caller_heard or [])
        )

    def urgent_advice(self) -> str:
        """What to tell a caller with a same day problem who cannot be booked urgently right now."""
        return (
            f"For the problem you mentioned, please ring from {self.next_urgent_release()}, when "
            "the day's urgent appointments are released. If you need help before then, call the "
            f"out of hours dental service on {config.OUT_OF_HOURS_DENTAL_NUMBER}, or NHS "
            f"{config.NHS_URGENT_NUMBER}."
        )

    def _carry_on(self) -> dict:
        """
        What to do next, once the caller is identified.

        A tester chose the earliest urgent appointment, gave his details,
        was registered, and then heard "How can I help you today?". The
        booking he had asked for was forgotten.
        """
        if self.waiting_booking is None:
            # Replayed, the same call registered him and then booked a routine
            # check up, because nothing had been tried yet: only today's
            # urgent appointments had been looked up.
            if self.offered_urgent and not self.bookings_made:
                return {"next": (
                    "The caller asked for an urgent appointment today. Offer today's urgent "
                    "appointments already found and book the one they choose with book_urgent_slot. "
                    "Not a check up. Do not ask how you can help."
                )}
            return {}
        return {
            "next": (
                f"The caller already asked for this booking: call {self.waiting_booking['tool']} with "
                f"{json.dumps(self.waiting_booking['arguments'])} now, then confirm it. "
                "Do not ask how you can help."
            )
        }

    # -- 1b. callers new to the practice ----------------------------------

    def register_new_patient(self, full_name: str, dob: str, postcode: str, phone: str) -> dict:
        """
        Set up a record for a caller who has never been to the practice.

        Until this existed, nobody could book unless they were already on
        file, so a genuinely new caller, the person a receptionist most
        needs to win, could only leave a message.

        The record is private: the practice accepts new private patients,
        while new NHS places are limited and the waiting list is paused.
        The caller is then treated as verified for their own new record
        only, which holds nothing but what they have just said.

        Every check here exists because the alternative went wrong somewhere
        else in this project, or would have:

          - every detail must be something the caller actually said, the
            same grounding used for postcodes after a model invented one
          - no new record for someone who already matches a patient, which
            would create a duplicate and let a caller who got their
            postcode wrong start a second history
          - refused on an outbound call, where Sophia rang a known patient
          - refused once a caller is verified as an existing patient
          - children go to the team, who need a parent or guardian
          - one patient per call, and the same details twice return the same record
        """
        for label, value in (("full name", full_name), ("date of birth", dob),
                             ("postcode", postcode), ("phone number", phone)):
            if (value or "").strip().casefold() in _PLACEHOLDERS:
                return {"registered": False, "reason": "missing_details", "missing": [label],
                        "say": self._ask_for([label])}

        if self.expected_patient_id is not None:
            raise ToolError("New patients cannot be registered on an outbound call.")

        if self.verified_patient_id is not None and self.verified_patient_id not in self.registered_patient_ids:
            return {"registered": False, "reason": "already_a_patient",
                    "say": "You are already registered with us, so there is no need to set up a new record."}

        full_name = _clean_name(full_name)
        if not _name_is_plausible(full_name) or len(full_name.split()) < 2:
            return {"registered": False, "reason": "missing_details", "missing": ["full name"],
                    "say": "Could I take your first name and surname, please?"}

        normalised_dob = _normalise_dob(dob)
        born = datetime.strptime(normalised_dob, clock.DB_DATE_FORMAT).date() if normalised_dob else None
        today = self.now.date()
        if born is None or born > today or (today.year - born.year) > _MAXIMUM_PLAUSIBLE_AGE:
            return {"registered": False, "reason": "date_not_understood",
                    "say": "Sorry, I did not catch that date of birth. Could you give it to me again, starting with the day?"}

        relaxed = config.DEMO_RELAXED_DETAILS
        spoken_postcode = _normalise_postcode(postcode).upper()
        if not (_DEMO_POSTCODE if relaxed else _UK_POSTCODE).match(spoken_postcode):
            return {"registered": False, "reason": "postcode_not_understood",
                    "say": "Sorry, I did not catch that postcode. Could you say it again, letter by letter?"}

        normalised_phone = _normalise_phone(phone, relaxed=relaxed)
        if normalised_phone is None:
            return {"registered": False, "reason": "phone_not_understood",
                    "say": "Sorry, I did not catch that phone number. Could you say it again, digit by digit?"}

        if self.caller_heard is not None:
            unsaid = [
                label for label, said in (
                    ("full name", _name_was_said(full_name, self.caller_heard)),
                    ("postcode", _postcode_was_said(postcode, self.caller_heard)),
                    ("phone number", _phone_was_said(normalised_phone, self.caller_heard)),
                )
                if not said
            ]
            if unsaid:
                return {"registered": False, "reason": "missing_details", "missing": unsaid,
                        "say": self._ask_again_or_first(unsaid[0])}

            # Verification can try both readings of "05/04/1990", but a new
            # record stores one, and a wrong one would fail this patient on
            # every call from now on. Speech to text has written a UK date
            # month first before. So an ambiguous number is confirmed by
            # asking, unless the caller has already said the month by name.
            readings = _dob_readings(dob, normalised_dob, self.caller_heard)
            month_name = clock._MONTHS[born.month - 1]
            names = month_name if month_name == "May" else f"{month_name}|{month_name[:3]}"
            # Next to a day or a year, because "May I book a check up?" is not a date.
            said_month = re.search(
                rf"(?:\bof|\d|\b\w+(?:st|nd|rd|th))\s+(?:{names})\b"
                rf"|\b(?:{names})\s+(?:\d|nineteen|twenty|two thousand|the\b)",
                " ".join(self.caller_heard),
                re.IGNORECASE,
            )
            if len(readings) > 1 and not said_month:
                spoken = sorted(
                    f"the {clock._ordinal(day.day)} of {clock._MONTHS[day.month - 1]} {day.year}"
                    for day in (datetime.strptime(r, clock.DB_DATE_FORMAT).date() for r in readings)
                )
                return {"registered": False, "reason": "date_ambiguous",
                        "say": f"Just to be sure I have it right, is that {spoken[0]}, or {spoken[1]}?"}

        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        if age < _MINIMUM_AGE_TO_REGISTER:
            return {"registered": False, "reason": "child",
                    "say": ("A child needs a parent or guardian registered alongside them, which the "
                            "team will arrange. Let me take a message and they will call you back.")}

        # The same details again on this call: the record already exists.
        for patient_id in self.registered_patient_ids:
            row = self._patient_row(patient_id)
            if (_normalise_name(row["full_name"]) == _normalise_name(full_name)
                    and row["dob"] == normalised_dob):
                self.verified_patient_id = patient_id
                self.verified_name = row["full_name"]
                return {"registered": True, "patient_id": patient_id, "already_done": True,
                        "say": "You are already set up from a moment ago."}

        # Already a patient under these details. The wording does not say
        # so, because that would confirm to a stranger that a named person
        # with that birthday is on the practice list.
        readings = sorted(_dob_readings(dob, normalised_dob, self.caller_heard))
        same_birthday = self.conn.execute(
            f"SELECT * FROM patients WHERE dob IN ({', '.join('?' for _ in readings)})", readings
        ).fetchall()
        same_person = [row for row in same_birthday if _name_similarity(full_name, row["full_name"]) >= _NAME_SIMILARITY]
        # A tester who had registered on an earlier call said "nope" to
        # "been here before?", gave the same details, and was offered a
        # message. Name, date of birth and postcode all matching one record
        # is exactly the identity check an existing patient passes, so they
        # are found rather than turned away. Anything less stays refused.
        at_address = [
            row for row in same_person
            if _normalise_postcode(row["postcode"]).upper() == spoken_postcode
        ]
        if len(at_address) == 1 and self.expected_patient_id is None:
            match = at_address[0]
            self.verified_patient_id = match["id"]
            self.verified_name = match["full_name"]
            self.caller_said_new = False
            first = match["full_name"].split()[0]
            return {"registered": False, "reason": "already_registered", "verified": True,
                    "patient_id": match["id"], "first_name": first,
                    "say": f"It looks like you are already registered with us, {first}, so I have found your record.",
                    **self._carry_on()}
        if same_person:
            return {"registered": False, "reason": "cannot_register",
                    "say": ("I am not able to set up a new record with those details over the phone. "
                            "Let me take a message, and the team will call you back to sort it out.")}

        if self.registered_patient_ids:
            return {"registered": False, "reason": "one_patient_per_call", "say": ONE_PATIENT_PER_CALL}

        name = full_name
        cursor = self.conn.execute(
            "INSERT INTO patients (code, full_name, dob, postcode, phone, patient_type, notes) "
            "VALUES (?, ?, ?, ?, ?, 'private', ?)",
            (
                f"new-{uuid.uuid4().hex[:10]}",
                name,
                normalised_dob,
                _format_postcode(spoken_postcode),
                normalised_phone,
                f"Registered by phone with Sophia on {today.isoformat()}. Not yet seen.",
            ),
        )
        self.conn.commit()

        self.registered_patient_ids.append(cursor.lastrowid)
        self.verified_patient_id = cursor.lastrowid
        self.verified_name = name
        return {
            "registered": True,
            "patient_id": cursor.lastrowid,
            "first_name": name.split()[0],
            "funding": "private",
            "say": (
                f"Thank you, {name.split()[0]}, you are set up as a new private patient. "
                "Your first appointment would be a new patient examination."
            ),
            **self._carry_on(),
        }

    # -- 2. patient status ------------------------------------------------

    def get_patient_status(self) -> dict:
        """Where the verified patient stands: registration, funding, record, bookings."""
        patient_id = self._require_verified()
        row = self._patient_row(patient_id)
        facts = PatientFacts.from_row(row)
        status = policies.assess_attendance(self._events(patient_id), facts, self.now.date())

        upcoming = [
            {
                "appointment_id": appointment["id"],
                "when": clock.spoken_datetime(clock.from_db(appointment["start_time"])),
                "type": appointment["type_name"],
                "clinician": appointment["clinician_name"],
            }
            for appointment in self.conn.execute(
                "SELECT a.id, a.start_time, t.name AS type_name, c.name AS clinician_name "
                "FROM appointments a "
                "JOIN appointment_types t ON t.code = a.type_code "
                "JOIN clinicians c ON c.id = a.clinician_id "
                "WHERE a.patient_id = ? AND a.status = 'booked' AND a.start_time >= ? "
                "ORDER BY a.start_time",
                (patient_id, clock.to_db(self.now)),
            )
        ]

        days = policies.days_since_last_attended(facts, self.now.date())
        return {
            "name": row["full_name"],
            "patient_type": row["patient_type"],
            "is_registered": policies.is_registered(facts, self.now.date()),
            "treated_as_new_patient_for_fees": policies.is_new_for_fees(
                facts, self.now.date()
            ),
            "last_attended": row["last_attended_date"],
            "years_since_last_seen": round(days / 365.25, 1) if days is not None else None,
            "short_notice_cancellations_last_24_months": status.snc_count,
            "missed_appointments_last_24_months": status.fta_count,
            "close_to_discharge": status.one_more_snc_discharges,
            "upcoming_appointments": upcoming,
        }

    # -- 3. availability --------------------------------------------------

    def _booked_intervals(self, clinician_id: int, day: date):
        rows = self.conn.execute(
            "SELECT start_time, end_time FROM appointments "
            "WHERE clinician_id = ? AND status = 'booked' AND date(start_time) = ?",
            (clinician_id, day.strftime(clock.DB_DATE_FORMAT)),
        ).fetchall()
        return [(clock.from_db(r["start_time"]), clock.from_db(r["end_time"])) for r in rows]

    def _eligible_clinicians(self, type_row):
        roles = ROLE_GROUPS.get(type_row["clinician_role"])
        if not roles:
            raise ToolError(f"Unknown clinician role {type_row['clinician_role']}.")

        funding_column = "sees_nhs" if type_row["funding"] == "nhs" else "sees_private"
        placeholders = ",".join("?" * len(roles))
        return self.conn.execute(
            f"SELECT id, name FROM clinicians "
            f"WHERE role IN ({placeholders}) AND {funding_column} = 1 ORDER BY id",
            roles,
        ).fetchall()

    def find_available_slots(
        self,
        appointment_type: str,
        days_ahead: int | None = None,
        clinician_name: str | None = None,
        earliest_date: str | None = None,
        limit: int = 5,
        after_time: str | None = None,
        before_time: str | None = None,
        on_date: str | None = None,
    ) -> dict:
        """
        Real availability, computed from working hours minus what is booked.

        Availability is never stored, so it cannot drift out of date. The
        lunch closure and the weekend fall out automatically, because a
        clinician simply has no hours block covering them.

        after_time and before_time exist because of an evaluation failure.
        A caller asked for "something in the afternoon, after two o'clock".
        With no way to ask for a time of day, and with the results spread
        by taking the earliest slot on each day, this only ever returned
        mornings. Sophia faithfully told the caller there were no afternoon
        appointments, which was false: the afternoons were almost empty.
        """
        window_start = _parse_hhmm(after_time)
        window_end = _parse_hhmm(before_time)
        type_row = self._type_row(appointment_type)
        if type_row["code"] in URGENT_TYPE_CODES:
            return self._urgent_only_on_the_day()
        if not type_row["is_bookable"]:
            return {
                "slots": [],
                "reason": "not_bookable",
                "say": (
                    f"{type_row['name']} is not something I can book directly. "
                    "The team will confirm it after an assessment. "
                    "Would you like me to take a message?"
                ),
            }

        duration = type_row["duration_min"]
        clinicians = self._eligible_clinicians(type_row)
        if clinician_name:
            wanted = _normalise_name(clinician_name)
            filtered = [c for c in clinicians if wanted in _normalise_name(c["name"])]
            if filtered:
                clinicians = filtered

        horizon = days_ahead or config.DEFAULT_SEARCH_DAYS
        start_day = self.now.date()

        # One day only. A tester asked for "any other slot on the same day",
        # and the search could only give the first free time on each of the
        # next few days, so Sophia had nothing to offer and said "no other
        # slots today since the practice is closed". It was not.
        single_day = None
        if on_date:
            try:
                single_day = datetime.strptime(on_date, clock.DB_DATE_FORMAT).date()
            except ValueError:
                single_day = None
        if single_day is not None:
            if single_day < self.now.date():
                return {"slots": [], "reason": "in_the_past", "say": "That day has already passed."}
            if not clock.is_open_day(single_day):
                return {"slots": [], "reason": "closed_that_day",
                        "say": "The practice is closed that day. It is open Monday to Friday."}
            start_day, horizon = single_day, 1
        elif earliest_date:
            try:
                requested = datetime.strptime(earliest_date, clock.DB_DATE_FORMAT).date()
                start_day = max(start_day, requested)
            except ValueError:
                pass

        found: list[Slot] = []
        days_with_availability = 0

        for offset in range(horizon):
            day = start_day + timedelta(days=offset)
            if not clock.is_open_day(day):
                continue
            slots_before_this_day = len(found)

            for clinician in clinicians:
                blocks = self.conn.execute(
                    "SELECT start_time, end_time FROM clinician_hours "
                    "WHERE clinician_id = ? AND weekday = ? ORDER BY start_time",
                    (clinician["id"], day.weekday()),
                ).fetchall()
                if not blocks:
                    continue

                booked = self._booked_intervals(clinician["id"], day)

                for block in blocks:
                    cursor = clock.combine(day, clock.parse_time(block["start_time"]))
                    block_end = clock.combine(day, clock.parse_time(block["end_time"]))

                    while cursor + timedelta(minutes=duration) <= block_end:
                        finish = cursor + timedelta(minutes=duration)
                        outside_window = (
                            (window_start and cursor.time() < window_start)
                            or (window_end and cursor.time() >= window_end)
                        )
                        if cursor <= self.now or outside_window:
                            cursor += timedelta(minutes=SLOT_GRID_MINUTES)
                            continue
                        clashes = any(
                            cursor < existing_end and finish > existing_start
                            for existing_start, existing_end in booked
                        )
                        if not clashes:
                            found.append(
                                Slot(
                                    clinician_id=clinician["id"],
                                    clinician_name=clinician["name"],
                                    start=cursor,
                                    duration_min=duration,
                                )
                            )
                        cursor += timedelta(minutes=SLOT_GRID_MINUTES)

            # Stop once we have enough DAYS to choose from, not enough
            # slots. A single morning easily yields dozens of ten minute
            # slots, so counting slots here would mean never looking past
            # the first day, and the caller would be offered four times on
            # the same morning instead of a real choice of days.
            if len(found) > slots_before_this_day:
                days_with_availability += 1
            if days_with_availability >= limit:
                break

        found.sort(key=lambda slot: slot.start)
        chosen = _spread_within_day(found, limit) if single_day is not None else _spread(found, limit)
        self.offered_slots = [slot.as_dict() for slot in chosen]
        if single_day is not None and not chosen:
            return {"slots": [], "reason": "day_full",
                    "say": "There is nothing free that day. Shall I look at the next day instead?"}

        return {
            "appointment_type": type_row["name"],
            "duration_minutes": duration,
            "fee": policies.format_fee(type_row["fee_gbp"], bool(type_row["fee_is_from"])),
            "slots": [slot.as_dict() for slot in chosen],
            # For the model, which once read five listed times as the only
            # times there were.
            "note": (
                "A sample only: each day has many more times. For a particular time "
                "or part of the day, search again with after_time and before_time. "
                "Never tell the caller a time is unavailable without searching for it."
            ),
        }

    def get_urgent_slots_today(self) -> dict:
        """
        Today's limited urgent appointments, respecting the 08:00 release.

        Before 8am there is nothing to offer, and saying so is the honest
        answer rather than showing tomorrow's.
        """
        self.urgent_requested = True
        # Already told when to ring: say only what is new, or it is read out
        # again. A replay repeated the whole advice straight after it.
        if self.urgent_advice_given and not self.urgent_bookable_now():
            return {
                "released": clock.urgent_slots_released(self.now),
                "slots": [],
                "reason": "already_told_when_to_ring",
                "say": "There are no urgent appointments to book right now.",
            }
        if not clock.is_open_day(self.now.date()):
            return {
                "released": False,
                "reason": "closed_today",
                "slots": [],
                "ring_from": self.next_urgent_release(),
                "say": (
                    "The practice is closed today. Urgent appointments are released by phone "
                    f"from {self.next_urgent_release()}, so please ring then. Until then, for "
                    f"urgent dental problems you can call {config.OUT_OF_HOURS_DENTAL_NUMBER}, "
                    f"or NHS {config.NHS_URGENT_NUMBER}."
                ),
            }

        if not clock.urgent_slots_released(self.now):
            return {
                "released": False,
                "reason": "before_release",
                "slots": [],
                "ring_from": self.next_urgent_release(),
                "say": (
                    "Today's urgent appointments are released at 8 o'clock this morning, "
                    "so they are not open yet. If you ring back from 8am we can look at "
                    f"what is available. If you cannot wait, NHS {config.NHS_URGENT_NUMBER} "
                    "can advise you."
                ),
            }

        rows = self.conn.execute(
            "SELECT u.id, u.slot_time, c.name AS clinician_name, u.clinician_id "
            "FROM urgent_slots u JOIN clinicians c ON c.id = u.clinician_id "
            "WHERE u.slot_date = ? AND u.status = 'available' ORDER BY u.slot_time",
            (self.now.strftime(clock.DB_DATE_FORMAT),),
        ).fetchall()

        remaining = [
            {
                "urgent_slot_id": row["id"],
                "time": row["slot_time"],
                "when": clock.spoken_time(clock.parse_time(row["slot_time"])),
                "clinician": row["clinician_name"],
            }
            for row in rows
            if clock.combine(self.now.date(), clock.parse_time(row["slot_time"])) > self.now
        ]

        if not remaining:
            return {
                "released": True,
                "slots": [],
                "reason": "none_left",
                "ring_from": self.next_urgent_release(),
                "say": (
                    "I am sorry, today's urgent appointments have all gone. "
                    f"If you need to be seen today, NHS {config.NHS_URGENT_NUMBER} can help, "
                    f"or the out of hours dental service on {config.OUT_OF_HOURS_DENTAL_NUMBER}. "
                    f"You are also welcome to ring us from {self.next_urgent_release()}."
                ),
            }

        self.offered_urgent = remaining
        return {"released": True, "slots": remaining, "remaining_count": len(remaining)}

    # -- 4. booking -------------------------------------------------------

    def book_appointment(self, slot_ref: str, appointment_type: str) -> dict:
        """
        Book a slot for the verified patient, re-checking everything first.

        The slot is re-validated at the moment of writing. Between being
        offered and being accepted it may have been taken, the clinician
        may not work then, or it may have fallen into the past.
        """
        if self.verified_patient_id is None:
            self.waiting_booking = {"tool": "book_appointment",
                                    "arguments": {"slot_ref": slot_ref, "appointment_type": appointment_type}}
        patient_id = self._require_verified()
        self.waiting_booking = None
        type_row = self._type_row(appointment_type)
        if type_row["code"] in URGENT_TYPE_CODES:
            return {"booked": False, **self._urgent_only_on_the_day()}

        if not type_row["is_bookable"]:
            raise ToolError(f"{type_row['name']} cannot be booked directly.")

        try:
            clinician_id_text, start_text = slot_ref.split("@", 1)
            clinician_id = int(clinician_id_text)
            start = clock.to_aware(datetime.strptime(start_text, "%Y-%m-%dT%H:%M"))
        except (ValueError, AttributeError):
            raise ToolError("That slot reference is not one I recognise.")

        duration = type_row["duration_min"]
        end = start + timedelta(minutes=duration)

        if start <= self.now:
            return {"booked": False, "reason": "in_the_past",
                    "say": "That time has already passed. Let me find you another."}

        if not clock.is_within_opening_hours(start):
            return {"booked": False, "reason": "outside_opening_hours",
                    "say": "The practice is not open then. Let me find you another time."}

        clinician = self.conn.execute(
            "SELECT id, name FROM clinicians WHERE id = ?", (clinician_id,)
        ).fetchone()
        if clinician is None:
            raise ToolError("That clinician is not on file.")

        works_then = self.conn.execute(
            "SELECT 1 FROM clinician_hours WHERE clinician_id = ? AND weekday = ? "
            "AND start_time <= ? AND end_time >= ?",
            (
                clinician_id,
                start.weekday(),
                start.strftime("%H:%M"),
                end.strftime("%H:%M"),
            ),
        ).fetchone()
        if works_then is None:
            return {"booked": False, "reason": "clinician_not_working",
                    "say": f"{clinician['name']} is not in then. Let me find another time."}

        clash = any(
            start < existing_end and end > existing_start
            for existing_start, existing_end in self._booked_intervals(clinician_id, start.date())
        )
        if clash:
            return {"booked": False, "reason": "just_taken",
                    "say": "I am sorry, that time has just been taken. Let me find you another."}

        cursor = self.conn.execute(
            "INSERT INTO appointments "
            "(patient_id, clinician_id, type_code, start_time, end_time, status, "
            " fee_quoted, booked_via, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'booked', ?, 'sophia', ?)",
            (
                patient_id,
                clinician_id,
                appointment_type,
                clock.to_db(start),
                clock.to_db(end),
                type_row["fee_gbp"],
                clock.to_db(self.now),
            ),
        )
        self.conn.commit()

        fee = policies.format_fee(type_row["fee_gbp"], bool(type_row["fee_is_from"]))
        # The practice takes a new patient's first examination fee when it is
        # booked. Sophia cannot take payment, so she says who will, rather
        # than leave the caller to find out on the day.
        payment = (
            " The first examination is paid when it is booked, so the team will "
            "call you to take payment."
            if appointment_type == "PRIV_NEW_EXAM" else ""
        )
        self.bookings_made.append(
            {
                "appointment_id": cursor.lastrowid,
                "when": clock.spoken_datetime(start),
                "clinician": clinician["name"],
                "type": type_row["name"],
                "fee": fee,
            }
        )
        return {
            "booked": True,
            "appointment_id": cursor.lastrowid,
            "when": clock.spoken_datetime(start),
            "clinician": clinician["name"],
            "appointment_type": type_row["name"],
            "fee": fee,
            "say": (
                f"That is booked. {clock.spoken_datetime(start)} with {clinician['name']}, "
                f"for a {type_row['name'].lower()}, and the charge is {fee}.{payment} "
                "If you need to change it, please give us at least 24 hours notice."
                + self._same_day_problem_note()
            ),
        }

    def _same_day_problem_note(self) -> str:
        """
        For a caller with a same day problem who has just been booked a routine appointment.

        A tester with severe tooth pain on a Saturday was booked Monday's
        check up and nothing more. A routine appointment is not care for
        the pain, so the booking itself says what is.
        """
        if not self.caller_has_same_day_problem() or self.urgent_advice_given:
            return ""
        if self.urgent_bookable_now():
            return " If you would rather be seen today for the problem you mentioned, there are still urgent appointments today."
        return " " + self.urgent_advice()

    def book_urgent_slot(self, urgent_slot_id: int) -> dict:
        """
        Take one of today's limited urgent appointments.

        The fee depends on the patient's funding and history, so the type
        is chosen by the policy rules rather than by the model.
        """
        if self.verified_patient_id is None:
            self.waiting_booking = {"tool": "book_urgent_slot", "arguments": {"urgent_slot_id": urgent_slot_id}}
        patient_id = self._require_verified()
        self.waiting_booking = None
        row = self.conn.execute(
            "SELECT * FROM urgent_slots WHERE id = ?", (urgent_slot_id,)
        ).fetchone()
        if row is None:
            raise ToolError("That urgent appointment is not one I recognise.")
        if row["status"] != "available":
            return {"booked": False, "reason": "just_taken",
                    "say": "I am sorry, that one has just gone."}
        if row["slot_date"] != self.now.strftime(clock.DB_DATE_FORMAT):
            return {"booked": False, "reason": "not_today",
                    "say": "Urgent appointments can only be booked on the day itself."}
        if not clock.urgent_slots_released(self.now):
            return {"booked": False, "reason": "before_release",
                    "say": "Today's urgent appointments open at 8am."}

        patient = self._patient_row(patient_id)
        facts = PatientFacts.from_row(patient)
        # The record says how a patient is usually seen, but urgent care is
        # open to NHS and private alike. Every new patient's record is
        # private, so without this a new caller who asked for NHS urgent care
        # was charged £85 instead of £27.90.
        funding = _latest_funding_request(self.caller_heard) or patient["patient_type"]
        type_code = policies.emergency_type_code(funding, facts, self.now.date())
        type_row = self._type_row(type_code)

        start = clock.combine(self.now.date(), clock.parse_time(row["slot_time"]))
        if start <= self.now:
            return {"booked": False, "reason": "in_the_past",
                    "say": "That time has already passed today."}

        end = start + timedelta(minutes=type_row["duration_min"])

        cursor = self.conn.execute(
            "INSERT INTO appointments "
            "(patient_id, clinician_id, type_code, start_time, end_time, status, "
            " fee_quoted, booked_via, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'booked', ?, 'sophia', ?)",
            (
                patient_id,
                row["clinician_id"],
                type_code,
                clock.to_db(start),
                clock.to_db(end),
                type_row["fee_gbp"],
                clock.to_db(self.now),
            ),
        )
        appointment_id = cursor.lastrowid
        self.conn.execute(
            "UPDATE urgent_slots SET status = 'taken', appointment_id = ? WHERE id = ?",
            (appointment_id, urgent_slot_id),
        )
        self.conn.commit()

        fee = policies.format_fee(type_row["fee_gbp"])
        self.bookings_made.append(
            {
                "appointment_id": appointment_id,
                "when": clock.spoken_datetime(start),
                "clinician": "",
                "type": type_row["name"],
                "fee": fee,
            }
        )
        return {
            "booked": True,
            "appointment_id": appointment_id,
            "when": clock.spoken_time(start.time()),
            "appointment_type": type_row["name"],
            "fee": fee,
            "say": (
                f"I can see you today at {clock.spoken_time(start.time())}. "
                f"That is an {type_row['name'].lower()}, and the charge is {fee}. "
                "It deals with the immediate problem, so any further treatment "
                "would be arranged separately."
            ),
        }

    # -- 5. cancelling, in two steps --------------------------------------

    def check_cancellation(self, appointment_id: int) -> dict:
        """
        Step one. Work out what cancelling would mean, and change nothing.

        This exists so the caller hears the consequence before it
        happens. Sophia must call this, say the warning, and get a clear
        yes before calling cancel_appointment.
        """
        patient_id = self._require_verified()
        appointment = self._owned_appointment(appointment_id, patient_id)

        start = clock.from_db(appointment["start_time"])
        assessment = policies.assess_cancellation(
            start, self._events(patient_id), self._facts(patient_id), self.now
        )

        return {
            "appointment_id": appointment_id,
            "when": clock.spoken_datetime(start),
            "hours_until": round(assessment.hours_until, 1),
            "is_short_notice": assessment.is_short_notice,
            "would_reach_discharge_limit": assessment.would_discharge,
            "needs_confirmation": True,
            "say": assessment.warning
            or (
                f"That is your appointment on {clock.spoken_datetime(start)}. "
                "That is more than 24 hours away, so there is no charge and nothing "
                "goes on your record. Shall I cancel it?"
            ),
        }

    def cancel_appointment(self, appointment_id: int, caller_confirmed: bool) -> dict:
        """
        Step two. Actually cancel, but only after an explicit yes.

        The confirmation flag is checked here rather than trusted to the
        conversation, so a model that skips the warning cannot cancel.
        """
        patient_id = self._require_verified()
        if not caller_confirmed:
            raise ToolError(
                "The caller has not confirmed. Call check_cancellation, tell them "
                "what it means, and only cancel once they clearly agree."
            )

        appointment = self._owned_appointment(appointment_id, patient_id)
        start = clock.from_db(appointment["start_time"])
        short_notice = policies.is_short_notice(start, self.now)

        self.conn.execute(
            "UPDATE appointments SET status = ?, cancelled_at = ?, cancelled_via = 'sophia' WHERE id = ?",
            ("snc" if short_notice else "cancelled", clock.to_db(self.now), appointment_id),
        )
        type_name = self._type_row(appointment["type_code"])["name"]
        self.cancellations_made.append(f"{type_name}, {clock.spoken_datetime(start)}")
        if short_notice:
            self.conn.execute(
                "INSERT INTO attendance_events (patient_id, event_type, event_date, appointment_id, notes) "
                "VALUES (?, 'SNC', ?, ?, 'Cancelled by phone with less than 24 hours notice')",
                (patient_id, self.now.strftime(clock.DB_DATE_FORMAT), appointment_id),
            )
        self.conn.execute(
            "UPDATE urgent_slots SET status = 'available', appointment_id = NULL "
            "WHERE appointment_id = ?",
            (appointment_id,),
        )
        self.conn.commit()

        status_after = policies.assess_attendance(
            self._events(patient_id), self._facts(patient_id), self.now.date()
        )

        return {
            "cancelled": True,
            "recorded_as_short_notice": short_notice,
            "now_discharged": status_after.is_discharged,
            "say": (
                "That is cancelled. It has gone on your record as a short notice "
                "cancellation, as I explained."
                if short_notice
                else "That is cancelled, and nothing goes on your record."
            ),
        }

    def _owned_appointment(self, appointment_id: int, patient_id: int):
        appointment = self.conn.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        if appointment is None:
            raise ToolError("I cannot find that appointment.")
        if appointment["patient_id"] != patient_id:
            # Same wording as a missing appointment, so nothing is leaked
            # about somebody else's booking.
            raise ToolError("I cannot find that appointment.")
        if appointment["status"] != "booked":
            raise ToolError("That appointment is not currently booked.")
        return appointment

    def reschedule_appointment(
        self, appointment_id: int, new_slot_ref: str, caller_confirmed: bool
    ) -> dict:
        """
        Move an appointment. Cancelling the old one still follows the 24 hour rule.

        Booking the new slot happens first. If it fails, the patient keeps
        the appointment they already had rather than losing both.
        """
        patient_id = self._require_verified()
        appointment = self._owned_appointment(appointment_id, patient_id)
        type_code = appointment["type_code"]

        booking = self.book_appointment(new_slot_ref, type_code)
        if not booking.get("booked"):
            return {"rescheduled": False, **booking}

        cancellation = self.cancel_appointment(appointment_id, caller_confirmed=caller_confirmed)

        return {
            "rescheduled": True,
            "new_appointment_id": booking["appointment_id"],
            "when": booking["when"],
            "clinician": booking["clinician"],
            "recorded_as_short_notice": cancellation["recorded_as_short_notice"],
            "say": (
                f"I have moved you to {booking['when']} with {booking['clinician']}. "
                + (
                    "Because the original was less than 24 hours away, it does go on "
                    "your record as a short notice cancellation."
                    if cancellation["recorded_as_short_notice"]
                    else "There is nothing on your record for the change."
                )
            ),
        }

    # -- 6. messages ------------------------------------------------------

    def record_call_outcome(self, outcome: str, note: str | None = None) -> dict:
        """
        Record how an outbound reminder call ended.

        Only valid on an outbound call. "wrong_person" and "call_back_later"
        need no verification, because recording them reveals nothing. Any
        outcome that describes what the patient decided needs the patient to
        have been verified first, so the call list can never say "confirmed"
        on the word of whoever happened to answer.
        """
        from . import outbound

        if self.reminder is None:
            raise ToolError("record_call_outcome is only for outbound reminder calls.")
        if outcome not in outbound.OUTCOMES:
            raise ToolError(f"Unknown outcome {outcome}. Use one of: {', '.join(outbound.OUTCOMES)}.")
        if outcome not in ("wrong_person", "call_back_later"):
            self._require_verified()

        # A tester answered "you are speaking to Jones", speech to text for
        # "Joan", and Sophia apologised and hung up on the patient she had
        # rung. Someone else only counts when they say so; anything less is
        # checked once by asking.
        if (outcome == "wrong_person" and self.caller_heard and not self._wrong_person_checked
                and not _NOT_THE_PATIENT.search(self.caller_heard[-1])):
            self._wrong_person_checked = True
            return {
                "recorded": False,
                "reason": "not_clear",
                "say": f"Sorry, just to check, am I speaking with {self.reminder.patient_name}?",
            }

        outbound.record_outcome(self.conn, self.reminder, outcome)
        return {"recorded": True, "outcome": outcome}

    def take_message(
        self,
        caller_name: str,
        reason: str,
        callback_number: str | None = None,
        detail: str | None = None,
    ) -> dict:
        """
        Hand anything Sophia cannot handle to the human team.

        This needs no verification, because it reveals nothing. It is the
        safety net for complaints, clinical questions, quotes that are not
        on the price list, and any failed identity check.
        """
        if not (caller_name or "").strip():
            raise ToolError("A message needs a name to go with it.")
        first = caller_name.split()[0]

        # A tester's message was taken with no number, twice, and only when
        # he asked "how will they contact me?" was he asked for one. A
        # message nobody can answer is not a message. The number must be one
        # the caller said, or the one on a verified patient's record.
        number = None
        if (callback_number or "").strip().casefold() not in _PLACEHOLDERS:
            number = _normalise_phone(callback_number, relaxed=config.DEMO_RELAXED_DETAILS)
            if number is None:
                return {"taken": False, "reason": "phone_not_understood",
                        "say": "Sorry, I did not catch that number. Could you say it again, digit by digit?"}
            if self.caller_heard is not None and not _phone_was_said(number, self.caller_heard):
                number = None
        if number is None and self.message_id is not None:
            number = self.conn.execute(
                "SELECT callback_number FROM messages WHERE id = ?", (self.message_id,)
            ).fetchone()["callback_number"]
        if number is None and self.verified_patient_id is not None:
            number = self._patient_row(self.verified_patient_id)["phone"] or None
        if number is None:
            return {"taken": False, "reason": "missing_details", "missing": ["phone number"],
                    "say": "Could I take a phone number for the team to call you back on?"}

        # One message per call. Something added later joins the first,
        # rather than a second message saying half the same thing.
        if self.message_id is not None:
            row = self.conn.execute("SELECT reason, detail FROM messages WHERE id = ?", (self.message_id,)).fetchone()
            extra = "; ".join(part for part in (reason, detail) if part)
            joined = "; ".join(part for part in (row["detail"], extra) if part)
            urgency, assigned_to = dashboard.triage_message(row["reason"], joined, self.caller_heard)
            self.conn.execute(
                "UPDATE messages SET callback_number = ?, detail = ?, urgency = ?, assigned_to = ? WHERE id = ?",
                (number, joined, urgency, assigned_to, self.message_id),
            )
            self.conn.commit()
            return {"taken": True, "message_id": self.message_id, "added_to_earlier_message": True,
                    "say": f"I have added that to your message for the team, {first}."}

        # How urgent, and for whom, is decided by rules over the whole call:
        # a tester asked about treatments, mentioned a swollen face while
        # leaving the message, and that message has to reach a dentist today.
        urgency, assigned_to = dashboard.triage_message(reason, detail, self.caller_heard)
        cursor = self.conn.execute(
            "INSERT INTO messages (caller_name, callback_number, patient_id, reason, detail, created_at, status, "
            "urgency, assigned_to) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (
                caller_name.strip(),
                number,
                self.verified_patient_id,
                reason,
                detail,
                clock.to_db(self.now),
                urgency,
                assigned_to,
            ),
        )
        self.conn.commit()
        self.message_id = cursor.lastrowid

        # A tester asked for a call "between one and five" and was promised
        # one. The time is passed on; when the team calls is theirs to say.
        said = " ".join([reason or "", detail or "", *(self.caller_heard or [])[-3:]])
        timing = (
            " I have noted when suits you best, though I can't promise exactly when they will ring."
            if _PREFERRED_TIME.search(said) else ""
        )
        return {
            "taken": True,
            "message_id": cursor.lastrowid,
            "say": (
                f"I have taken that down for the team, {first}, and they will call you back "
                f"on the number you gave me.{timing}"
            ),
        }

    # -- 7. practice facts that are structured, not prose -----------------

    def search_practice_info(self, question: str) -> dict:
        """
        Look something up in the practice's own documents.

        Opening hours, the lunch closure, where the practice is, how to
        register, what the attendance policy says, the out of hours
        numbers. Anything a receptionist would know and a caller might
        ask.

        Returns nothing rather than a weak match when the question is not
        covered, so that not knowing stays a possible outcome.
        """
        return knowledge.answer(question)

    def get_fee(self, appointment_type: str) -> dict:
        """Look up a fee from the price table. Never from memory."""
        type_row = self._type_row(appointment_type)
        return {
            "appointment_type": type_row["name"],
            "fee": policies.format_fee(type_row["fee_gbp"], bool(type_row["fee_is_from"])),
            "funding": type_row["funding"],
            "nhs_band": type_row["nhs_band"],
            "bookable_by_sophia": bool(type_row["is_bookable"]),
            "description": type_row["description"],
        }

    def recommend_appointment_type(
        self, purpose: str, funding: str | None = None, minutes: int | None = None
    ) -> dict:
        """
        Choose the right appointment type for a verified patient.

        The model says what the caller wants in plain words. The eligibility
        rules, and therefore the fee, are decided here.
        """
        if self.verified_patient_id is None and self.caller_said_new:
            # Someone new to the practice has no record to read, and the
            # rules already cover them: never seen, so a new patient type,
            # private unless they asked for NHS. A tester asking "how much
            # will that cost?" was told it could be checked once registered.
            facts = PatientFacts(patient_type="private")
            on_record = "private"
        else:
            patient = self._patient_row(self._require_verified())
            facts = PatientFacts.from_row(patient)
            on_record = patient["patient_type"]
        # Funding decides the fee, so the model does not get to choose it.
        # A fallback model once sent "private" for an NHS patient who had
        # said nothing about paying privately, and quoted £50 for a £27.90
        # check up. An override now counts only if the caller asked for it.
        if (
            funding
            and funding != on_record
            and self.caller_heard is not None
            and not _funding_was_asked_for(funding, self.caller_heard)
        ):
            funding = None
        funding = funding or on_record
        today = self.now.date()

        if purpose == "check_up":
            # Someone new, or away more than three years, is a new patient,
            # and new NHS places are limited with the waiting list paused.
            # Booking them an NHS check up anyway would promise a place the
            # practice does not have. Urgent NHS care is different: it is
            # open to people with no regular dentist, so it is not stopped.
            if funding == "nhs" and not policies.is_registered(facts, today):
                return {
                    "appointment_type": None,
                    "reason": "new_nhs_places_paused",
                    "say": (
                        "New NHS places are very limited at the moment and the waiting list "
                        "is paused, so I cannot book an NHS check up. I can book a private new "
                        "patient examination, or take your details for the team to call you "
                        "back if NHS places open up."
                    ),
                }
            code = policies.exam_type_code(funding, facts, today)
        elif purpose == "urgent":
            code = policies.emergency_type_code(funding, facts, today)
        elif purpose == "hygiene":
            if funding == "nhs":
                return {
                    "appointment_type": None,
                    "say": (
                        "Hygiene treatment on the NHS needs a dentist to assess you and "
                        "refer you first. I can book you an examination, and we can take "
                        "it from there. Or you are welcome to book privately with a "
                        "hygiene therapist."
                    ),
                }
            code = policies.hygiene_type_code(facts, minutes, today)
        else:
            return {
                "appointment_type": None,
                "say": (
                    "I am not sure which appointment that would be, so let me take a "
                    "message and the team will call you back."
                ),
            }

        type_row = self._type_row(code)
        return {
            "appointment_type": code,
            "name": type_row["name"],
            "duration_minutes": type_row["duration_min"],
            "fee": policies.format_fee(type_row["fee_gbp"], bool(type_row["fee_is_from"])),
            "reason_for_this_type": (
                "treated as a new patient, either never seen here or not in three years"
                if policies.is_new_for_fees(facts, today)
                else "seen within the last three years"
            ),
        }


# A caller saying when suits them for a call back.
_PREFERRED_TIME = re.compile(
    r"\bbetween\b[^.?!]{0,20}\b(?:\d|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b"
    r"|\b\d{1,2}\s?(?:am|pm|a\.m\.|p\.m\.)\b|\b(?:in the |this )?(?:morning|afternoon|evening)\b|\bafter \d",
    re.IGNORECASE,
)

# Someone saying they are not the patient Sophia rang.
_NOT_THE_PATIENT = re.compile(
    r"^\W*(?:no|nope|nah)\b|\bnot (?:here|in|home|available|around|me)\b|\bwrong number\b"
    r"|\b(?:she|he|they)(?:'s| is| are) (?:not|out|away|at work|busy)\b|\bisn'?t (?:here|in|home|available)\b"
    r"|\b(?:her|his|their) (?:husband|wife|son|daughter|partner|mother|father|mum|dad|carer|brother|sister)\b"
    r"|\bnobody (?:by|of|called)\b|\bdoesn'?t live here\b|\bno one (?:by|of|called)\b",
    re.IGNORECASE,
)

# Urgent care is booked only from the day's urgent slots, never ahead.
URGENT_TYPE_CODES = frozenset({"NHS_URGENT", "PRIV_EMERG_NEW", "PRIV_EMERG_EXISTING"})


def _spread_within_day(slots: list[Slot], limit: int) -> list[Slot]:
    """
    A handful of times across one day, each in a different hour.

    Ten minute slots mean a free morning has dozens; reading out 9:00,
    9:10 and 9:20 is no choice at all. Taking the first free slot in each
    hour, then thinning those evenly, covers the morning and the afternoon.
    """
    first_in_hour: dict[tuple[int, int], Slot] = {}
    for slot in slots:
        first_in_hour.setdefault((slot.start.hour, slot.clinician_id), slot)
    by_hour: dict[int, Slot] = {}
    for (hour, _), slot in sorted(first_in_hour.items(), key=lambda item: item[1].start):
        by_hour.setdefault(hour, slot)
    hourly = sorted(by_hour.values(), key=lambda slot: slot.start)
    if len(hourly) <= limit:
        return hourly
    step = (len(hourly) - 1) / (limit - 1) if limit > 1 else 0
    return [hourly[round(index * step)] for index in range(limit)]


def _spread(slots: list[Slot], limit: int) -> list[Slot]:
    """
    Pick a handful of slots that are actually useful to hear read aloud.

    Offering the first five consecutive ten minute slots on one morning is
    useless. This spreads them across different days so the caller gets a
    real choice, and alternates morning and afternoon.

    It used to take the earliest slot on each day. Every result was then
    8am, and a tester's call went: only 8am slots, no 3pm tomorrow, nothing
    between 2 and 5, no lunchtime appointments, all false, because the
    model concluded from five 8am slots that nothing else existed.
    """
    if len(slots) <= limit:
        return slots

    chosen: list[Slot] = []
    seen_days: set[date] = set()
    by_day: dict[date, list[Slot]] = {}
    for slot in slots:
        by_day.setdefault(slot.start.date(), []).append(slot)

    for day, day_slots in by_day.items():
        want_afternoon = len(chosen) % 2 == 1
        pick = next(
            (slot for slot in day_slots if (slot.start.hour >= 12) == want_afternoon),
            day_slots[0],
        )
        chosen.append(pick)
        seen_days.add(day)
        if len(chosen) == limit:
            return chosen

    for slot in slots:
        if slot not in chosen:
            chosen.append(slot)
        if len(chosen) == limit:
            break
    return chosen
