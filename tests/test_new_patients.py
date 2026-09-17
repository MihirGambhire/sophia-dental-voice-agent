"""
Tests for callers who have never been to the practice.

Until register_new_patient existed, a caller not already on file could not
book at all. Opening that door safely is mostly about what it must refuse,
so most of these are refusals: invented details, duplicates of real
patients, children, outbound calls, and anything that would let a new
record reach someone else's data.
"""

from datetime import date, time, timedelta

import pytest

from sophia import clock, config
from sophia.tools import SophiaTools, ToolError

MONDAY_10 = clock.combine(date(2026, 9, 21), time(10))

NEW_CALLER = [
    "Hi, I've never been to you before, can I book a check up?",
    "Priya Sharma",
    "fourth of May nineteen ninety",
    "W A 1, 3 B X",
    "07700 900123",
]


def caller(conn, said=NEW_CALLER, when=MONDAY_10):
    session = SophiaTools(conn, now=when)
    session.caller_heard = list(said)
    return session


def register(session, name="Priya Sharma", dob="1990-05-04", postcode="WA1 3BX", phone="07700 900123"):
    return session.register_new_patient(name, dob, postcode, phone)


@pytest.fixture
def strict_details(monkeypatch):
    """Real UK formats required, as outside the demo."""
    monkeypatch.setattr(config, "DEMO_RELAXED_DETAILS", False)


def patients(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]


# ---------------------------------------------------------------------------
# The route working
# ---------------------------------------------------------------------------


def test_a_new_caller_is_registered_and_verified_for_their_own_record(conn):
    session = caller(conn)
    result = register(session)

    assert result["registered"] is True
    assert session.verified_patient_id == result["patient_id"]
    row = conn.execute("SELECT * FROM patients WHERE id = ?", (result["patient_id"],)).fetchone()
    assert row["full_name"] == "Priya Sharma"
    assert row["dob"] == "1990-05-04"
    assert row["postcode"] == "WA1 3BX"
    assert row["phone"] == "07700900123"
    assert row["patient_type"] == "private"
    assert row["last_attended_date"] is None


def test_a_new_patient_is_quoted_the_new_patient_examination(conn):
    session = caller(conn)
    register(session)
    result = session.recommend_appointment_type("check_up")

    assert result["appointment_type"] == "PRIV_NEW_EXAM"
    assert result["fee"] == "£85"


def test_a_new_patient_can_book_and_is_told_who_takes_payment(conn):
    """The practice takes the first examination fee at booking. Sophia cannot, so she says who will."""
    session = caller(conn)
    register(session)
    slot = session.find_available_slots("PRIV_NEW_EXAM")["slots"][0]
    result = session.book_appointment(slot["slot_ref"], "PRIV_NEW_EXAM")

    assert result["booked"] is True
    assert "payment" in result["say"]


def test_a_new_patient_asking_for_hygiene_gets_direct_access(conn):
    session = caller(conn)
    register(session)
    assert session.recommend_appointment_type("hygiene", minutes=30)["appointment_type"] == "HYG_DIRECT"


def test_the_record_is_there_to_verify_on_a_later_call(conn):
    register(caller(conn))
    later = caller(conn, said=["Priya Sharma, 4 May 1990, W A 1 3 B X"])
    assert later.verify_patient("Priya Sharma", "1990-05-04", "WA1 3BX")["verified"] is True


def test_a_phone_number_read_out_in_words_is_heard(conn):
    said = [*NEW_CALLER[:4], "oh seven seven double oh, nine double oh, one two three"]
    assert register(caller(conn, said=said))["registered"] is True


def test_a_number_given_with_the_country_code_is_stored_as_a_uk_number(conn):
    said = [*NEW_CALLER[:4], "plus 44 7700 900123"]
    result = register(caller(conn, said=said), phone="+44 7700 900123")
    row = conn.execute("SELECT phone FROM patients WHERE id = ?", (result["patient_id"],)).fetchone()
    assert row["phone"] == "07700900123"


