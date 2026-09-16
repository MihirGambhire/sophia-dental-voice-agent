"""
Every business rule that could give a caller wrong information.

These are pure functions. They take plain values in and return plain
values out, they never touch the database, and they never call a model.
That makes them cheap to test exhaustively, which matters because a
mistake in here is a mistake a patient hears out loud.

The rules come from the practice's published policy, recorded in
CLAUDE.md sections 6.5 to 6.7. Where the published wording leaves a gap,
the assumption is written down next to the code rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import clock, config


# ---------------------------------------------------------------------------
# The facts a rule needs about a patient
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatientFacts:
    """
    The subset of a patient record the rules actually care about.

    Kept separate from the database row so the rules can be tested with
    invented values, and so a caller who is not on file yet can be
    represented by first_seen_date and last_attended_date of None.
    """

    first_seen_date: date | None = None
    last_attended_date: date | None = None
    patient_type: str = "private"

    @classmethod
    def from_row(cls, row) -> "PatientFacts":
        """Build from a sqlite3.Row out of the patients table."""
        return cls(
            first_seen_date=_parse_date(row["first_seen_date"]),
            last_attended_date=_parse_date(row["last_attended_date"]),
            patient_type=row["patient_type"],
        )


def _parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return datetime.strptime(value, clock.DB_DATE_FORMAT).date()


def _reference_date(reference: date | None) -> date:
    return reference or clock.today()


# ---------------------------------------------------------------------------
# Registration and the three year rule
# ---------------------------------------------------------------------------


def days_since_last_attended(
    facts: PatientFacts, reference: date | None = None
) -> int | None:
    """Days since the patient was last seen, or None if they never have been."""
    if facts.last_attended_date is None:
        return None
    return (_reference_date(reference) - facts.last_attended_date).days


def is_registered(facts: PatientFacts, reference: date | None = None) -> bool:
    """
    True if the patient is still registered with the practice.

    Registration lapses after more than three years without attending.
    Someone who has never attended is not registered.
    """
    days = days_since_last_attended(facts, reference)
    if days is None:
        return False
    return days <= config.REGISTRATION_LAPSE_YEARS * 365


def is_new_for_fees(facts: PatientFacts, reference: date | None = None) -> bool:
    """
    True if this patient pays new patient prices.

    That covers two cases the price list treats identically: someone who
    has never been to the practice, and someone who has not been seen for
    at least three years. This is the rule behind the £85 new patient
    examination and the £140 hygienist direct access fee.
    """
    return not is_registered(facts, reference)


def is_new_for_attendance_policy(
    facts: PatientFacts, reference: date | None = None
) -> bool:
    """
    True if the stricter new patient discharge limits apply.

    ASSUMPTION. The published policy sets different limits for new and
    existing patients but never defines which is which. We treat anyone
    who has been with the practice for less than twelve months as new,
    because the policy separately says an existing patient "of twelve
    months or more" may rebook after a first failure to attend. That is
    the only figure the policy gives, so it is the one we use. Recorded
    in the README as an assumption.
    """
    if facts.first_seen_date is None:
        return True
    days_registered = (_reference_date(reference) - facts.first_seen_date).days
    return days_registered < config.NEW_PATIENT_MONTHS * 30


# ---------------------------------------------------------------------------
# Choosing the right appointment type, which is what sets the fee
# ---------------------------------------------------------------------------


def exam_type_code(
    funding: str, facts: PatientFacts, reference: date | None = None
) -> str:
    """
    The appointment type for a routine check up.

    NHS examinations are a single Band 1 charge whatever the history.
    Private examinations split on the three year rule: £85 for a new or
    lapsed patient, £50 for someone seen regularly.
    """
    if funding == "nhs":
        return "NHS_EXAM"
    return "PRIV_NEW_EXAM" if is_new_for_fees(facts, reference) else "PRIV_ROUTINE_EXAM"


def emergency_type_code(
    funding: str, facts: PatientFacts, reference: date | None = None
) -> str:
    """
    The appointment type for an urgent same day problem.

    NHS urgent treatment is one flat charge. Privately it is £85 for a new
    or lapsed patient and £50 for an existing one, the same three year
    split as examinations.
    """
    if funding == "nhs":
        return "NHS_URGENT"
    return (
        "PRIV_EMERG_NEW" if is_new_for_fees(facts, reference) else "PRIV_EMERG_EXISTING"
    )


def hygiene_type_code(
    facts: PatientFacts,
    minutes: int | None = None,
    reference: date | None = None,
) -> str:
    """
    The appointment type for a hygiene appointment.

    A patient who is new or lapsed cannot book a standard hygiene slot,
    because they have not been assessed by a dentist. They go through
    hygienist direct access at £140 instead. Everyone else picks a length.

    Hygiene is private only. NHS hygiene needs a dentist's referral
    first, which is handled by the caller, not here.
    """
    if is_new_for_fees(facts, reference):
        return "HYG_DIRECT"

    available = {20: "HYG_20", 30: "HYG_30", 40: "HYG_40", 60: "HYG_60"}
    if minutes in available:
        return available[minutes]

    # No length asked for, or an unsupported one. Round up to the next
    # length the practice actually sells, so the patient is never quoted
    # less time than they asked for.
    for length in sorted(available):
        if minutes is None or minutes <= length:
            return available[length]
    return "HYG_60"


def hygiene_direct_access_allowed(
    facts: PatientFacts, reference: date | None = None
) -> bool:
    """Direct access is only for patients who are new or not seen in three years."""
    return is_new_for_fees(facts, reference)


# ---------------------------------------------------------------------------
# Cancellations
# ---------------------------------------------------------------------------


def is_short_notice(
    appointment_start: datetime, reference: datetime | None = None
) -> bool:
    """
    True if cancelling now would be recorded as a Short Notice Cancellation.

    The line is 24 hours. Exactly 24 hours is not short notice, since the
    policy asks for "at least 24 hours" notice. An appointment already in
    the past is short notice, because cancelling after the fact is worse,
    not better.
    """
    return clock.hours_until(appointment_start, reference) < config.SHORT_NOTICE_HOURS


# ---------------------------------------------------------------------------
# The attendance record and the discharge thresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttendanceStatus:
    """What the patient's recent record means for them."""

    snc_count: int
    fta_count: int
    weighted_score: int
    threshold: int
    is_new_patient: bool

    @property
    def is_discharged(self) -> bool:
        """True if the record already meets the discharge threshold."""
        return self.weighted_score >= self.threshold

    @property
    def remaining_before_discharge(self) -> int:
        """
        How many further short notice cancellations they could afford.

        Zero means the next one reaches the threshold.
        """
        return max(0, self.threshold - self.weighted_score)

    @property
    def one_more_snc_discharges(self) -> bool:
        """True if a single further short notice cancellation would discharge them."""
        return self.weighted_score + 1 >= self.threshold


