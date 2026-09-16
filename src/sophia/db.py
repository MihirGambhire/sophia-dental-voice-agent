"""
SQLite schema, connection handling and seeding.

The database is the single source of truth for everything Sophia is
allowed to say about availability, fees and a patient's history. The
language model never invents any of it, it only calls tools that read
and write these tables.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from . import clock, config

SCHEMA = """
PRAGMA foreign_keys = ON;

-- Clinicians. Names in the seed data are invented, the roles mirror the
-- real practice structure.
CREATE TABLE IF NOT EXISTS clinicians (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    role             TEXT NOT NULL,
    sees_nhs         INTEGER NOT NULL DEFAULT 0,
    sees_private     INTEGER NOT NULL DEFAULT 1,
    special_interest TEXT
);

-- Working hours, one row per continuous block. Splitting each day into a
-- morning and an afternoon block means the 13:00 to 14:00 lunch closure
-- cannot be booked over by accident.
CREATE TABLE IF NOT EXISTS clinician_hours (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clinician_id INTEGER NOT NULL REFERENCES clinicians(id) ON DELETE CASCADE,
    weekday      INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time   TEXT NOT NULL,
    end_time     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hours_clinician_weekday
    ON clinician_hours (clinician_id, weekday);

-- What can be booked, how long it takes and what it costs.
CREATE TABLE IF NOT EXISTS appointment_types (
    code             TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    duration_min     INTEGER NOT NULL,
    clinician_role   TEXT NOT NULL,
    funding          TEXT NOT NULL CHECK (funding IN ('nhs', 'private')),
    nhs_band         TEXT,
    fee_gbp          REAL NOT NULL,
    fee_is_from      INTEGER NOT NULL DEFAULT 0,
    eligibility_rule TEXT NOT NULL,
    is_bookable      INTEGER NOT NULL DEFAULT 1,
    description      TEXT
);

-- Patients. Entirely fake data.
CREATE TABLE IF NOT EXISTS patients (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL UNIQUE,
    full_name          TEXT NOT NULL,
    dob                TEXT NOT NULL,
    postcode           TEXT NOT NULL,
    phone              TEXT,
    email              TEXT,
    patient_type       TEXT NOT NULL CHECK (patient_type IN ('nhs', 'private')),
    first_seen_date    TEXT,
    last_attended_date TEXT,
    guardian_name      TEXT,
    guardian_dob       TEXT,
    notes              TEXT
);
CREATE INDEX IF NOT EXISTS idx_patients_lookup ON patients (full_name, dob, postcode);

-- Booked appointments. start_time and end_time are local wall clock,
-- see clock.py for why.
CREATE TABLE IF NOT EXISTS appointments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id   INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    clinician_id INTEGER NOT NULL REFERENCES clinicians(id),
    type_code    TEXT NOT NULL REFERENCES appointment_types(code),
    start_time   TEXT NOT NULL,
    end_time     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'booked'
                 CHECK (status IN ('booked', 'cancelled', 'snc', 'fta', 'attended')),
    fee_quoted   REAL,
    booked_via   TEXT DEFAULT 'sophia',
    created_at   TEXT NOT NULL,
    cancelled_at TEXT,
    notes        TEXT
);
CREATE INDEX IF NOT EXISTS idx_appts_patient ON appointments (patient_id, status);
CREATE INDEX IF NOT EXISTS idx_appts_when ON appointments (start_time, status);
CREATE INDEX IF NOT EXISTS idx_appts_clinician ON appointments (clinician_id, start_time);

-- The limited number of same day urgent appointments, released from 08:00.
CREATE TABLE IF NOT EXISTS urgent_slots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_date      TEXT NOT NULL,
    slot_time      TEXT NOT NULL,
    clinician_id   INTEGER NOT NULL REFERENCES clinicians(id),
    status         TEXT NOT NULL DEFAULT 'available'
                   CHECK (status IN ('available', 'taken')),
    appointment_id INTEGER REFERENCES appointments(id),
    UNIQUE (slot_date, slot_time, clinician_id)
);
CREATE INDEX IF NOT EXISTS idx_urgent_date ON urgent_slots (slot_date, status);

