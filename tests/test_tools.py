"""
Tests for the tool layer.

The important ones here are the refusals. A booking tool that works is
worth less than a booking tool that cannot be talked into working when it
should not.
"""

from datetime import date, datetime, time, timedelta

import pytest

from sophia import clock, config
from sophia.tools import SophiaTools, ToolError


def at(conn, when):
    """A tool session pinned to a specific moment."""
    return SophiaTools(conn, now=when)


def a_weekday_at(hour, minute=0, days_ahead=0):
    """
    A timestamp on a weekday, so tests do not fail on Saturdays.

    Monday 21 September 2026 is the anchor.
    """
    day = date(2026, 9, 21) + timedelta(days=days_ahead)
    return clock.combine(day, time(hour, minute))


def verified(conn, code="hollis", when=None):
    """A session with a known seeded patient already verified."""
    row = conn.execute("SELECT * FROM patients WHERE code = ?", (code,)).fetchone()
    session = at(conn, when or a_weekday_at(10))
    result = session.verify_patient(row["full_name"], row["dob"], row["postcode"])
    assert result["verified"], f"could not verify {code}"
    return session, row


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_correct_details_verify(conn):
    session, row = verified(conn)
    assert session.verified_patient_id == row["id"]


def test_verification_is_not_case_or_space_sensitive(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient(
        "  margaret   HOLLIS ", row["dob"], row["postcode"].lower().replace(" ", "")
    )
    assert result["verified"]


def test_a_date_of_birth_in_uk_written_form_is_accepted(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    spoken = datetime.strptime(row["dob"], clock.DB_DATE_FORMAT).strftime("%d/%m/%Y")
    session = at(conn, a_weekday_at(10))
    assert session.verify_patient(row["full_name"], spoken, row["postcode"])["verified"]


def test_a_wrong_date_of_birth_reveals_nothing(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'bannerman'").fetchone()
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient(row["full_name"], "1901-01-01", row["postcode"])

    assert not result["verified"]
    assert session.verified_patient_id is None
    assert row["full_name"] not in result["say"]


def test_a_wrong_postcode_fails_even_with_the_right_name_and_birthday(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient(row["full_name"], row["dob"], "ZZ99 9ZZ")
    assert not result["verified"]


def test_a_failed_match_does_not_say_which_part_was_wrong(conn):
    """
    Telling a caller "the postcode is right" would confirm real data about
    a patient to someone who failed the check.
    """
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient(row["full_name"], row["dob"], "ZZ99 9ZZ")

    lowered = result["say"].lower()
    assert "postcode" not in lowered
    assert "date of birth" not in lowered


def test_an_unparseable_date_asks_again_rather_than_guessing(conn):
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient("Margaret Hollis", "sometime in the sixties", "WA1 2NF")
    assert not result["verified"]
    assert result["reason"] == "date_not_understood"


def test_a_stranger_is_not_verified(conn):
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient("Nobody At All", "1990-01-01", "ZZ1 1ZZ")
    assert not result["verified"]


# ---------------------------------------------------------------------------
# Nothing works before verification. This is the core privacy guarantee.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_patient_status(),
        lambda s: s.book_appointment("1@2026-09-21T10:00", "NHS_EXAM"),
        lambda s: s.book_urgent_slot(1),
        lambda s: s.check_cancellation(1),
        lambda s: s.cancel_appointment(1, caller_confirmed=True),
        lambda s: s.reschedule_appointment(1, "1@2026-09-21T10:00", caller_confirmed=True),
        lambda s: s.recommend_appointment_type("check_up"),
    ],
)
def test_every_patient_tool_refuses_before_verification(conn, call):
    session = at(conn, a_weekday_at(10))
    with pytest.raises(ToolError, match="not been verified"):
        call(session)


def test_taking_a_message_does_not_need_verification(conn):
    """It reveals nothing, and it is the fallback when verification fails."""
    session = at(conn, a_weekday_at(10))
    result = session.take_message("Jane Caller", "Wants to register", "07700 900000")
    assert result["message_id"]


def test_looking_up_a_fee_does_not_need_verification(conn):
    session = at(conn, a_weekday_at(10))
    assert session.get_fee("PRIV_NEW_EXAM")["fee"] == "£85"


def test_a_verified_caller_cannot_touch_another_patients_appointment(conn):
    session, _ = verified(conn, "hollis")
    other = conn.execute(
        "SELECT a.id FROM appointments a JOIN patients p ON p.id = a.patient_id "
        "WHERE p.code = 'okafor' AND a.status = 'booked'"
    ).fetchone()

    with pytest.raises(ToolError, match="cannot find that appointment"):
        session.check_cancellation(other["id"])


def test_a_missing_and_a_someone_elses_appointment_look_identical(conn):
    """Different wording would leak that an appointment exists."""
    session, _ = verified(conn, "hollis")
    other = conn.execute(
        "SELECT a.id FROM appointments a JOIN patients p ON p.id = a.patient_id "
        "WHERE p.code = 'okafor' AND a.status = 'booked'"
    ).fetchone()

    with pytest.raises(ToolError) as missing:
        session.check_cancellation(999999)
    with pytest.raises(ToolError) as belongs_to_other:
        session.check_cancellation(other["id"])

    assert str(missing.value) == str(belongs_to_other.value)


# ---------------------------------------------------------------------------
# Patient status
# ---------------------------------------------------------------------------


def test_status_reports_registration_and_history(conn):
    session, _ = verified(conn, "hollis")
    status = session.get_patient_status()

    assert status["patient_type"] == "nhs"
    assert status["is_registered"]
    assert not status["treated_as_new_patient_for_fees"]
    assert status["short_notice_cancellations_last_24_months"] == 0


def test_status_flags_a_lapsed_registration(conn):
    session, _ = verified(conn, "pritchard")
    status = session.get_patient_status()

    assert not status["is_registered"]
    assert status["treated_as_new_patient_for_fees"]
    assert status["years_since_last_seen"] > 3


def test_status_flags_a_patient_close_to_discharge(conn):
    session, _ = verified(conn, "kaur")
    assert session.get_patient_status()["close_to_discharge"]


def test_status_lists_upcoming_appointments(conn):
    session, _ = verified(conn, "okafor", when=a_weekday_at(9))
    upcoming = session.get_patient_status()["upcoming_appointments"]
    assert len(upcoming) >= 1
    assert "with" not in upcoming[0]["when"]  # the date, not a sentence


# ---------------------------------------------------------------------------
# Choosing the right appointment type, which decides the fee
# ---------------------------------------------------------------------------


def test_a_lapsed_patient_is_quoted_the_new_patient_fee(conn):
    session, _ = verified(conn, "pritchard")
    result = session.recommend_appointment_type("check_up", funding="private")

    assert result["appointment_type"] == "PRIV_NEW_EXAM"
    assert result["fee"] == "£85"


def test_an_established_patient_is_quoted_the_routine_fee(conn):
    session, _ = verified(conn, "okafor")
    result = session.recommend_appointment_type("check_up", funding="private")

    assert result["appointment_type"] == "PRIV_ROUTINE_EXAM"
    assert result["fee"] == "£50"


def test_an_nhs_patient_is_quoted_the_band_one_charge(conn):
    session, _ = verified(conn, "hollis")
    result = session.recommend_appointment_type("check_up")

    assert result["appointment_type"] == "NHS_EXAM"
    assert result["fee"] == "£27.90"


def test_a_month_first_date_from_speech_to_text_still_verifies(conn):
    """
    Over a real voice call, "sixth of December nineteen forty nine" came
    back from speech to text as "12/06/1949", and the patient was refused.
    This is that transcript, word for word, including the misheard name and
    the dropped postcode letter.
    """
    session = at(conn, a_weekday_at(10))
    session.caller_heard = ["Hello. Yes. Speaking.", "It's Aileen Ashworth, 12/06/1949 w one one q n."]
    result = session.verify_patient("Aileen Ashworth", "1949-06-12", "W1 1QN")
    assert result["verified"] is True


def test_the_other_reading_needs_the_ambiguous_date_in_the_callers_words(conn):
    """Without "12/06/1949" actually said, the wrong day and month is just wrong."""
    session = at(conn, a_weekday_at(10))
    session.caller_heard = ["Eileen Ashworth, twelfth of June nineteen forty nine, W A 1, 1 Q N"]
    result = session.verify_patient("Eileen Ashworth", "1949-06-12", "WA1 1QN")
    assert result["verified"] is False


def test_an_unambiguous_wrong_date_is_not_rescued(conn):
    session = at(conn, a_weekday_at(10))
    session.caller_heard = ["Susan Pritchard, 07/25/1971, W A 2, 8 H L"]
    result = session.verify_patient("Susan Pritchard", "1971-07-24", "WA2 8HL")
    assert result["verified"] is False


def test_the_model_cannot_switch_an_nhs_patient_to_private_on_its_own(conn):
    """
    A fallback model sent funding="private" for an NHS patient who never
    mentioned paying privately, and quoted £50 instead of £27.90.
    """
    session, _ = verified(conn, "hollis")
    session.caller_heard = ["I'd like to book a check up please."]
    result = session.recommend_appointment_type("check_up", funding="private")

    assert result["appointment_type"] == "NHS_EXAM"
    assert result["fee"] == "£27.90"


def test_a_caller_who_asks_to_go_private_can(conn):
    session, _ = verified(conn, "hollis")
    session.caller_heard = ["Could I see someone privately instead? I don't mind paying."]
    result = session.recommend_appointment_type("check_up", funding="private")

    assert result["appointment_type"].startswith("PRIV_")


def test_a_caller_asking_for_the_nhs_is_heard_in_spoken_forms(conn):
    session, _ = verified(conn, "okafor")
    session.caller_heard = ["Can I have that on the N H S?"]
    result = session.recommend_appointment_type("check_up", funding="nhs")

    assert result["appointment_type"] == "NHS_EXAM"


def test_a_lapsed_patient_asking_for_hygiene_gets_direct_access(conn):
    session, _ = verified(conn, "pritchard")
    result = session.recommend_appointment_type("hygiene", funding="private", minutes=30)

    assert result["appointment_type"] == "HYG_DIRECT"
    assert result["fee"] == "£140"


def test_nhs_hygiene_is_explained_rather_than_booked(conn):
    session, _ = verified(conn, "hollis")
    result = session.recommend_appointment_type("hygiene", funding="nhs")

    assert result["appointment_type"] is None
    assert "refer" in result["say"]


def test_an_unrecognised_purpose_falls_back_to_a_message(conn):
    session, _ = verified(conn, "hollis")
    result = session.recommend_appointment_type("teeth whitening on the nhs")

    assert result["appointment_type"] is None
    assert "message" in result["say"]


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_slots_are_offered_and_look_sensible(conn):
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM")

    assert result["slots"]
    assert result["fee"] == "£27.90"
    for slot in result["slots"]:
        assert "@" in slot["slot_ref"]


def test_no_slot_is_ever_offered_during_the_lunch_hour(conn):
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM", days_ahead=21, limit=50)

    for slot in result["slots"]:
        assert not ("13:0" <= slot["time"] < "14:00"), f"lunch slot offered: {slot}"


def test_no_slot_is_ever_offered_at_the_weekend(conn):
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM", days_ahead=21, limit=50)

    for slot in result["slots"]:
        day = datetime.strptime(slot["date"], clock.DB_DATE_FORMAT).date()
        assert day.weekday() < 5, f"weekend slot offered: {slot}"


def test_no_slot_is_ever_offered_in_the_past(conn):
    when = a_weekday_at(14, 30)
    session, _ = verified(conn, "hollis", when=when)
    result = session.find_available_slots("NHS_EXAM", days_ahead=3, limit=50)

    for slot in result["slots"]:
        start = clock.combine(
            datetime.strptime(slot["date"], clock.DB_DATE_FORMAT).date(),
            clock.parse_time(slot["time"]),
        )
        assert start > when


def test_slots_are_spread_across_days_rather_than_bunched(conn):
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM", limit=4)

    dates = [slot["date"] for slot in result["slots"]]
    assert len(set(dates)) == len(dates), "all offered slots fell on the same day"


def test_hygiene_slots_only_come_from_hygiene_therapists(conn):
    session, _ = verified(conn, "okafor")
    result = session.find_available_slots("HYG_30", days_ahead=14, limit=20)

    therapists = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM clinicians WHERE role = 'hygiene_therapist'"
        )
    }
    assert result["slots"]
    for slot in result["slots"]:
        assert slot["clinician"] in therapists


