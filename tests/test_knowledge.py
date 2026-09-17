"""
Tests for retrieval over the practice documents.

These exist because of a specific failure. Asked for the opening hours,
Sophia answered "nine am until six pm, and on Saturdays from nine am
until one pm", called no tool, and was wrong on three counts. The real
hours are eight to six, weekdays only, closed between one and two.

So the first group of tests below is not about retrieval quality in the
abstract. It pins the exact facts that were invented.
"""

import re

import pytest

from sophia import knowledge


def text_of(result: dict) -> str:
    """
    All retrieved text as one lowercase string with whitespace collapsed.

    The collapsing matters: the source markdown is hard wrapped, so a
    phrase like "failure to attend" can arrive split across a newline.
    """
    joined = " ".join(extract["text"] for extract in result["extracts"])
    return re.sub(r"\s+", " ", joined).lower()


# ---------------------------------------------------------------------------
# The question that was answered wrongly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "what are your opening hours?",
        "when are you open?",
        "what time do you open",
        "what time do you close",
        "are you open on saturday?",
        "what are your hours",
        "when do you shut",
    ],
)
def test_any_way_of_asking_about_hours_finds_the_hours(question):
    result = knowledge.answer(question)
    assert result["found"], f"no passage found for: {question}"
    assert "8am to 6pm" in text_of(result) or "8am" in text_of(result)


def test_the_retrieved_hours_say_eight_not_nine():
    """The invented answer said nine. The documents say eight."""
    body = text_of(knowledge.answer("what are your opening hours?"))
    assert "8am" in body
    assert "9am to 6pm" not in body


def test_the_retrieved_hours_mention_the_lunch_closure():
    """The invented answer left this out entirely."""
    body = text_of(knowledge.answer("what are your opening hours?"))
    assert "1pm and 2pm" in body or "lunch" in body


def test_the_retrieved_hours_say_closed_at_the_weekend():
    """The invented answer claimed Saturday mornings."""
    body = text_of(knowledge.answer("are you open on saturdays?"))
    assert "closed on saturdays" in body or "saturdays and sundays" in body


# ---------------------------------------------------------------------------
# Other things a caller actually asks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question,expected",
    [
        ("where are you?", "museum street"),
        ("what is your address", "warrington"),
        ("is there parking?", "parking"),
        ("what is your phone number", "01925 630221"),
        ("are you taking new nhs patients?", "waiting list"),
        ("how do I join the practice", "private"),
        ("what happens if I miss an appointment", "failure to attend"),
        ("how much notice do I need to cancel", "24 hours"),
        ("do you do implants", "implant"),
        ("what do I do if I have a dental emergency at night", "111"),
        ("how long do I stay registered", "three years"),
    ],
)
def test_common_questions_reach_the_right_document(question, expected):
    result = knowledge.answer(question)
    assert result["found"], f"nothing found for: {question}"
    assert expected in text_of(result), f"{expected!r} missing for: {question}"


def test_the_999_red_flags_are_retrievable():
    body = text_of(knowledge.answer("when should I call 999 for a dental problem"))
    assert "999" in body or "a and e" in body


# ---------------------------------------------------------------------------
# Not knowing has to stay possible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "do you sell hamsters",
        "what is the capital of peru",
        "",
        "   ",
        "the the the and of",
    ],
)
def test_an_unanswerable_question_returns_nothing_rather_than_a_weak_match(question):
    """
    A confidently irrelevant passage is worse than no passage, because it
    invites exactly the invented answer this module exists to prevent.
    """
    result = knowledge.answer(question)
    assert not result["found"]
    assert "message" in result["say"]


# ---------------------------------------------------------------------------
# Shape and cost
# ---------------------------------------------------------------------------


def test_every_practice_document_is_loaded():
    passages = knowledge.load_passages()
    keys = {passage.key for passage in passages}
    assert len(keys) >= 8, f"only loaded {len(keys)} documents"


def test_passages_carry_a_source_so_an_answer_can_be_traced():
    for passage in knowledge.load_passages():
        assert passage.source
        assert passage.key


def test_a_result_stays_inside_a_sensible_token_budget():
    """
    Retrieved text is sent to the model, and the free tier allows 8000
    tokens a minute. A verbose passage is a slow conversation.
    """
    result = knowledge.answer("what are your opening hours and where are you?")
    total = sum(len(extract["text"]) for extract in result["extracts"])
    assert total <= 800, f"retrieved {total} characters, too much for one answer"


def test_results_are_ordered_best_first():
    result = knowledge.answer("what time do you open in the morning")
    assert result["extracts"][0]["heading"]


def test_loading_is_cached_so_a_call_does_not_reread_the_disk():
    assert knowledge.load_passages() is knowledge.load_passages()


def test_a_question_about_something_the_practice_information_never_mentions_is_not_found():
    """
    "Do you accept Bupa?" used to return whichever passage shared a word
    with the question, which invites a confident wrong answer.
    """
    assert knowledge.answer("Do you accept Bupa?")["found"] is False
    assert knowledge.answer("Can I pay with Apple Pay?")["found"] is False


def test_one_unusual_word_among_covered_ones_is_still_answered():
    assert knowledge.answer("are you taking new nhs patients?")["found"] is True


def test_fillings_and_invisalign_reach_the_charges_they_are_mentioned_in():
    filling = " ".join(e["text"] for e in knowledge.answer("How much is a filling?")["extracts"]).lower()
    invisalign = " ".join(e["text"] for e in knowledge.answer("Do you offer Invisalign?")["extracts"]).lower()
    assert "band" in filling
    assert "orthodontic" in invisalign