-- Short notice cancellations and failures to attend, counted over a
-- rolling 24 month window.
CREATE TABLE IF NOT EXISTS attendance_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id     INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    event_type     TEXT NOT NULL CHECK (event_type IN ('SNC', 'FTA')),
    event_date     TEXT NOT NULL,
    appointment_id INTEGER REFERENCES appointments(id),
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_patient ON attendance_events (patient_id, event_date);

-- Anything Sophia cannot handle becomes a message for the human team.
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_name     TEXT NOT NULL,
    callback_number TEXT,
    patient_id      INTEGER REFERENCES patients(id),
    reason          TEXT NOT NULL,
    detail          TEXT,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'actioned'))
);

-- Outbound call queue, used in phase 2.
CREATE TABLE IF NOT EXISTS reminder_queue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id     INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    appointment_id INTEGER REFERENCES appointments(id),
    reason         TEXT NOT NULL CHECK (reason IN ('day_before', 'recall')),
    due_at         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'called', 'no_answer', 'skipped')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_outcome   TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminder_queue (due_at, status);
"""


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """
    Open a connection with sensible defaults.

    Rows come back as sqlite3.Row so callers can use column names, and
    foreign keys are enforced, which SQLite does not do by default.
    """
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Create every table and index. Safe to run more than once."""
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _load_seed(filename: str) -> dict:
    with open(config.SEED_DIR / filename, encoding="utf-8") as handle:
        return json.load(handle)


def _days_ago(days: int, reference: date) -> str:
    return (reference - timedelta(days=days)).strftime(clock.DB_DATE_FORMAT)


def _clinician_blocks(conn: sqlite3.Connection, clinician_id: int, weekday: int):
    return conn.execute(
        "SELECT start_time, end_time FROM clinician_hours "
        "WHERE clinician_id = ? AND weekday = ? ORDER BY start_time",
        (clinician_id, weekday),
    ).fetchall()


def _find_real_slot(
    conn: sqlite3.Connection,
    clinician_id: int,
    wanted_start: datetime,
    duration_min: int,
    search_days: int = 30,
) -> datetime:
    """
    Move a requested appointment time to a moment the clinician actually works.

    The seed data asks for things like "in 5 days at 10:00". Depending on
    which day the database is seeded, that may land on a Saturday, in the
    lunch hour, or on a day this clinician is not in. We walk forwards a
    day at a time, keeping the requested time where possible, and fall
    back to the start of their first block of the day if not.
    """
    wanted_time = wanted_start.time()

    for offset in range(search_days):
        day = wanted_start.date() + timedelta(days=offset)
        if not clock.is_open_day(day):
            continue
        blocks = _clinician_blocks(conn, clinician_id, day.weekday())
        if not blocks:
            continue

        # First choice: the exact time requested, if it fits in a block.
        for block in blocks:
            block_start = clock.parse_time(block["start_time"])
            block_end = clock.parse_time(block["end_time"])
            if block_start <= wanted_time:
                candidate = clock.combine(day, wanted_time)
                finish = candidate + timedelta(minutes=duration_min)
                if finish.time() <= block_end and finish.date() == day:
                    if candidate > clock.now():
                        return candidate

        # Second choice: the earliest block start that still fits.
        for block in blocks:
            candidate = clock.combine(day, clock.parse_time(block["start_time"]))
            finish = candidate + timedelta(minutes=duration_min)
            if (
                finish.time() <= clock.parse_time(block["end_time"])
                and candidate > clock.now()
            ):
                return candidate

    raise RuntimeError(
        f"Could not place an appointment for clinician {clinician_id} "
        f"within {search_days} days of {wanted_start}"
    )