def test_root_canal_slots_only_come_from_the_endodontist(conn):
    session, _ = verified(conn, "marsden")
    result = session.find_available_slots("RCT_CONSULT", days_ahead=21, limit=20)

    assert result["slots"]
    for slot in result["slots"]:
        assert slot["clinician"] == "Dr Thomas Ainsworth"


def test_nhs_appointments_are_never_offered_with_a_private_only_clinician(conn):
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM", days_ahead=21, limit=50)

    private_only = {
        row["name"]
        for row in conn.execute("SELECT name FROM clinicians WHERE sees_nhs = 0")
    }
    for slot in result["slots"]:
        assert slot["clinician"] not in private_only


def test_a_named_clinician_is_honoured_when_possible(conn):
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM", clinician_name="Nair", limit=5)

    assert result["slots"]
    for slot in result["slots"]:
        assert "Nair" in slot["clinician"]


def test_a_treatment_that_needs_assessment_is_not_bookable(conn):
    session, _ = verified(conn, "okafor")
    result = session.find_available_slots("EXTRACTION_SURGICAL")

    assert result["slots"] == []
    assert result["reason"] == "not_bookable"
    assert "message" in result["say"]


def test_an_invented_appointment_type_is_rejected(conn):
    session, _ = verified(conn, "hollis")
    with pytest.raises(ToolError, match="no appointment type"):
        session.find_available_slots("FREE_GOLD_TEETH")


