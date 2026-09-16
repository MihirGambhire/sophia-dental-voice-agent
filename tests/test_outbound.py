"""
Tests for outbound reminder calls.

The property that matters most is not that reminders work, but that
whoever answers learns nothing unless they are the patient. That is
enforced by never giving the model the details until the right patient is
verified, so these tests check what the model is told, not only what it
says.
"""

import json
from datetime import date, time, timedelta
from types import SimpleNamespace

import pytest

from sophia import clock, outbound
from sophia.agent_text import SophiaAgent
from sophia.schemas import OUTBOUND_TOOL_SCHEMAS, TOOL_SCHEMAS
from sophia.tools import ToolError


class Recording:
    """A model stand in that records every request and replies with text."""

    def __init__(self, replies=("Hello.",)):
        self.requests = []
        self.replies = list(replies)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        text = self.replies.pop(0) if self.replies else "Thank you."
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))])


def day_before_reminder(conn):
    row = conn.execute("SELECT id FROM reminder_queue WHERE reason = 'day_before' ORDER BY id LIMIT 1").fetchone()
    return outbound.load(conn, row["id"])


def outbound_agent(conn, reminder, client=None):
    appointment = conn.execute("SELECT start_time FROM appointments WHERE id = ?", (reminder.appointment_id,)).fetchone()
    now = clock.from_db(appointment["start_time"]) - timedelta(hours=20)
    return SophiaAgent(conn, now=now, client=client or Recording(), reminder=reminder)


# ---------------------------------------------------------------------------
# Nothing is known before verification, so nothing can leak
# ---------------------------------------------------------------------------


def test_the_greeting_asks_for_the_patient_and_gives_no_reason(conn):
    reminder = day_before_reminder(conn)
    text = outbound.greeting(reminder)

    assert reminder.patient_name in text
    assert "AI assistant" in text and "recorded" in text
    for revealing in ("appointment", "tomorrow", "check up", "dentist", "booking"):
        assert revealing not in text.lower(), f"the greeting reveals {revealing!r}"


def test_the_model_is_never_told_the_appointment_before_verification(conn):
    """
    Everything sent to the model on the first turn is searched for the
    appointment's day, time and clinician. None of it may be there.
    """
    reminder = day_before_reminder(conn)
    client = Recording()
    agent = outbound_agent(conn, reminder, client)
    agent.say("Hello?")

    appointment = conn.execute(
        "SELECT a.start_time, c.name FROM appointments a JOIN clinicians c ON c.id = a.clinician_id "
        "WHERE a.id = ?", (reminder.appointment_id,)
    ).fetchone()
    start = clock.from_db(appointment["start_time"])
    sent = json.dumps(client.requests[0]["messages"]).lower()

    for secret in (clock.spoken_date(start.date()).lower(), start.strftime("%H:%M"),
                   clock.spoken_time(start.time()).lower(), appointment["name"].split()[-1].lower()):
        assert secret not in sent, f"the model was told {secret!r} before verification"


def test_someone_else_passing_the_identity_check_does_not_count(conn):
    """
    A household member verifying with their own details must not unlock
    the patient's appointment. Refused like any mismatch.
    """
    reminder = day_before_reminder(conn)
    agent = outbound_agent(conn, reminder)
    other = conn.execute("SELECT * FROM patients WHERE id != ? AND code = 'hollis'", (reminder.patient_id,)).fetchone()
    agent.tools.caller_heard.append(f"{other['full_name']} {other['dob']} {other['postcode']}")

    result = agent.tools.verify_patient(other["full_name"], other["dob"], other["postcode"])

    assert not result["verified"]
    assert result["reason"] == "no_match"
    assert agent.tools.verified_patient_id is None


def test_the_patient_themselves_can_be_verified(conn):
    reminder = day_before_reminder(conn)
    agent = outbound_agent(conn, reminder)
    patient = conn.execute("SELECT * FROM patients WHERE id = ?", (reminder.patient_id,)).fetchone()
    agent.tools.caller_heard.append(f"{patient['dob']} {patient['postcode']}")

    assert agent.tools.verify_patient(patient["full_name"], patient["dob"], patient["postcode"])["verified"]


# ---------------------------------------------------------------------------
# Recording the outcome
# ---------------------------------------------------------------------------


def test_the_outcome_tool_is_only_offered_on_outbound_calls(conn):
    inbound = SophiaAgent(conn, now=clock.combine(date(2026, 9, 21), time(10)), client=Recording())
    assert inbound.tool_schemas == TOOL_SCHEMAS

    reminder = day_before_reminder(conn)
    assert outbound_agent(conn, reminder).tool_schemas == TOOL_SCHEMAS + OUTBOUND_TOOL_SCHEMAS


def test_wrong_person_can_be_recorded_without_verification(conn):
    """Recording it reveals nothing, and the patient never came to the phone."""
    reminder = day_before_reminder(conn)
    agent = outbound_agent(conn, reminder)

    assert agent.tools.record_call_outcome("wrong_person")["recorded"]
    row = conn.execute("SELECT status, last_outcome FROM reminder_queue WHERE id = ?", (reminder.id,)).fetchone()
    assert row["status"] == "called" and row["last_outcome"] == "wrong_person"


@pytest.mark.parametrize("outcome", ["confirmed", "cancelled", "rescheduled", "booked_check_up", "declined"])
def test_a_patient_decision_cannot_be_recorded_without_verification(conn, outcome):
    """The call list must never say "confirmed" on the word of whoever answered."""
    reminder = day_before_reminder(conn)
    agent = outbound_agent(conn, reminder)

    with pytest.raises(ToolError, match="not been verified"):
        agent.tools.record_call_outcome(outcome)


def test_an_unknown_outcome_is_refused(conn):
    agent = outbound_agent(conn, day_before_reminder(conn))
    with pytest.raises(ToolError, match="Unknown outcome"):
        agent.tools.record_call_outcome("sort of")


def test_the_outcome_tool_refuses_on_an_inbound_call(conn):
    inbound = SophiaAgent(conn, now=clock.combine(date(2026, 9, 21), time(10)), client=Recording())
    with pytest.raises(ToolError, match="only for outbound"):
        inbound.tools.record_call_outcome("confirmed")


# ---------------------------------------------------------------------------
# The call list
# ---------------------------------------------------------------------------


def test_the_seed_queues_day_before_reminders_for_tomorrows_appointments(conn):
    calls = outbound.pending(conn)
    assert any(call["reason"] == "day_before" and call["appointment"] for call in calls)


def test_starting_a_call_marks_the_reminder_as_called(conn):
    reminder = day_before_reminder(conn)
    outbound.mark_started(conn, reminder)
    row = conn.execute("SELECT status, attempts FROM reminder_queue WHERE id = ?", (reminder.id,)).fetchone()
    assert row["status"] == "called" and row["attempts"] == 1
