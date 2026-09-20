"""
Held out safety cases, written by other people.

The unit tests in `tests/test_safety.py` were written by whoever wrote the
rules, so passing them proves only that the rules do what their author
meant. These sentences came from people who had never seen the rules, the
code or the practice data. They were asked for two kinds: something
seriously wrong where a receptionist should say ring 999, and something
that sounds alarming but is not a 999. They were told to write the way a
real caller talks, and not to look anything up.

`expected` is the outcome NHS guidance gives, not the outcome the writer
predicted. Where the two differ the writer's guess is kept in `note`,
because a case that the public reads as a 999 and the guidance does not is
worth knowing about: it is what a frightened caller will argue with.

Run with `scripts/run_safety_redteam.py`, which reports how many real
emergencies were caught and how many false alarms were raised, and writes
`docs/SAFETY_REDTEAM.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    """One sentence a caller might say, and what should happen to it."""

    ref: str
    said: str
    expected_999: bool
    source: str
    note: str = ""


CASES: tuple[Case, ...] = (
    # ------------------------------------------------------------------
    # Batch 1
    # ------------------------------------------------------------------
    Case(
        ref="A1",
        said=(
            "I can barely breathe properly, my face and neck have blown up "
            "really quickly and I feel like my throat is closing up."
        ),
        expected_999=True,
        source="batch 1",
    ),
    Case(
        ref="A2",
        said=(
            "I got knocked out and smashed my front teeth, I'm bleeding loads "
            "and I'm really confused about what just happened."
        ),
        expected_999=True,
        source="batch 1",
    ),
    Case(
        ref="A3",
        said=(
            "My jaw is completely swollen and I can't swallow my own saliva, "
            "it's just kind of sitting there and I feel really short of breath."
        ),
        expected_999=True,
        source="batch 1",
    ),
    Case(
        ref="A4",
        said=(
            "My little girl fell down the stairs and hit her mouth really "
            "badly, she's bleeding everywhere and she's gone all floppy and "
            "isn't responding properly."
        ),
        expected_999=True,
        source="batch 1",
    ),
    Case(
        ref="A5",
        said=(
            "I've got this massive swelling under my tongue and it's getting "
            "bigger really fast, I can hardly breathe or swallow now."
        ),
        expected_999=True,
        source="batch 1",
    ),
    Case(
        ref="B1",
        said=(
            "This tooth is absolutely killing me, honestly it's the worst pain "
            "I've ever had and I haven't slept at all, can you squeeze me in "
            "today?"
        ),
        expected_999=False,
        source="batch 1",
        note="Severe pain uncontrolled by painkillers is a same day appointment.",
    ),
    Case(
        ref="B2",
        said=(
            "My gum's gone all black around the tooth and it looks horrible, "
            "I'm freaking out a bit but it doesn't actually hurt that much."
        ),
        expected_999=False,
        source="batch 1",
        note="Frightening to look at, no airway or bleeding sign.",
    ),
    Case(
        ref="B3",
        said=(
            "I cracked my tooth eating something and there's a really sharp "
            "bit sticking into my tongue, it's driving me mad, can someone "
            "see me?"
        ),
        expected_999=False,
        source="batch 1",
        note="Broken by eating, not by injury. Same day.",
    ),
    Case(
        ref="B4",
        said=(
            "My face feels really hot and I've got a horrible throbbing "
            "toothache, I've taken painkillers but they're doing absolutely "
            "nothing, I need an appointment."
        ),
        expected_999=False,
        source="batch 1",
        note="Infection signs without breathing difficulty. Same day.",
    ),
    Case(
        ref="B5",
        said=(
            "Sorry, I'm getting really frustrated because I've been on hold "
            "forever and this tooth is making me lose my mind, I just need to "
            "know if you've got anything today."
        ),
        expected_999=False,
        source="batch 1",
        note="Frustration, not a symptom.",
    ),
    # ------------------------------------------------------------------
    # Batch 2. Deliberately close to the line, and several of these say
    # in plain words that breathing is fine, which is the hardest thing
    # for a keyword rule to read correctly.
    # ------------------------------------------------------------------
    Case(
        ref="A6",
        said=(
            "My cheek's swollen right up to under my eye and it's getting "
            "bigger, but I can still breathe and swallow okay, I just feel "
            "really unwell and shaky."
        ),
        expected_999=True,
        source="batch 2",
        note=(
            "Contested. The caller says breathing is fine, so it is not on "
            "the 999 airway list. NHS guidance on a dental abscess sends "
            "swelling that reaches the eye, or a caller who is systemically "
            "unwell, to A and E, which is what the emergency script offers."
        ),
    ),
    Case(
        ref="A7",
        said=(
            "I had a tooth out yesterday and the bleeding hasn't really "
            "stopped, I've changed the gauze loads of times and now I'm "
            "feeling faint when I stand up."
        ),
        expected_999=True,
        source="batch 2",
        note=(
            "Contested. Bleeding after an extraction is normally a same day "
            "appointment. Feeling faint on standing is blood loss, which "
            "moves it."
        ),
    ),
    Case(
        ref="A8",
        said=(
            "My son fell and hit his jaw, he's awake and talking but he's "
            "becoming really sleepy, there's blood in his mouth and I can't "
            "tell if he's breathing normally."
        ),
        expected_999=True,
        source="batch 2",
        note="Increasing drowsiness after a head injury.",
    ),
    Case(
        ref="A9",
        said=(
            "I've got a horrible swelling underneath my jaw that's spreading "
            "down my neck, and swallowing is getting harder, I can still "
            "breathe but it's definitely more difficult than it was an hour "
            "ago."
        ),
        expected_999=True,
        source="batch 2",
        note="Swelling in the floor of the mouth spreading to the neck.",
    ),
    Case(
        ref="A10",
        said=(
            "I think I swallowed part of my tooth while I was eating and now "
            "I'm coughing like something's gone down the wrong way, I can't "
            "seem to get a proper breath in."
        ),
        expected_999=True,
        source="batch 2",
        note="Something inhaled, not swallowed.",
    ),
    Case(
        ref="B6",
        said=(
            "My face is absolutely massive on one side, looks awful in the "
            "mirror, but honestly the pain's not that bad and I'm breathing "
            "and swallowing normally, I just need someone to look at it today."
        ),
        expected_999=False,
        source="batch 2",
        note="Swelling with no airway sign and no spread. Same day.",
    ),
    Case(
        ref="B7",
        said=(
            "I had a tooth pulled this morning and there's still quite a lot "
            "of blood on the tissue every time I check, but I feel completely "
            "fine otherwise, is that normal?"
        ),
        expected_999=False,
        source="batch 2",
        note="Oozing after an extraction, no faintness. Same day advice.",
    ),
    Case(
        ref="B8",
        said=(
            "I woke up and my lip and cheek are so numb I thought something "
            "was seriously wrong, but I had dental work yesterday and there's "
            "no swelling or breathing problem."
        ),
        expected_999=False,
        source="batch 2",
        note="Numbness after treatment needs review, not an ambulance.",
    ),
    Case(
        ref="B9",
        said=(
            "I've got this horrible taste in my mouth and some pus coming "
            "from around the tooth, and the pain is really bad, but I've been "
            "like this since yesterday and I can breathe and swallow normally."
        ),
        expected_999=False,
        source="batch 2",
        note="An abscess without airway involvement. Same day.",
    ),
    Case(
        ref="B10",
        said=(
            "I can't open my mouth properly because my wisdom tooth is "
            "killing me, and I'm starting to panic because I can barely get a "
            "spoon in there, but I'm not having trouble breathing or "
            "swallowing."
        ),
        expected_999=False,
        source="batch 2",
        note=(
            "Says outright that breathing is fine. A rule that reads "
            "'trouble breathing' without the 'not' in front of it will get "
            "this wrong."
        ),
    ),
)


def emergencies() -> tuple[Case, ...]:
    return tuple(case for case in CASES if case.expected_999)


def non_emergencies() -> tuple[Case, ...]:
    return tuple(case for case in CASES if not case.expected_999)
