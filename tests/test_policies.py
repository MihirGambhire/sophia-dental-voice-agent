"""
Tests for the business rules.

Each rule is tested at its boundary, not just in the middle, because the
boundaries are where a patient gets charged the wrong fee or discharged
from the practice by mistake.
"""

from datetime import date, datetime, time, timedelta

import pytest

from sophia import clock, config, policies
from sophia.policies import PatientFacts

TODAY = date(2026, 9, 16)


def days_before(days: int) -> date:
    return TODAY - timedelta(days=days)


# ---------------------------------------------------------------------------
# Registration and the three year rule
# ---------------------------------------------------------------------------


def test_a_patient_seen_recently_is_registered():
    facts = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(200))
    assert policies.is_registered(facts, TODAY)
    assert not policies.is_new_for_fees(facts, TODAY)


def test_a_patient_never_seen_is_not_registered():
    facts = PatientFacts()
    assert not policies.is_registered(facts, TODAY)
    assert policies.is_new_for_fees(facts, TODAY)


def test_exactly_three_years_is_still_registered():
    """The policy says lapsed after MORE than three years, so the day itself counts."""
    facts = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(1095))
    assert policies.is_registered(facts, TODAY)


def test_one_day_past_three_years_has_lapsed():
    facts = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(1096))
    assert not policies.is_registered(facts, TODAY)
    assert policies.is_new_for_fees(facts, TODAY)


def test_days_since_last_attended_is_none_for_a_stranger():
    assert policies.days_since_last_attended(PatientFacts(), TODAY) is None


# ---------------------------------------------------------------------------
# New patient for the attendance policy, which is a different question
# ---------------------------------------------------------------------------


def test_a_patient_of_five_months_is_new_for_the_attendance_policy():
    facts = PatientFacts(first_seen_date=days_before(150), last_attended_date=days_before(150))
    assert policies.is_new_for_attendance_policy(facts, TODAY)


def test_a_patient_of_two_years_is_not_new_for_the_attendance_policy():
    facts = PatientFacts(first_seen_date=days_before(730), last_attended_date=days_before(100))
    assert not policies.is_new_for_attendance_policy(facts, TODAY)


def test_someone_not_on_file_is_new_for_the_attendance_policy():
    assert policies.is_new_for_attendance_policy(PatientFacts(), TODAY)


def test_the_two_new_patient_definitions_are_independent():
    """
    A patient can be established for fees but new for the attendance policy,
    and the reverse. Conflating them would either overcharge someone or
    apply the wrong discharge limit.
    """
    recent_joiner = PatientFacts(
        first_seen_date=days_before(100), last_attended_date=days_before(30)
    )
    assert not policies.is_new_for_fees(recent_joiner, TODAY)
    assert policies.is_new_for_attendance_policy(recent_joiner, TODAY)

    long_lapsed = PatientFacts(
        first_seen_date=days_before(3000), last_attended_date=days_before(1500)
    )
    assert policies.is_new_for_fees(long_lapsed, TODAY)
    assert not policies.is_new_for_attendance_policy(long_lapsed, TODAY)


# ---------------------------------------------------------------------------
# Picking the appointment type, which is what sets the fee
# ---------------------------------------------------------------------------


def test_nhs_examinations_do_not_split_on_history():
    established = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(100))
    lapsed = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(1500))
    assert policies.exam_type_code("nhs", established, TODAY) == "NHS_EXAM"
    assert policies.exam_type_code("nhs", lapsed, TODAY) == "NHS_EXAM"


def test_private_examination_is_85_for_a_new_or_lapsed_patient():
    assert policies.exam_type_code("private", PatientFacts(), TODAY) == "PRIV_NEW_EXAM"

    lapsed = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(1500))
    assert policies.exam_type_code("private", lapsed, TODAY) == "PRIV_NEW_EXAM"


def test_private_examination_is_50_for_an_established_patient():
    established = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(180))
    assert policies.exam_type_code("private", established, TODAY) == "PRIV_ROUTINE_EXAM"


def test_emergency_consultation_splits_on_the_same_three_year_rule():
    established = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(180))
    lapsed = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(1500))

    assert policies.emergency_type_code("private", established, TODAY) == "PRIV_EMERG_EXISTING"
    assert policies.emergency_type_code("private", lapsed, TODAY) == "PRIV_EMERG_NEW"
    assert policies.emergency_type_code("private", PatientFacts(), TODAY) == "PRIV_EMERG_NEW"


def test_nhs_urgent_is_one_flat_type():
    established = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(180))
    assert policies.emergency_type_code("nhs", established, TODAY) == "NHS_URGENT"
    assert policies.emergency_type_code("nhs", PatientFacts(), TODAY) == "NHS_URGENT"


