"""
Tests for clinical safety screening.

This is the most heavily tested file in the project, and the only one
where the tests are deliberately asymmetric. A false alarm sends someone
to A and E who only needed a dentist, which wastes their evening. A miss
sends someone with a spreading facial infection home to wait until
Thursday. Those are not equivalent, so the tests below check the misses
far harder than the false alarms.

Everything here runs without a model and without a network. That is the
point: the screen must not depend on anything that can have an opinion.
"""

from datetime import date, time
from types import SimpleNamespace

import pytest

from sophia import clock, safety
from sophia.safety import Level


def level_of(text: str) -> Level:
    return safety.screen(text).level


# ---------------------------------------------------------------------------
# 999. Things that must never be offered an appointment.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        # Breathing and swallowing, the NHS red flag
        "my face is swollen and I can't breathe properly",
        "the swelling is so bad I'm having trouble breathing",
        "my throat is closing up",
        "I can't swallow at all",
        "my face has blown up and I'm struggling to breathe",
        "I can't speak properly, my tongue is swollen",
        # Eye
        "my face is swollen and I can't open my eye",
        "the swelling has spread and my eye is closed",
        # Bleeding
        "I've been bleeding for hours and it won't stop",
        "the bleeding won't stop, it's pouring with blood",
        "I can't stop the bleeding from the socket",
        # Head injury
        "I fell and hit my head and now my tooth is broken",
        "I got knocked out and my jaw hurts",
        "I blacked out after the accident",
        "my teeth are broken and I'm seeing double",
        "I banged my head and I've been sick since",
        # Facial injury
        "I think my jaw is broken",
        "I was in a car crash and smashed my face",
        # Allergy
        "I think I'm having an allergic reaction, my lips are swelling and I've got a rash",
    ],
)
def test_these_are_emergencies(said):
    screening = safety.screen(said)
    assert screening.level is Level.EMERGENCY, f"MISSED an emergency: {said!r}"
    assert screening.blocks_booking
    assert screening.matched


def test_the_emergency_script_says_999_and_not_to_drive():
    screening = safety.screen("my face is swollen and I can't breathe")
    assert "999" in screening.say
    assert "not drive" in screening.say
    assert "accident and emergency" in screening.say


def test_the_emergency_script_refuses_to_book():
    """
    Offering an appointment implies the problem can wait. It cannot.
    """
    screening = safety.screen("I can't breathe and my face is swollen")
    assert "not able to book" in screening.say


# ---------------------------------------------------------------------------
# The distinction this project got wrong before checking nhs.uk
# ---------------------------------------------------------------------------


def test_swelling_alone_is_urgent_not_an_emergency():
    """
    The first draft of this project treated any facial swelling as a 999
    call. NHS guidance is more precise: swelling on its own needs to be
    seen today, swelling WITH difficulty breathing or an eye closing is
    an emergency.
    """
    assert level_of("my cheek is quite swollen and it's sore") is Level.URGENT


def test_swelling_plus_breathing_is_an_emergency():
    assert level_of("my cheek is swollen and I can't breathe properly") is Level.EMERGENCY


def test_swelling_plus_an_eye_closing_is_an_emergency():
    assert level_of("my face is swollen and I can't open my eye") is Level.EMERGENCY


def test_the_combination_is_recorded_in_what_matched():
    screening = safety.screen("face is swollen and I am struggling to breathe")
    assert screening.matched


# ---------------------------------------------------------------------------
# Same day, but not 999
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "I'm in agony with my back tooth",
        "the pain is unbearable",
        "painkillers aren't touching it",
        "I've been up all night with the pain",
        "I've got an abscess I think",
        "there's a gum boil on my gum",
        "I knocked my tooth out playing football",
        "I've broken a tooth",
        "my cheek is swollen",
        "it's still bleeding since the extraction yesterday",
        "I think I've got dry socket",
    ],
)
def test_these_need_seeing_today_but_are_not_999(said):
    screening = safety.screen(said)
    assert screening.level is Level.URGENT, f"wrong level for {said!r}"
    assert not screening.blocks_booking, "an urgent problem should still be bookable"


