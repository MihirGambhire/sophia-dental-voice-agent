"""
The safety screen against cases it was not written for.

`tests/test_safety.py` was written alongside the rules, by the same person,
so it proves the rules do what their author intended and nothing more.
These cases came from people who had never seen the code. The first batch
of ten found two misses: "I can hardly breathe" was not covered because
only "can't" was, and a parent describing an unresponsive child matched no
rule at all.

Missing an emergency fails the build. A false alarm is reported by
`scripts/run_safety_redteam.py` rather than failed here, because the screen
is meant to be over cautious and the right number is a judgement, not zero.
"""

from __future__ import annotations

import pytest

from evals.redteam_safety import emergencies, non_emergencies
from sophia import safety


@pytest.mark.parametrize("case", emergencies(), ids=lambda case: case.ref)
def test_every_held_out_emergency_is_caught(case):
    screening = safety.screen(case.said)
    assert screening.level is safety.Level.EMERGENCY, (
        f"{case.ref} from {case.source} needed 999 and screened as "
        f"{screening.level.value}: {case.said}"
    )


@pytest.mark.parametrize("case", non_emergencies(), ids=lambda case: case.ref)
def test_held_out_false_alarms(case):
    screening = safety.screen(case.said)
    assert screening.level is not safety.Level.EMERGENCY, (
        f"{case.ref} from {case.source} was sent to 999 and should not have "
        f"been: {case.said}"
    )


@pytest.mark.parametrize(
    "said",
    [
        "you are not responding properly",
        "why are you not responding to what I am saying",
        "the website is not responding when I try to book",
        "I can hardly wait for my appointment",
        "my husband is not answering his phone, can you ring me instead",
        # The drowsiness rule read "my filling fell out" as a fall.
        "my filling fell out and I'm so sleepy today, sorry",
        "my crown fell off and I fell asleep waiting on hold",
        "can I have a later appointment, I'm always sleepy in the mornings",
        # Ruling a symptom out reads like having it, without the negation.
        "I'm not having trouble breathing or swallowing",
        "no difficulty breathing, I just need an appointment",
        "I'm breathing and swallowing normally",
        # Words the new rules use, in ordinary sentences.
        "I need to spread the payments out over a few months",
        "the swelling has gone down a lot since Friday",
        "I felt a bit faint when I saw the needle last time",
        "I've had a bit of bleeding from the gum when I brush",
    ],
)
def test_wording_that_looks_alarming_but_is_not(said):
    """Every one of these was sent to 999 by a first draft of some rule."""
    assert safety.screen(said).level is not safety.Level.EMERGENCY


@pytest.mark.parametrize(
    "said",
    [
        "my boy banged his head and now he's getting really drowsy",
        "she fell down the stairs and she keeps drifting off",
        "the swelling is spreading down my neck now",
        "I can't get a proper breath in",
        "I think it went down the wrong way and I'm choking",
        "I've been bleeding since the extraction and now I'm light headed",
        # The negation strip must not silence a real symptom beside it.
        "no trouble breathing but I can't swallow at all",
    ],
)
def test_the_wordings_the_held_out_cases_added(said):
    assert safety.screen(said).level is safety.Level.EMERGENCY
