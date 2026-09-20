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

# The few words allowed between a difficulty and the thing that is
# difficult. They may not cross into the next clause, because a held out
# caller said "I can barely eat, but I'm breathing normally" and the gap
# read it as "barely ... breathing" and called an ambulance. Punctuation
# is already stripped by then, so the conjunction is the only boundary
# left to go on.
_GAP = r"((?!but\b|and\b|although\b|though\b|however\b|whereas\b)[\w']+\s+){0,3}"

BREATHING = (
    # The gap allows apostrophes: a parent said "I can't tell if he's
    # breathing normally", and "he's" stopped \w+ from matching.
    rf"{_NOT_ABOUT_SOPHIA.format(struggle=_STRUGGLE)}\s+{_GAP}({_AIRWAY})|"
    r"can'?t catch my breath|short of breath|gasping|"
    r"closing (up|over)|throat.*clos|airway|"
    # Held out cases. "Can't seem to get a proper breath in" put five
    # words between the difficulty and the breath, which is past what the
    # gap allows, and choking had no rule at all.
    r"can'?t (seem to |manage to )?(get|catch|take) (a|my|any)( proper| full| deep)? ?(breath|air)|"
    r"down the wrong way|gone down my windpipe|breathed it in|inhaled it|chok(e|ing)|"
    # Getting worse, rather than already impossible.
    r"(swallow|breath)(e|ing)?\s+(is\s+)?(getting|becoming)\s+(harder|more difficult|difficult)|"
    r"(getting|becoming) (harder|more difficult|difficult) to (swallow|breathe)|"
    # "I can breathe, well, not breathe properly": the correction is
    # the symptom, and no difficulty word appears anywhere in it.
    # Nothing may follow it: "I'm not swallowing my pride" is not an
    # airway. Swallowing a thing, "I can't swallow my own saliva", is
    # caught by the difficulty rule above instead.
    r"\bnot\s+(really\s+|properly\s+)?(able to\s+)?(breathe|breathing|swallow|swallowing)\b"
    r"(?!\s+(my|the|a|an|any|it|that|this|his|her|their)\b)"
)

# Swelling that is moving is an emergency before it reaches the airway,
# which is why a caller can truthfully say breathing is still fine and
# still need to be seen tonight rather than tomorrow.
SPREADING_SWELLING = (
    r"(swell\w*|lump)[^.]{0,60}\bspread\w*\b|"
    r"\bspread\w*\b[^.]{0,40}(down|into|to)( my| the)? (neck|throat|eye|chest)|"
    r"swelling.{0,40}(up to|under|around|near)( my| the)? eye|"
    r"(swell\w*|lump)[^.]{0,60}(getting|growing) (bigger|worse)"
    r"( really| very| pretty| quite| so)? (fast|quickly)|"
    # Swelling in the floor of the mouth pushes the tongue up before it
    # reaches the airway. A held out caller described exactly that while
    # saying she was not choking, and only the word "choking" caught it.
    r"tongue[^.]{0,40}(pushed|lifted|raised|forced) (up|upwards|back)|"
    r"swell\w*[^.]{0,30}under(neath)? (my |the )?tongue|"
    r"under(neath)? (my |the )?tongue[^.]{0,40}swell\w*|"
    r"(floor of my mouth|under my chin)[^.]{0,40}swell\w*"
)

SWELLING = (
    r"swell|swollen|puffed up|blown up|ballooned|"
    r"face is (huge|massive|big)|lump under my (jaw|chin)"
)

EYE = (
    rf"({_STRUGGLE})\s+{_GAP}open\s+(my\s+|one\s+|the\s+)?eye|"
    r"eye (is )?(closed|shut)|eye.*swollen (shut|closed)|"
    r"swollen.*eye|shut my eye|eye has closed"
)

BLEEDING = (
    r"bleeding (that )?(won'?t|will not) stop|won'?t stop bleeding|"
    r"cannot stop the bleeding|can'?t stop the bleeding|"
    r"bleeding heavily|pouring (with )?blood|lots of blood|"
    r"bleeding for (hours|ages)|soaked through|"
    # How a parent describes it: "she's bleeding everywhere".
    r"blood everywhere|bleeding everywhere|covered in blood|bleeding loads|"
    # "Hasn't really stopped" is how a held out caller put it, a day after
    # an extraction, and none of the "won't stop" wordings covered it.
    r"bleed\w*[^.]{0,30}(hasn'?t|has not|still hasn'?t)( really| properly)? stopped|"
    # Bleeding plus feeling faint is blood loss, not anxiety. Feeling
    # faint on its own is left to the urgent rules.
    r"(bleed\w*|blood)[^.]{0,80}(feeling faint|gone faint|light ?headed|"
    r"dizzy when i stand|faint when i stand)|"
    r"(feeling faint|light ?headed|dizzy)[^.]{0,80}(bleed\w*|blood)"
)

# A head injury that is getting worse rather than better. Drowsiness on
# its own means nothing, so it only counts alongside the injury.
# "Fell" on its own is not an injury in a dental call: things fall out of
# teeth. "My filling fell out and I'm so sleepy today" was sent to 999 by
# the first draft, so a fall has to be a fall down, off or over.
DROWSY_AFTER_INJURY = (
    r"(fell (down|off|over)|fall(en)? (down|off|over)|hit|knock\w*|bang\w*|"
    r"accident|injur\w*|smash\w*)"
    r"[^.]{0,170}(sleep(y|ier)|drowsy|hard to (keep|wake)|"
    r"can'?t keep (him|her|them|me) awake|"
    r"keeps? (falling|drifting) (asleep|off)|going in and out of it)"
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
    r"won'?t wake (up|him|her|them)|can'?t\s+(\w+\s+){0,2}wake\s+(him|her|them|my)"
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
    ("drowsy_after_injury", DROWSY_AFTER_INJURY),
    ("spreading_swelling", SPREADING_SWELLING),
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


# A caller who rules a symptom out uses the same words as one who has it.
# A held out case said "I'm not having trouble breathing or swallowing"
# and was sent to 999, because "trouble" sat next to "breathing" and
# nothing read the "not".
#
# Only the difficulty word is removed, and only when it is the one being
# denied, so "no trouble breathing but I can't swallow" still leaves
# "can't swallow" to be caught. Negation is deliberately not handled in
# general: "I'm not able to breathe" must keep working, and a rule that
# blanked everything after a "not" would break it.
# People correct themselves mid sentence, and on the phone the first
# version is usually the dramatic one: "there's blood everywhere, well
# not everywhere, on the tissue". Reading only the first half of that
# sends an ambulance to someone with a stained tissue.
#
# Only words about how much are allowed to be withdrawn this way. A
# retraction of a symptom is left alone on purpose, because "I can
# breathe, well, not breathe properly" corrects towards danger, not away
# from it, and must still be caught.
_RETRACTED_AMOUNT = re.compile(
    r"\b(everywhere|loads|lots|gushing|pouring|streaming|gallons|buckets)\b"
    r"[^.]{0,30}?\bnot\s+\1\b",
    re.IGNORECASE,
)

_DENIED_DIFFICULTY = re.compile(
    r"\b(?:no|not|never)\s+(?:\w+\s+){0,2}"
    r"(?:trouble|difficulty|difficulties|problem|problems|issue|issues|choking)\b",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    """Lower case, collapse whitespace, and strip most punctuation."""
    cleaned = re.sub(r"[^\w\s']", " ", (text or "").lower())
    cleaned = _RETRACTED_AMOUNT.sub(" ", cleaned)
    cleaned = _DENIED_DIFFICULTY.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


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
