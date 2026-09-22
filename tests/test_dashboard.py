"""
Tests for the practice team page.

The page is only worth showing if it is true: urgent messages flagged by
the same rules as the calls, summaries built from what happened, and the
calendar showing what Sophia actually booked and cancelled.
"""

from datetime import date, time

import pytest

from sophia import clock, dashboard
from sophia.agent_text import SophiaAgent
from sophia.tools import SophiaTools

# Anchored on the next open day from today, not a fixed date. The seed
# data is built relative to the day the database is created, so a
# hardcoded anchor goes stale the moment the calendar passes it, and
# every urgent slot test starts failing on a day nobody changed code.
WEEKDAY_10 = clock.combine(clock.next_open_day(clock.today()), time(10))


@pytest.mark.parametrize(
    "reason, detail, words, urgency, assigned",
    [
        ("Questions about treatments", None, ["tell them I have a swollen face"], "urgent", dashboard.DUTY_DENTIST),
        ("Wants a call back", None, ["my face is swollen and I can't breathe"], "emergency", dashboard.DUTY_DENTIST),
        ("Is a filling painful?", None, [], "routine", dashboard.DENTIST),
        ("Wants to make a complaint about a bill", None, [], "routine", dashboard.PRACTICE_MANAGER),
        ("Asked about NHS waiting list", None, [], "routine", dashboard.RECEPTION),
    ],
)
def test_messages_are_triaged_by_rules(reason, detail, words, urgency, assigned):
    """A tester mentioned a swollen face while leaving a message; it must reach a dentist today."""
    assert dashboard.triage_message(reason, detail, words) == (urgency, assigned)


def test_a_message_is_stored_with_its_urgency_and_owner(conn):
    session = SophiaTools(conn, now=WEEKDAY_10)
    session.caller_heard = ["Mihir, 07700 900123", "tell them I have a swollen face"]
    session.take_message("Mihir Gambhire", "Questions about treatments", "07700 900123")
    row = conn.execute("SELECT urgency, assigned_to FROM messages").fetchone()
    assert (row["urgency"], row["assigned_to"]) == ("urgent", dashboard.DUTY_DENTIST)


def test_a_cancellation_is_marked_as_made_by_sophia(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'okafor'").fetchone()
    session = SophiaTools(conn, now=WEEKDAY_10)
    session.verify_patient(row["full_name"], row["dob"], row["postcode"])
    appointment = conn.execute(
        "SELECT id FROM appointments WHERE patient_id = ? AND status = 'booked' AND start_time > ?",
        (row["id"], clock.to_db(WEEKDAY_10)),
    ).fetchone()
    session.cancel_appointment(appointment["id"], caller_confirmed=True)

    assert conn.execute("SELECT cancelled_via FROM appointments WHERE id = ?", (appointment["id"],)).fetchone()[0] == "sophia"
    assert session.cancellations_made
    view = dashboard.team_view(conn, WEEKDAY_10)
    assert any(a["kind"] == "cancelled" and "Daniel Okafor" in a["text"] for a in view["actions"])


class _Client:
    def __init__(self):
        from types import SimpleNamespace
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_: None))


def test_a_call_summary_is_built_from_what_happened(conn):
    agent = SophiaAgent(conn, now=WEEKDAY_10, client=_Client())
    agent.tools.caller_heard = ["Priya Sharma", "07700 900123", "my tooth is aching"]
    agent.tools.caller_name = "Priya Sharma"
    agent.tools.take_message("Priya Sharma", "Toothache question", "07700 900123")

    from sophia.agent_text import TurnRecord
    agent.history.append(TurnRecord(user="my tooth is aching", reply="I have taken that down.",
                                    tool_calls=[("take_message", {})], safety_level="routine"))
    summary = dashboard.summarise_call(agent, WEEKDAY_10, WEEKDAY_10, "the caller")
    assert summary["caller"].startswith("Priya Sharma")
    assert summary["asked_about"] == ["a message for the team"]
    assert summary["outcomes"] == ["Message left for Dentist (routine)"]
    assert summary["first_words"] == "my tooth is aching"

    dashboard.record_call(conn, summary)
    calls = dashboard.team_view(conn, WEEKDAY_10)["calls"]
    assert calls[0]["caller"].startswith("Priya Sharma")


def test_a_call_where_nothing_was_said_is_not_recorded(conn):
    agent = SophiaAgent(conn, now=WEEKDAY_10, client=_Client())
    dashboard.record_call(conn, dashboard.summarise_call(agent, WEEKDAY_10, WEEKDAY_10, "the caller"))
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


def test_the_view_has_five_working_days_and_urgent_messages_first(conn):
    session = SophiaTools(conn, now=WEEKDAY_10)
    session.caller_heard = ["07700 900123", "a question about the waiting list"]
    session.take_message("Routine Caller", "NHS waiting list", "07700 900123")
    later = SophiaTools(conn, now=WEEKDAY_10)
    later.caller_heard = ["07700 900456", "my face is swollen"]
    later.take_message("Urgent Caller", "Swelling", "07700 900456")

    view = dashboard.team_view(conn, WEEKDAY_10)
    assert len(view["calendar"]) == 5 and view["calendar"][0]["today"]
    assert [m["caller"] for m in view["messages"]][0] == "Urgent Caller"
    assert view["stats"]["urgent_messages"] == 1
    # Marked done, it drops below the open ones and out of the count.
    assert dashboard.mark_message_done(conn, view["messages"][0]["id"])
    assert dashboard.team_view(conn, WEEKDAY_10)["stats"]["urgent_messages"] == 0


def test_an_older_database_gains_the_new_columns(tmp_path):
    """A database built before this page had no urgency column, and taking a message failed."""
    import sqlite3

    from sophia import db

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, caller_name TEXT NOT NULL, callback_number TEXT, "
        "patient_id INTEGER, reason TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open');"
        "CREATE TABLE appointments (id INTEGER PRIMARY KEY, status TEXT, cancelled_at TEXT);"
    )
    old.close()
    conn = db.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    assert {"urgency", "assigned_to"} <= columns
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'calls'").fetchone()


def test_the_dashboard_data_is_served(tmp_path):
    from fastapi.testclient import TestClient

    from sophia import voice_app
    from sophia.sessions import SessionStore

    app = voice_app.create_app()
    app.state.sessions = SessionStore(tmp_path)
    client = TestClient(app)
    assert "Reception desk" in client.get("/").text
    data = client.get("/api/team?session=11111111-2222-3333-4444-555555555555").json()
    assert {"stats", "calendar", "messages", "actions", "calls"} <= set(data)


def test_a_same_day_problem_with_nothing_booked_needs_follow_up(conn):
    """A test call mentioned a swollen face and ended with nothing booked and no message."""
    from sophia.agent_text import TurnRecord

    agent = SophiaAgent(conn, now=WEEKDAY_10, client=_Client())
    agent.history.append(TurnRecord(user="tell them I have a swollen face", reply="Is it affecting your breathing?",
                                    safety_level="urgent_same_day"))
    summary = dashboard.summarise_call(agent, WEEKDAY_10, WEEKDAY_10, "the caller")
    assert summary["follow_up"] and summary["safety"] == "Same day problem mentioned"
    dashboard.record_call(conn, summary)
    assert dashboard.team_view(conn, WEEKDAY_10)["stats"]["follow_ups"] == 1
