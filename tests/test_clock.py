"""
Tests for time handling.

Two things here are easy to get wrong and expensive to get wrong:

  1. Urgent appointments are released at 08:00 on the day itself. A caller
     at 07:45 must be told to ring back, a caller at 09:00 must be offered
     one. Getting this backwards means Sophia either turns away someone in
     pain or promises a slot that does not exist yet.

  2. The clocks go back on the last Sunday of October. Any "hours until the
     appointment" calculation that ignores that will be an hour out, which
     matters because 24 hours is the line between a normal cancellation and
     a short notice cancellation.
"""

from datetime import date, datetime, time, timedelta

from sophia import clock, config


# ---------------------------------------------------------------------------
# Opening hours
# ---------------------------------------------------------------------------


def test_weekdays_are_open_and_weekends_are_not():
    # 21 September 2026 is a Monday.
    assert clock.is_open_day(date(2026, 9, 21))
    assert clock.is_open_day(date(2026, 9, 25))  # Friday
    assert not clock.is_open_day(date(2026, 9, 26))  # Saturday
    assert not clock.is_open_day(date(2026, 9, 27))  # Sunday


def test_the_lunch_hour_is_closed():
    monday = date(2026, 9, 21)
    assert clock.is_within_opening_hours(clock.combine(monday, time(12, 59)))
    assert not clock.is_within_opening_hours(clock.combine(monday, time(13, 0)))
    assert not clock.is_within_opening_hours(clock.combine(monday, time(13, 59)))
    assert clock.is_within_opening_hours(clock.combine(monday, time(14, 0)))


def test_before_opening_and_after_closing_are_closed():
    monday = date(2026, 9, 21)
    assert not clock.is_within_opening_hours(clock.combine(monday, time(7, 59)))
    assert clock.is_within_opening_hours(clock.combine(monday, time(8, 0)))
    assert clock.is_within_opening_hours(clock.combine(monday, time(17, 59)))
    assert not clock.is_within_opening_hours(clock.combine(monday, time(18, 0)))


def test_saturday_is_closed_all_day():
    saturday = date(2026, 9, 26)
    for hour in range(0, 24):
        assert not clock.is_within_opening_hours(clock.combine(saturday, time(hour, 0)))


def test_next_open_day_skips_the_weekend():
    assert clock.next_open_day(date(2026, 9, 26)) == date(2026, 9, 28)  # Sat to Mon
    assert clock.next_open_day(date(2026, 9, 27)) == date(2026, 9, 28)  # Sun to Mon
    assert clock.next_open_day(date(2026, 9, 21)) == date(2026, 9, 21)  # already open


# ---------------------------------------------------------------------------
# The 08:00 urgent release
# ---------------------------------------------------------------------------


def test_urgent_slots_are_not_released_before_eight():
    monday = date(2026, 9, 21)
    assert not clock.urgent_slots_released(clock.combine(monday, time(7, 45)))
    assert not clock.urgent_slots_released(clock.combine(monday, time(7, 59)))


def test_urgent_slots_are_released_from_eight_exactly():
    monday = date(2026, 9, 21)
    assert clock.urgent_slots_released(clock.combine(monday, time(8, 0)))
    assert clock.urgent_slots_released(clock.combine(monday, time(9, 0)))
    assert clock.urgent_slots_released(clock.combine(monday, time(17, 30)))


def test_urgent_slots_are_never_released_at_the_weekend():
    saturday = date(2026, 9, 26)
    assert not clock.urgent_slots_released(clock.combine(saturday, time(9, 0)))


# ---------------------------------------------------------------------------
# Hours until, including British Summer Time
# ---------------------------------------------------------------------------


def test_hours_until_is_positive_for_the_future_and_negative_for_the_past():
    reference = clock.combine(date(2026, 9, 21), time(10, 0))
    later = clock.combine(date(2026, 9, 22), time(10, 0))
    earlier = clock.combine(date(2026, 9, 20), time(10, 0))

    assert clock.hours_until(later, reference) == 24.0
    assert clock.hours_until(earlier, reference) == -24.0


