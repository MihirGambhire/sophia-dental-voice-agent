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
    ],
)
def test_not_responding_about_anything_but_a_person_is_not_999(said):
    """The first draft of the unresponsive rule sent all of these to 999."""
    assert safety.screen(said).level is not safety.Level.EMERGENCY
