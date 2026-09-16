"""
Tests for the database schema and the seed data.

These are deliberately strict. If the seed data ever drifts into an
impossible state, for example an appointment inside the lunch hour or a
clinician booked on a day they do not work, Sophia would confidently tell
a caller something false. Catching it here is much cheaper.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from sophia import clock, config


# ---------------------------------------------------------------------------
# Seed data is present and sane
# ---------------------------------------------------------------------------


def test_every_table_exists(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    expected = {
        "clinicians",
        "clinician_hours",
        "appointment_types",
        "patients",
        "appointments",
        "urgent_slots",
        "attendance_events",
        "messages",
        "reminder_queue",
    }
    assert expected <= tables


def test_seed_populates_the_core_tables(conn):
    counts = {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("clinicians", "appointment_types", "patients", "urgent_slots")
    }
    assert counts["clinicians"] == 6
    assert counts["appointment_types"] >= 20
    assert counts["patients"] >= 10
    assert counts["urgent_slots"] > 0


def test_practice_has_the_clinician_roles_the_scenarios_need(conn):
    roles = {
        row["role"] for row in conn.execute("SELECT DISTINCT role FROM clinicians")
    }
    assert "principal_dentist" in roles
    assert "associate_dentist" in roles
    assert "endodontist" in roles
    assert "hygiene_therapist" in roles


def test_hygiene_therapists_do_not_see_nhs_patients_directly(conn):
    """NHS hygiene is only available after a dentist refers the patient."""
    rows = conn.execute(
        "SELECT name, sees_nhs FROM clinicians WHERE role = 'hygiene_therapist'"
    ).fetchall()
    assert rows
    for row in rows:
        assert row["sees_nhs"] == 0, f"{row['name']} should not take NHS directly"


# ---------------------------------------------------------------------------
# Opening hours are impossible to violate
# ---------------------------------------------------------------------------


def test_no_clinician_hours_cross_the_lunch_closure(conn):
    lunch_start = clock.parse_time(config.LUNCH_START)
    lunch_end = clock.parse_time(config.LUNCH_END)

    for row in conn.execute("SELECT start_time, end_time FROM clinician_hours"):
        start = clock.parse_time(row["start_time"])
        end = clock.parse_time(row["end_time"])
        overlaps_lunch = start < lunch_end and end > lunch_start
        assert not overlaps_lunch, f"{row['start_time']} to {row['end_time']} covers lunch"


def test_no_clinician_works_at_the_weekend(conn):
    weekdays = {
        row["weekday"] for row in conn.execute("SELECT DISTINCT weekday FROM clinician_hours")
    }
    assert weekdays <= set(config.OPEN_WEEKDAYS)


def test_clinician_hours_sit_inside_practice_opening_hours(conn):
    opening = clock.parse_time(config.OPENING_TIME)
    closing = clock.parse_time(config.CLOSING_TIME)

    for row in conn.execute("SELECT start_time, end_time FROM clinician_hours"):
        assert clock.parse_time(row["start_time"]) >= opening
        assert clock.parse_time(row["end_time"]) <= closing


# ---------------------------------------------------------------------------
# Seeded appointments are genuinely bookable moments
# ---------------------------------------------------------------------------


def test_seeded_appointments_fall_inside_opening_hours(conn):
    rows = conn.execute(
        "SELECT start_time, end_time FROM appointments WHERE status = 'booked'"
    ).fetchall()
    assert rows, "the seed should create some appointments"

    for row in rows:
        start = clock.from_db(row["start_time"])
        # The last minute of an appointment, so a slot ending exactly at
        # 13:00 or 18:00 is still valid.
        last_minute = clock.from_db(row["end_time"]) - timedelta(minutes=1)
        assert clock.is_within_opening_hours(start), f"{start} is outside opening hours"
        assert clock.is_within_opening_hours(last_minute), f"{row['end_time']} runs past closing"


def test_seeded_appointments_are_with_a_clinician_who_actually_works_then(conn):
    rows = conn.execute(
        "SELECT a.start_time, a.end_time, c.name, c.id AS clinician_id "
        "FROM appointments a JOIN clinicians c ON c.id = a.clinician_id "
        "WHERE a.status = 'booked'"
    ).fetchall()

    for row in rows:
        start = clock.from_db(row["start_time"])
        end = clock.from_db(row["end_time"])
        blocks = conn.execute(
            "SELECT start_time, end_time FROM clinician_hours "
            "WHERE clinician_id = ? AND weekday = ?",
            (row["clinician_id"], start.weekday()),
        ).fetchall()

        fits = any(
            clock.parse_time(block["start_time"]) <= start.time()
            and end.time() <= clock.parse_time(block["end_time"])
            for block in blocks
        )
        assert fits, f"{row['name']} is not working at {row['start_time']}"


def test_seeded_appointments_are_in_the_future(conn):
    rows = conn.execute(
        "SELECT start_time FROM appointments WHERE status = 'booked'"
    ).fetchall()
    for row in rows:
        assert clock.from_db(row["start_time"]) > clock.now()


def test_no_clinician_is_double_booked(conn):
    rows = conn.execute(
        "SELECT clinician_id, start_time, end_time FROM appointments "
        "WHERE status = 'booked' ORDER BY clinician_id, start_time"
    ).fetchall()

    by_clinician: dict[int, list] = {}
    for row in rows:
        by_clinician.setdefault(row["clinician_id"], []).append(row)

    for clinician_id, appointments in by_clinician.items():
        for earlier, later in zip(appointments, appointments[1:]):
            assert earlier["end_time"] <= later["start_time"], (
                f"clinician {clinician_id} is double booked at {later['start_time']}"
            )


# ---------------------------------------------------------------------------
# Urgent slots
# ---------------------------------------------------------------------------


def test_urgent_slots_exist_only_on_open_days(conn):
    for row in conn.execute("SELECT DISTINCT slot_date FROM urgent_slots"):
        day = datetime.strptime(row["slot_date"], clock.DB_DATE_FORMAT).date()
        assert clock.is_open_day(day), f"{row['slot_date']} is not a working day"


def test_urgent_slots_avoid_the_lunch_hour(conn):
    lunch_start = clock.parse_time(config.LUNCH_START)
    lunch_end = clock.parse_time(config.LUNCH_END)
    for row in conn.execute("SELECT DISTINCT slot_time FROM urgent_slots"):
        slot_time = clock.parse_time(row["slot_time"])
        assert not (lunch_start <= slot_time < lunch_end)


def test_each_day_has_at_most_the_configured_number_of_urgent_slots(conn):
    rows = conn.execute(
        "SELECT slot_date, COUNT(*) AS n FROM urgent_slots GROUP BY slot_date"
    ).fetchall()
    for row in rows:
        assert row["n"] <= config.URGENT_SLOTS_PER_DAY


# ---------------------------------------------------------------------------
# Scenario coverage. These guard the eval suite that comes on Day 6.
# ---------------------------------------------------------------------------


def test_there_is_a_patient_whose_registration_has_lapsed(conn):
    cutoff = (
        clock.today() - timedelta(days=config.REGISTRATION_LAPSE_YEARS * 365)
    ).strftime(clock.DB_DATE_FORMAT)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM patients WHERE last_attended_date < ?", (cutoff,)
    ).fetchone()
    assert row["n"] >= 1


def test_there_is_a_patient_close_to_the_short_notice_limit(conn):
    row = conn.execute(
        "SELECT patient_id, COUNT(*) AS n FROM attendance_events "
        "WHERE event_type = 'SNC' GROUP BY patient_id ORDER BY n DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["n"] >= 2


def test_there_is_a_patient_with_a_failure_to_attend(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM attendance_events WHERE event_type = 'FTA'"
    ).fetchone()
    assert row["n"] >= 1


def test_there_is_a_child_patient_with_a_guardian(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM patients WHERE guardian_name IS NOT NULL"
    ).fetchone()
    assert row["n"] >= 1


def test_the_outbound_queue_has_a_reminder_for_tomorrow(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM reminder_queue WHERE reason = 'day_before'"
    ).fetchone()
    assert row["n"] >= 1


# ---------------------------------------------------------------------------
# Constraints actually bite
# ---------------------------------------------------------------------------


def test_an_unknown_appointment_status_is_rejected(empty_conn):
    empty_conn.execute(
        "INSERT INTO clinicians (code, name, role) VALUES ('x', 'Test', 'associate_dentist')"
    )
    empty_conn.execute(
        "INSERT INTO appointment_types "
        "(code, name, duration_min, clinician_role, funding, fee_gbp, eligibility_rule) "
        "VALUES ('T', 'Test', 20, 'dentist', 'nhs', 10.0, 'any')"
    )
    empty_conn.execute(
        "INSERT INTO patients (code, full_name, dob, postcode, patient_type) "
        "VALUES ('p', 'Test Patient', '1990-01-01', 'WA1 1AA', 'nhs')"
    )

    with pytest.raises(sqlite3.IntegrityError):
        empty_conn.execute(
            "INSERT INTO appointments "
            "(patient_id, clinician_id, type_code, start_time, end_time, status, created_at) "
            "VALUES (1, 1, 'T', '2026-09-16 09:00:00', '2026-09-16 09:20:00', 'maybe', '2026-09-16 08:00:00')"
        )


def test_an_appointment_for_a_missing_patient_is_rejected(empty_conn):
    with pytest.raises(sqlite3.IntegrityError):
        empty_conn.execute(
            "INSERT INTO appointments "
            "(patient_id, clinician_id, type_code, start_time, end_time, created_at) "
            "VALUES (999, 999, 'NOPE', '2026-09-16 09:00:00', '2026-09-16 09:20:00', '2026-09-16 08:00:00')"
        )