def test_a_landline_is_accepted(conn):
    said = [*NEW_CALLER[:4], "01925 630 221"]
    assert register(caller(conn, said=said), phone="01925 630221")["registered"] is True


def test_a_mismatch_offers_the_new_patient_route_without_saying_why(conn):
    session = caller(conn, said=["Nobody Here, 1 January 1980, W A 1 1 A A"])
    result = session.verify_patient("Nobody Here", "1980-01-01", "WA1 1AA")
    assert result["verified"] is False
    assert "new patient" in result["say"]


# ---------------------------------------------------------------------------
# NHS
# ---------------------------------------------------------------------------


def test_a_new_patient_cannot_be_booked_an_nhs_check_up(conn):
    """New NHS places are paused. Booking one anyway would promise a place that does not exist."""
    session = caller(conn, said=[*NEW_CALLER, "Can I have it on the NHS?"])
    register(session)
    result = session.recommend_appointment_type("check_up", funding="nhs")

    assert result["appointment_type"] is None
    assert result["reason"] == "new_nhs_places_paused"
    assert "private" in result["say"]


def test_a_lapsed_nhs_patient_is_a_new_nhs_patient_too(conn):
    """Carlos Reyes last attended just over three years ago."""
    row = conn.execute("SELECT * FROM patients WHERE code = 'reyes'").fetchone()
    session = SophiaTools(conn, now=MONDAY_10)
    assert session.verify_patient(row["full_name"], row["dob"], row["postcode"])["verified"]

    result = session.recommend_appointment_type("check_up")
    assert result["appointment_type"] is None
    assert result["reason"] == "new_nhs_places_paused"


def test_nhs_urgent_care_is_still_open_to_a_new_patient(conn):
    """Urgent appointments are open to people with no regular dentist."""
    session = caller(conn, said=[*NEW_CALLER, "I'd like that on the NHS please"])
    register(session)
    assert session.recommend_appointment_type("urgent", funding="nhs")["appointment_type"] == "NHS_URGENT"


# ---------------------------------------------------------------------------
# Details that were not really given
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("postcode", "WA9 9ZZ"),
        ("phone", "07700 111222"),
        ("name", "Priya Patel"),
    ],
)
def test_a_detail_the_caller_never_said_is_refused(conn, field, value):
    before = patients(conn)
    arguments = {"name": "Priya Sharma", "dob": "1990-05-04", "postcode": "WA1 3BX", "phone": "07700 900123"}
    arguments[field] = value
    result = register(caller(conn), **arguments)

    assert result["registered"] is False
    assert result["reason"] == "missing_details"
    assert patients(conn) == before


@pytest.mark.parametrize("placeholder", ["unknown", "", "not given"])
def test_a_placeholder_is_a_missing_detail(conn, placeholder):
    result = register(caller(conn), postcode=placeholder)
    assert result["reason"] == "missing_details"


def test_a_first_name_alone_is_not_enough(conn):
    result = register(caller(conn), name="Priya")
    assert result["reason"] == "missing_details"


@pytest.mark.parametrize("dob", ["2031-01-01", "1850-01-01", "sometime in May"])
def test_an_impossible_date_of_birth_is_refused(conn, dob):
    assert register(caller(conn), dob=dob)["reason"] == "date_not_understood"


def test_a_postcode_that_is_not_a_uk_postcode_is_refused(conn, strict_details):
    said = [*NEW_CALLER[:3], "12345", NEW_CALLER[4]]
    assert register(caller(conn, said=said), postcode="12345")["reason"] == "postcode_not_understood"


@pytest.mark.parametrize("phone", ["12345", "07700 9001", "not a number"])
def test_a_phone_number_that_is_not_a_uk_number_is_refused(conn, phone, strict_details):
    assert register(caller(conn), phone=phone)["reason"] == "phone_not_understood"


# ---------------------------------------------------------------------------
# Existing patients, and who is allowed to register at all
# ---------------------------------------------------------------------------


