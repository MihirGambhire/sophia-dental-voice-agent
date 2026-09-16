"""
The tools Sophia can call, and the guard rails around them.

This is the only way the language model can touch the practice's data.
Two principles run through the whole file:

  1. **Nothing is revealed or changed before the caller is verified.**
     Verification state lives here, on the session, not in the
     conversation. A model cannot talk its way past it, because the
     check is an `if` statement in Python, not an instruction in a
     prompt.

  2. **Every tool re-validates its own inputs.** A slot the model offers
     might have been taken since. A type code it invents might not
     exist. An appointment it names might belong to someone else. Each
     of those is checked again at the moment of writing, not trusted
     from earlier in the conversation.

Every method returns a plain dictionary, ready to be handed back to the
model as a tool result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import clock, config, policies
from .policies import PatientFacts

# Availability is searched on this grid. Ten minutes keeps the number of
# candidate slots manageable while still offering times that sound
# natural to say out loud, like twenty past nine.
SLOT_GRID_MINUTES = 10

# Which real clinician roles can deliver each appointment type's role.
ROLE_GROUPS: dict[str, tuple[str, ...]] = {
    "dentist": ("principal_dentist", "associate_dentist"),
    "endodontist": ("endodontist",),
    "hygiene_therapist": ("hygiene_therapist",),
    "dentist_or_therapist": (
        "principal_dentist",
        "associate_dentist",
        "hygiene_therapist",
    ),
}


class ToolError(Exception):
    """Raised when a tool is asked to do something it must not do."""


@dataclass
class Slot:
    """One bookable moment. Not stored, computed on demand."""

    clinician_id: int
    clinician_name: str
    start: datetime
    duration_min: int

    @property
    def ref(self) -> str:
        """
        A compact reference the model can pass back to book_appointment.

        Encoding the clinician and the exact start time means a booking
        request carries everything needed to re-check availability, and
        nothing has to be held in server side session state.
        """
        return f"{self.clinician_id}@{self.start.strftime('%Y-%m-%dT%H:%M')}"

    def spoken(self) -> str:
        return f"{clock.spoken_datetime(self.start)} with {self.clinician_name}"

    def as_dict(self) -> dict:
        return {
            "slot_ref": self.ref,
            "when": self.spoken(),
            "date": self.start.strftime(clock.DB_DATE_FORMAT),
            "time": self.start.strftime("%H:%M"),
            "clinician": self.clinician_name,
        }


def _normalise_name(value: str) -> str:
    """Collapse whitespace and case so "  mary  smith " matches "Mary Smith"."""
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _normalise_postcode(value: str) -> str:
    """Postcodes are compared without spaces or case. "wa1 1qn" matches "WA11QN"."""
    return re.sub(r"\s+", "", (value or "")).casefold()


def _normalise_dob(value: str) -> str | None:
    """
    Accept the several ways a caller might give a date of birth.

    A voice pipeline will transcribe "the third of March 1985" in more
    than one shape, so we accept the common written forms and normalise
    to the stored format. Anything unrecognised returns None, which the
    caller treats as a failed match rather than guessing.
    """
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).strftime(clock.DB_DATE_FORMAT)
        except ValueError:
            continue
    return None


class SophiaTools:
    """
    One conversation's worth of tool access.

    A new instance per call, so verification never leaks between callers.
    """

    def __init__(self, conn, now: datetime | None = None):
        self.conn = conn
        self._fixed_now = now
        self.verified_patient_id: int | None = None

    # -- helpers ----------------------------------------------------------

    @property
    def now(self) -> datetime:
        """Current time, or a pinned time when a test needs one."""
        return self._fixed_now or clock.now()

    def _require_verified(self, patient_id: int | None = None) -> int:
        """
        Refuse to continue unless the caller has been verified.

        This is the single choke point that protects every piece of
        patient data. It is intentionally boring.
        """
        if self.verified_patient_id is None:
            raise ToolError(
                "The caller has not been verified yet. Ask for their full name, "
                "date of birth and postcode, then call verify_patient."
            )
        if patient_id is not None and patient_id != self.verified_patient_id:
            raise ToolError(
                "That record belongs to a different patient than the one verified."
            )
        return self.verified_patient_id

    def _patient_row(self, patient_id: int):
        row = self.conn.execute(
            "SELECT * FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if row is None:
            raise ToolError("No such patient.")
        return row

    def _facts(self, patient_id: int) -> PatientFacts:
        return PatientFacts.from_row(self._patient_row(patient_id))

    def _events(self, patient_id: int) -> list[tuple[str, date]]:
        return [
            (
                row["event_type"],
                datetime.strptime(row["event_date"], clock.DB_DATE_FORMAT).date(),
            )
            for row in self.conn.execute(
                "SELECT event_type, event_date FROM attendance_events WHERE patient_id = ?",
                (patient_id,),
            )
        ]

    def _type_row(self, type_code: str):
        row = self.conn.execute(
            "SELECT * FROM appointment_types WHERE code = ?", (type_code,)
        ).fetchone()
        if row is None:
            raise ToolError(f"There is no appointment type called {type_code}.")
        return row

    # -- 1. verification --------------------------------------------------

    def verify_patient(self, full_name: str, dob: str, postcode: str) -> dict:
        """
        Confirm who is calling, using all three identifiers.

        All three must match. A partial match is reported as a failure
        with no hint about which part was wrong, because telling a caller
        "the postcode is right but the date of birth is wrong" would
        confirm that the postcode belongs to a real patient.
        """
        normalised_dob = _normalise_dob(dob)
        if normalised_dob is None:
            return {
                "verified": False,
                "reason": "date_not_understood",
                "say": "Sorry, I did not catch that date of birth. Could you give it to me again, starting with the day?",
            }

        row = self.conn.execute(
            "SELECT * FROM patients WHERE dob = ?", (normalised_dob,)
        ).fetchall()

        for candidate in row:
            name_matches = _normalise_name(candidate["full_name"]) == _normalise_name(
                full_name
            )
            postcode_matches = _normalise_postcode(
                candidate["postcode"]
            ) == _normalise_postcode(postcode)
            if name_matches and postcode_matches:
                self.verified_patient_id = candidate["id"]
                return {
                    "verified": True,
                    "patient_id": candidate["id"],
                    "first_name": candidate["full_name"].split()[0],
                    "say": "Thank you, I have found your record.",
                }

        # Deliberately identical response whether the patient does not
        # exist or the details did not line up.
        self.verified_patient_id = None
        return {
            "verified": False,
            "reason": "no_match",
            "say": (
                "I am sorry, I cannot find a record matching those details. "
                "I can take a message for the team, and they will call you back."
            ),
        }

    # -- 2. patient status ------------------------------------------------

    def get_patient_status(self) -> dict:
        """Where the verified patient stands: registration, funding, record, bookings."""
        patient_id = self._require_verified()
        row = self._patient_row(patient_id)
        facts = PatientFacts.from_row(row)
        status = policies.assess_attendance(self._events(patient_id), facts, self.now.date())

        upcoming = [
            {
                "appointment_id": appointment["id"],
                "when": clock.spoken_datetime(clock.from_db(appointment["start_time"])),
                "type": appointment["type_name"],
                "clinician": appointment["clinician_name"],
            }
            for appointment in self.conn.execute(
                "SELECT a.id, a.start_time, t.name AS type_name, c.name AS clinician_name "
                "FROM appointments a "
                "JOIN appointment_types t ON t.code = a.type_code "
                "JOIN clinicians c ON c.id = a.clinician_id "
                "WHERE a.patient_id = ? AND a.status = 'booked' AND a.start_time >= ? "
                "ORDER BY a.start_time",
                (patient_id, clock.to_db(self.now)),
            )
        ]

        days = policies.days_since_last_attended(facts, self.now.date())
        return {
            "name": row["full_name"],
            "patient_type": row["patient_type"],
            "is_registered": policies.is_registered(facts, self.now.date()),
            "treated_as_new_patient_for_fees": policies.is_new_for_fees(
                facts, self.now.date()
            ),
            "last_attended": row["last_attended_date"],
            "years_since_last_seen": round(days / 365.25, 1) if days is not None else None,
            "short_notice_cancellations_last_24_months": status.snc_count,
            "missed_appointments_last_24_months": status.fta_count,
            "close_to_discharge": status.one_more_snc_discharges,
            "upcoming_appointments": upcoming,
        }

    # -- 3. availability --------------------------------------------------

    def _booked_intervals(self, clinician_id: int, day: date):
        rows = self.conn.execute(
            "SELECT start_time, end_time FROM appointments "
            "WHERE clinician_id = ? AND status = 'booked' AND date(start_time) = ?",
            (clinician_id, day.strftime(clock.DB_DATE_FORMAT)),
        ).fetchall()
        return [(clock.from_db(r["start_time"]), clock.from_db(r["end_time"])) for r in rows]

    def _eligible_clinicians(self, type_row):
        roles = ROLE_GROUPS.get(type_row["clinician_role"])
        if not roles:
            raise ToolError(f"Unknown clinician role {type_row['clinician_role']}.")

        funding_column = "sees_nhs" if type_row["funding"] == "nhs" else "sees_private"
        placeholders = ",".join("?" * len(roles))
        return self.conn.execute(
            f"SELECT id, name FROM clinicians "
            f"WHERE role IN ({placeholders}) AND {funding_column} = 1 ORDER BY id",
            roles,
        ).fetchall()

    def find_available_slots(
        self,
        appointment_type: str,
        days_ahead: int | None = None,
        clinician_name: str | None = None,
        earliest_date: str | None = None,
        limit: int = 5,
    ) -> dict:
        """
        Real availability, computed from working hours minus what is booked.

        Availability is never stored, so it cannot drift out of date. The
        lunch closure and the weekend fall out automatically, because a
        clinician simply has no hours block covering them.
        """
        type_row = self._type_row(appointment_type)
        if not type_row["is_bookable"]:
            return {
                "slots": [],
                "reason": "not_bookable",
                "say": (
                    f"{type_row['name']} is not something I can book directly. "
                    "The team will confirm it after an assessment. "
                    "Would you like me to take a message?"
                ),
            }

        duration = type_row["duration_min"]
        clinicians = self._eligible_clinicians(type_row)
        if clinician_name:
            wanted = _normalise_name(clinician_name)
            filtered = [c for c in clinicians if wanted in _normalise_name(c["name"])]
            if filtered:
                clinicians = filtered

        horizon = days_ahead or config.DEFAULT_SEARCH_DAYS
        start_day = self.now.date()
        if earliest_date:
            try:
                requested = datetime.strptime(earliest_date, clock.DB_DATE_FORMAT).date()
                start_day = max(start_day, requested)
            except ValueError:
                pass

        found: list[Slot] = []
        days_with_availability = 0

        for offset in range(horizon):
            day = start_day + timedelta(days=offset)
            if not clock.is_open_day(day):
                continue
            slots_before_this_day = len(found)

            for clinician in clinicians:
                blocks = self.conn.execute(
                    "SELECT start_time, end_time FROM clinician_hours "
                    "WHERE clinician_id = ? AND weekday = ? ORDER BY start_time",
                    (clinician["id"], day.weekday()),
                ).fetchall()
                if not blocks:
                    continue

                booked = self._booked_intervals(clinician["id"], day)

                for block in blocks:
                    cursor = clock.combine(day, clock.parse_time(block["start_time"]))
                    block_end = clock.combine(day, clock.parse_time(block["end_time"]))

                    while cursor + timedelta(minutes=duration) <= block_end:
                        finish = cursor + timedelta(minutes=duration)
                        if cursor <= self.now:
                            cursor += timedelta(minutes=SLOT_GRID_MINUTES)
                            continue
                        clashes = any(
                            cursor < existing_end and finish > existing_start
                            for existing_start, existing_end in booked
                        )
                        if not clashes:
                            found.append(
                                Slot(
                                    clinician_id=clinician["id"],
                                    clinician_name=clinician["name"],
                                    start=cursor,
                                    duration_min=duration,
                                )
                            )
                        cursor += timedelta(minutes=SLOT_GRID_MINUTES)

            # Stop once we have enough DAYS to choose from, not enough
            # slots. A single morning easily yields dozens of ten minute
            # slots, so counting slots here would mean never looking past
            # the first day, and the caller would be offered four times on
            # the same morning instead of a real choice of days.
            if len(found) > slots_before_this_day:
                days_with_availability += 1
            if days_with_availability >= limit:
                break

        found.sort(key=lambda slot: slot.start)
        chosen = _spread(found, limit)

        return {
            "appointment_type": type_row["name"],
            "duration_minutes": duration,
            "fee": policies.format_fee(type_row["fee_gbp"], bool(type_row["fee_is_from"])),
            "slots": [slot.as_dict() for slot in chosen],
        }

    def get_urgent_slots_today(self) -> dict:
        """
        Today's limited urgent appointments, respecting the 08:00 release.

        Before 8am there is nothing to offer, and saying so is the honest
        answer rather than showing tomorrow's.
        """
        if not clock.is_open_day(self.now.date()):
            return {
                "released": False,
                "reason": "closed_today",
                "slots": [],
                "say": (
                    "The practice is closed today. For urgent dental problems you can "
                    f"call {config.OUT_OF_HOURS_DENTAL_NUMBER}, or NHS {config.NHS_URGENT_NUMBER}."
                ),
            }

        if not clock.urgent_slots_released(self.now):
            return {
                "released": False,
                "reason": "before_release",
                "slots": [],
                "say": (
                    "Today's urgent appointments are released at 8 o'clock this morning, "
                    "so they are not open yet. If you ring back from 8am we can look at "
                    f"what is available. If you cannot wait, NHS {config.NHS_URGENT_NUMBER} "
                    "can advise you."
                ),
            }

        rows = self.conn.execute(
            "SELECT u.id, u.slot_time, c.name AS clinician_name, u.clinician_id "
            "FROM urgent_slots u JOIN clinicians c ON c.id = u.clinician_id "
            "WHERE u.slot_date = ? AND u.status = 'available' ORDER BY u.slot_time",
            (self.now.strftime(clock.DB_DATE_FORMAT),),
        ).fetchall()

        remaining = [
            {
                "urgent_slot_id": row["id"],
                "time": row["slot_time"],
                "when": clock.spoken_time(clock.parse_time(row["slot_time"])),
                "clinician": row["clinician_name"],
            }
            for row in rows
            if clock.combine(self.now.date(), clock.parse_time(row["slot_time"])) > self.now
        ]

        if not remaining:
            return {
                "released": True,
                "slots": [],
                "reason": "none_left",
                "say": (
                    "I am sorry, today's urgent appointments have all gone. "
                    f"If you need to be seen today, NHS {config.NHS_URGENT_NUMBER} can help, "
                    f"or the out of hours dental service on {config.OUT_OF_HOURS_DENTAL_NUMBER}. "
                    "You are also welcome to ring us from 8am tomorrow."
                ),
            }

        return {"released": True, "slots": remaining, "remaining_count": len(remaining)}

    # -- 4. booking -------------------------------------------------------

    def book_appointment(self, slot_ref: str, appointment_type: str) -> dict:
        """
        Book a slot for the verified patient, re-checking everything first.

        The slot is re-validated at the moment of writing. Between being
        offered and being accepted it may have been taken, the clinician
        may not work then, or it may have fallen into the past.
        """
        patient_id = self._require_verified()
        type_row = self._type_row(appointment_type)

        if not type_row["is_bookable"]:
            raise ToolError(f"{type_row['name']} cannot be booked directly.")

        try:
            clinician_id_text, start_text = slot_ref.split("@", 1)
            clinician_id = int(clinician_id_text)
            start = clock.to_aware(datetime.strptime(start_text, "%Y-%m-%dT%H:%M"))
        except (ValueError, AttributeError):
            raise ToolError("That slot reference is not one I recognise.")

        duration = type_row["duration_min"]
        end = start + timedelta(minutes=duration)

        if start <= self.now:
            return {"booked": False, "reason": "in_the_past",
                    "say": "That time has already passed. Let me find you another."}

        if not clock.is_within_opening_hours(start):
            return {"booked": False, "reason": "outside_opening_hours",
                    "say": "The practice is not open then. Let me find you another time."}

        clinician = self.conn.execute(
            "SELECT id, name FROM clinicians WHERE id = ?", (clinician_id,)
        ).fetchone()
        if clinician is None:
            raise ToolError("That clinician is not on file.")

        works_then = self.conn.execute(
            "SELECT 1 FROM clinician_hours WHERE clinician_id = ? AND weekday = ? "
            "AND start_time <= ? AND end_time >= ?",
            (
                clinician_id,
                start.weekday(),
                start.strftime("%H:%M"),
                end.strftime("%H:%M"),
            ),
        ).fetchone()
        if works_then is None:
            return {"booked": False, "reason": "clinician_not_working",
                    "say": f"{clinician['name']} is not in then. Let me find another time."}

        clash = any(
            start < existing_end and end > existing_start
            for existing_start, existing_end in self._booked_intervals(clinician_id, start.date())
        )
        if clash:
            return {"booked": False, "reason": "just_taken",
                    "say": "I am sorry, that time has just been taken. Let me find you another."}

        cursor = self.conn.execute(
            "INSERT INTO appointments "
            "(patient_id, clinician_id, type_code, start_time, end_time, status, "
            " fee_quoted, booked_via, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'booked', ?, 'sophia', ?)",
            (
                patient_id,
                clinician_id,
                appointment_type,
                clock.to_db(start),
                clock.to_db(end),
                type_row["fee_gbp"],
                clock.to_db(self.now),
            ),
        )
        self.conn.commit()

        fee = policies.format_fee(type_row["fee_gbp"], bool(type_row["fee_is_from"]))
        return {
            "booked": True,
            "appointment_id": cursor.lastrowid,
            "when": clock.spoken_datetime(start),
            "clinician": clinician["name"],
            "appointment_type": type_row["name"],
            "fee": fee,
            "say": (
                f"That is booked. {clock.spoken_datetime(start)} with {clinician['name']}, "
                f"for a {type_row['name'].lower()}, and the charge is {fee}. "
                "If you need to change it, please give us at least 24 hours notice."
            ),
        }

    def book_urgent_slot(self, urgent_slot_id: int) -> dict:
        """
        Take one of today's limited urgent appointments.

        The fee depends on the patient's funding and history, so the type
        is chosen by the policy rules rather than by the model.
        """
        patient_id = self._require_verified()
        row = self.conn.execute(
            "SELECT * FROM urgent_slots WHERE id = ?", (urgent_slot_id,)
        ).fetchone()
        if row is None:
            raise ToolError("That urgent appointment is not one I recognise.")
        if row["status"] != "available":
            return {"booked": False, "reason": "just_taken",
                    "say": "I am sorry, that one has just gone."}
        if row["slot_date"] != self.now.strftime(clock.DB_DATE_FORMAT):
            return {"booked": False, "reason": "not_today",
                    "say": "Urgent appointments can only be booked on the day itself."}
        if not clock.urgent_slots_released(self.now):
            return {"booked": False, "reason": "before_release",
                    "say": "Today's urgent appointments open at 8am."}

        patient = self._patient_row(patient_id)
        facts = PatientFacts.from_row(patient)
        type_code = policies.emergency_type_code(
            patient["patient_type"], facts, self.now.date()
        )
        type_row = self._type_row(type_code)

        start = clock.combine(self.now.date(), clock.parse_time(row["slot_time"]))
        if start <= self.now:
            return {"booked": False, "reason": "in_the_past",
                    "say": "That time has already passed today."}

        end = start + timedelta(minutes=type_row["duration_min"])

        cursor = self.conn.execute(
            "INSERT INTO appointments "
            "(patient_id, clinician_id, type_code, start_time, end_time, status, "
            " fee_quoted, booked_via, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'booked', ?, 'sophia', ?)",
            (
                patient_id,
                row["clinician_id"],
                type_code,
                clock.to_db(start),
                clock.to_db(end),
                type_row["fee_gbp"],
                clock.to_db(self.now),
            ),
        )
        appointment_id = cursor.lastrowid
        self.conn.execute(
            "UPDATE urgent_slots SET status = 'taken', appointment_id = ? WHERE id = ?",
            (appointment_id, urgent_slot_id),
        )
        self.conn.commit()

        fee = policies.format_fee(type_row["fee_gbp"])
        return {
            "booked": True,
            "appointment_id": appointment_id,
            "when": clock.spoken_time(start.time()),
            "appointment_type": type_row["name"],
            "fee": fee,
            "say": (
                f"I can see you today at {clock.spoken_time(start.time())}. "
                f"That is an {type_row['name'].lower()}, and the charge is {fee}. "
                "It deals with the immediate problem, so any further treatment "
                "would be arranged separately."
            ),
        }

    # -- 5. cancelling, in two steps --------------------------------------

    def check_cancellation(self, appointment_id: int) -> dict:
        """
        Step one. Work out what cancelling would mean, and change nothing.

        This exists so the caller hears the consequence before it
        happens. Sophia must call this, say the warning, and get a clear
        yes before calling cancel_appointment.
        """
        patient_id = self._require_verified()
        appointment = self._owned_appointment(appointment_id, patient_id)

        start = clock.from_db(appointment["start_time"])
        assessment = policies.assess_cancellation(
            start, self._events(patient_id), self._facts(patient_id), self.now
        )

        return {
            "appointment_id": appointment_id,
            "when": clock.spoken_datetime(start),
            "hours_until": round(assessment.hours_until, 1),
            "is_short_notice": assessment.is_short_notice,
            "would_reach_discharge_limit": assessment.would_discharge,
            "needs_confirmation": True,
            "say": assessment.warning
            or (
                f"That is your appointment on {clock.spoken_datetime(start)}. "
                "That is more than 24 hours away, so there is no charge and nothing "
                "goes on your record. Shall I cancel it?"
            ),
        }

    def cancel_appointment(self, appointment_id: int, caller_confirmed: bool) -> dict:
        """
        Step two. Actually cancel, but only after an explicit yes.

        The confirmation flag is checked here rather than trusted to the
        conversation, so a model that skips the warning cannot cancel.
        """
        patient_id = self._require_verified()
        if not caller_confirmed:
            raise ToolError(
                "The caller has not confirmed. Call check_cancellation, tell them "
                "what it means, and only cancel once they clearly agree."
            )

        appointment = self._owned_appointment(appointment_id, patient_id)
        start = clock.from_db(appointment["start_time"])
        short_notice = policies.is_short_notice(start, self.now)

        self.conn.execute(
            "UPDATE appointments SET status = ?, cancelled_at = ? WHERE id = ?",
            ("snc" if short_notice else "cancelled", clock.to_db(self.now), appointment_id),
        )
        if short_notice:
            self.conn.execute(
                "INSERT INTO attendance_events (patient_id, event_type, event_date, appointment_id, notes) "
                "VALUES (?, 'SNC', ?, ?, 'Cancelled by phone with less than 24 hours notice')",
                (patient_id, self.now.strftime(clock.DB_DATE_FORMAT), appointment_id),
            )
        self.conn.execute(
            "UPDATE urgent_slots SET status = 'available', appointment_id = NULL "
            "WHERE appointment_id = ?",
            (appointment_id,),
        )
        self.conn.commit()

        status_after = policies.assess_attendance(
            self._events(patient_id), self._facts(patient_id), self.now.date()
        )

        return {
            "cancelled": True,
            "recorded_as_short_notice": short_notice,
            "now_discharged": status_after.is_discharged,
            "say": (
                "That is cancelled. It has gone on your record as a short notice "
                "cancellation, as I explained."
                if short_notice
                else "That is cancelled, and nothing goes on your record."
            ),
        }

    def _owned_appointment(self, appointment_id: int, patient_id: int):
        appointment = self.conn.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        if appointment is None:
            raise ToolError("I cannot find that appointment.")
        if appointment["patient_id"] != patient_id:
            # Same wording as a missing appointment, so nothing is leaked
            # about somebody else's booking.
            raise ToolError("I cannot find that appointment.")
        if appointment["status"] != "booked":
            raise ToolError("That appointment is not currently booked.")
        return appointment

    def reschedule_appointment(
        self, appointment_id: int, new_slot_ref: str, caller_confirmed: bool
    ) -> dict:
        """
        Move an appointment. Cancelling the old one still follows the 24 hour rule.

        Booking the new slot happens first. If it fails, the patient keeps
        the appointment they already had rather than losing both.
        """
        patient_id = self._require_verified()
        appointment = self._owned_appointment(appointment_id, patient_id)
        type_code = appointment["type_code"]

        booking = self.book_appointment(new_slot_ref, type_code)
        if not booking.get("booked"):
            return {"rescheduled": False, **booking}

        cancellation = self.cancel_appointment(appointment_id, caller_confirmed=caller_confirmed)

        return {
            "rescheduled": True,
            "new_appointment_id": booking["appointment_id"],
            "when": booking["when"],
            "clinician": booking["clinician"],
            "recorded_as_short_notice": cancellation["recorded_as_short_notice"],
            "say": (
                f"I have moved you to {booking['when']} with {booking['clinician']}. "
                + (
                    "Because the original was less than 24 hours away, it does go on "
                    "your record as a short notice cancellation."
                    if cancellation["recorded_as_short_notice"]
                    else "There is nothing on your record for the change."
                )
            ),
        }

    # -- 6. messages ------------------------------------------------------

    def take_message(
        self,
        caller_name: str,
        reason: str,
        callback_number: str | None = None,
        detail: str | None = None,
    ) -> dict:
        """
        Hand anything Sophia cannot handle to the human team.

        This needs no verification, because it reveals nothing. It is the
        safety net for complaints, clinical questions, quotes that are not
        on the price list, and any failed identity check.
        """
        if not (caller_name or "").strip():
            raise ToolError("A message needs a name to go with it.")

        cursor = self.conn.execute(
            "INSERT INTO messages (caller_name, callback_number, patient_id, reason, detail, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open')",
            (
                caller_name.strip(),
                (callback_number or "").strip() or None,
                self.verified_patient_id,
                reason,
                detail,
                clock.to_db(self.now),
            ),
        )
        self.conn.commit()

        return {
            "message_id": cursor.lastrowid,
            "say": (
                f"I have taken that down for the team, {caller_name.split()[0]}. "
                "They will get back to you."
            ),
        }

    # -- 7. practice facts that are structured, not prose -----------------

    def get_fee(self, appointment_type: str) -> dict:
        """Look up a fee from the price table. Never from memory."""
        type_row = self._type_row(appointment_type)
        return {
            "appointment_type": type_row["name"],
            "fee": policies.format_fee(type_row["fee_gbp"], bool(type_row["fee_is_from"])),
            "funding": type_row["funding"],
            "nhs_band": type_row["nhs_band"],
            "bookable_by_sophia": bool(type_row["is_bookable"]),
            "description": type_row["description"],
        }

    def recommend_appointment_type(
        self, purpose: str, funding: str | None = None, minutes: int | None = None
    ) -> dict:
        """
        Choose the right appointment type for a verified patient.

        The model says what the caller wants in plain words. The eligibility
        rules, and therefore the fee, are decided here.
        """
        patient_id = self._require_verified()
        patient = self._patient_row(patient_id)
        facts = PatientFacts.from_row(patient)
        funding = funding or patient["patient_type"]
        today = self.now.date()

        if purpose == "check_up":
            code = policies.exam_type_code(funding, facts, today)
        elif purpose == "urgent":
            code = policies.emergency_type_code(funding, facts, today)
        elif purpose == "hygiene":
            if funding == "nhs":
                return {
                    "appointment_type": None,
                    "say": (
                        "Hygiene treatment on the NHS needs a dentist to assess you and "
                        "refer you first. I can book you an examination, and we can take "
                        "it from there. Or you are welcome to book privately with a "
                        "hygiene therapist."
                    ),
                }
            code = policies.hygiene_type_code(facts, minutes, today)
        else:
            return {
                "appointment_type": None,
                "say": (
                    "I am not sure which appointment that would be, so let me take a "
                    "message and the team will call you back."
                ),
            }

        type_row = self._type_row(code)
        return {
            "appointment_type": code,
            "name": type_row["name"],
            "duration_minutes": type_row["duration_min"],
            "fee": policies.format_fee(type_row["fee_gbp"], bool(type_row["fee_is_from"])),
            "reason_for_this_type": (
                "treated as a new patient, either never seen here or not in three years"
                if policies.is_new_for_fees(facts, today)
                else "seen within the last three years"
            ),
        }


def _spread(slots: list[Slot], limit: int) -> list[Slot]:
    """
    Pick a handful of slots that are actually useful to hear read aloud.

    Offering the first five consecutive ten minute slots on one morning is
    useless. This takes the earliest, then spreads the rest across
    different days so the caller gets a real choice.
    """
    if len(slots) <= limit:
        return slots

    chosen: list[Slot] = []
    seen_days: set[date] = set()

    for slot in slots:
        if slot.start.date() not in seen_days:
            chosen.append(slot)
            seen_days.add(slot.start.date())
        if len(chosen) == limit:
            return chosen

    for slot in slots:
        if slot not in chosen:
            chosen.append(slot)
        if len(chosen) == limit:
            break
    return chosen