def count_attendance_events(
    events: list[tuple[str, date]], reference: date | None = None
) -> tuple[int, int]:
    """
    Count short notice cancellations and failures to attend in the window.

    Events are (event_type, event_date) pairs. Anything older than the
    rolling 24 month window is ignored, because the policy counts within
    24 months.
    """
    cutoff = _reference_date(reference) - timedelta(
        days=int(config.ATTENDANCE_WINDOW_MONTHS * 30.44)
    )
    snc = sum(1 for kind, when in events if kind == "SNC" and when >= cutoff)
    fta = sum(1 for kind, when in events if kind == "FTA" and when >= cutoff)
    return snc, fta


def assess_attendance(
    events: list[tuple[str, date]],
    facts: PatientFacts,
    reference: date | None = None,
) -> AttendanceStatus:
    """
    Turn a patient's recent record into a discharge risk assessment.

    The published policy gives four separate numbers plus a combination
    rule, and they all collapse into one weighted score. A failure to
    attend counts as two short notice cancellations, and the threshold is
    the short notice limit: three for an existing patient, two for a new
    one.

    That single rule reproduces every published case:

      existing, 3 short notice cancellations   3 >= 3  discharged
      existing, 2 failures to attend           4 >= 3  discharged
      existing, 2 short notice plus 1 failure  4 >= 3  discharged
      existing, 1 failure to attend            2 <  3  may rebook
      new, 2 short notice cancellations        2 >= 2  discharged
      new, 1 failure to attend                 2 >= 2  discharged

    The last two lines are why a new patient is so much more exposed, and
    why Sophia should warn them before recording anything.
    """
    snc, fta = count_attendance_events(events, reference)
    is_new = is_new_for_attendance_policy(facts, reference)

    return AttendanceStatus(
        snc_count=snc,
        fta_count=fta,
        weighted_score=snc + (fta * config.FTA_WEIGHT_IN_SNC),
        threshold=(
            config.NEW_PATIENT_SNC_LIMIT if is_new else config.EXISTING_PATIENT_SNC_LIMIT
        ),
        is_new_patient=is_new,
    )


