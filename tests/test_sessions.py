"""
Tests for per tester demo sessions.

The shared demo used one database and one transcript for everyone, so
testers took each other's slots and, calling at once, saw each other's
conversations. These tests pin down the isolation.
"""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from sophia import db, sessions
from sophia.sessions import SessionStore

ALICE = "alice-session-0001"
BOB = "bob-session-0002"


def untouched_patients_in(path):
    """How many seeded patients no tester has changed."""
    conn = db.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM patients WHERE postcode != 'ZZ9 9ZZ'").fetchone()[0]
    finally:
        conn.close()


def change_every_patient(path):
    """Change every patient, standing in for a tester's bookings and cancellations."""
    conn = db.connect(path)
    try:
        conn.execute("UPDATE patients SET postcode = 'ZZ9 9ZZ'")
        conn.commit()
    finally:
        conn.close()


def test_one_testers_changes_never_reach_another(tmp_path):
    store = SessionStore(tmp_path)
    alice_db = store.database(store.get(ALICE))
    bob_db = store.database(store.get(BOB))
    seeded = untouched_patients_in(bob_db)
    assert seeded > 0

    change_every_patient(alice_db)

    assert untouched_patients_in(alice_db) == 0
    assert untouched_patients_in(bob_db) == seeded


def test_a_tester_keeps_their_own_changes_between_calls(tmp_path):
    """Book on one call, cancel on the next: the booking has to still be there."""
    store = SessionStore(tmp_path)
    first = store.database(store.get(ALICE))
    change_every_patient(first)
    again = store.database(store.get(ALICE))
    assert again == first
    assert untouched_patients_in(again) == 0


def test_reset_restores_the_seed_and_clears_the_transcript(tmp_path):
    store = SessionStore(tmp_path)
    session = store.get(ALICE)
    change_every_patient(store.database(session))
    store.record(session, {"type": "caller", "text": "hello"})

    fresh = store.reset(session)

    assert untouched_patients_in(fresh) > 0
    assert session.events == []


def test_a_malformed_id_never_becomes_a_file_name(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.get("../../escape")
    path = store.database(session)
    assert session.id != "../../escape"
    assert path.parent == tmp_path / "sessions"


def test_a_session_built_yesterday_is_rebuilt_today(tmp_path):
    """Seed data is relative to the day it is built, so yesterday's slots may be in the past."""
    day = [date(2026, 9, 21)]
    store = SessionStore(tmp_path, today=lambda: day[0])
    session = store.get(ALICE)
    change_every_patient(store.database(session))

    day[0] += timedelta(days=1)

    assert untouched_patients_in(store.database(session)) > 0


def test_the_least_recently_used_session_is_evicted(tmp_path):
    store = SessionStore(tmp_path, max_sessions=2)
    oldest = store.get("session-one-1")
    store.database(oldest)
    store.get("session-two-2")
    store.get("session-three-3")

    assert store.find("session-one-1") is None
    assert store.find("session-three-3") is not None


def test_finding_a_session_does_not_create_one(tmp_path):
    store = SessionStore(tmp_path)
    assert store.find(ALICE) is None
    assert store.find(ALICE) is None


def test_files_left_by_a_previous_server_are_cleared(tmp_path):
    (tmp_path / "session-old-1.db").write_bytes(b"")
    SessionStore(tmp_path).clear_leftovers()
    assert not (tmp_path / "session-old-1.db").exists()


# ---------------------------------------------------------------------------
# Through the web server
# ---------------------------------------------------------------------------


def client_with_store(tmp_path):
    from sophia import voice_app

    app = voice_app.create_app()
    app.state.sessions = SessionStore(tmp_path)
    return TestClient(app), app.state.sessions


def test_transcripts_are_kept_apart(tmp_path):
    client, store = client_with_store(tmp_path)
    store.record(store.get(ALICE), {"type": "caller", "text": "I am Alice"})
    store.get(BOB)

    assert client.get(f"/api/events?session={ALICE}").json()["events"][0]["text"] == "I am Alice"
    assert client.get(f"/api/events?session={BOB}").json()["events"] == []
    assert client.get("/api/events").json()["events"] == []


def test_the_reset_route_only_resets_the_caller(tmp_path):
    client, store = client_with_store(tmp_path)
    alice_db = store.database(store.get(ALICE))
    bob_db = store.database(store.get(BOB))
    change_every_patient(alice_db)
    change_every_patient(bob_db)

    assert client.post(f"/api/reset?session={ALICE}").status_code == 200

    assert untouched_patients_in(store.database(store.get(ALICE))) > 0
    assert untouched_patients_in(bob_db) == 0


def test_the_call_list_comes_from_the_testers_own_data(tmp_path):
    client, _ = client_with_store(tmp_path)
    calls = client.get(f"/api/reminders?session={ALICE}").json()["calls"]
    assert calls, "the seeded call list should not be empty"
    assert sessions.valid_session_id(ALICE)
