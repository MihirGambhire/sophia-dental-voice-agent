"""
JSON schemas describing Sophia's tools to the language model.

These are written in the OpenAI style function calling format, which is
what Groq accepts, and what Gemini accepts with a thin adapter.

Two rules shaped these descriptions:

  1. **The description is the prompt.** A tool the model misuses is
     usually a tool that was described vaguely. Each description says
     when to call it and, where it matters, when not to.

  2. **Keep the surface small.** CLAUDE.md sets a budget of about nine
     tools. More tools means more tokens on every single turn, which
     burns the free tier and slows the voice loop down.
"""

from __future__ import annotations

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "verify_patient",
            "description": (
                "Confirm who is calling. You MUST call this, and get verified: true, "
                "before you can look at, book, move or cancel anything. Ask for all "
                "three details first, one question at a time. If it fails, do not "
                "retry endlessly, offer to take a message instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "full_name": {
                        "type": "string",
                        "description": "The patient's full name as they gave it.",
                    },
                    "dob": {
                        "type": "string",
                        "description": (
                            "Date of birth. Pass it as YYYY-MM-DD if you can, "
                            "or as the caller said it, for example 03/03/1985."
                        ),
                    },
                    "postcode": {
                        "type": "string",
                        "description": "Home postcode, spaces do not matter.",
                    },
                },
                "required": ["full_name", "dob", "postcode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_patient_status",
            "description": (
                "Look up the verified patient: whether they are still registered, "
                "NHS or private, what is on their attendance record, and what they "
                "already have booked. Call this before booking so you quote the "
                "right fee, and before discussing an existing appointment."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_appointment_type",
            "description": (
                "Work out which appointment the caller needs and what it costs. "
                "Always use this rather than deciding the fee yourself, because "
                "the price depends on rules you cannot see, such as whether they "
                "have been seen in the last three years."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "purpose": {
                        "type": "string",
                        "enum": ["check_up", "urgent", "hygiene"],
                        "description": "What the caller actually wants.",
                    },
                    "funding": {
                        "type": "string",
                        "enum": ["nhs", "private"],
                        "description": (
                            "Only pass this if the caller states a preference. "
                            "Otherwise their usual funding is used."
                        ),
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "For hygiene only: 20, 30, 40 or 60.",
                    },
                },
                "required": ["purpose"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_available_slots",
            "description": (
                "Find real appointment times. Never invent or guess availability, "
                "and never offer a time this has not returned. Offer two or three "
                "at a time, not the whole list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_type": {
                        "type": "string",
                        "description": "The code from recommend_appointment_type, for example PRIV_NEW_EXAM.",
                    },
                    "clinician_name": {
                        "type": "string",
                        "description": "Only if the caller asked for someone by name.",
                    },
                    "earliest_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD, if the caller cannot come before a certain date.",
                    },
                    "days_ahead": {
                        "type": "integer",
                        "description": "How far ahead to look. Defaults to three weeks.",
                    },
                },
                "required": ["appointment_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_urgent_slots_today",
            "description": (
                "Check today's limited urgent appointments. Use this for someone in "
                "pain or with a dental problem today, NOT for a routine check up. "
                "These are released at 8am on the day and cannot be booked ahead. "
                "The reply tells you what to say if they are not open yet or have "
                "all gone."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": (
                "Book a routine slot. Only use a slot_ref that find_available_slots "
                "returned. Confirm the day, the time, the clinician and the fee back "
                "to the caller before calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slot_ref": {
                        "type": "string",
                        "description": "The slot_ref exactly as returned, do not edit it.",
                    },
                    "appointment_type": {
                        "type": "string",
                        "description": "The same appointment type code used to search.",
                    },
                },
                "required": ["slot_ref", "appointment_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_urgent_slot",
            "description": (
                "Take one of today's urgent appointments, using an urgent_slot_id "
                "from get_urgent_slots_today. The fee is worked out for you."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "urgent_slot_id": {
                        "type": "integer",
                        "description": "The urgent_slot_id exactly as returned.",
                    }
                },
                "required": ["urgent_slot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_cancellation",
            "description": (
                "Find out what cancelling would mean BEFORE cancelling. This changes "
                "nothing. You must call this first, say the 'say' text back to the "
                "caller in your own words, and get a clear yes. Cancelling inside 24 "
                "hours goes on the patient's record and can get them removed from the "
                "practice, so they have to be told before it happens, not after."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "integer",
                        "description": "From get_patient_status.",
                    }
                },
                "required": ["appointment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": (
                "Actually cancel, after check_cancellation and after the caller has "
                "clearly agreed. Only pass caller_confirmed true if they really said "
                "yes to this specific appointment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"},
                    "caller_confirmed": {
                        "type": "boolean",
                        "description": "True only if the caller explicitly agreed after hearing the consequence.",
                    },
                },
                "required": ["appointment_id", "caller_confirmed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": (
                "Move an appointment to a new slot in one step. The same 24 hour rule "
                "applies to the old appointment, so run check_cancellation first and "
                "tell the caller if it will go on their record."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"},
                    "new_slot_ref": {
                        "type": "string",
                        "description": "A slot_ref from find_available_slots.",
                    },
                    "caller_confirmed": {"type": "boolean"},
                },
                "required": ["appointment_id", "new_slot_ref", "caller_confirmed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fee",
            "description": (
                "Look up a price. Use this for any question about cost. Never quote a "
                "figure from memory. If the treatment is not in the list, say the team "
                "will confirm after an assessment and offer to take a message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_type": {
                        "type": "string",
                        "description": "An appointment type code, for example PRIV_NEW_EXAM or NHS_BAND2.",
                    }
                },
                "required": ["appointment_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_message",
            "description": (
                "Pass something to the human team. Use this whenever you cannot help: "
                "complaints, clinical or medication questions, prices that are not "
                "listed, a failed identity check, or anything you are unsure about. "
                "This is always better than guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "description": "A short summary for the team, for example 'Asking about implant costs'.",
                    },
                    "callback_number": {"type": "string"},
                    "detail": {
                        "type": "string",
                        "description": "Anything else useful the caller said.",
                    },
                },
                "required": ["caller_name", "reason"],
            },
        },
    },
]


def tool_names() -> list[str]:
    """Just the names, used by tests and by the dispatcher."""
    return [schema["function"]["name"] for schema in TOOL_SCHEMAS]
