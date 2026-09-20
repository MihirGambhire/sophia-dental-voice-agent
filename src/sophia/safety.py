"""
Clinical safety screening.

This runs on what the caller says, before the model gets a chance to
decide anything. It is the one part of Sophia that is not allowed to be
clever.

WHY IT IS DETERMINISTIC
=======================
Everywhere else in this project, the model interprets and the code
enforces. Here the code interprets too, because the cost of being wrong
is not a wasted appointment, it is somebody with a spreading facial
infection being offered a slot on Thursday.

A keyword rule has a failure mode you can enumerate and test. A model has
one you cannot. So the rules below run first, and they are allowed to be
over cautious: sending someone to 999 who only needed a dentist wastes an
ambulance call, and the opposite can kill. Those are not symmetric, and
the thresholds here reflect that.

The model still sees the call afterwards and can escalate further. It can
never de-escalate. Nothing in this file can be talked out of by the
caller or by the model.

THE RED FLAGS
=============
Taken from NHS guidance on finding an emergency dentist and on dental
abscess, checked 16 September 2026. The draft list in this project before
that check was wrong in two places, which is recorded in the engineering
log.

The NHS wording is careful and this module follows it: swelling alone is
urgent, swelling **together with** difficulty breathing or difficulty
opening an eye is an emergency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from . import config


class Level(str, Enum):
    """How serious this is, in the order we check for it."""

    EMERGENCY = "emergency_999"
    URGENT = "urgent_same_day"
    ROUTINE = "routine"


@dataclass(frozen=True)
class Screening:
    """The outcome of screening one thing the caller said."""

    level: Level
    matched: tuple[str, ...] = ()
    say: str = ""

    @property
    def is_emergency(self) -> bool:
        return self.level is Level.EMERGENCY

    @property
    def blocks_booking(self) -> bool:
        """
        True when no appointment should be offered at all.

        An emergency needs an ambulance, not a slot on Thursday, and
        offering one implies the problem can wait.
        """
        return self.level is Level.EMERGENCY


# ---------------------------------------------------------------------------
# What the caller might actually say
# ---------------------------------------------------------------------------
#
# These are phrased the way a frightened person on the phone says them,
# not the way a textbook does. "Can't breathe" and "face is massive", not
# "dyspnoea" and "facial oedema".

# The words in between matter. An earlier version of this required
# "can't" and "breathe" to be adjacent, and so missed "I can't really
# breathe very well", downgrading a 999 call to an ordinary urgent one.
# Up to three words are now allowed between the difficulty and the thing
# being difficult, because that is how people actually speak when they
# are frightened and playing it down.
#
# "hardly" was added after a held out case: "I can hardly breathe or
# swallow now". "Can't" was covered and "can hardly" was not, so a swelling
# under the tongue closing an airway screened as an ordinary urgent call.
_STRUGGLE = (
    r"can'?t|cannot|couldn'?t|could not|hard to|difficult to|difficulty|"
    r"struggling to|struggle to|trouble|unable to|barely|hardly"
)
# Speaking or talking counts unless it is about being busy: "I can't talk
# right now, I'm at work" is not an airway problem.
_BUSY = r"(?!\s+(?:right\s+)?now|\s+at\s+the\s+moment|\s+at\s+work|\s+for\s+long)"
_AIRWAY = rf"breathe|breathing|breath|swallow|swallowing|speak{_BUSY}|speaking{_BUSY}|talk{_BUSY}"

# A tester, annoyed with Sophia, said "why can't you just talk?" and was
# told to ring 999. A struggle aimed at her, "can't you" or "you can't",
# is not the caller's airway. Anything the caller says of themselves still
# counts, however it is worded.
_NOT_ABOUT_SOPHIA = r"(?<!\byou )(?:{struggle})(?!\s+you\b)"

BREATHING = (
    rf"{_NOT_ABOUT_SOPHIA.format(struggle=_STRUGGLE)}\s+(\w+\s+){{0,3}}({_AIRWAY})|"
    r"can'?t catch my breath|short of breath|gasping|"
    r"closing (up|over)|throat.*clos|airway"
)

SWELLING = (
    r"swell|swollen|puffed up|blown up|ballooned|"
    r"face is (huge|massive|big)|lump under my (jaw|chin)"
)

EYE = (
    rf"({_STRUGGLE})\s+(\w+\s+){{0,3}}open\s+(my\s+|one\s+|the\s+)?eye|"
    r"eye (is )?(closed|shut)|eye.*swollen (shut|closed)|"
    r"swollen.*eye|shut my eye|eye has closed"
)

BLEEDING = (
    r"bleeding (that )?(won'?t|will not) stop|won'?t stop bleeding|"
    r"cannot stop the bleeding|can'?t stop the bleeding|"
    r"bleeding heavily|pouring (with )?blood|lots of blood|"
    r"bleeding for (hours|ages)|soaked through|"
    # How a parent describes it: "she's bleeding everywhere".
    r"blood everywhere|bleeding everywhere|covered in blood|bleeding loads"
)

# Someone who is not responding needs an ambulance whoever is describing
# them, and it is usually a parent describing a child rather than the
# caller describing themselves.
#
# "Not responding" on its own is far too loose: a probe of this rule sent
# "the website is not responding when I try to book" and "you are not
# responding properly" to 999. A person has to be the subject, so the
# phrase only counts within a few words of someone being talked about.
_PERSON = (
    r"(?:he|she|they|him|her|his|them|baby|child|kid|son|daughter|girl|boy|"
    r"mum|mother|dad|father|wife|husband|partner|gran|nan|grandad|patient)"
)
_UNRESPONSIVE = (
    rf"\b{_PERSON}(?:'?s)?\b(?:\s+\w+){{0,5}}\s+(?:isn'?t|is not|not|won'?t)\s+respond(ing|ed)?|"
    rf"\b{_PERSON}(?:'?s)?\b(?:\s+\w+){{0,5}}\s+unresponsive|"
    r"gone (all )?floppy|floppy and|"
    r"won'?t wake (up|him|her|them)|can'?t wake (him|her|them|my)"
)

HEAD_INJURY = (
    rf"knocked out|passed out|blacked out|lost consciousness|unconscious|{_UNRESPONSIVE}|"
    # A parent rings about a child, so the possessive is not always "my".
    r"hit (my|his|her|their|the) head|banged (my|his|her|their|the) head|head injury|"
    r"(been|being) sick.*(after|since).*(hit|fall|accident)|"
    r"double vision|seeing double|vision.*blurr"
)

FACIAL_INJURY = (
    r"broken jaw|jaw (is )?broken|fractured jaw|jaw.*dislocat|"
    r"car (crash|accident)|smashed my face|face.*smashed"
)

ALLERGY = (
    r"allergic reaction|anaphyla|throat.*swelling.*rash|"
    r"lips.*swelling.*rash|covered in (a )?rash"
)

# Urgent, meaning it should be seen today, but it is not a 999 call.
URGENT_PATTERNS = (
    r"agony|excruciating|unbearable|worst pain|severe pain|"
    r"pain.*(keeping|kept) me (up|awake)|can'?t sleep.*pain|"
    r"(up|awake) all night|all night with the pain|kept me (up|awake)|"
    r"painkillers.*(not|aren'?t|don'?t).*(work|touch|help)|"
    r"nothing.*touch(ing)? the pain|"
    r"knocked (out|my tooth out)|tooth.*knocked out|"
    r"brok(e|en)\s+(a\s+|my\s+|another\s+)?tooth|tooth.*brok(e|en)|"
    r"crack(ed)?\s+(a\s+|my\s+)?tooth|chipped\s+(a\s+|my\s+)?tooth|"
    r"abscess|absess|gum boil|pus|"
    r"bleeding.*(after|since).*(extraction|taken out)|"
    r"dry socket|"
    r"face.*swollen|swollen face|swollen cheek|swollen gum"
)

_MEDICINES = r"ibuprofen|paracetamol|aspirin|co-?codamol|codeine|naproxen|nurofen|amoxicillin|antibiotics?"

# Anything about medicines, which Sophia must never advise on.
#
# "can i take" used to match on its own. Harmless while nothing acted on it,
# but once the medicine rule started adding a referral to replies, a husband
# answering a reminder call with "can I take a message for her?" was told
# Sophia could not advise on medicines, and could help "with an
# appointment", on a call that must not say what it is about. It failed all
# three runs of that scenario. "take" now has to be followed, within a few
# words, by something that is a medicine.
_MEDICINE_WORDS = rf"(?:{_MEDICINES}|pain ?killers?|pills?|tablets?|medicines?|medication)"
MEDICATION = (
    r"what painkiller|which painkiller|how many (paracetamol|ibuprofen|pills|tablets)|"
    rf"\b(?:can|should|could) i (?:take|have)(?:\s+\w+){{0,3}}\s+{_MEDICINE_WORDS}\b|"
    r"\bwhat (?:can|should|could) i take\b|\btake\b.{0,25}\bfor (?:the|my) (?:pain|toothache)|"
    r"antibiotic|amoxicillin|is it safe to take|mix.*(paracetamol|ibuprofen)|overdose"
)

# Medicine advice in Sophia's own reply: a dose, a schedule, or a
# recommendation of a named medicine. Checked on what she is about to say,
# not on what the caller asked, so it catches advice however it arose.
DOSE_ADVICE = (
    r"\b\d+\s?(?:mg|milligrams?)\b|"
    r"\btake (?:one|two|three|four|1|2|3|4) (?:tablets?|pills?|capsules?)|"
    r"\bevery (?:four|six|eight|4|6|8) hours\b|"
    r"\bmaximum (?:of|dose)\b|\bup to (?:\d+|four|eight) (?:tablets|times)\b|"
    rf"\b(?:you (?:can|could|should|might) (?:try |take |use |have )|"
    rf"i(?:'d| would) (?:suggest|recommend) (?:taking |trying )?)(?:some )?(?:{_MEDICINES})\b"
)

REFERRAL = r"pharmacist|\b111\b"


EMERGENCY_RULES: tuple[tuple[str, str], ...] = (
    ("breathing_or_swallowing", BREATHING),
    ("eye_closing", EYE),
    ("uncontrolled_bleeding", BLEEDING),
    ("head_injury", HEAD_INJURY),
    ("facial_injury", FACIAL_INJURY),
    ("allergic_reaction", ALLERGY),
)


EMERGENCY_SCRIPT = (
    f"I need to stop you there, because from what you are describing you need "
    f"emergency help rather than a dental appointment. Please ring "
    f"{config.EMERGENCY_NUMBER} now, or get to your nearest accident and emergency "
    f"department. Please do not drive yourself, ask someone to take you or call an "
    f"ambulance. I am not able to book you an appointment for this, it needs to be "
    f"seen straight away."
)

MEDICATION_SCRIPT = (
    "I am not able to give advice about medicines, I am sorry. A pharmacist can help "
    f"with that today, or you can ring NHS {config.NHS_URGENT_NUMBER} for advice. "
    "I can still help you with an appointment if you would like one."
)


def _matches(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def _normalise(text: str) -> str:
    """Lower case, collapse whitespace, and strip most punctuation."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", (text or "").lower())).strip()