def test_an_existing_patient_with_a_wrong_postcode_gets_no_second_record(conn):
    """
    Margaret Hollis giving the wrong postcode must not become a new
    patient with an empty history, and the refusal must not confirm that
    a Margaret Hollis born on that day is on the list.
    """
    said = ["Margaret Hollis", "12 March 1958", "W A 9 9 Z Z", "07700 900555"]
    before = patients(conn)
    result = register(caller(conn, said=said), name="Margaret Hollis", dob="1958-03-12",
                      postcode="WA9 9ZZ", phone="07700 900555")

    assert result["registered"] is False
    assert result["reason"] == "cannot_register"
    assert patients(conn) == before
    assert "already" not in result["say"].lower()


def test_a_misheard_existing_name_is_still_caught_as_a_duplicate(conn):
    said = ["Margret Hollis", "12 March 1958", "W A 9 9 Z Z", "07700 900555"]
    result = register(caller(conn, said=said), name="Margret Hollis", dob="1958-03-12",
                      postcode="WA9 9ZZ", phone="07700 900555")
    assert result["reason"] == "cannot_register"


def test_a_child_is_referred_to_the_team(conn):
    said = ["Leo Sharma", "fourth of May twenty twenty", "W A 1, 3 B X", "07700 900123"]
    before = patients(conn)
    result = register(caller(conn, said=said), name="Leo Sharma", dob="2020-05-04")

    assert result["reason"] == "child"
    assert patients(conn) == before