# ---------------------------------------------------------------------------
# The 8am urgent release
# ---------------------------------------------------------------------------


def test_before_eight_there_is_nothing_to_offer(conn):
    session, _ = verified(conn, "hollis", when=a_weekday_at(7, 45))
    result = session.get_urgent_slots_today()

    assert not result["released"]
    assert result["reason"] == "before_release"
    assert result["slots"] == []
    assert "8" in result["say"]


def test_after_eight_urgent_slots_are_offered(conn):
    session, _ = verified(conn, "hollis", when=a_weekday_at(9))
    result = session.get_urgent_slots_today()

    assert result["released"]
    assert result["slots"]


def test_at_the_weekend_the_practice_is_closed(conn):
    saturday = clock.combine(date(2026, 9, 26), time(10, 0))
    session = at(conn, saturday)
    result = session.get_urgent_slots_today()

    assert not result["released"]
    assert result["reason"] == "closed_today"
    assert "111" in result["say"]


def test_when_the_slots_have_gone_the_out_of_hours_numbers_are_given(conn):
    when = a_weekday_at(9)
    conn.execute(
        "UPDATE urgent_slots SET status = 'taken' WHERE slot_date = ?",
        (when.strftime(clock.DB_DATE_FORMAT),),
    )
    conn.commit()

    session, _ = verified(conn, "hollis", when=when)
    result = session.get_urgent_slots_today()

    assert result["released"]
    assert result["slots"] == []
    assert "111" in result["say"]
    assert "0161 476 9651" in result["say"]


def test_urgent_slots_earlier_today_are_not_offered(conn):
    session, _ = verified(conn, "hollis", when=a_weekday_at(16, 0))
    result = session.get_urgent_slots_today()

    for slot in result["slots"]:
        assert slot["time"] > "16:00"


def test_booking_an_urgent_slot_charges_by_history(conn):
    when = a_weekday_at(9)

    session, _ = verified(conn, "hollis", when=when)
    slot = session.get_urgent_slots_today()["slots"][0]
    result = session.book_urgent_slot(slot["urgent_slot_id"])

    assert result["booked"]
    assert result["fee"] == "£27.90"  # NHS urgent


def test_a_lapsed_private_patient_pays_the_higher_emergency_fee(conn):
    when = a_weekday_at(9)
    session, _ = verified(conn, "pritchard", when=when)
    slot = session.get_urgent_slots_today()["slots"][0]
    result = session.book_urgent_slot(slot["urgent_slot_id"])

    assert result["booked"]
    assert result["fee"] == "£85"


def test_an_urgent_slot_cannot_be_taken_twice(conn):
    when = a_weekday_at(9)
    first, _ = verified(conn, "hollis", when=when)
    slot = first.get_urgent_slots_today()["slots"][0]
    assert first.book_urgent_slot(slot["urgent_slot_id"])["booked"]

    second, _ = verified(conn, "okafor", when=when)
    result = second.book_urgent_slot(slot["urgent_slot_id"])

    assert not result["booked"]
    assert result["reason"] == "just_taken"


def test_an_urgent_slot_cannot_be_booked_before_release(conn):
    early = a_weekday_at(7, 30)
    slot = conn.execute(
        "SELECT id FROM urgent_slots WHERE slot_date = ? AND status = 'available'",
        (early.strftime(clock.DB_DATE_FORMAT),),
    ).fetchone()

    session, _ = verified(conn, "hollis", when=early)
    result = session.book_urgent_slot(slot["id"])

    assert not result["booked"]
    assert result["reason"] == "before_release"


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------


