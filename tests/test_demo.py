"""
Tests for the demo patient list and the call stage.

Testers could not try anything about existing patients, because nobody
told them who was on the list. And calls wandered, because the order a
receptionist follows lived only in the prompt.
"""

from datetime import date, time
from types import SimpleNamespace

import pytest

from sophia import clock, demo
from sophia.agent_text import SophiaAgent
from sophia.tools import SophiaTools

# Anchored on the next open day from today, not a fixed date. The seed
# data is built relative to the day the database is created, so a
# hardcoded anchor goes stale the moment the calendar passes it, and
# every urgent slot test starts failing on a day nobody changed code.
WEEKDAY_10 = clock.combine(clock.next_open_day(clock.today()), time(10))


def test_the_demo_list_shows_seeded_patients_worth_trying(conn):
    cards = demo.patient_cards(conn, now=WEEKDAY_10)
    names = [card["name"] for card in cards]

    assert "Margaret Hollis" in names
    assert "Liam Doherty" not in names, "the child's guardian flow is not built to try"
    hollis = next(card for card in cards if card["name"] == "Margaret Hollis")
    assert hollis["born"] == "12 March 1958"
    assert hollis["postcode"] == "WA1 2NF"
    assert "£27.90" in hollis["try"]
    assert hollis["phone"] is None


def test_the_demo_list_shows_a_patients_next_appointment(conn):
    cards = demo.patient_cards(conn, now=clock.combine(clock.today(), time(0, 1)))
    okafor = next(card for card in cards if card["name"] == "Daniel Okafor")
    assert okafor["next_appointment"]


def test_someone_the_tester_registered_appears_so_they_can_call_back(conn):
    session = SophiaTools(conn, now=WEEKDAY_10)
    session.caller_heard = ["Priya Sharma", "fourth of May nineteen ninety", "W A 1 3 B X", "07700 900123"]
    session.register_new_patient("Priya Sharma", "1990-05-04", "WA1 3BX", "07700 900123")

    card = demo.patient_cards(conn, now=WEEKDAY_10)[-1]
    assert card["name"] == "Priya Sharma"
    assert card["registered_by_you"] is True
    assert card["phone"] == "07700900123"


# ---------------------------------------------------------------------------
# The call stage
# ---------------------------------------------------------------------------


def agent(conn):
    return SophiaAgent(conn, now=WEEKDAY_10, client=SimpleNamespace())


def test_an_unidentified_caller_is_asked_new_or_existing_before_details(conn):
    stage = agent(conn)._call_stage()
    assert "have been a patient" in stage
    assert "needs seeing today" in stage


def test_a_same_day_problem_skips_the_urgency_question(conn):
    sophia = agent(conn)
    from sophia import safety

    sophia.last_screening = safety.screen("I'm in agony, painkillers aren't touching it")
    stage = sophia._call_stage()
    assert "urgent appointments" in stage
    assert "needs seeing today" not in stage


def test_after_999_advice_the_stage_says_not_to_book(conn):
    from sophia import safety

    sophia = agent(conn)
    sophia.last_screening = safety.screen("my face is swollen and I can't breathe")
    assert "EMERGENCY" in sophia._call_stage()


def test_a_verified_existing_patient_moves_on_to_helping(conn):
    sophia = agent(conn)
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    sophia.tools.caller_heard = None
    sophia.tools.verify_patient(row["full_name"], row["dob"], row["postcode"])
    assert "existing patient verified" in sophia._call_stage()


def test_a_call_sophia_placed_has_no_inbound_stage(conn):
    sophia = agent(conn)
    sophia.reminder = object()
    assert sophia._call_stage() == ""


def test_the_practice_panel_shows_the_facts_sophia_works_from(conn):
    card = demo.practice_card(conn)

    assert card["hours"][0]["times"] == "8am to 6pm, closed 1pm to 2pm for lunch"
    assert {"label": "Practice phone", "value": "01925 630221"} in card["contact"]
    nhs = {fee["name"]: fee["fee"] for fee in card["fees"]["nhs"]}
    private = {fee["name"]: fee["fee"] for fee in card["fees"]["private"]}
    assert nhs["NHS examination and diagnosis"] == "£27.90"
    assert private["New patient examination"] == "£85"
    assert any("waiting list is paused" in rule for rule in card["rules"])


# ---------------------------------------------------------------------------
# Registered patients put back after a server restart
# ---------------------------------------------------------------------------


def test_registered_patients_come_back_after_the_server_forgets_them(conn):
    """Render's free plan wipes the server when it sleeps; a tester's new patient vanished."""
    from sophia import demo

    kept = [{"name": "Mihir Gambhire", "dob": "2003-04-13", "postcode": "411027", "phone": "07700900123"}]
    assert demo.restore_registered(conn, kept) == 1
    assert demo.restore_registered(conn, kept) == 0  # already there
    cards = [c for c in demo.patient_cards(conn) if c["registered_by_you"]]
    assert [c["name"] for c in cards] == ["Mihir Gambhire"]
    assert cards[0]["dob"] == "2003-04-13"


@pytest.mark.parametrize(
    "patient",
    [
        {"name": "Robert'); DROP TABLE patients;--", "dob": "2003-04-13", "postcode": "411027", "phone": "07700900123"},
        {"name": "Mihir Gambhire", "dob": "13 April", "postcode": "411027", "phone": "07700900123"},
        {"name": "Mihir Gambhire", "dob": "2003-04-13", "postcode": "x" * 40, "phone": "07700900123"},
        "not a patient",
    ],
)
def test_anything_not_shaped_like_a_registration_is_skipped(conn, patient):
    from sophia import demo

    assert demo.restore_registered(conn, [patient]) == 0