# ---------------------------------------------------------------------------
# Hygiene
# ---------------------------------------------------------------------------


def test_a_new_or_lapsed_patient_goes_through_direct_access():
    assert policies.hygiene_type_code(PatientFacts(), 30, TODAY) == "HYG_DIRECT"

    lapsed = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(1500))
    assert policies.hygiene_type_code(lapsed, 30, TODAY) == "HYG_DIRECT"
    assert policies.hygiene_direct_access_allowed(lapsed, TODAY)


def test_an_established_patient_picks_a_length():
    established = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(180))
    assert policies.hygiene_type_code(established, 20, TODAY) == "HYG_20"
    assert policies.hygiene_type_code(established, 30, TODAY) == "HYG_30"
    assert policies.hygiene_type_code(established, 40, TODAY) == "HYG_40"
    assert policies.hygiene_type_code(established, 60, TODAY) == "HYG_60"


def test_an_odd_length_rounds_up_so_nobody_is_short_changed():
    established = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(180))
    assert policies.hygiene_type_code(established, 25, TODAY) == "HYG_30"
    assert policies.hygiene_type_code(established, 45, TODAY) == "HYG_60"
    assert policies.hygiene_type_code(established, 90, TODAY) == "HYG_60"


def test_no_length_asked_for_gives_the_shortest():
    established = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(180))
    assert policies.hygiene_type_code(established, None, TODAY) == "HYG_20"


def test_an_established_patient_cannot_use_direct_access():
    established = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(180))
    assert not policies.hygiene_direct_access_allowed(established, TODAY)


# ---------------------------------------------------------------------------
# The 24 hour cancellation line
# ---------------------------------------------------------------------------


def test_thirty_hours_ahead_is_not_short_notice():
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    appointment = now + timedelta(hours=30)
    assert not policies.is_short_notice(appointment, now)


def test_five_hours_ahead_is_short_notice():
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    appointment = now + timedelta(hours=5)
    assert policies.is_short_notice(appointment, now)


def test_exactly_24_hours_is_not_short_notice():
    """The policy asks for at least 24 hours, so the boundary itself is fine."""
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    appointment = now + timedelta(hours=24)
    assert not policies.is_short_notice(appointment, now)


def test_one_minute_inside_24_hours_is_short_notice():
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    appointment = now + timedelta(hours=24) - timedelta(minutes=1)
    assert policies.is_short_notice(appointment, now)


def test_an_appointment_already_past_counts_as_short_notice():
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    appointment = now - timedelta(hours=2)
    assert policies.is_short_notice(appointment, now)


def test_the_24_hour_line_survives_the_clocks_going_back():
    """
    Saturday 10am to Sunday 10am across the October change is 25 real
    hours, so it is NOT short notice, even though the wall clock says 24.
    """
    saturday = clock.combine(date(2026, 10, 24), time(10, 0))
    sunday = clock.combine(date(2026, 10, 25), time(10, 0))
    assert not policies.is_short_notice(sunday, saturday)


def test_the_24_hour_line_survives_the_clocks_going_forward():
    """
    In March the same wall clock gap is only 23 real hours, so it IS short
    notice. Getting this wrong would record a cancellation against a
    patient who gave a full day of notice.
    """
    saturday = clock.combine(date(2026, 3, 28), time(10, 0))
    sunday = clock.combine(date(2026, 3, 29), time(10, 0))
    assert policies.is_short_notice(sunday, saturday)


# ---------------------------------------------------------------------------
# Counting the record
# ---------------------------------------------------------------------------


def test_events_outside_the_24_month_window_do_not_count():
    events = [
        ("SNC", days_before(100)),
        ("SNC", days_before(400)),
        ("SNC", days_before(900)),  # older than 24 months
    ]
    snc, fta = policies.count_attendance_events(events, TODAY)
    assert snc == 2
    assert fta == 0


def test_short_notice_and_failures_are_counted_separately():
    events = [("SNC", days_before(100)), ("FTA", days_before(200)), ("SNC", days_before(300))]
    snc, fta = policies.count_attendance_events(events, TODAY)
    assert snc == 2
    assert fta == 1


def test_an_empty_record_counts_as_zero():
    assert policies.count_attendance_events([], TODAY) == (0, 0)


# ---------------------------------------------------------------------------
# The discharge thresholds. Every published case is checked.
# ---------------------------------------------------------------------------


ESTABLISHED = PatientFacts(first_seen_date=days_before(2000), last_attended_date=days_before(180))
BRAND_NEW = PatientFacts(first_seen_date=days_before(150), last_attended_date=days_before(150))