def test_a_booking_is_written_and_read_back_with_the_fee(conn):
    session, row = verified(conn, "hollis")
    slot = session.find_available_slots("NHS_EXAM")["slots"][0]
    result = session.book_appointment(slot["slot_ref"], "NHS_EXAM")

    assert result["booked"]
    assert "£27.90" in result["say"]
    assert "24 hours" in result["say"]

    stored = conn.execute(
        "SELECT * FROM appointments WHERE id = ?", (result["appointment_id"],)
    ).fetchone()
    assert stored["patient_id"] == row["id"]
    assert stored["status"] == "booked"


def test_the_same_slot_cannot_be_booked_twice(conn):
    first, _ = verified(conn, "hollis")
    slot = first.find_available_slots("NHS_EXAM")["slots"][0]
    assert first.book_appointment(slot["slot_ref"], "NHS_EXAM")["booked"]

    second, _ = verified(conn, "okafor")
    result = second.book_appointment(slot["slot_ref"], "NHS_EXAM")

    assert not result["booked"]
    assert result["reason"] == "just_taken"


def test_a_slot_in_the_past_is_refused(conn):
    session, _ = verified(conn, "hollis", when=a_weekday_at(15))
    past = a_weekday_at(9)
    ref = f"1@{past.strftime('%Y-%m-%dT%H:%M')}"

    result = session.book_appointment(ref, "NHS_EXAM")
    assert not result["booked"]
    assert result["reason"] == "in_the_past"


def test_a_slot_in_the_lunch_hour_is_refused_even_if_asked_for_directly(conn):
    """
    The model could construct this reference itself. The tool must still
    refuse, because opening hours are not negotiable.
    """
    session, _ = verified(conn, "hollis", when=a_weekday_at(9))
    lunch = a_weekday_at(13, 20)
    ref = f"1@{lunch.strftime('%Y-%m-%dT%H:%M')}"

    result = session.book_appointment(ref, "NHS_EXAM")
    assert not result["booked"]
    assert result["reason"] == "outside_opening_hours"


def test_a_slot_at_the_weekend_is_refused(conn):
    session, _ = verified(conn, "hollis", when=a_weekday_at(9))
    saturday = clock.combine(date(2026, 9, 26), time(10, 0))
    ref = f"1@{saturday.strftime('%Y-%m-%dT%H:%M')}"

    result = session.book_appointment(ref, "NHS_EXAM")
    assert not result["booked"]
    assert result["reason"] == "outside_opening_hours"


def test_a_slot_on_a_day_the_clinician_does_not_work_is_refused(conn):
    """The endodontist works Wednesday and Thursday only."""
    endodontist = conn.execute(
        "SELECT id FROM clinicians WHERE role = 'endodontist'"
    ).fetchone()
    monday = a_weekday_at(10)  # 21 September 2026 is a Monday
    ref = f"{endodontist['id']}@{monday.strftime('%Y-%m-%dT%H:%M')}"

    session, _ = verified(conn, "marsden", when=a_weekday_at(9))
    result = session.book_appointment(ref, "RCT_CONSULT")

    assert not result["booked"]
    assert result["reason"] == "clinician_not_working"


def test_a_malformed_slot_reference_is_rejected(conn):
    session, _ = verified(conn, "hollis")
    with pytest.raises(ToolError, match="not one I recognise"):
        session.book_appointment("next tuesday please", "NHS_EXAM")


def test_a_treatment_needing_assessment_cannot_be_booked(conn):
    session, _ = verified(conn, "okafor")
    with pytest.raises(ToolError, match="cannot be booked directly"):
        session.book_appointment("1@2026-09-21T10:00", "EXTRACTION_SURGICAL")


# ---------------------------------------------------------------------------
# Cancelling, in two steps
# ---------------------------------------------------------------------------


def upcoming_appointment_for(conn, code):
    return conn.execute(
        "SELECT a.* FROM appointments a JOIN patients p ON p.id = a.patient_id "
        "WHERE p.code = ? AND a.status = 'booked' ORDER BY a.start_time LIMIT 1",
        (code,),
    ).fetchone()


def test_cancelling_well_ahead_says_there_is_no_penalty(conn):
    appointment = upcoming_appointment_for(conn, "okafor")
    start = clock.from_db(appointment["start_time"])
    when = start - timedelta(hours=30)

    session, _ = verified(conn, "okafor", when=when)
    check = session.check_cancellation(appointment["id"])

    assert not check["is_short_notice"]
    assert "no charge" in check["say"]


def test_cancelling_late_warns_before_anything_is_recorded(conn):
    appointment = upcoming_appointment_for(conn, "okafor")
    start = clock.from_db(appointment["start_time"])
    when = start - timedelta(hours=5)

    session, _ = verified(conn, "okafor", when=when)
    check = session.check_cancellation(appointment["id"])

    assert check["is_short_notice"]
    assert "short notice cancellation" in check["say"]

    # Nothing has been written yet.
    still_booked = conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment["id"],)
    ).fetchone()
    assert still_booked["status"] == "booked"


def test_cancelling_refuses_without_an_explicit_yes(conn):
    appointment = upcoming_appointment_for(conn, "okafor")
    session, _ = verified(conn, "okafor", when=clock.from_db(appointment["start_time"]) - timedelta(hours=5))

    with pytest.raises(ToolError, match="not confirmed"):
        session.cancel_appointment(appointment["id"], caller_confirmed=False)


def test_a_late_cancellation_records_a_short_notice_event(conn):
    appointment = upcoming_appointment_for(conn, "okafor")
    start = clock.from_db(appointment["start_time"])
    when = start - timedelta(hours=5)

    session, row = verified(conn, "okafor", when=when)
    result = session.cancel_appointment(appointment["id"], caller_confirmed=True)

    assert result["cancelled"]
    assert result["recorded_as_short_notice"]

    stored = conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment["id"],)
    ).fetchone()
    assert stored["status"] == "snc"

    events = conn.execute(
        "SELECT COUNT(*) AS n FROM attendance_events WHERE patient_id = ? AND event_type = 'SNC'",
        (row["id"],),
    ).fetchone()
    assert events["n"] == 1


