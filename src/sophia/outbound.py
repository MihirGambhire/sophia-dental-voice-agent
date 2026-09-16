"""
Outbound reminder calls: Sophia rings the patient.

WHY THIS EXISTS
===============
The practice no longer sends reminders to book check ups, and says so on
its website. Remembering is left to the patient. Every missed appointment
is a slot another patient could have had, and for the patient it counts
towards being removed from the practice list. A day before reminder is the
cheapest possible fix for both.

THE RULE THAT SHAPES EVERYTHING HERE
====================================
Whoever answers might not be the patient. A partner, a parent, a flatmate.
So before the person answering has passed the identity check, Sophia must
not reveal that there is an appointment, when it is, who it is with, or
anything about the patient's dental care.

The usual way to get that is a line in the prompt saying "do not reveal
the appointment". This project has watched the model ignore lines like
that several times. So instead the model is simply never told. Its call
context contains the patient's name, so it can ask for them, and nothing
else. Appointment details only exist for it after verify_patient succeeds
for that exact patient and get_patient_status returns them. There is
nothing to leak, because there is nothing yet to know.

Verifying as anybody else does not count either: if the person answering
passes the identity check for a different patient, the check is refused
exactly as a wrong date of birth would be.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import clock, config

OUTCOMES = (
    "confirmed",          # the patient will attend
    "rescheduled",        # moved to another slot on this call
    "cancelled",          # cancelled on this call
    "booked_check_up",    # a recall call that ended in a booking
    "declined",           # a recall the patient did not want
    "wrong_person",       # someone else answered, nothing was shared
    "call_back_later",    # the patient could not talk now
)


@dataclass(frozen=True)
class Reminder:
    """One queued outbound call, holding only what is safe to know up front."""

    id: int
    patient_id: int
    patient_name: str
    reason: str  # "day_before" or "recall"
    appointment_id: int | None


def load(conn, reminder_id: int) -> Reminder | None:
    row = conn.execute(
        "SELECT r.id, r.patient_id, r.reason, r.appointment_id, p.full_name "
        "FROM reminder_queue r JOIN patients p ON p.id = r.patient_id WHERE r.id = ?",
        (reminder_id,),
    ).fetchone()
    if row is None:
        return None
    return Reminder(
        id=row["id"],
        patient_id=row["patient_id"],
        patient_name=row["full_name"],
        reason=row["reason"],
        appointment_id=row["appointment_id"],
    )


def pending(conn) -> list[dict]:
    """
    The call list, for the practice side of the demo page.

    This is a staff view, so it may show the appointment. The caller side
    never sees it, and neither does the model before verification.
    """
    rows = conn.execute(
        "SELECT r.id, r.reason, r.status, r.last_outcome, p.full_name, "
        "a.start_time, c.name AS clinician "
        "FROM reminder_queue r "
        "JOIN patients p ON p.id = r.patient_id "
        "LEFT JOIN appointments a ON a.id = r.appointment_id "
        "LEFT JOIN clinicians c ON c.id = a.clinician_id "
        "ORDER BY r.status = 'pending' DESC, r.due_at"
    ).fetchall()
    calls = []
    for row in rows:
        when = (
            clock.spoken_datetime(clock.from_db(row["start_time"]))
            if row["start_time"]
            else None
        )
        calls.append(
            {
                "id": row["id"],
                "patient": row["full_name"],
                "reason": row["reason"],
                "appointment": f"{when} with {row['clinician']}" if when else None,
                "status": row["status"],
                "outcome": row["last_outcome"],
            }
        )
    return calls


def mark_started(conn, reminder: Reminder) -> None:
    conn.execute(
        "UPDATE reminder_queue SET status = 'called', attempts = attempts + 1, "
        "last_outcome = COALESCE(last_outcome, 'no_outcome_recorded') WHERE id = ?",
        (reminder.id,),
    )
    conn.commit()


def record_outcome(conn, reminder: Reminder, outcome: str) -> None:
    conn.execute(
        "UPDATE reminder_queue SET status = 'called', last_outcome = ? WHERE id = ?",
        (outcome, reminder.id),
    )
    conn.commit()


def greeting(reminder: Reminder) -> str:
    """
    What Sophia says when the call is answered.

    It names the practice and asks for the patient by name, and deliberately
    says nothing about why she is calling. Even "about your appointment"
    tells whoever answered that the patient has one.
    """
    return (
        f"Hello, this is {config.AGENT_NAME}, the AI assistant calling from "
        f"{config.PRACTICE_NAME}. Calls may be recorded. "
        f"Could I speak to {reminder.patient_name}, please?"
    )


def call_context(reminder: Reminder) -> str:
    """The part of the call state that is specific to an outbound call."""
    purpose = (
        "to remind them about an appointment tomorrow, and confirm, move or cancel it"
        if reminder.reason == "day_before"
        else "because they are due a check up, and to offer to book one"
    )
    return (
        f"OUTBOUND CALL. You rang {reminder.patient_name}. You are calling {purpose}.\n"
        "You know nothing else about them. Until verify_patient succeeds for "
        f"{reminder.patient_name}, do not say why you are calling, do not mention "
        "any appointment, and do not discuss their care, even if the person says "
        "they are the patient. Ask for their date of birth and postcode first.\n"
        "If someone else answered, say you will try again another time, share "
        "nothing, and call record_call_outcome with wrong_person.\n"
        "Once verified, call get_patient_status to learn the details, then tell "
        "them. Before the call ends, call record_call_outcome."
    )