# ---------------------------------------------------------------------------
# Routine. The false alarm side.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "I'd like to book a check up",
        "can I move my appointment to next week",
        "what are your opening hours",
        "how much is a filling",
        "are you taking new NHS patients",
        "I need to cancel Thursday",
        "can I see the hygienist",
        "my daughter needs a check up",
        "do you do implants",
        "where do I park",
        "",
        "   ",
    ],
)
def test_ordinary_calls_are_not_escalated(said):
    assert level_of(said) is Level.ROUTINE, f"false alarm on {said!r}"


def test_a_calm_mention_of_a_past_problem_is_not_escalated():
    """
    Someone describing something resolved should not trigger an ambulance.
    This is the main false alarm risk with keyword matching, so it is
    worth stating plainly that it is accepted where it is unavoidable.
    """
    assert level_of("I had a filling done last year") is Level.ROUTINE
    assert level_of("I'd like a check up, no problems at all") is Level.ROUTINE


# ---------------------------------------------------------------------------
# It should not be foolable
# ---------------------------------------------------------------------------


def test_capitals_and_punctuation_do_not_matter():
    assert level_of("I CAN'T BREATHE!!! MY FACE IS SWOLLEN!!") is Level.EMERGENCY
    assert level_of("i cant breathe, my face is swollen") is Level.EMERGENCY


def test_a_missing_apostrophe_still_matches():
    """Speech to text output is inconsistent about these."""
    assert level_of("i cant breathe and my face is swollen") is Level.EMERGENCY
    assert level_of("the bleeding wont stop") is Level.EMERGENCY


def test_extra_words_around_the_phrase_do_not_hide_it():
    assert level_of(
        "sorry to bother you, it's probably nothing, but I can't really breathe "
        "very well and my whole face has swollen up since last night"
    ) is Level.EMERGENCY


def test_an_emergency_buried_in_a_booking_request_is_still_caught():
    """
    The dangerous case. The caller leads with the ordinary request and
    mentions the serious symptom in passing.
    """
    assert level_of(
        "hi, I'd like to book a check up please, also my face is swollen "
        "and I'm finding it hard to breathe"
    ) is Level.EMERGENCY


# ---------------------------------------------------------------------------
# Medication questions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "what painkiller should I take",
        "can I take ibuprofen with paracetamol",
        "how many paracetamol can I take",
        "should I take antibiotics",
        "is it safe to take co-codamol",
        "what can I take for the pain",
        "can I take some painkillers",
        "could I have a couple of nurofen",
    ],
)
def test_medication_questions_are_recognised(said):
    assert safety.asks_about_medication(said)


@pytest.mark.parametrize(
    "said",
    [
        "I'd like to book a check up",
        "how much is a filling",
        "what time do you open",
        # A husband answering a reminder call said this, and was told Sophia
        # could not advise on medicines.
        "No, she's not in, this is her husband. Can I take a message for her?",
        "can I take the three o'clock slot",
        "should I take my son with me",
    ],
)
def test_ordinary_questions_are_not_medication_questions(said):
    assert not safety.asks_about_medication(said)


def test_the_medication_script_points_somewhere_useful():
    assert "pharmacist" in safety.MEDICATION_SCRIPT
    assert "111" in safety.MEDICATION_SCRIPT


def test_a_medication_question_is_not_by_itself_an_emergency():
    """It changes what Sophia says, not how urgent the problem is."""
    assert level_of("what painkiller should I take") is Level.ROUTINE


# ---------------------------------------------------------------------------
# The screen runs before the model, and cannot be overridden
# ---------------------------------------------------------------------------


class NeverCalled:
    """A client that fails the test if the model is consulted at all."""

    def __init__(self):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        raise AssertionError(
            "the model was consulted during an emergency, it must not be"
        )


def test_an_emergency_never_reaches_the_model(conn):
    """
    The whole point of screening first. A caller who cannot breathe is not
    waiting on a model round trip, and no model gets the chance to decide
    otherwise.
    """
    from sophia.agent_text import SophiaAgent

    agent = SophiaAgent(
        conn,
        now=clock.combine(date(2026, 9, 21), time(10, 0)),
        client=NeverCalled(),
    )
    turn = agent.say("my face is swollen and I can't breathe")

    assert turn.escalated_without_model
    assert turn.safety_level == Level.EMERGENCY.value
    assert "999" in turn.reply
    assert turn.tool_calls == []


