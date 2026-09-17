"""
The fake patient records a tester can call as.

WHY THIS EXISTS
===============
Everything about an existing patient, their fee, their cancellations,
their reminder call, needs someone to call as that patient. Testers on the
shared link had no way to know who was on the list, so half of what Sophia
does could not be tried. The page now lists the invented records with
what each one is for, and anything the tester registered as a new patient,
so they can call back as them.

A real practice system would never show this. It is safe here only because
every record is invented, and each tester sees their own copy of the data.
"""

from __future__ import annotations

from datetime import datetime

from . import clock

# What each seeded patient is for, in the words a tester needs. Seeded
# patients without an entry here are not listed, including the child, whose
# guardian flow is not built for a caller to try.
TRY: dict[str, str] = {
    "hollis": "NHS patient. Book a routine check up, which should cost £27.90.",
    "pritchard": "Not seen for over three years. A check up should be the £85 new patient examination.",
    "okafor": "Private patient with an appointment in a few days. Cancelling it brings no penalty.",
    "kaur": "New patient with an appointment soon. Cancelling inside 24 hours brings a warning.",
    "brennan": "NHS patient with two late cancellations already. Cancelling brings a warning.",
    "reyes": "NHS patient not seen for three years. An NHS check up is not available, private is.",
    "bannerman": "Give her name and postcode with a wrong date of birth. Sophia should reveal nothing.",
    "ashworth": "NHS patient with an appointment coming up.",
    "fairbanks": "Due a check up. Sophia may ring her: see Sophia calls you.",
}

REGISTERED = "Registered by you as a new patient on this demo."


def _spoken_birth_date(value: str) -> str:
    day = datetime.strptime(value, clock.DB_DATE_FORMAT).date()
    return f"{day.day} {clock._MONTHS[day.month - 1]} {day.year}"


def patient_cards(conn, now: datetime | None = None) -> list[dict]:
    """Seeded patients worth trying, then any the tester registered, newest last."""
    now = now or clock.now()
    rows = conn.execute("SELECT * FROM patients ORDER BY id").fetchall()
    cards = []
    for row in rows:
        registered = row["code"].startswith("new-")
        if not registered and row["code"] not in TRY:
            continue
        upcoming = conn.execute(
            "SELECT start_time FROM appointments WHERE patient_id = ? AND status = 'booked' "
            "AND start_time > ? ORDER BY start_time LIMIT 1",
            (row["id"], clock.to_db(now)),
        ).fetchone()
        cards.append(
            {
                "name": row["full_name"],
                "born": _spoken_birth_date(row["dob"]),
                "postcode": row["postcode"],
                "phone": row["phone"] if registered else None,
                "try": REGISTERED if registered else TRY[row["code"]],
                "next_appointment": (
                    clock.spoken_datetime(clock.from_db(upcoming["start_time"])) if upcoming else None
                ),
                "registered_by_you": registered,
            }
        )
    return cards