def test_an_early_cancellation_records_nothing_against_the_patient(conn):
    appointment = upcoming_appointment_for(conn, "okafor")
    start = clock.from_db(appointment["start_time"])
    when = start - timedelta(hours=30)

    session, row = verified(conn, "okafor", when=when)
    result = session.cancel_appointment(appointment["id"], caller_confirmed=True)

    assert not result["recorded_as_short_notice"]
    stored = conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment["id"],)
    ).fetchone()
    assert stored["status"] == "cancelled"

    events = conn.execute(
        "SELECT COUNT(*) AS n FROM attendance_events WHERE patient_id = ?", (row["id"],)
    ).fetchone()
    assert events["n"] == 0


def test_a_new_patient_one_away_is_warned_they_would_be_discharged(conn):
    appointment = upcoming_appointment_for(conn, "kaur")
    start = clock.from_db(appointment["start_time"])
    when = start - timedelta(hours=3)

    session, _ = verified(conn, "kaur", when=when)
    check = session.check_cancellation(appointment["id"])

    assert check["would_reach_discharge_limit"]
    assert "limit" in check["say"]
    assert "message" in check["say"]


def test_cancelling_an_urgent_appointment_returns_the_slot_to_the_pool(conn):
    when = a_weekday_at(9)
    session, _ = verified(conn, "hollis", when=when)

    before = len(session.get_urgent_slots_today()["slots"])
    slot = session.get_urgent_slots_today()["slots"][0]
    booking = session.book_urgent_slot(slot["urgent_slot_id"])
    assert len(session.get_urgent_slots_today()["slots"]) == before - 1

    session.cancel_appointment(booking["appointment_id"], caller_confirmed=True)
    assert len(session.get_urgent_slots_today()["slots"]) == before


def test_an_already_cancelled_appointment_cannot_be_cancelled_again(conn):
    appointment = upcoming_appointment_for(conn, "okafor")
    when = clock.from_db(appointment["start_time"]) - timedelta(hours=30)
    session, _ = verified(conn, "okafor", when=when)
    session.cancel_appointment(appointment["id"], caller_confirmed=True)

    with pytest.raises(ToolError, match="not currently booked"):
        session.cancel_appointment(appointment["id"], caller_confirmed=True)


# ---------------------------------------------------------------------------
# Rescheduling
# ---------------------------------------------------------------------------


def test_rescheduling_moves_the_appointment(conn):
    appointment = upcoming_appointment_for(conn, "okafor")
    when = clock.from_db(appointment["start_time"]) - timedelta(hours=48)

    session, _ = verified(conn, "okafor", when=when)
    new_slot = session.find_available_slots(
        appointment["type_code"], days_ahead=21, limit=5
    )["slots"][-1]

    result = session.reschedule_appointment(
        appointment["id"], new_slot["slot_ref"], caller_confirmed=True
    )

    assert result["rescheduled"]
    assert not result["recorded_as_short_notice"]

    old = conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment["id"],)
    ).fetchone()
    assert old["status"] == "cancelled"


def test_rescheduling_inside_24_hours_still_records_a_short_notice_event(conn):
    appointment = upcoming_appointment_for(conn, "okafor")
    when = clock.from_db(appointment["start_time"]) - timedelta(hours=5)

    session, _ = verified(conn, "okafor", when=when)
    new_slot = session.find_available_slots(
        appointment["type_code"], days_ahead=21, limit=5
    )["slots"][-1]

    result = session.reschedule_appointment(
        appointment["id"], new_slot["slot_ref"], caller_confirmed=True
    )

    assert result["rescheduled"]
    assert result["recorded_as_short_notice"]
    assert "short notice" in result["say"]


def test_a_failed_rebooking_leaves_the_original_appointment_alone(conn):
    """
    The patient must never end up with nothing because the new slot was
    taken in between.
    """
    appointment = upcoming_appointment_for(conn, "okafor")
    when = clock.from_db(appointment["start_time"]) - timedelta(hours=48)
    session, _ = verified(conn, "okafor", when=when)

    result = session.reschedule_appointment(
        appointment["id"], "1@2020-01-01T10:00", caller_confirmed=True
    )

    assert not result["rescheduled"]
    still_booked = conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment["id"],)
    ).fetchone()
    assert still_booked["status"] == "booked"


# ---------------------------------------------------------------------------
# Messages and fees
# ---------------------------------------------------------------------------


def test_a_message_is_stored_for_the_team(conn):
    session = at(conn, a_weekday_at(10))
    result = session.take_message(
        "Alan Peters", "Complaint about a filling", "07700 900123", "Wants a call back today"
    )

    stored = conn.execute(
        "SELECT * FROM messages WHERE id = ?", (result["message_id"],)
    ).fetchone()
    assert stored["caller_name"] == "Alan Peters"
    assert stored["status"] == "open"


def test_a_message_from_a_verified_caller_is_linked_to_their_record(conn):
    session, row = verified(conn, "hollis")
    result = session.take_message("Margaret Hollis", "Asking about a crown")

    stored = conn.execute(
        "SELECT patient_id FROM messages WHERE id = ?", (result["message_id"],)
    ).fetchone()
    assert stored["patient_id"] == row["id"]


def test_a_message_needs_a_name(conn):
    session = at(conn, a_weekday_at(10))
    with pytest.raises(ToolError, match="needs a name"):
        session.take_message("   ", "No idea who this is")