def test_an_emergency_response_is_effectively_instant(conn):
    """No network round trip means no wait."""
    from sophia.agent_text import SophiaAgent

    agent = SophiaAgent(
        conn,
        now=clock.combine(date(2026, 9, 21), time(10, 0)),
        client=NeverCalled(),
    )
    turn = agent.say("I've been bleeding for hours and it won't stop")

    assert turn.latency_seconds < 0.5


def test_a_routine_call_does_reach_the_model(conn):
    """The screen must not block ordinary calls."""
    from sophia.agent_text import SophiaAgent

    class Answers:
        def __init__(self):
            self.called = False
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        def _create(self, **kwargs):
            self.called = True
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Of course.", tool_calls=None)
                    )
                ]
            )

    client = Answers()
    agent = SophiaAgent(
        conn, now=clock.combine(date(2026, 9, 21), time(10, 0)), client=client
    )
    turn = agent.say("I'd like to book a check up")

    assert client.called
    assert not turn.escalated_without_model
    assert turn.safety_level == Level.ROUTINE.value


def test_an_urgent_call_reaches_the_model_and_is_flagged_to_it(conn):
    """
    Urgent is not blocked, because an urgent problem still wants an
    appointment. But the state block tells the model to offer today's
    urgent slots rather than a routine check up.
    """
    from sophia.agent_text import SophiaAgent

    captured = {}

    class Capturing:
        def __init__(self):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        def _create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Let me look.", tool_calls=None)
                    )
                ]
            )

    agent = SophiaAgent(
        conn, now=clock.combine(date(2026, 9, 21), time(10, 0)), client=Capturing()
    )
    turn = agent.say("I'm in agony, painkillers aren't touching it")

    assert turn.safety_level == Level.URGENT.value
    assert not turn.escalated_without_model

    state = captured["messages"][-1]["content"]
    assert "same day" in state
    assert "urgent" in state.lower()


# ---------------------------------------------------------------------------
# The medicine rule, enforced on what Sophia is about to say
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "advice",
    [
        "You could take ibuprofen for that.",
        "I'd recommend taking paracetamol.",
        "Take two tablets every four hours.",
        "The usual dose is 400mg.",
        "You can have up to eight tablets a day.",
        "I would suggest some co-codamol.",
    ],
)
def test_medicine_advice_in_a_reply_is_replaced_with_a_referral(advice):
    result = safety.enforce_medication_rule(advice, caller_asked=True)
    assert result == safety.MEDICATION_SCRIPT


def test_a_refusal_that_names_a_medicine_is_left_alone():
    """Naming a medicine to refuse advice about it is not advice."""
    reply = "I'm not able to advise on ibuprofen or paracetamol. A pharmacist can help."
    assert safety.enforce_medication_rule(reply, caller_asked=True) is None


def test_a_medicine_question_without_a_referral_gets_one_added():
    reply = "I'm sorry, I can't help with that."
    result = safety.enforce_medication_rule(reply, caller_asked=True)
    assert result.startswith("I'm sorry, I can't help with that.")
    assert "pharmacist" in result and "111" in result


def test_an_ordinary_reply_to_an_ordinary_question_is_untouched():
    assert safety.enforce_medication_rule("We open at 8am.", caller_asked=False) is None


def test_the_rule_is_applied_by_the_agent_before_the_caller_hears_it(conn):
    from sophia.agent_text import SophiaAgent

    class Advises:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="You should take ibuprofen, 400mg every eight hours.", tool_calls=None))])

    agent = SophiaAgent(conn, now=clock.combine(date(2026, 9, 21), time(10, 0)), client=Advises())
    turn = agent.say("What painkiller should I take?")

    assert turn.medication_rule_applied
    assert "400" not in turn.reply
    assert "pharmacist" in turn.reply
    assert agent.messages[-1]["content"] == turn.reply