def test_existing_patient_three_short_notice_cancellations_discharges():
    events = [("SNC", days_before(d)) for d in (100, 200, 300)]
    status = policies.assess_attendance(events, ESTABLISHED, TODAY)
    assert status.weighted_score == 3
    assert status.is_discharged


def test_existing_patient_two_short_notice_cancellations_does_not_discharge():
    events = [("SNC", days_before(d)) for d in (100, 200)]
    status = policies.assess_attendance(events, ESTABLISHED, TODAY)
    assert not status.is_discharged
    assert status.one_more_snc_discharges


def test_existing_patient_two_failures_to_attend_discharges():
    events = [("FTA", days_before(d)) for d in (100, 200)]
    status = policies.assess_attendance(events, ESTABLISHED, TODAY)
    assert status.weighted_score == 4
    assert status.is_discharged


def test_existing_patient_one_failure_to_attend_may_still_rebook():
    """The policy explicitly allows this for an established patient."""
    events = [("FTA", days_before(100))]
    status = policies.assess_attendance(events, ESTABLISHED, TODAY)
    assert status.weighted_score == 2
    assert not status.is_discharged


def test_existing_patient_two_short_notice_plus_one_failure_discharges():
    """This is the combination rule stated in the published policy."""
    events = [("SNC", days_before(100)), ("SNC", days_before(200)), ("FTA", days_before(300))]
    status = policies.assess_attendance(events, ESTABLISHED, TODAY)
    assert status.weighted_score == 4
    assert status.is_discharged


def test_new_patient_two_short_notice_cancellations_discharges():
    events = [("SNC", days_before(d)) for d in (30, 60)]
    status = policies.assess_attendance(events, BRAND_NEW, TODAY)
    assert status.is_new_patient
    assert status.threshold == config.NEW_PATIENT_SNC_LIMIT
    assert status.is_discharged


def test_new_patient_one_failure_to_attend_discharges():
    """A new patient gets no second chance on a missed appointment."""
    events = [("FTA", days_before(30))]
    status = policies.assess_attendance(events, BRAND_NEW, TODAY)
    assert status.weighted_score == 2
    assert status.is_discharged


def test_new_patient_one_short_notice_cancellation_is_one_away():
    events = [("SNC", days_before(60))]
    status = policies.assess_attendance(events, BRAND_NEW, TODAY)
    assert not status.is_discharged
    assert status.one_more_snc_discharges
    assert status.remaining_before_discharge == 1


def test_a_clean_record_is_well_clear():
    status = policies.assess_attendance([], ESTABLISHED, TODAY)
    assert status.weighted_score == 0
    assert not status.is_discharged
    assert not status.one_more_snc_discharges
    assert status.remaining_before_discharge == 3


# ---------------------------------------------------------------------------
# What Sophia actually says
# ---------------------------------------------------------------------------


def test_cancelling_well_ahead_produces_no_warning():
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    result = policies.assess_cancellation(now + timedelta(hours=30), [], ESTABLISHED, now)
    assert not result.is_short_notice
    assert result.warning == ""
    assert not result.would_discharge


def test_cancelling_late_explains_the_consequence_before_anything_is_recorded():
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    result = policies.assess_cancellation(now + timedelta(hours=5), [], ESTABLISHED, now)
    assert result.is_short_notice
    assert "short notice cancellation" in result.warning
    assert "less than 24 hours" in result.warning


def test_a_new_patient_one_away_is_warned_about_being_discharged():
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    events = [("SNC", days_before(60))]
    result = policies.assess_cancellation(now + timedelta(hours=5), events, BRAND_NEW, now)

    assert result.would_discharge
    assert "limit" in result.warning
    assert "take a message" in result.warning


def test_the_discharge_warning_offers_a_route_out_rather_than_just_refusing():
    """
    A caller close to being discharged should be offered the human team,
    not simply told no. This is a tone requirement as much as a rule.
    """
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    events = [("SNC", days_before(d)) for d in (100, 200)]
    result = policies.assess_cancellation(now + timedelta(hours=3), events, ESTABLISHED, now)

    assert result.would_discharge
    assert "speaking to the team" in result.warning


def test_a_patient_with_some_history_is_told_where_they_stand():
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    events = [("SNC", days_before(100))]
    result = policies.assess_cancellation(now + timedelta(hours=5), events, ESTABLISHED, now)

    assert not result.would_discharge
    assert "one short notice cancellation" in result.warning


