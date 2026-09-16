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
_STRUGGLE = (
    r"can'?t|cannot|couldn'?t|could not|hard to|difficult to|difficulty|"
    r"struggling to|struggle to|trouble|unable to|barely"
)
_AIRWAY = r"breathe|breathing|breath|swallow|swallowing|speak|speaking|talk"

BREATHING = (
    rf"({_STRUGGLE})\s+(\w+\s+){{0,3}}({_AIRWAY})|"
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
    r"bleeding for (hours|ages)|soaked through"
)

HEAD_INJURY = (
    r"knocked out|passed out|blacked out|lost consciousness|unconscious|"
    r"hit my head|banged my head|head injury|"
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

# Anything about medicines, which Sophia must never advise on.
MEDICATION = (
    r"what painkiller|which painkiller|how many (paracetamol|ibuprofen|pills|tablets)|"
    r"can i take|should i take|antibiotic|amoxicillin|"
    r"is it safe to take|mix.*(paracetamol|ibuprofen)|overdose"
)


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