# ---------------------------------------------------------------------------
# Turning a rule into something Sophia can say
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CancellationAssessment:
    """
    Everything Sophia needs to warn a caller before a cancellation is recorded.

    This is deliberately returned before anything is written, so the
    caller gets the consequence explained and then chooses. The two step
    confirm is the point.
    """

    is_short_notice: bool
    hours_until: float
    status_before: AttendanceStatus
    would_discharge: bool
    warning: str


def assess_cancellation(
    appointment_start: datetime,
    events: list[tuple[str, date]],
    facts: PatientFacts,
    reference: datetime | None = None,
) -> CancellationAssessment:
    """
    Work out what cancelling this appointment right now would mean.

    Returns the warning text as well as the facts, so the wording of
    something consequential is decided in tested code rather than
    improvised by a language model.
    """
    reference = reference or clock.now()
    hours = clock.hours_until(appointment_start, reference)
    short_notice = is_short_notice(appointment_start, reference)
    status = assess_attendance(events, facts, reference.date())

    if not short_notice:
        return CancellationAssessment(
            is_short_notice=False,
            hours_until=hours,
            status_before=status,
            would_discharge=False,
            warning="",
        )

    would_discharge = status.one_more_snc_discharges

    warning = (
        "That appointment is less than 24 hours away, so if I cancel it now "
        "the practice will record it as a short notice cancellation."
    )

    if would_discharge:
        warning += (
            " I should tell you that this one would reach the practice's limit, "
            "which normally means being taken off the patient list. "
            "If you are able to attend, or if something unavoidable has come up, "
            "it would be worth speaking to the team first. "
            "Would you like me to take a message for them instead?"
        )
    elif status.weighted_score > 0:
        remaining = status.remaining_before_discharge
        warning += (
            f" You have {_plain_count(status)} on record in the last two years, "
            f"so this would leave you {remaining} away from the practice's limit."
        )

    return CancellationAssessment(
        is_short_notice=True,
        hours_until=hours,
        status_before=status,
        would_discharge=would_discharge,
        warning=warning,
    )


def _plain_count(status: AttendanceStatus) -> str:
    """Describe a record in words a caller would use, not policy jargon."""
    parts = []
    if status.snc_count == 1:
        parts.append("one short notice cancellation")
    elif status.snc_count > 1:
        parts.append(f"{status.snc_count} short notice cancellations")
    if status.fta_count == 1:
        parts.append("one missed appointment")
    elif status.fta_count > 1:
        parts.append(f"{status.fta_count} missed appointments")
    if not parts:
        return "nothing"
    if len(parts) == 1:
        return parts[0]
    return " and ".join(parts)


def format_fee(amount: float, is_from: bool = False) -> str:
    """
    Format a fee the way Sophia should say it.

    Whole pounds lose the decimals, because "eighty five pounds" is what a
    person says, not "eighty five pounds zero zero".
    """
    prefix = "from " if is_from else ""
    if amount == int(amount):
        return f"{prefix}£{int(amount)}"
    return f"{prefix}£{amount:.2f}"
