"""
Tests for the evaluation suite itself, run offline.

The live suite reports pass rates and says which layer each failure came
from. If that attribution were wrong, the report would send someone to fix
the prompt when the retrieval was broken, which is exactly the mistake the
suite exists to prevent. So the attribution is tested like any other logic.
"""

import json
import sys
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import framework  # noqa: E402
from evals.framework import (  # noqa: E402
    GENERATION,
    RETRIEVAL,
    TOOL_LOGIC,
    CheckResult,
    Conversation,
    RunResult,
    explain_missing_tool_outcome,
    reply_mentions,
    reply_never_mentions,
)
from evals.scenarios import SCENARIOS  # noqa: E402
from sophia import clock, db  # noqa: E402
from sophia.agent_text import SophiaAgent, TurnRecord  # noqa: E402


def conversation_with(conn, turns):
    agent = SophiaAgent(conn, now=clock.combine(date(2026, 9, 21), time(10)), client=SimpleNamespace())
    return Conversation(agent=agent, conn=conn, turns=turns)


def turn(reply, calls=()):
    record = TurnRecord(user="caller", reply=reply)
    for name, args, result in calls:
        record.tool_calls.append((name, args))
        record.tool_results.append((name, result))
    return record


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_a_fact_in_a_tool_result_but_not_the_reply_is_a_generation_failure(conn):
    conversation = conversation_with(conn, [
        turn("We're open most days.", [("search_practice_info", {}, {"extracts": [{"text": "closed 1pm to 2pm for lunch"}]})]),
    ])
    result = reply_mentions("mentions lunch", r"lunch")(conversation)
    assert not result.passed
    assert result.layer == GENERATION


def test_a_fact_no_tool_returned_is_a_retrieval_failure(conn):
    conversation = conversation_with(conn, [
        turn("We're open most days.", [("search_practice_info", {}, {"found": False})]),
    ])
    result = reply_mentions("mentions lunch", r"lunch")(conversation)
    assert result.layer == RETRIEVAL


def test_answering_without_any_tool_is_a_generation_failure(conn):
    conversation = conversation_with(conn, [turn("We open at nine.")])
    result = reply_mentions("opens at 8", r"\b8\b")(conversation)
    assert result.layer == GENERATION


def test_a_booking_tool_never_called_is_generation(conn):
    conversation = conversation_with(conn, [turn("That's all booked.")])
    assert explain_missing_tool_outcome(conversation, "book_appointment")[0] == GENERATION


def test_a_booking_tool_that_refused_is_tool_logic_and_quotes_the_refusal(conn):
    conversation = conversation_with(conn, [
        turn("Sorry.", [("book_appointment", {}, {"booked": False, "reason": "just_taken"})]),
    ])
    layer, why = explain_missing_tool_outcome(conversation, "book_appointment")
    assert layer == TOOL_LOGIC
    assert "just_taken" in why


def test_never_mentions_reports_the_offending_turn(conn):
    conversation = conversation_with(conn, [turn("Hello."), turn("That is £50.")])
    result = reply_never_mentions("no £50", r"£\s?50")(conversation)
    assert not result.passed
    assert "turn 2" in result.detail


# ---------------------------------------------------------------------------
# The scenarios are well formed
# ---------------------------------------------------------------------------


def test_scenario_ids_are_unique_and_every_scenario_checks_something():
    ids = [scenario.id for scenario in SCENARIOS]
    assert len(ids) == len(set(ids))
    for scenario in SCENARIOS:
        assert scenario.checks, f"{scenario.id} checks nothing"
        assert scenario.turns, f"{scenario.id} says nothing"


def test_every_scenario_clock_resolves_against_a_fresh_database(tmp_path):
    """Pinned times depend on the seed, so they must work on any day the suite runs."""
    conn = db.reset_database(tmp_path / "clock.db")
    try:
        for scenario in SCENARIOS:
            if scenario.setup:
                scenario.setup(conn)
            now = scenario.at(conn)
            assert now.tzinfo is not None, scenario.id
    finally:
        conn.close()


def test_time_sensitive_scenarios_are_where_their_rule_says():
    lookup = {scenario.id: scenario for scenario in SCENARIOS}
    early = lookup["urgent_before_release"].at(None)
    assert early.time() < time(8, 0)
    assert clock.is_open_day(early.date())


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_the_report_states_rates_layers_and_transcripts(tmp_path):
    scenario = SCENARIOS[0]
    good = RunResult(scenario, [CheckResult("a", True)], [turn("fine")])
    bad = RunResult(
        scenario,
        [CheckResult("b", False, "the fact was in a tool result but not in the reply", GENERATION)],
        [turn("wrong", [("get_fee", {}, {"fee": "£27.90"})])],
    )
    out = tmp_path / "report.md"
    text = framework.write_report([good, bad], out, "gemini", "model-x", repeat=2)

    assert out.exists()
    assert "1 of 2" in text
    assert "| generation | 1 |" in text
    assert "Transcript:" in text
    assert "Sophia: wrong" in text


# ---------------------------------------------------------------------------
# Lessons from the first live run
# ---------------------------------------------------------------------------


def test_a_hyphenated_spoken_fee_counts_as_quoting_it(conn):
    """
    The first live run failed Sophia for saying "twenty-seven pounds
    ninety", and attributed it to the model. The fault was the pattern.
    """
    scenario = {s.id: s for s in SCENARIOS}["book_nhs_checkup"]
    fee_check = next(check for check in scenario.checks if check.__name__ == "quotes £27.90")
    conversation = conversation_with(conn, [turn("That's booked, and the fee is twenty-seven pounds ninety.")])
    assert fee_check(conversation).passed


def test_rate_limit_errors_are_recognised_and_the_requested_wait_is_used():
    error = "ClientError: 429 RESOURCE_EXHAUSTED ... 'retryDelay': '34s'"
    assert framework.is_rate_limited(error)
    assert framework.retry_delay_seconds(error) == 36
    assert framework.retry_delay_seconds("429 please retry in 12.5s") == 14.5
    assert not framework.is_rate_limited("ValueError: something else")


def test_rate_limited_runs_are_excluded_from_the_pass_rate(tmp_path):
    scenario = SCENARIOS[0]
    passed = RunResult(scenario, [CheckResult("a", True)], [turn("fine")])
    limited = RunResult(scenario, [], [], error="ClientError: 429 RESOURCE_EXHAUSTED")
    text = framework.write_report([passed, limited], tmp_path / "r.md", "gemini", "m", repeat=2)
    assert "**1 of 1**" in text
    assert "could not complete because the free tier rate" in text