@pytest.mark.parametrize(
    "code,expected",
    [
        ("NHS_EXAM", "£27.90"),
        ("NHS_BAND2", "£76.60"),
        ("NHS_BAND3", "£332.10"),
        ("PRIV_NEW_EXAM", "£85"),
        ("PRIV_ROUTINE_EXAM", "£50"),
        ("HYG_DIRECT", "£140"),
        ("HYG_60", "£135"),
        ("EXTRACTION_SURGICAL", "from £325"),
    ],
)
def test_fees_come_from_the_price_table(conn, code, expected):
    session = at(conn, a_weekday_at(10))
    assert session.get_fee(code)["fee"] == expected


def test_an_unknown_fee_is_refused_rather_than_guessed(conn):
    session = at(conn, a_weekday_at(10))
    with pytest.raises(ToolError, match="no appointment type"):
        session.get_fee("TOOTH_WHITENING")


# ---------------------------------------------------------------------------
# Verification problems found on live voice calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("postcode", ["", "  ", "unknown", "not provided"])
def test_verifying_before_the_postcode_is_given_asks_for_it(conn, postcode):
    """
    The model called verify_patient with only a name and date of birth, and
    the caller was told no record existed. A missing detail is now asked
    for, which reveals nothing about any patient.
    """
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient("Margaret Hollis", "1958-03-12", postcode)

    assert not result["verified"]
    assert result["reason"] == "missing_details"
    assert result["missing"] == ["postcode"]
    assert "postcode" in result["say"]
    assert "cannot find" not in result["say"]


def test_a_misheard_surname_still_verifies_when_date_and_postcode_match(conn):
    """Speech to text heard "Hollis" as "Hollies" on a real call."""
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient("Margaret Hollies", "1958-03-12", "WA1 2NF")
    assert result["verified"]
    assert session.verified_name == "Margaret Hollis"


def test_a_different_name_does_not_verify_even_with_the_right_date_and_postcode(conn):
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient("Susan Pritchard", "1958-03-12", "WA1 2NF")
    assert not result["verified"]
    assert session.verified_patient_id is None


def test_twins_at_one_address_are_not_confused_by_a_close_name(conn):
    """
    Two records with the same date of birth and postcode and similar names:
    a fuzzy match must refuse rather than pick one.
    """
    conn.execute(
        "INSERT INTO patients (code, full_name, dob, postcode, patient_type) "
        "VALUES ('twin_a', 'Jamie Clarke', '2001-05-05', 'WA1 1AA', 'nhs'), "
        "       ('twin_b', 'James Clarke', '2001-05-05', 'WA1 1AA', 'nhs')"
    )
    conn.commit()
    session = at(conn, a_weekday_at(10))

    assert not session.verify_patient("Jame Clarke", "2001-05-05", "WA1 1AA")["verified"]
    # An exact name still works for either twin.
    assert session.verify_patient("James Clarke", "2001-05-05", "WA1 1AA")["verified"]
    assert session.verified_name == "James Clarke"


@pytest.mark.parametrize(
    "spoken",
    ["March 12. 1958.", "12th March 1958", "twelfth", "12 of March, 1958", "March 12th, 1958"],
)
def test_spoken_date_shapes_are_understood(conn, spoken):
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient("Margaret Hollis", spoken, "WA1 2NF")
    if spoken == "twelfth":
        assert result["reason"] == "date_not_understood"
    else:
        assert result["verified"], f"did not understand {spoken!r}"


def test_a_postcode_said_letter_by_letter_is_understood(conn):
    session = at(conn, a_weekday_at(10))
    assert session.verify_patient("Margaret Hollis", "1958-03-12", "W A 1 2 N F")["verified"]


def test_a_postcode_that_is_not_a_uk_postcode_is_asked_for_again(conn, monkeypatch):
    """Outside the demo, an Indian PIN code is re-asked, not searched."""
    from sophia import config

    monkeypatch.setattr(config, "DEMO_RELAXED_DETAILS", False)
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient("Mihir Gambhire", "2004-04-13", "411027")
    assert result["reason"] == "postcode_not_understood"


def test_in_the_demo_a_made_up_postcode_is_no_match_and_offers_registration(conn, monkeypatch):
    """A tester was asked for his PIN code letter by letter, three times."""
    from sophia import config

    monkeypatch.setattr(config, "DEMO_RELAXED_DETAILS", True)
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient("Mihir Gambhire", "2004-04-13", "411027")
    assert result["reason"] == "no_match"
    assert "new patient" in result["say"]


# ---------------------------------------------------------------------------
# Identifiers must come from the caller, not the model
# ---------------------------------------------------------------------------


def listening_session(conn, *utterances):
    session = at(conn, a_weekday_at(10))
    session.caller_heard = list(utterances)
    return session


def test_an_invented_postcode_is_refused_and_asked_for(conn):
    """
    Captured on a real run: the model called verify_patient with WA1 1LZ,
    a postcode the caller never said, and the caller was told no record
    existed. The tool now checks the caller's own words.
    """
    session = listening_session(conn, "Margaret Hollis", "March 12. 1958.")
    result = session.verify_patient("Margaret Hollis", "1958-03-12", "WA1 1LZ")

    assert result["reason"] == "missing_details"
    assert result["missing"] == ["postcode"]
    assert session.verified_patient_id is None


def test_a_postcode_the_caller_said_is_accepted(conn):
    session = listening_session(conn, "Margaret Hollis", "March 12. 1958.", "W A 1, 2 N F")
    assert session.verify_patient("Margaret Hollis", "1958-03-12", "WA1 2NF")["verified"]


def test_a_postcode_read_as_number_words_is_accepted(conn):
    session = listening_session(conn, "Margaret Hollis", "March 12 1958", "W A one, two N F")
    assert session.verify_patient("Margaret Hollis", "1958-03-12", "WA1 2NF")["verified"]


def test_one_letter_lost_by_speech_to_text_still_verifies(conn):
    """Heard as "W one two n f" on a real call, the A was dropped."""
    session = listening_session(conn, "Margaret Hollis", "March 12. 1958.", "W one two n f")
    result = session.verify_patient("Margaret Hollis", "1958-03-12", "W1 2NF")
    assert result["verified"]
    assert session.verified_name == "Margaret Hollis"