def test_a_verified_existing_patient_cannot_register_again(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    session = caller(conn)
    session.caller_heard = None
    assert session.verify_patient(row["full_name"], row["dob"], row["postcode"])["verified"]
    session.caller_heard = list(NEW_CALLER)

    assert register(session)["reason"] == "already_a_patient"


def test_no_registration_on_an_outbound_call(conn):
    """Sophia rang a known patient. Whoever answers is not signing up."""
    session = caller(conn)
    session.expected_patient_id = 1
    with pytest.raises(ToolError):
        register(session)


def test_the_same_details_twice_give_the_same_record(conn):
    session = caller(conn)
    first = register(session)
    before = patients(conn)
    second = register(session)

    assert second["patient_id"] == first["patient_id"]
    assert patients(conn) == before


def test_only_one_person_is_registered_per_call(conn):
    """One patient per call: anyone else becomes a message for the team."""
    said = [*NEW_CALLER, "Arjun Sharma, first of June nineteen eighty eight"]
    session = caller(conn, said=said)
    assert register(session)["registered"]
    before = patients(conn)

    result = register(session, name="Arjun Sharma", dob="1988-06-01")
    assert result["reason"] == "one_patient_per_call"
    assert patients(conn) == before


def test_a_new_record_reaches_no_one_elses_appointments(conn):
    """The new caller is verified for their own empty record, and nothing more."""
    session = caller(conn)
    register(session)
    okafor_appointment = conn.execute(
        "SELECT a.id FROM appointments a JOIN patients p ON p.id = a.patient_id WHERE p.code = 'okafor'"
    ).fetchone()["id"]

    with pytest.raises(ToolError):
        session.check_cancellation(okafor_appointment)
    assert session.get_patient_status()["upcoming_appointments"] == []


# ---------------------------------------------------------------------------
# Urgent care for someone with no dentist
# ---------------------------------------------------------------------------

MONDAY_9 = clock.combine(date(2026, 9, 21), time(9))


def book_urgent(session):
    slot = session.get_urgent_slots_today()["slots"][0]
    return session.book_urgent_slot(slot["urgent_slot_id"])


def test_a_new_caller_who_asks_for_nhs_urgent_care_pays_the_nhs_charge(conn):
    """
    Every new record is private, and the urgent booking used to take the
    fee from the record alone, so this caller was charged £85.
    """
    session = caller(conn, said=[*NEW_CALLER, "On the NHS please."], when=MONDAY_9)
    register(session)
    result = book_urgent(session)

    assert result["booked"] is True
    assert result["fee"] == "£27.90"


def test_a_new_caller_who_says_nothing_about_funding_is_booked_privately(conn):
    session = caller(conn, when=MONDAY_9)
    register(session)
    assert book_urgent(session)["fee"] == "£85"


@pytest.mark.parametrize(
    "said, expected",
    [
        (["Not private, NHS please."], "nhs"),
        (["NHS please, actually no, privately."], "private"),
        (["I haven't got an NHS dentist."], None),
        (["I don't want NHS, I'll pay privately."], "private"),
        (["Hello, I'd like an appointment."], None),
    ],
)
def test_the_latest_funding_request_is_read_from_the_callers_words(said, expected):
    from sophia.tools import _latest_funding_request

    assert _latest_funding_request(said) == expected


# ---------------------------------------------------------------------------
# Names and dates as they really arrive
# ---------------------------------------------------------------------------


def test_a_title_is_not_stored_as_part_of_the_name(conn):
    said = ["Mrs Priya Sharma", *NEW_CALLER[2:]]
    result = register(caller(conn, said=said), name="mrs priya sharma")
    row = conn.execute("SELECT full_name FROM patients WHERE id = ?", (result["patient_id"],)).fetchone()
    assert row["full_name"] == "Priya Sharma"


def test_a_title_does_not_hide_an_existing_patient(conn):
    said = ["Mrs Margaret Hollis", "12 March 1958", "W A 9 9 Z Z", "07700 900555"]
    result = register(caller(conn, said=said), name="Mrs Margaret Hollis", dob="1958-03-12",
                      postcode="WA9 9ZZ", phone="07700 900555")
    assert result["reason"] == "cannot_register"


def test_hyphens_and_apostrophes_are_capitalised_properly(conn):
    said = ["mary-jane o'neil", *NEW_CALLER[2:]]
    result = register(caller(conn, said=said), name="mary-jane o'neil")
    row = conn.execute("SELECT full_name FROM patients WHERE id = ?", (result["patient_id"],)).fetchone()
    assert row["full_name"] == "Mary-Jane O'Neil"


def test_a_name_with_digits_is_refused(conn):
    said = ["Priya Sharma 2", *NEW_CALLER[2:]]
    assert register(caller(conn, said=said), name="Priya Sharma 2")["reason"] == "missing_details"


def test_a_name_spelled_out_letter_by_letter_is_heard(conn):
    said = ["Priya, surname S H A R M A", *NEW_CALLER[2:]]
    assert register(caller(conn, said=said))["registered"] is True


def test_an_ambiguous_numeric_date_is_confirmed_before_it_is_stored(conn):
    """
    "05/04/1990" is the 5th of April or the 4th of May. Verification can try
    both, but a stored record keeps one for good, so Sophia asks.
    """
    said = ["Priya Sharma", "05/04/1990", "W A 1, 3 B X", "07700 900123"]
    before = patients(conn)
    result = register(caller(conn, said=said), dob="1990-04-05")

    assert result["reason"] == "date_ambiguous"
    assert "April" in result["say"] and "May" in result["say"]
    assert patients(conn) == before


def test_once_the_caller_names_the_month_the_date_is_stored(conn):
    said = ["Priya Sharma", "05/04/1990", "W A 1, 3 B X", "07700 900123", "the fourth of May"]
    result = register(caller(conn, said=said), dob="1990-05-04")
    assert result["registered"] is True


def test_may_as_a_word_is_not_taken_as_the_month(conn):
    said = ["May I register? Priya Sharma", "05/04/1990", "W A 1, 3 B X", "07700 900123"]
    assert register(caller(conn, said=said), dob="1990-05-04")["reason"] == "date_ambiguous"


def test_a_date_that_can_only_be_read_one_way_needs_no_confirming(conn):
    said = ["Priya Sharma", "25/04/1990", "W A 1, 3 B X", "07700 900123"]
    assert register(caller(conn, said=said), dob="1990-04-25")["registered"] is True


# ---------------------------------------------------------------------------
# Made up details, allowed for demo testers
# ---------------------------------------------------------------------------


def test_a_demo_tester_can_register_with_a_made_up_postcode_and_number(conn):
    """Testers on the shared link were refused for not having a real UK postcode."""
    said = ["Test Person", "first of January nineteen ninety", "one two three four five", "one two three four five six"]
    result = register(caller(conn, said=said), name="Test Person", dob="1990-01-01", postcode="12345", phone="123456")

    assert result["registered"] is True
    row = conn.execute("SELECT postcode, phone FROM patients WHERE id = ?", (result["patient_id"],)).fetchone()
    assert row["postcode"] == "12345"
    assert row["phone"] == "123456"


def test_made_up_details_must_still_be_what_the_caller_said(conn):
    said = ["Test Person", "first of January nineteen ninety", "one two three four five", "one two three four five six"]
    result = register(caller(conn, said=said), name="Test Person", dob="1990-01-01", postcode="99999", phone="123456")
    assert result["reason"] == "missing_details"


def test_a_made_up_number_still_needs_enough_digits(conn):
    said = ["Test Person", "first of January nineteen ninety", "A B 1", "one two three"]
    result = register(caller(conn, said=said), name="Test Person", dob="1990-01-01", postcode="AB1", phone="123")
    assert result["reason"] == "phone_not_understood"


def test_existing_patients_still_need_their_real_postcode_in_the_demo(conn):
    """Relaxing new patients' formats must not relax who counts as an existing patient."""
    session = caller(conn, said=["Margaret Hollis, 12 March 1958, one two three four five"])
    assert session.verify_patient("Margaret Hollis", "1958-03-12", "12345")["verified"] is False


# ---------------------------------------------------------------------------
# A caller who has said they are new
# ---------------------------------------------------------------------------


def test_a_caller_who_said_they_are_new_is_sent_to_registration(conn):
    """A tester said "nope" and the existing patient check still ran five times."""
    session = caller(conn)
    session.caller_said_new = True
    result = session.verify_patient("Priya Sharma", "1990-05-04", "WA1 3BX")
    assert result["reason"] == "caller_is_new"
    assert result["use_instead"] == "register_new_patient"
    assert session.verified_patient_id is None


def test_the_booking_asked_for_before_registering_is_finished_after(conn):
    """He chose the earliest urgent slot, registered, and heard "How can I help you today?"."""
    from sophia.tools import ToolError

    session = caller(conn)
    slot = conn.execute("SELECT id FROM urgent_slots WHERE status = 'available' LIMIT 1").fetchone()[0]
    with pytest.raises(ToolError):
        session.book_urgent_slot(slot)
    assert session.waiting_booking == {"tool": "book_urgent_slot", "arguments": {"urgent_slot_id": slot}}

    result = register(session)
    assert result["registered"]
    assert "book_urgent_slot" in result["next"] and str(slot) in result["next"]

    session.book_urgent_slot(slot)
    assert session.waiting_booking is None


def test_nothing_to_carry_on_with_when_no_booking_was_tried(conn):
    result = register(caller(conn))
    assert result["registered"] and "next" not in result


@pytest.mark.parametrize(
    "said, last_reply, expected",
    [
        ("nope", "Have you been a patient with us before?", True),
        ("No, I haven't", "Have you been to the practice before?", True),
        ("yes", "Have you been a patient here before?", False),
        ("yes sure", "Would you like me to leave a message for the team?", None),
        ("no", "Is there anything else I can help with?", None),
        ("I've never been to you before, can I book?", "How can I help?", True),
        ("I'm an existing patient", "How can I help?", False),
        ("Are you taking new patients?", "How can I help?", None),
    ],
)
def test_new_or_existing_is_read_from_the_callers_words(said, last_reply, expected):
    from sophia.agent_text import said_new_or_existing

    assert said_new_or_existing(said, last_reply) is expected


def test_a_caller_who_looked_at_urgent_slots_is_steered_back_to_them(conn):
    """Replayed, a caller who wanted urgent care was registered and booked a routine check up."""
    session = caller(conn)
    session.offered_urgent = [{"urgent_slot_id": 3, "when": "11:40 am"}]
    result = register(session)
    assert "book_urgent_slot" in result["next"] and "Not a check up" in result["next"]