def test_the_short_notice_boundary_sits_exactly_at_24_hours():
    reference = clock.combine(date(2026, 9, 21), time(10, 0))
    exactly_24h = clock.combine(date(2026, 9, 22), time(10, 0))
    just_inside = clock.combine(date(2026, 9, 22), time(9, 59))

    assert clock.hours_until(exactly_24h, reference) >= config.SHORT_NOTICE_HOURS
    assert clock.hours_until(just_inside, reference) < config.SHORT_NOTICE_HOURS


def test_the_night_the_clocks_go_back_adds_an_hour():
    """
    British Summer Time ends at 02:00 on Sunday 25 October 2026.

    A wall clock reading of 10:00 on Saturday to 10:00 on Sunday looks like
    24 hours, but it is really 25. If this ever returns 24.0, the timezone
    handling has silently broken and every cancellation near the boundary
    that weekend would be judged wrongly.
    """
    saturday = clock.combine(date(2026, 10, 24), time(10, 0))
    sunday = clock.combine(date(2026, 10, 25), time(10, 0))

    assert clock.hours_until(sunday, saturday) == 25.0


def test_the_night_the_clocks_go_forward_loses_an_hour():
    """British Summer Time begins at 01:00 on Sunday 29 March 2026."""
    saturday = clock.combine(date(2026, 3, 28), time(10, 0))
    sunday = clock.combine(date(2026, 3, 29), time(10, 0))

    assert clock.hours_until(sunday, saturday) == 23.0


def test_stored_times_survive_a_round_trip_through_the_database_format():
    original = clock.combine(date(2026, 10, 25), time(9, 30))
    assert clock.from_db(clock.to_db(original)) == original


# ---------------------------------------------------------------------------
# How Sophia says dates and times out loud
# ---------------------------------------------------------------------------


def test_dates_are_spoken_in_uk_style():
    assert clock.spoken_date(date(2026, 9, 29)) == "Tuesday the 29th of September"
    assert clock.spoken_date(date(2026, 9, 1)) == "Tuesday the 1st of September"
    assert clock.spoken_date(date(2026, 9, 2)) == "Wednesday the 2nd of September"
    assert clock.spoken_date(date(2026, 9, 3)) == "Thursday the 3rd of September"


def test_the_teens_use_th_not_st_nd_rd():
    assert clock.spoken_date(date(2026, 9, 11)).endswith("11th of September")
    assert clock.spoken_date(date(2026, 9, 12)).endswith("12th of September")
    assert clock.spoken_date(date(2026, 9, 13)).endswith("13th of September")


def test_times_are_spoken_without_leading_zeros_or_24_hour_clock():
    assert clock.spoken_time(time(9, 0)) == "9 am"
    assert clock.spoken_time(time(9, 20)) == "9:20 am"
    assert clock.spoken_time(time(14, 40)) == "2:40 pm"
    assert clock.spoken_time(time(12, 0)) == "12 pm"
    assert clock.spoken_time(time(0, 30)) == "12:30 am"


def test_full_spoken_datetime_reads_naturally():
    value = clock.combine(date(2026, 9, 29), time(9, 20))
    assert clock.spoken_datetime(value) == "Tuesday the 29th of September at 9:20 am"


# ---------------------------------------------------------------------------
# Sanity on the helpers themselves
# ---------------------------------------------------------------------------


def test_now_and_today_agree_and_are_timezone_aware():
    current = clock.now()
    assert current.tzinfo is not None
    assert current.date() == clock.today()


def test_naive_datetimes_are_treated_as_practice_local_time():
    naive = datetime(2026, 9, 21, 10, 0)
    aware = clock.to_aware(naive)
    assert aware.tzinfo is not None
    assert aware.hour == 10


def test_a_day_of_seeded_urgent_slot_times_all_parse():
    for value in ("08:20", "09:00", "11:40", "14:20", "15:40", "17:20"):
        assert isinstance(clock.parse_time(value), time)


def test_appointment_end_times_do_not_silently_roll_past_midnight():
    start = clock.combine(date(2026, 9, 21), time(17, 40))
    end = start + timedelta(minutes=20)
    assert end.date() == start.date()