def test_a_one_character_postcode_slip_needs_the_name_to_be_right_too(conn):
    session = listening_session(conn, "Susan Pritchard", "12 March 1958", "W one two n f")
    assert not session.verify_patient("Susan Pritchard", "1958-03-12", "W1 2NF")["verified"]


def test_without_a_caller_the_tools_do_not_check_spoken_words(conn):
    """Scripts and tests use the tools directly, with no call to check against."""
    session = at(conn, a_weekday_at(10))
    assert session.caller_heard is None
    assert session.verify_patient("Margaret Hollis", "1958-03-12", "WA1 2NF")["verified"]


# ---------------------------------------------------------------------------
# Time of day filtering, found missing by the scenario suite
# ---------------------------------------------------------------------------


def test_an_afternoon_request_returns_only_afternoon_slots(conn):
    """
    The suite caught Sophia telling a caller there were no afternoon
    appointments, when the afternoons were almost empty. The search could
    not filter by time, and spreading results took each day's earliest.
    """
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM", after_time="14:00", limit=5)

    assert result["slots"], "afternoon availability exists and must be found"
    assert all(slot["time"] >= "14:00" for slot in result["slots"])


def test_a_morning_request_returns_only_morning_slots(conn):
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM", before_time="12:00", limit=5)

    assert result["slots"]
    assert all(slot["time"] < "12:00" for slot in result["slots"])


@pytest.mark.parametrize("spoken", ["14:00", "2pm", "2 pm", "2:00 pm", "14"])
def test_the_time_filter_accepts_the_ways_a_model_writes_a_time(conn, spoken):
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM", after_time=spoken, limit=5)
    assert result["slots"] and all(slot["time"] >= "14:00" for slot in result["slots"])


def test_an_unreadable_time_is_ignored_rather_than_breaking_the_search(conn):
    session, _ = verified(conn, "hollis")
    assert session.find_available_slots("NHS_EXAM", after_time="teatime", limit=5)["slots"]


def test_a_time_window_still_never_offers_the_lunch_hour(conn):
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM", after_time="12:30", before_time="14:30", limit=20)
    assert all(not ("13:00" <= slot["time"] < "14:00") for slot in result["slots"])


def test_offered_times_mix_mornings_and_afternoons(conn):
    """
    Taking each day's earliest slot made every offer 8am, and the model
    told a tester nothing else existed.
    """
    session, _ = verified(conn, "hollis")
    result = session.find_available_slots("NHS_EXAM")
    hours = [int(slot["time"][:2]) for slot in result["slots"]]

    assert any(hour < 12 for hour in hours) and any(hour >= 12 for hour in hours)
    assert "after_time" in result["note"]


# ---------------------------------------------------------------------------
# One patient per call
# ---------------------------------------------------------------------------


def test_after_one_patient_is_verified_another_cannot_be(conn):
    """
    A tester registered, was then offered a check on "Sarah Smith" for her
    husband, and Sophia began verifying a third name. With someone else's
    details, a caller could have heard their appointments.
    """
    session, _ = verified(conn, "hollis")
    okafor = conn.execute("SELECT * FROM patients WHERE code = 'okafor'").fetchone()

    result = session.verify_patient(okafor["full_name"], okafor["dob"], okafor["postcode"])

    assert result["verified"] is False
    assert result["reason"] == "one_patient_per_call"
    assert session.verified_name == "Margaret Hollis"


def test_the_refusal_is_the_same_whether_or_not_the_other_person_exists(conn):
    session, _ = verified(conn, "hollis")
    okafor = conn.execute("SELECT * FROM patients WHERE code = 'okafor'").fetchone()
    real = session.verify_patient(okafor["full_name"], okafor["dob"], okafor["postcode"])
    invented = session.verify_patient("Sarah Smith", "1980-02-02", "WA1 1AA")
    assert real == invented


def test_confirming_the_same_patient_again_is_fine(conn):
    session, row = verified(conn, "hollis")
    again = session.verify_patient(row["full_name"], row["dob"], row["postcode"])
    assert again["verified"] is True


def test_a_first_name_alone_is_asked_to_be_completed_before_any_lookup(conn):
    """"This is Margaret speaking", right date of birth and postcode, told no record matched."""
    session = at(conn, a_weekday_at(10))
    result = session.verify_patient("Margaret", "1958-03-12", "WA1 2NF")
    assert result["reason"] == "missing_details"
    assert result["say"] == "Could I take your surname as well, Margaret?"
    # And the same for a name nobody has: the answer reveals nothing.
    assert session.verify_patient("Zelda", "1958-03-12", "WA1 2NF")["reason"] == "missing_details"


# ---------------------------------------------------------------------------
# Messages need a way back to the caller
# ---------------------------------------------------------------------------


def _message_session(conn, said):
    session = at(conn, a_weekday_at(10))
    session.caller_heard = list(said)
    return session


def test_a_message_without_a_number_asks_for_one_first(conn):
    """A tester's message was taken twice with no number, then he asked how they would reach him."""
    session = _message_session(conn, ["Mihir Gambhire", "are treatments painful?"])
    result = session.take_message("Mihir Gambhire", "Question about pain during treatment")
    assert result["taken"] is False
    assert result["say"] == "Could I take a phone number for the team to call you back on?"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_a_number_the_caller_never_said_is_not_used(conn):
    session = _message_session(conn, ["Mihir Gambhire", "are treatments painful?"])
    assert session.take_message("Mihir Gambhire", "Pain question", "07700 900999")["taken"] is False


