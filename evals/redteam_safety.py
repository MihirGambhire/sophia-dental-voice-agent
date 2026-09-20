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
)


def emergencies() -> tuple[Case, ...]:
    return tuple(case for case in CASES if case.expected_999)


def non_emergencies() -> tuple[Case, ...]:
    return tuple(case for case in CASES if not case.expected_999)
