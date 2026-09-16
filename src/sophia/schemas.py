"""
JSON schemas describing Sophia's tools to the language model.

OpenAI style function calling format, which is what Groq accepts.

WHY THESE ARE SO TERSE
======================
The schemas are resent on every single turn, so their size is a fixed tax
on every request. The first version of this file cost about 1,719 prompt
tokens per turn. Groq's free tier allows 8,000 tokens per minute, so that
was roughly four turns before the account was throttled and the SDK
started backing off, which showed up as 20 to 90 second stalls in the
middle of a conversation. That is fatal for a voice agent.

So the descriptions here say only what the model cannot work out from the
name and the parameters. Everything else, the tone, the order to do
things in, when to refuse, lives in the system prompt, which is sent once
per turn either way.

Two behaviours are deliberately kept, because getting them wrong is
harmful rather than merely unhelpful: verification before anything else,
and warning before cancelling. Both are also enforced in Python, so the
text here is a hint, not the guarantee.
"""

from __future__ import annotations


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_BOOLEAN = {"type": "boolean"}


TOOL_SCHEMAS: list[dict] = [
    _tool(
        "verify_patient",
        "Required before any other patient tool. Confirms identity.",
        {
            "full_name": _STRING,
            "dob": {"type": "string", "description": "YYYY-MM-DD or as spoken"},
            "postcode": _STRING,
        },
        ["full_name", "dob", "postcode"],
    ),
    _tool(
        "get_patient_status",
        "Registration, funding, attendance record and existing bookings.",
        {},
        [],
    ),
    _tool(
        "recommend_appointment_type",
        "Returns the correct appointment type code and fee. Never decide the fee yourself.",
        {
            "purpose": {"type": "string", "enum": ["check_up", "urgent", "hygiene"]},
            "funding": {"type": "string", "enum": ["nhs", "private"]},
            "minutes": {"type": "integer", "description": "hygiene only: 20/30/40/60"},
        },
        ["purpose"],
    ),
    _tool(
        "find_available_slots",
        "Real availability. Never offer a time this has not returned.",
        {
            "appointment_type": _STRING,
            "clinician_name": _STRING,
            "earliest_date": {"type": "string", "description": "YYYY-MM-DD"},
        },
        ["appointment_type"],
    ),
    _tool(
        "get_urgent_slots_today",
        "Today's limited urgent appointments. For a problem today, not a check up.",
        {},
        [],
    ),
    _tool(
        "book_appointment",
        "Book a routine slot using a slot_ref from find_available_slots.",
        {"slot_ref": _STRING, "appointment_type": _STRING},
        ["slot_ref", "appointment_type"],
    ),
    _tool(
        "book_urgent_slot",
        "Take a same day urgent appointment by urgent_slot_id.",
        {"urgent_slot_id": _INTEGER},
        ["urgent_slot_id"],
    ),
    _tool(
        "check_cancellation",
        "Call this BEFORE cancelling. Changes nothing. Returns what it would cost the "
        "patient, which you must tell them before they decide.",
        {"appointment_id": _INTEGER},
        ["appointment_id"],
    ),
    _tool(
        "cancel_appointment",
        "Cancel, only after check_cancellation and an explicit yes from the caller.",
        {
            "appointment_id": _INTEGER,
            "caller_confirmed": {
                "type": "boolean",
                "description": "True only if they agreed after hearing the consequence",
            },
        },
        ["appointment_id", "caller_confirmed"],
    ),
    _tool(
        "reschedule_appointment",
        "Move an appointment. The 24 hour rule still applies to the old one.",
        {
            "appointment_id": _INTEGER,
            "new_slot_ref": _STRING,
            "caller_confirmed": _BOOLEAN,
        },
        ["appointment_id", "new_slot_ref", "caller_confirmed"],
    ),
    _tool(
        "get_fee",
        "Look up a price by appointment type code. Never quote from memory.",
        {"appointment_type": _STRING},
        ["appointment_type"],
    ),
    _tool(
        "take_message",
        "Pass anything you cannot handle to the human team. Always better than guessing.",
        {
            "caller_name": _STRING,
            "reason": _STRING,
            "callback_number": _STRING,
            "detail": _STRING,
        },
        ["caller_name", "reason"],
    ),
]


def tool_names() -> list[str]:
    """Just the names, used by tests and by the dispatcher."""
    return [schema["function"]["name"] for schema in TOOL_SCHEMAS]


def estimated_tokens() -> int:
    """
    Rough token cost of sending the schemas, at about four characters per
    token. Used by the test that stops this file growing back.
    """
    import json

    return len(json.dumps(TOOL_SCHEMAS)) // 4