def screen(text: str) -> Screening:
    """
    Decide how serious what the caller just said is.

    Order matters. Emergencies are checked first and win outright, then
    the swelling combination rule, then same day urgency. Anything else is
    routine.
    """
    cleaned = _normalise(text)
    if not cleaned:
        return Screening(level=Level.ROUTINE)

    matched: list[str] = []

    for name, pattern in EMERGENCY_RULES:
        if _matches(pattern, cleaned):
            matched.append(name)

    # The NHS distinction this project got wrong at first: swelling on its
    # own is urgent, swelling together with difficulty breathing or with
    # an eye closing is an emergency. The breathing and eye rules above
    # already catch the second half, so this adds the combination itself.
    if _matches(SWELLING, cleaned) and (
        _matches(BREATHING, cleaned) or _matches(EYE, cleaned)
    ):
        matched.append("swelling_with_breathing_or_eye")

    if matched:
        return Screening(
            level=Level.EMERGENCY,
            matched=tuple(dict.fromkeys(matched)),
            say=EMERGENCY_SCRIPT,
        )

    if _matches(URGENT_PATTERNS, cleaned) or _matches(SWELLING, cleaned):
        return Screening(level=Level.URGENT, matched=("same_day_symptom",))

    return Screening(level=Level.ROUTINE)