def seed_clinicians(conn: sqlite3.Connection) -> None:
    data = _load_seed("clinicians.json")
    for clinician in data["clinicians"]:
        cursor = conn.execute(
            "INSERT INTO clinicians (code, name, role, sees_nhs, sees_private, special_interest) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                clinician["code"],
                clinician["name"],
                clinician["role"],
                int(clinician["sees_nhs"]),
                int(clinician["sees_private"]),
                clinician.get("special_interest"),
            ),
        )
        clinician_id = cursor.lastrowid
        for block in clinician["hours"]:
            conn.execute(
                "INSERT INTO clinician_hours (clinician_id, weekday, start_time, end_time) "
                "VALUES (?, ?, ?, ?)",
                (clinician_id, block["weekday"], block["start"], block["end"]),
            )
    conn.commit()


def seed_appointment_types(conn: sqlite3.Connection) -> None:
    data = _load_seed("appointment_types.json")
    for appointment_type in data["appointment_types"]:
        conn.execute(
            "INSERT INTO appointment_types "
            "(code, name, duration_min, clinician_role, funding, nhs_band, fee_gbp, "
            " fee_is_from, eligibility_rule, is_bookable, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                appointment_type["code"],
                appointment_type["name"],
                appointment_type["duration_min"],
                appointment_type["clinician_role"],
                appointment_type["funding"],
                appointment_type["nhs_band"],
                appointment_type["fee_gbp"],
                int(appointment_type.get("fee_is_from", False)),
                appointment_type["eligibility_rule"],
                int(appointment_type["is_bookable"]),
                appointment_type.get("description"),
            ),
        )
    conn.commit()


def seed_patients(conn: sqlite3.Connection) -> None:
    """Insert the fake patients, their history and their booked appointments."""
    data = _load_seed("fake_patients.json")
    today = clock.today()
    created_at = clock.to_db(clock.now())

    for patient in data["patients"]:
        cursor = conn.execute(
            "INSERT INTO patients "
            "(code, full_name, dob, postcode, phone, email, patient_type, "
            " first_seen_date, last_attended_date, guardian_name, guardian_dob, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                patient["code"],
                patient["full_name"],
                patient["dob"],
                patient["postcode"],
                patient.get("phone"),
                patient.get("email"),
                patient["patient_type"],
                _days_ago(patient["first_seen_days_ago"], today),
                _days_ago(patient["last_attended_days_ago"], today),
                patient.get("guardian_name"),
                patient.get("guardian_dob"),
                patient.get("scenario"),
            ),
        )
        patient_id = cursor.lastrowid

        for event in patient.get("attendance_events", []):
            conn.execute(
                "INSERT INTO attendance_events (patient_id, event_type, event_date, notes) "
                "VALUES (?, ?, ?, ?)",
                (
                    patient_id,
                    event["event_type"],
                    _days_ago(event["days_ago"], today),
                    "Seeded history",
                ),
            )

        for appointment in patient.get("appointments", []):
            type_row = conn.execute(
                "SELECT duration_min, fee_gbp FROM appointment_types WHERE code = ?",
                (appointment["type_code"],),
            ).fetchone()
            clinician_row = conn.execute(
                "SELECT id FROM clinicians WHERE code = ?",
                (appointment["clinician_code"],),
            ).fetchone()

            wanted = clock.combine(
                today + timedelta(days=appointment["in_days"]),
                clock.parse_time(appointment["time"]),
            )
            start = _find_real_slot(
                conn, clinician_row["id"], wanted, type_row["duration_min"]
            )
            end = start + timedelta(minutes=type_row["duration_min"])

            conn.execute(
                "INSERT INTO appointments "
                "(patient_id, clinician_id, type_code, start_time, end_time, status, "
                " fee_quoted, booked_via, created_at, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    patient_id,
                    clinician_row["id"],
                    appointment["type_code"],
                    clock.to_db(start),
                    clock.to_db(end),
                    appointment.get("status", "booked"),
                    type_row["fee_gbp"],
                    "seed",
                    created_at,
                    appointment.get("scenario"),
                ),
            )
    conn.commit()


