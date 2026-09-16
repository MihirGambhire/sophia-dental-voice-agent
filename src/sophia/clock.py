"""
Time handling for Sophia.

Design decision, stated plainly because it affects everything else:

  All appointment times are stored in the database as local Warrington
  wall clock time, in the format "YYYY-MM-DD HH:MM:SS".

Why not UTC. The practice thinks in local time ("quarter past nine on
Tuesday"), the opening hours are local, and storing local strings keeps
date queries and sorting trivial. The usual danger with local storage is
the hour that repeats when the clocks go back, but that happens at 02:00
on the last Sunday in October, and the practice is closed at 02:00 on a
Sunday, so an ambiguous appointment time cannot occur.

Every comparison that matters (how many hours until an appointment, is it
in the past) converts to a timezone aware datetime first, so British
Summer Time is handled correctly.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from . import config

DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DB_DATE_FORMAT = "%Y-%m-%d"
DB_TIME_FORMAT = "%H:%M"

# Spoken month and weekday names, used when Sophia reads a date aloud.
_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def now() -> datetime:
    """Current time in the practice timezone, timezone aware."""
    return datetime.now(config.TIMEZONE)


def today() -> date:
    """Today's date in the practice timezone."""
    return now().date()


def to_aware(value: datetime) -> datetime:
    """Attach the practice timezone to a naive datetime, leave aware ones alone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=config.TIMEZONE)
    return value.astimezone(config.TIMEZONE)


def to_db(value: datetime) -> str:
    """Format a datetime for storage. Aware values are converted to local first."""
    return to_aware(value).strftime(DB_DATETIME_FORMAT)


def from_db(value: str) -> datetime:
    """Parse a stored datetime string back into a timezone aware datetime."""
    return to_aware(datetime.strptime(value, DB_DATETIME_FORMAT))


def parse_time(value: str) -> time:
    """Parse a "HH:MM" string from the seed data or config."""
    return datetime.strptime(value, DB_TIME_FORMAT).time()


def combine(day: date, clock_time: time) -> datetime:
    """Build a timezone aware datetime from a date and a time."""
    return to_aware(datetime.combine(day, clock_time))


def hours_until(value: datetime, reference: datetime | None = None) -> float:
    """
    Real elapsed hours from the reference moment until the given datetime.

    Negative if the datetime is already in the past. This is what decides
    whether a cancellation is a Short Notice Cancellation, so it has to be
    right across the nights the clocks change.

    The conversion to UTC below is not decoration. Python defines
    subtraction between two aware datetimes that share the same tzinfo
    object as wall clock arithmetic, and it ignores the timezone
    completely. So 10am on Saturday 24 October to 10am on Sunday 25
    October would come back as 24 hours, when 25 hours have really
    passed. Converting both sides to UTC first forces a true elapsed time
    comparison. See test_clock.py for the case that caught this.
    """
    reference = reference or now()
    delta = to_aware(value).astimezone(timezone.utc) - to_aware(reference).astimezone(
        timezone.utc
    )
    return delta.total_seconds() / 3600.0


def is_open_day(day: date) -> bool:
    """True if the practice opens at all on this date. Weekdays only."""
    return day.weekday() in config.OPEN_WEEKDAYS


def is_within_opening_hours(value: datetime) -> bool:
    """
    True if the moment falls inside opening hours, excluding the lunch closure.

    Opening hours are 08:00 to 18:00, Monday to Friday, closed 13:00 to 14:00.
    """
    value = to_aware(value)
    if not is_open_day(value.date()):
        return False
    clock_time = value.time()
    if clock_time < parse_time(config.OPENING_TIME):
        return False
    if clock_time >= parse_time(config.CLOSING_TIME):
        return False
    if parse_time(config.LUNCH_START) <= clock_time < parse_time(config.LUNCH_END):
        return False
    return True


def next_open_day(day: date) -> date:
    """The first day on or after the given date on which the practice opens."""
    candidate = day
    for _ in range(14):
        if is_open_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    raise RuntimeError("No open day found within two weeks, check OPEN_WEEKDAYS")


def urgent_slots_released(reference: datetime | None = None) -> bool:
    """
    True if today's limited urgent appointments have been released yet.

    They are released from 08:00 on the day itself, so a caller at 07:45
    is told to ring back, and a caller at 09:00 can be offered one.
    """
    reference = to_aware(reference or now())
    if not is_open_day(reference.date()):
        return False
    return reference.time() >= parse_time(config.URGENT_RELEASE_TIME)


def spoken_date(day: date) -> str:
    """
    A date in the form Sophia should say it: "Tuesday the 29th of September".

    UK style, spelled out, because a voice agent reading "29/09" aloud
    sounds wrong and "September 29" sounds American.
    """
    return (
        f"{_WEEKDAYS[day.weekday()]} the {_ordinal(day.day)} of {_MONTHS[day.month - 1]}"
    )


def spoken_time(clock_time: time) -> str:
    """A time in the form Sophia should say it: "twenty past nine" becomes 9:20 am."""
    hour_12 = clock_time.hour % 12 or 12
    meridiem = "am" if clock_time.hour < 12 else "pm"
    if clock_time.minute == 0:
        return f"{hour_12} {meridiem}"
    return f"{hour_12}:{clock_time.minute:02d} {meridiem}"


def spoken_datetime(value: datetime) -> str:
    """Full spoken form, for example "Tuesday the 29th of September at 9:20 am"."""
    value = to_aware(value)
    return f"{spoken_date(value.date())} at {spoken_time(value.time())}"


def _ordinal(number: int) -> str:
    """1 becomes 1st, 2 becomes 2nd, 11 becomes 11th, and so on."""
    if 11 <= number % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"
