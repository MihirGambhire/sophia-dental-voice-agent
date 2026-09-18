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

import re
import uuid
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
                # Kept by the browser, to put back after a server restart.
                "dob": row["dob"] if registered else None,
                "try": REGISTERED if registered else TRY[row["code"]],
                "next_appointment": (
                    clock.spoken_datetime(clock.from_db(upcoming["start_time"])) if upcoming else None
                ),
                "registered_by_you": registered,
            }
        )
    return cards


def practice_card(conn) -> dict:
    """
    The practice facts Sophia works from, for testers to check her against.

    Built from the same configuration and fee table her tools read, not
    typed out separately, so the page cannot drift from what she says.
    """
    from . import config, policies

    fees = {"nhs": [], "private": []}
    for row in conn.execute(
        "SELECT name, funding, fee_gbp, fee_is_from, is_bookable FROM appointment_types ORDER BY funding, fee_gbp"
    ):
        fees[row["funding"]].append(
            {
                "name": row["name"],
                "fee": policies.format_fee(row["fee_gbp"], bool(row["fee_is_from"])),
                "bookable_by_phone": bool(row["is_bookable"]),
            }
        )

    def twelve_hour(value: str) -> str:
        hour, minute = (int(part) for part in value.split(":"))
        suffix = "am" if hour < 12 else "pm"
        hour = hour % 12 or 12
        return f"{hour}{':%02d' % minute if minute else ''}{suffix}"

    return {
        "hours": [
            {
                "days": "Monday to Friday",
                "times": f"{twelve_hour(config.OPENING_TIME)} to {twelve_hour(config.CLOSING_TIME)}, "
                f"closed {twelve_hour(config.LUNCH_START)} to {twelve_hour(config.LUNCH_END)} for lunch",
            },
            {"days": "Saturday and Sunday", "times": "Closed"},
        ],
        "contact": [
            {"label": "Practice phone", "value": config.PRACTICE_PHONE},
            {"label": "Address", "value": config.PRACTICE_ADDRESS},
            {"label": "Parking", "value": "Nearby, not on site"},
            {"label": "Out of hours dental", "value": config.OUT_OF_HOURS_DENTAL_NUMBER},
            {"label": "NHS urgent advice", "value": config.NHS_URGENT_NUMBER},
            {"label": "Life threatening emergency", "value": config.EMERGENCY_NUMBER},
        ],
        "rules": [
            f"Urgent appointments: a limited number each weekday, released by phone from "
            f"{twelve_hour(config.URGENT_RELEASE_TIME)} on the day and not bookable ahead. Open to "
            "patients and to people with no regular dentist.",
            "New NHS patients: places are very limited and the waiting list is paused. New private "
            "patients are welcome.",
            f"Registration lapses after {config.REGISTRATION_LAPSE_YEARS} years without attending, "
            "and the patient is then treated as new.",
            f"Cancelling needs at least {config.SHORT_NOTICE_HOURS} hours notice. Less is a short "
            "notice cancellation, and repeated late cancellations or missed appointments can mean "
            "removal from the patient list.",
            "Sophia cannot give clinical or medicine advice, and cannot take payment.",
        ],
        "fees": fees,
        "not_priced": (
            "Private fillings, imaging, implants, whitening, crowns, orthodontics and dentures are not "
            "priced here. Asked about them, Sophia should take a message rather than give a figure."
        ),
    }


# How many registered patients a browser may put back, and the shapes they
# must have. Each tester's database is their own copy, so this only ever
# restores fake data into the tester's own sandbox.
MAX_RESTORED = 20
_RESTORE_NAME = re.compile(r"^[A-Za-z][A-Za-z '\-]{1,79}$")
_RESTORE_POSTCODE = re.compile(r"^[A-Za-z0-9 ]{2,10}$")
_RESTORE_PHONE = re.compile(r"^[0-9+ ]{6,20}$")


def restore_registered(conn, patients: list[dict]) -> int:
    """
    Put back patients a tester registered, after the server lost them.

    Render's free plan wipes the server's files whenever it sleeps, after
    fifteen idle minutes, and on every deploy. A tester who registered and
    came back later found themselves gone. The browser keeps a copy of what
    it was shown and sends it back; anything already there, or not in the
    shape a registration produces, is skipped. Returns how many were added.
    """
    added = 0
    for patient in patients[:MAX_RESTORED]:
        if not isinstance(patient, dict):
            continue
        name = str(patient.get("name") or "").strip()
        dob = str(patient.get("dob") or "").strip()
        postcode = str(patient.get("postcode") or "").strip()
        phone = str(patient.get("phone") or "").strip()
        try:
            datetime.strptime(dob, clock.DB_DATE_FORMAT)
        except ValueError:
            continue
        if not (_RESTORE_NAME.match(name) and _RESTORE_POSTCODE.match(postcode) and _RESTORE_PHONE.match(phone)):
            continue
        exists = conn.execute(
            "SELECT 1 FROM patients WHERE lower(full_name) = lower(?) AND dob = ?", (name, dob)
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO patients (code, full_name, dob, postcode, phone, patient_type, notes) "
            "VALUES (?, ?, ?, ?, ?, 'private', ?)",
            (f"new-{uuid.uuid4().hex[:10]}", name, dob, postcode, phone,
             "Registered by phone with Sophia on an earlier call. Not yet seen."),
        )
        added += 1
    conn.commit()
    return added