def seed_urgent_slots(conn: sqlite3.Connection) -> None:
    """
    Create the limited daily urgent appointments.

    A real practice holds a handful of slots back each morning. We spread
    six across the day and attach each one to a dentist who is actually
    working at that time and who sees NHS patients.
    """
    times = ["08:20", "09:00", "11:40", "14:20", "15:40", "17:20"]
    today = clock.today()

    dentists = conn.execute(
        "SELECT id FROM clinicians WHERE role IN ('principal_dentist', 'associate_dentist') "
        "AND sees_nhs = 1 ORDER BY id"
    ).fetchall()

    for offset in range(config.URGENT_SLOT_HORIZON_DAYS):
        day = today + timedelta(days=offset)
        if not clock.is_open_day(day):
            continue
        for slot_time in times:
            wanted = clock.parse_time(slot_time)
            for dentist in dentists:
                blocks = _clinician_blocks(conn, dentist["id"], day.weekday())
                works_then = any(
                    clock.parse_time(b["start_time"])
                    <= wanted
                    < clock.parse_time(b["end_time"])
                    for b in blocks
                )
                if works_then:
                    conn.execute(
                        "INSERT OR IGNORE INTO urgent_slots "
                        "(slot_date, slot_time, clinician_id, status) VALUES (?, ?, ?, 'available')",
                        (day.strftime(clock.DB_DATE_FORMAT), slot_time, dentist["id"]),
                    )
                    break
    conn.commit()


def seed_reminder_queue(conn: sqlite3.Connection) -> None:
    """
    Queue the outbound calls used in phase 2.

    Day before reminders for every appointment happening tomorrow, plus
    recall calls for patients who are overdue a check up but not yet past
    the three year registration lapse.
    """
    now_local = clock.now()
    tomorrow = clock.today() + timedelta(days=1)

    upcoming = conn.execute(
        "SELECT id, patient_id, start_time FROM appointments "
        "WHERE status = 'booked' AND date(start_time) = ?",
        (tomorrow.strftime(clock.DB_DATE_FORMAT),),
    ).fetchall()
    for appointment in upcoming:
        due = clock.from_db(appointment["start_time"]) - timedelta(days=1)
        conn.execute(
            "INSERT INTO reminder_queue (patient_id, appointment_id, reason, due_at, status) "
            "VALUES (?, ?, 'day_before', ?, 'pending')",
            (appointment["patient_id"], appointment["id"], clock.to_db(due)),
        )

    lapse_days = config.REGISTRATION_LAPSE_YEARS * 365
    recall_cutoff = (clock.today() - timedelta(days=int(lapse_days * 0.8))).strftime(
        clock.DB_DATE_FORMAT
    )
    lapse_cutoff = (clock.today() - timedelta(days=lapse_days)).strftime(
        clock.DB_DATE_FORMAT
    )
    overdue = conn.execute(
        "SELECT id FROM patients WHERE last_attended_date < ? AND last_attended_date >= ?",
        (recall_cutoff, lapse_cutoff),
    ).fetchall()
    for patient in overdue:
        conn.execute(
            "INSERT INTO reminder_queue (patient_id, reason, due_at, status) "
            "VALUES (?, 'recall', ?, 'pending')",
            (patient["id"], clock.to_db(now_local)),
        )
    conn.commit()


def seed_all(conn: sqlite3.Connection) -> None:
    """Populate an empty database in dependency order."""
    seed_clinicians(conn)
    seed_appointment_types(conn)
    seed_patients(conn)
    seed_urgent_slots(conn)
    seed_reminder_queue(conn)


def is_seeded(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM clinicians").fetchone()
    return row["n"] > 0


def reset_database(db_path: Path | str | None = None) -> sqlite3.Connection:
    """
    Delete the database file and build a fresh, fully seeded one.

    Used by scripts/init_db.py and by the test fixtures.
    """
    path = Path(db_path) if db_path else config.DB_PATH
    if path.exists():
        path.unlink()
    conn = connect(path)
    create_schema(conn)
    seed_all(conn)
    return conn


def summary(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts per table, used by the init script to confirm the seed worked."""
    tables = [
        "clinicians",
        "clinician_hours",
        "appointment_types",
        "patients",
        "appointments",
        "urgent_slots",
        "attendance_events",
        "messages",
        "reminder_queue",
    ]
    return {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in tables
    }