def test_later_additions_join_the_first_message(conn):
    session = _message_session(conn, ["Mihir Gambhire", "call me on 07700 900123"])
    first = session.take_message("Mihir Gambhire", "Pain question", "07700 900123")
    second = session.take_message("Mihir Gambhire", "Worried about comfort")
    assert first["taken"] and second["added_to_earlier_message"]
    rows = conn.execute("SELECT callback_number, detail FROM messages").fetchall()
    assert len(rows) == 1
    assert rows[0]["callback_number"] == "07700900123" and "Worried about comfort" in rows[0]["detail"]


def test_a_verified_patient_is_called_back_on_the_number_on_record(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'okafor'").fetchone()
    session = at(conn, a_weekday_at(10))
    session.verify_patient(row["full_name"], row["dob"], row["postcode"])
    assert session.take_message(row["full_name"], "Question for the dentist")["taken"] is True


# ---------------------------------------------------------------------------
# Times on one particular day
# ---------------------------------------------------------------------------


def test_a_single_day_offers_several_times_across_it(conn):
    """"Any other slot on the same day?" had no answer, and Sophia said the practice was closed."""
    session = at(conn, a_weekday_at(7, 30))
    day = session.now.date().isoformat()
    result = session.find_available_slots("PRIV_NEW_EXAM", on_date=day)
    starts = [slot["time"] for slot in result["slots"]]
    assert len(starts) >= 3
    assert all(slot["date"] == day for slot in result["slots"])
    assert len({time[:2] for time in starts}) == len(starts)  # each in a different hour
    assert any(time >= "12:00" for time in starts) and any(time < "12:00" for time in starts)


def test_a_weekend_day_is_closed_not_empty(conn):
    session = at(conn, a_weekday_at(10))
    saturday = session.now.date() + timedelta(days=(5 - session.now.weekday()) % 7)
    result = session.find_available_slots("PRIV_NEW_EXAM", on_date=saturday.isoformat())
    assert result["reason"] == "closed_that_day"



# ---------------------------------------------------------------------------
# A same day problem when urgent appointments cannot be booked
# ---------------------------------------------------------------------------

SATURDAY_11 = clock.combine(date(2026, 9, 19), time(11))
FRIDAY_17_50 = clock.combine(date(2026, 9, 25), time(17, 50))


@pytest.mark.parametrize(
    "when, expected",
    [
        (clock.combine(date(2026, 9, 21), time(7, 30)), "8am this morning"),
        (clock.combine(date(2026, 9, 21), time(15)), "8am tomorrow, Tuesday the 22nd of September"),
        (SATURDAY_11, "8am on Monday the 21st of September"),
        (FRIDAY_17_50, "8am on Monday the 28th of September"),
    ],
)
def test_the_next_urgent_release_names_the_right_day(conn, when, expected):
    """"8am tomorrow" was wrong every Friday, and a closed day named no time at all."""
    assert at(conn, when).next_urgent_release() == expected


def test_a_closed_day_says_when_to_ring(conn):
    result = at(conn, SATURDAY_11).get_urgent_slots_today()
    assert result["reason"] == "closed_today"
    assert "8am on Monday the 21st of September" in result["say"]


def test_a_routine_booking_for_someone_in_pain_says_when_to_ring(conn):
    """A tester with severe tooth pain on a Saturday was booked Monday's check up and nothing more."""
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    session = at(conn, SATURDAY_11)
    session.verify_patient(row["full_name"], row["dob"], row["postcode"])
    session.caller_heard = ["it needs seeing today, I have a severe pain in my tooth"]
    slot = session.find_available_slots("NHS_EXAM")["slots"][0]
    result = session.book_appointment(slot["slot_ref"], "NHS_EXAM")
    assert result["booked"]
    assert "ring from 8am on Monday the 21st of September" in result["say"]
    assert config.OUT_OF_HOURS_DENTAL_NUMBER in result["say"]


def test_a_routine_booking_without_a_same_day_problem_says_nothing_extra(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    session = at(conn, SATURDAY_11)
    session.verify_patient(row["full_name"], row["dob"], row["postcode"])
    session.caller_heard = ["I'd like a check up please"]
    slot = session.find_available_slots("NHS_EXAM")["slots"][0]
    assert "ring from" not in session.book_appointment(slot["slot_ref"], "NHS_EXAM")["say"]



def test_asking_to_be_seen_today_counts_as_a_same_day_problem(conn):
    """A tester asked for it to be seen today without naming a symptom the screen knows."""
    session = at(conn, SATURDAY_11)
    session.caller_heard = ["if possible yes i would like it if it is seen today"]
    assert not session.caller_has_same_day_problem()
    session.get_urgent_slots_today()
    assert session.caller_has_same_day_problem()


def test_the_urgent_lookup_does_not_repeat_advice_already_given(conn):
    session = at(conn, SATURDAY_11)
    session.urgent_advice_given = True
    result = session.get_urgent_slots_today()
    assert result["reason"] == "already_told_when_to_ring"
    assert "8am" not in result["say"]



@pytest.mark.parametrize("code", ["PRIV_EMERG_NEW", "PRIV_EMERG_EXISTING", "NHS_URGENT"])
def test_an_urgent_type_cannot_be_searched_or_booked_ahead(conn, code):
    """A replay on a Saturday booked an emergency consultation for Monday at 8am."""
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    session = at(conn, SATURDAY_11)
    session.verify_patient(row["full_name"], row["dob"], row["postcode"])
    found = session.find_available_slots(code)
    assert found["slots"] == [] and found["reason"] == "urgent_only_on_the_day"
    booked = session.book_appointment("1@2026-09-21T08:00", code)
    assert booked["booked"] is False



def test_a_preferred_call_back_time_is_noted_not_promised(conn):
    """A tester asked for a call "between one and five" and was promised one."""
    session = at(conn, a_weekday_at(10))
    session.caller_heard = ["call me anything between one to 5PM", "it's 9307799220"]
    result = session.take_message("Mihir Gambhire", "Question about gum treatment", "9307799220")
    assert "can't promise exactly when" in result["say"]
