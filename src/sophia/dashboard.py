"""
The practice team's side of Sophia.

A receptionist that answers the phone is only half a product. The other
half is what the team sees afterwards: who needs a call back and how
urgently, what was booked or cancelled while they were busy, and what each
call was about. This module builds that view from the same database the
calls write to.

Two rules shape it, both from the rest of the project:

  - Business logic in code. Which messages are urgent and who deals with
    them is decided by rules here, using the same safety screen as the
    calls, not by asking the model.
  - Nothing invented. A call summary is assembled from what actually
    happened on the call, the tools that ran and what they changed, never
    written by the model, so it cannot describe a booking that was not made.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from . import clock, safety

# Who deals with what. The practice's real staffing is not public, so these
# are roles, the way a practice rota is usually described.
DUTY_DENTIST = "Duty dentist"
DENTIST = "Dentist"
PRACTICE_MANAGER = "Practice manager"
RECEPTION = "Reception"

URGENCY_ORDER = {"emergency": 0, "urgent": 1, "routine": 2}

_COMPLAINT = re.compile(
    r"\b(complain\w*|unhappy|not happy|refund|rude|disappointed|appeal|discharg\w*|feedback)\b",
    re.IGNORECASE,
)
_CLINICAL = re.compile(
    r"\b(pain\w*|hurt\w*|ache|aching|sore|swell\w*|swollen|bleed\w*|tooth\w*|teeth|gum\w*|filling|crown"
    r"|root canal|extraction|wisdom|abscess|sensitiv\w*|treatment\w*|x-?ray|medic\w*|numb\w*"
    r"|comfort\w*|nervous|anxious|scared|dentist|clinician|hygien\w*)\b",
    re.IGNORECASE,
)


def triage_message(reason: str, detail: str | None, caller_words: list[str] | None = None) -> tuple[str, str]:
    """
    How urgent a message is, and who should deal with it.

    Urgency comes from the same safety screen that runs on every caller
    turn, over the message and everything the caller said on the call, so a
    message about "questions about treatment" from a caller who mentioned a
    swollen face is urgent, as it should be. The worst level found wins.
    """
    texts = [reason or "", detail or "", *(caller_words or [])]
    levels = {safety.screen(text).level for text in texts if text}
    if safety.Level.EMERGENCY in levels:
        urgency = "emergency"
    elif safety.Level.URGENT in levels:
        urgency = "urgent"
    else:
        urgency = "routine"

    about = " ".join(part for part in (reason, detail) if part)
    if urgency != "routine":
        assigned = DUTY_DENTIST
    elif _COMPLAINT.search(about):
        assigned = PRACTICE_MANAGER
    elif _CLINICAL.search(about):
        assigned = DENTIST
    else:
        assigned = RECEPTION
    return urgency, assigned


# ---------------------------------------------------------------------------
# Call summaries
# ---------------------------------------------------------------------------

_ASKED_ABOUT = {
    "find_available_slots": "a booking",
    "book_appointment": "a booking",
    "recommend_appointment_type": "a booking",
    "get_urgent_slots_today": "an urgent appointment",
    "book_urgent_slot": "an urgent appointment",
    "check_cancellation": "cancelling",
    "cancel_appointment": "cancelling",
    "reschedule_appointment": "moving an appointment",
    "get_patient_status": "their appointments",
    "search_practice_info": "practice information",
    "get_fee": "fees",
    "take_message": "a message for the team",
    "register_new_patient": "joining the practice",
}

_SAFETY_RANK = {
    safety.Level.ROUTINE.value: 0,
    safety.Level.URGENT.value: 1,
    safety.Level.EMERGENCY.value: 2,
}


def summarise_call(agent, started: datetime, ended: datetime, ended_by: str) -> dict:
    """
    What happened on one call, from the record of the call itself.

    Only facts the call produced are used: the caller's own words, the
    tools that ran, and what the tools changed.
    """
    tools = agent.tools
    history = list(agent.history)

    if tools.verified_patient_id in tools.registered_patient_ids:
        who = f"{tools.verified_name} (new patient, registered on this call)"
    elif tools.verified_patient_id:
        who = f"{tools.verified_name} (existing patient, identity checked)"
    elif tools.caller_name:
        who = f"{tools.caller_name} (identity not checked)"
    else:
        who = "Unknown caller (identity not checked)"
    if agent.reminder is not None:
        who = f"{agent.reminder.patient_name} (Sophia rang them)"

    asked: list[str] = []
    for turn in history:
        for name in turn.tools_used:
            topic = _ASKED_ABOUT.get(name)
            if topic and topic not in asked:
                asked.append(topic)

    outcomes: list[str] = []
    for patient_id in tools.registered_patient_ids:
        outcomes.append("Registered as a new private patient")
    for booking in tools.bookings_made:
        outcomes.append(f"Booked {booking['type']}, {booking['when']}, {booking['fee']}")
    for cancelled in getattr(tools, "cancellations_made", []):
        outcomes.append(f"Cancelled {cancelled}")
    if tools.message_id is not None:
        row = agent.conn.execute(
            "SELECT urgency, assigned_to FROM messages WHERE id = ?", (tools.message_id,)
        ).fetchone()
        if row is not None:
            outcomes.append(f"Message left for {row['assigned_to']} ({row['urgency']})")
    if not outcomes:
        outcomes.append("Questions answered, nothing booked or changed")

    worst = max((turn.safety_level for turn in history), key=lambda level: _SAFETY_RANK.get(level, 0),
                default=safety.Level.ROUTINE.value)
    safety_note = {
        safety.Level.EMERGENCY.value: "999 advice given",
        safety.Level.URGENT.value: "Same day problem mentioned",
    }.get(worst, "")

    # A same day problem, or worse, that ended with nothing booked and no
    # message: a test call mentioned a swollen face and left with neither.
    # The team should see that, rather than find it in a list of calls.
    follow_up = bool(safety_note) and not tools.bookings_made and tools.message_id is None

    first_words = next((turn.user for turn in history if turn.user.strip()), "")
    return {
        "follow_up": follow_up,
        "started_at": clock.to_db(started),
        "ended_at": clock.to_db(ended),
        "caller": who,
        "patient_id": tools.verified_patient_id,
        "direction": "outbound" if agent.reminder is not None else "inbound",
        "asked_about": asked,
        "outcomes": outcomes,
        "safety": safety_note,
        "turns": len(history),
        "first_words": first_words[:160],
        "ended_by": ended_by,
    }


def record_call(conn, summary: dict) -> None:
    """Keep a call's summary for the team. Calls where nothing was said are skipped."""
    if summary["turns"] == 0:
        return
    conn.execute(
        "INSERT INTO calls (started_at, ended_at, caller, patient_id, direction, asked_about, "
        "outcomes, safety, turns, first_words, ended_by, follow_up) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            summary["started_at"], summary["ended_at"], summary["caller"], summary["patient_id"],
            summary["direction"], json.dumps(summary["asked_about"]), json.dumps(summary["outcomes"]),
            summary["safety"], summary["turns"], summary["first_words"], summary["ended_by"],
            int(summary.get("follow_up", False)),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# The whole view
# ---------------------------------------------------------------------------

URGENT_TYPES = {"NHS_URGENT", "PRIV_EMERG_NEW", "PRIV_EMERG_EXISTING"}
CALENDAR_DAYS = 5


def _open_days_from(day: date, count: int) -> list[date]:
    days: list[date] = []
    while len(days) < count:
        if clock.is_open_day(day):
            days.append(day)
        day += timedelta(days=1)
    return days


def _time_label(value: datetime) -> str:
    return value.strftime("%H:%M")


def team_view(conn, now: datetime | None = None) -> dict:
    """Everything the practice team page shows, for one tester's data."""
    now = now or clock.now()
    days = _open_days_from(now.date(), CALENDAR_DAYS)
    first, last = days[0], days[-1] + timedelta(days=1)

    rows = conn.execute(
        "SELECT a.*, p.full_name AS patient, c.name AS clinician, t.name AS type_name "
        "FROM appointments a JOIN patients p ON p.id = a.patient_id "
        "JOIN clinicians c ON c.id = a.clinician_id JOIN appointment_types t ON t.code = a.type_code "
        "WHERE a.start_time >= ? AND a.start_time < ? ORDER BY a.start_time",
        (clock.to_db(clock.combine(first, clock.parse_time("00:00"))),
         clock.to_db(clock.combine(last, clock.parse_time("00:00")))),
    ).fetchall()

    calendar = []
    for day in days:
        on_day = [row for row in rows if clock.from_db(row["start_time"]).date() == day]
        slots = conn.execute(
            "SELECT status, COUNT(*) AS n FROM urgent_slots WHERE slot_date = ? GROUP BY status",
            (day.strftime(clock.DB_DATE_FORMAT),),
        ).fetchall()
        counts = {row["status"]: row["n"] for row in slots}
        calendar.append({
            "date": day.isoformat(),
            "label": day.strftime("%a %d %b"),
            "today": day == now.date(),
            "urgent_free": counts.get("available", 0),
            "urgent_taken": counts.get("taken", 0),
            "appointments": [
                {
                    "time": _time_label(clock.from_db(row["start_time"])),
                    "patient": row["patient"],
                    "clinician": row["clinician"],
                    "type": row["type_name"],
                    "fee": row["fee_quoted"],
                    "status": row["status"],
                    "urgent": row["type_code"] in URGENT_TYPES,
                    "by_sophia": row["booked_via"] == "sophia",
                    "cancelled_by_sophia": row["cancelled_via"] == "sophia",
                }
                for row in on_day
            ],
        })

    messages = [
        {
            "id": row["id"],
            "caller": row["caller_name"],
            "callback": row["callback_number"],
            "reason": row["reason"],
            "detail": row["detail"],
            "urgency": row["urgency"],
            "assigned_to": row["assigned_to"],
            "status": row["status"],
            "at": _time_label(clock.from_db(row["created_at"])),
        }
        for row in conn.execute("SELECT * FROM messages ORDER BY created_at DESC").fetchall()
    ]
    messages.sort(key=lambda m: (m["status"] != "open", URGENCY_ORDER.get(m["urgency"], 9)))

    actions = []
    for row in conn.execute(
        "SELECT a.*, p.full_name AS patient, t.name AS type_name FROM appointments a "
        "JOIN patients p ON p.id = a.patient_id JOIN appointment_types t ON t.code = a.type_code "
        "WHERE a.booked_via = 'sophia' OR a.cancelled_via = 'sophia'"
    ).fetchall():
        when = clock.spoken_datetime(clock.from_db(row["start_time"]))
        if row["booked_via"] == "sophia":
            actions.append({"at": row["created_at"], "kind": "booked",
                            "text": f"Booked {row['patient']}: {row['type_name']}, {when}"})
        if row["cancelled_via"] == "sophia" and row["cancelled_at"]:
            short = " (short notice)" if row["status"] == "snc" else ""
            actions.append({"at": row["cancelled_at"], "kind": "cancelled",
                            "text": f"Cancelled {row['patient']}: {row['type_name']}, {when}{short}"})
    for row in conn.execute("SELECT full_name, notes, id FROM patients WHERE code LIKE 'new-%'").fetchall():
        actions.append({"at": "", "kind": "registered", "text": f"Registered {row['full_name']} as a new patient"})
    actions.sort(key=lambda a: a["at"], reverse=True)
    for action in actions:
        action["at"] = _time_label(clock.from_db(action["at"])) if action["at"] else ""

    calls = []
    for row in conn.execute("SELECT * FROM calls ORDER BY started_at DESC").fetchall():
        started, ended = clock.from_db(row["started_at"]), clock.from_db(row["ended_at"])
        seconds = max(0, int((ended - started).total_seconds()))
        calls.append({
            "at": _time_label(started),
            "caller": row["caller"],
            "direction": row["direction"],
            "asked_about": json.loads(row["asked_about"] or "[]"),
            "outcomes": json.loads(row["outcomes"] or "[]"),
            "safety": row["safety"],
            "turns": row["turns"],
            "length": f"{seconds // 60}m {seconds % 60:02d}s",
            "first_words": row["first_words"],
            "ended_by": row["ended_by"],
            "follow_up": bool(row["follow_up"]),
        })

    today = next((day for day in calendar if day["today"]), None)
    open_messages = [m for m in messages if m["status"] == "open"]
    return {
        "now": now.strftime("%A %d %B, %H:%M"),
        "stats": {
            "appointments_today": len([a for a in (today or {}).get("appointments", []) if a["status"] == "booked"]),
            "urgent_free_today": (today or {}).get("urgent_free", 0),
            "open_messages": len(open_messages),
            "urgent_messages": len([m for m in open_messages if m["urgency"] != "routine"]),
            "calls": len(calls),
            "follow_ups": len([c for c in calls if c["follow_up"]]),
        },
        "calendar": calendar,
        "messages": messages,
        "actions": actions,
        "calls": calls,
    }


def mark_message_done(conn, message_id: int) -> bool:
    """The team has dealt with a message."""
    cursor = conn.execute("UPDATE messages SET status = 'actioned' WHERE id = ?", (message_id,))
    conn.commit()
    return cursor.rowcount == 1
