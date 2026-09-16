"""
Build the Sophia database from scratch.

Usage, from the repository root:

    python scripts/init_db.py

This deletes any existing data/sophia.db and rebuilds it with the seed
data, so every demo run starts from a known, sensible state.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sophia import clock, config, db  # noqa: E402


def main() -> None:
    print(f"Building {config.DB_PATH}")
    conn = db.reset_database()

    counts = db.summary(conn)
    width = max(len(name) for name in counts)
    for table, count in counts.items():
        print(f"  {table.ljust(width)}  {count}")

    print("\nUpcoming appointments:")
    rows = conn.execute(
        "SELECT a.start_time, p.full_name, c.name AS clinician, t.name AS type_name "
        "FROM appointments a "
        "JOIN patients p ON p.id = a.patient_id "
        "JOIN clinicians c ON c.id = a.clinician_id "
        "JOIN appointment_types t ON t.code = a.type_code "
        "WHERE a.status = 'booked' ORDER BY a.start_time"
    ).fetchall()
    for row in rows:
        when = clock.spoken_datetime(clock.from_db(row["start_time"]))
        print(f"  {when}: {row['full_name']}, {row['type_name']}, {row['clinician']}")

    today = clock.today().strftime(clock.DB_DATE_FORMAT)
    urgent_today = conn.execute(
        "SELECT COUNT(*) AS n FROM urgent_slots WHERE slot_date = ? AND status = 'available'",
        (today,),
    ).fetchone()["n"]
    released = "released" if clock.urgent_slots_released() else "not released yet"
    print(f"\nUrgent slots today: {urgent_today} available, {released}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