@pytest.mark.parametrize(
    "events",
    [
        [],
        [("SNC", days_before(100))],
        [("FTA", days_before(100))],
        [("SNC", days_before(100)), ("SNC", days_before(200))],
        [("SNC", days_before(100)), ("FTA", days_before(200))],
    ],
)
def test_the_warning_never_uses_policy_jargon_a_caller_would_not_know(events):
    """
    A caller has never heard of an SNC or an FTA. Whatever branch the
    warning takes, it has to be in plain words.
    """
    now = clock.combine(date(2026, 9, 16), time(10, 0))
    result = policies.assess_cancellation(now + timedelta(hours=5), events, ESTABLISHED, now)

    assert "FTA" not in result.warning
    assert "SNC" not in result.warning
    assert "weighted" not in result.warning.lower()


def test_a_record_is_described_in_words_a_caller_would_use():
    """
    The wording helper is tested directly. With the current thresholds an
    established patient who has a missed appointment on record is always
    one cancellation from discharge, so the "missed appointment" wording
    only surfaces if those thresholds are ever loosened. Testing the
    helper directly keeps it honest either way.
    """
    assert policies._plain_count(
        policies.AttendanceStatus(0, 0, 0, 3, False)
    ) == "nothing"
    assert policies._plain_count(
        policies.AttendanceStatus(1, 0, 1, 3, False)
    ) == "one short notice cancellation"
    assert policies._plain_count(
        policies.AttendanceStatus(2, 0, 2, 3, False)
    ) == "2 short notice cancellations"
    assert policies._plain_count(
        policies.AttendanceStatus(0, 1, 2, 3, False)
    ) == "one missed appointment"
    assert policies._plain_count(
        policies.AttendanceStatus(1, 1, 3, 3, False)
    ) == "one short notice cancellation and one missed appointment"


# ---------------------------------------------------------------------------
# Speaking fees aloud
# ---------------------------------------------------------------------------


def test_whole_pounds_lose_the_decimals():
    assert policies.format_fee(85.00) == "£85"
    assert policies.format_fee(140.0) == "£140"


def test_pence_are_kept():
    assert policies.format_fee(27.90) == "£27.90"
    assert policies.format_fee(76.60) == "£76.60"


def test_from_prices_are_marked_as_estimates():
    assert policies.format_fee(325.0, is_from=True) == "from £325"


# ---------------------------------------------------------------------------
# The rules agree with the seeded data
# ---------------------------------------------------------------------------


def test_the_seeded_lapsed_patient_is_treated_as_new(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'pritchard'").fetchone()
    facts = PatientFacts.from_row(row)
    assert policies.is_new_for_fees(facts)
    assert policies.exam_type_code("private", facts) == "PRIV_NEW_EXAM"
    assert policies.hygiene_type_code(facts, 30) == "HYG_DIRECT"


def test_the_seeded_boundary_patient_has_just_lapsed(conn):
    """Carlos Reyes attended just over three years ago, by a small margin."""
    row = conn.execute("SELECT * FROM patients WHERE code = 'reyes'").fetchone()
    facts = PatientFacts.from_row(row)
    assert not policies.is_registered(facts)


def test_the_seeded_new_patient_with_one_cancellation_is_one_away(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'kaur'").fetchone()
    facts = PatientFacts.from_row(row)
    events = [
        (e["event_type"], datetime.strptime(e["event_date"], clock.DB_DATE_FORMAT).date())
        for e in conn.execute(
            "SELECT event_type, event_date FROM attendance_events WHERE patient_id = ?",
            (row["id"],),
        )
    ]
    status = policies.assess_attendance(events, facts)
    assert status.is_new_patient
    assert status.one_more_snc_discharges
    assert not status.is_discharged


def test_the_seeded_established_patient_with_two_cancellations_is_one_away(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'brennan'").fetchone()
    facts = PatientFacts.from_row(row)
    events = [
        (e["event_type"], datetime.strptime(e["event_date"], clock.DB_DATE_FORMAT).date())
        for e in conn.execute(
            "SELECT event_type, event_date FROM attendance_events WHERE patient_id = ?",
            (row["id"],),
        )
    ]
    status = policies.assess_attendance(events, facts)
    assert not status.is_new_patient
    assert status.one_more_snc_discharges
    assert not status.is_discharged


@pytest.mark.parametrize(
    "code,expected_registered",
    [
        ("hollis", True),
        ("okafor", True),
        ("pritchard", False),
        ("reyes", False),
        ("fairbanks", True),
    ],
)
def test_seeded_registration_states_are_what_the_scenarios_expect(
    conn, code, expected_registered
):
    row = conn.execute("SELECT * FROM patients WHERE code = ?", (code,)).fetchone()
    facts = PatientFacts.from_row(row)
    assert policies.is_registered(facts) is expected_registered