def asks_about_medication(text: str) -> bool:
    """
    True if the caller is asking what to take.

    Kept separate from screening, because it changes what Sophia says but
    not how urgent the problem is. Someone can ask about painkillers in
    the middle of an entirely routine call.
    """
    return _matches(MEDICATION, _normalise(text))


def enforce_medication_rule(reply: str, caller_asked: bool) -> str | None:
    """
    Make sure Sophia's reply gives no medicine advice. None means no change.

    Until this existed, the only thing stopping medicine advice was a line
    in the prompt, which contradicts the rule the whole project runs on:
    anything that matters is enforced in code. Two cases:

      the reply contains a dose, a schedule, or recommends a named medicine
          -> replaced entirely with the scripted refusal and referral
      the caller asked about medicine and the reply does not point them to
          a pharmacist or 111 -> the referral is added

    Matching is on Sophia's own words, so a refusal that happens to name a
    medicine ("I can't advise on ibuprofen") passes, while "you could take
    ibuprofen" does not.
    """
    text = reply or ""
    if re.search(DOSE_ADVICE, text, re.IGNORECASE):
        return MEDICATION_SCRIPT
    if caller_asked and not re.search(REFERRAL, text, re.IGNORECASE):
        return f"{text.rstrip()} {MEDICATION_SCRIPT}".strip()
    return None
