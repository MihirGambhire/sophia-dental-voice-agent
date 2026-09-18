"""
Tests for the conversation loop.

These run against a scripted fake model, not a real one. That is
deliberate: the loop, the tool dispatch and the guard rails are
deterministic and should be tested deterministically. Whether the real
model chooses the right tool is a different question, answered by the
scenario suite on Day 6.

No API key is needed to run any of this.
"""

import json
from datetime import date, time, timedelta
from types import SimpleNamespace

import pytest

from sophia import clock
from sophia.agent_text import MAX_TOOL_ROUNDS, SophiaAgent
from sophia.schemas import TOOL_SCHEMAS, estimated_tokens, tool_names
from sophia.tools import SophiaTools


def a_weekday_at(hour, minute=0):
    return clock.combine(date(2026, 9, 21), time(hour, minute))


# ---------------------------------------------------------------------------
# A scripted stand in for the model
# ---------------------------------------------------------------------------


def text_response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))]
    )


def tool_response(calls):
    """calls is a list of (name, arguments dict)."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id=f"call_{index}",
                            function=SimpleNamespace(
                                name=name, arguments=json.dumps(arguments)
                            ),
                        )
                        for index, (name, arguments) in enumerate(calls)
                    ],
                )
            )
        ]
    )


class FakeClient:
    """Returns queued responses in order, and records what it was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            return text_response("(nothing scripted)")
        return self.responses.pop(0)


def agent_with(conn, responses, when=None):
    return SophiaAgent(
        conn, now=when or a_weekday_at(10), client=FakeClient(responses)
    )


# ---------------------------------------------------------------------------
# The schemas themselves
# ---------------------------------------------------------------------------


def test_every_declared_tool_actually_exists(conn):
    """A schema naming a method that is not there would fail only at runtime."""
    session = SophiaTools(conn)
    for name in tool_names():
        assert hasattr(session, name), f"{name} is declared but not implemented"
        assert callable(getattr(session, name))


def test_tool_names_are_unique():
    names = tool_names()
    assert len(names) == len(set(names))


def test_the_tool_surface_stays_small():
    """
    CLAUDE.md budgets about nine tools, and the schemas are resent on
    every turn, so this is a free tier and latency constraint rather than
    tidiness. Fourteen is the agreed ceiling: the twelve original tools,
    search_practice_info, added after Sophia invented the opening hours
    because she had nothing to look them up in, and register_new_patient,
    added because a caller not already on file could not book at all.

    The real budget is enforced by the token test below. This one just
    stops the list growing without anyone noticing.
    """
    assert len(TOOL_SCHEMAS) <= 14


def test_every_tool_has_a_description():
    for schema in TOOL_SCHEMAS:
        assert schema["function"]["description"].strip()


def test_the_tool_schemas_stay_inside_the_token_budget():
    """
    The schemas are resent on every turn, so their size is a fixed tax on
    every request. Groq's free tier allows 8000 tokens per minute. When
    this file cost 1719 tokens, a conversation was throttled after about
    four turns, which showed up as 20 to 90 second stalls mid call.

    Raised from 1200 to 1250 on 18 September 2026 for on_date, which lets
    Sophia list times on one day. Groq is now only the last fallback, and
    Gemini, which answers almost every turn, allows far more per minute.
    """
    assert estimated_tokens() < 1250, (
        "tool schemas have grown, which will throttle the free tier mid conversation"
    )


def test_required_parameters_are_all_declared():
    for schema in TOOL_SCHEMAS:
        parameters = schema["function"]["parameters"]
        for required in parameters.get("required", []):
            assert required in parameters["properties"], (
                f"{schema['function']['name']} requires {required} but does not declare it"
            )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_a_plain_answer_comes_straight_back(conn):
    agent = agent_with(conn, [text_response("We are open until six.")])
    turn = agent.say("What time do you close?")

    assert turn.reply == "We are open until six."
    assert turn.tool_calls == []


def test_a_tool_call_is_run_and_the_result_fed_back(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    agent = agent_with(
        conn,
        [
            tool_response(
                [
                    (
                        "verify_patient",
                        {
                            "full_name": row["full_name"],
                            "dob": row["dob"],
                            "postcode": row["postcode"],
                        },
                    )
                ]
            ),
            text_response("Thank you, I have found your record."),
        ],
    )

    turn = agent.say(f"It's Margaret Hollis, {row['dob']}, {row['postcode']}")

    assert turn.tools_used == ["verify_patient"]
    assert agent.tools.verified_patient_id == row["id"]

    assert turn.result_for("verify_patient")["verified"] is True


def test_several_tools_in_one_turn_all_run(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    agent = agent_with(
        conn,
        [
            tool_response(
                [
                    (
                        "verify_patient",
                        {
                            "full_name": row["full_name"],
                            "dob": row["dob"],
                            "postcode": row["postcode"],
                        },
                    ),
                    ("get_fee", {"appointment_type": "NHS_EXAM"}),
                ]
            ),
            text_response("You are on file, and an NHS check up is £27.90."),
        ],
    )

    turn = agent.say("Margaret Hollis, and how much is a check up?")
    assert turn.tools_used == ["verify_patient", "get_fee"]


def test_a_refused_tool_comes_back_as_an_error_not_a_crash(conn):
    """
    The model asks to book before verifying. The tool refuses. That
    refusal must reach the model as a result it can recover from.
    """
    agent = agent_with(
        conn,
        [
            tool_response(
                [("book_appointment", {"slot_ref": "1@2026-09-21T10:00", "appointment_type": "NHS_EXAM"})]
            ),
            text_response("Before I book that, could I take your full name?"),
        ],
    )

    turn = agent.say("Book me in for Monday")

    assert turn.reply.startswith("Before I book")
    assert "not been verified" in turn.result_for("book_appointment")["error"]


def test_an_invented_tool_name_is_handled_gracefully(conn):
    agent = agent_with(
        conn,
        [
            tool_response([("cancel_everything_immediately", {})]),
            text_response("Sorry, let me take a message for the team."),
        ],
    )

    turn = agent.say("Delete all my appointments")

    assert "no tool called" in turn.result_for("cancel_everything_immediately")["error"]
    assert turn.reply


def test_wrong_arguments_are_reported_rather_than_crashing(conn):
    agent = agent_with(
        conn,
        [
            tool_response([("get_fee", {"treatment": "a filling"})]),
            text_response("Let me check that for you."),
        ],
    )

    turn = agent.say("How much is a filling?")

    assert "do not fit" in turn.result_for("get_fee")["error"]


def test_malformed_tool_arguments_do_not_break_the_turn(conn):
    """A model occasionally emits arguments that are not valid JSON."""
    broken = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call_0",
                            function=SimpleNamespace(
                                name="get_patient_status", arguments="{not json"
                            ),
                        )
                    ],
                )
            )
        ]
    )
    agent = agent_with(conn, [broken, text_response("Could I take your name?")])
    turn = agent.say("What have I got booked?")
    assert turn.reply


def test_a_runaway_tool_loop_is_stopped_and_offers_a_message(conn):
    """
    A model that keeps calling tools forever would drain the free tier.
    After the cap, Sophia falls back to something safe to say.
    """
    forever = [tool_response([("get_fee", {"appointment_type": "NHS_EXAM"})])] * (
        MAX_TOOL_ROUNDS + 2
    )
    agent = agent_with(conn, forever)
    turn = agent.say("How much is everything?")

    assert "take a message" in turn.reply


# ---------------------------------------------------------------------------
# What the model is actually sent
# ---------------------------------------------------------------------------


def test_the_system_prompt_carries_the_current_time_and_open_state(conn):
    agent = agent_with(conn, [text_response("ok")], when=a_weekday_at(7, 30))
    agent.say("hello")

    system = agent.client.requests[0]["messages"][0]["content"]
    assert "Monday the 21st of September" in system
    assert "practice is closed" in system


def test_the_system_prompt_knows_when_the_practice_is_open(conn):
    agent = agent_with(conn, [text_response("ok")], when=a_weekday_at(10))
    agent.say("hello")

    system = agent.client.requests[0]["messages"][0]["content"]
    assert "practice is open" in system


def test_the_call_opens_with_the_ai_and_recording_notice(conn):
    agent = agent_with(conn, [text_response("ok")])
    greeting = agent.messages[1]["content"]

    assert "AI assistant" in greeting
    assert "recorded" in greeting


def test_the_system_prompt_stays_short_enough_for_the_free_tier(conn):
    """
    The prompt is resent every turn. Practice facts belong in the tools
    and in retrieval, not in here.
    """
    agent = agent_with(conn, [text_response("ok")])
    system = agent.messages[0]["content"]
    assert len(system) < 4000, "the system prompt is growing, move facts into tools"


def test_the_prompt_does_not_hardcode_prices(conn):
    """
    A price in the prompt is a price that can drift out of date and cannot
    be tested. Every figure must come from the price table.
    """
    agent = agent_with(conn, [text_response("ok")])
    system = agent.messages[0]["content"]

    for price in ("£85", "£50", "£27.90", "£140", "£332.10"):
        assert price not in system, f"{price} is hardcoded in the prompt"


def test_the_tools_are_offered_on_every_request(conn):
    agent = agent_with(conn, [text_response("ok")])
    agent.say("hello")
    assert agent.client.requests[0]["tools"] == TOOL_SCHEMAS


def test_history_records_latency_for_each_turn(conn):
    agent = agent_with(conn, [text_response("ok"), text_response("ok again")])
    agent.say("hello")
    agent.say("still there?")

    assert len(agent.history) == 2
    assert all(turn.latency_seconds >= 0 for turn in agent.history)


# ---------------------------------------------------------------------------
# An end to end booking, still with no real model involved
# ---------------------------------------------------------------------------


def test_a_full_booking_conversation_writes_a_real_appointment(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    when = a_weekday_at(10)

    # Turn one: verify.
    agent = agent_with(
        conn,
        [
            tool_response(
                [
                    (
                        "verify_patient",
                        {
                            "full_name": row["full_name"],
                            "dob": row["dob"],
                            "postcode": row["postcode"],
                        },
                    )
                ]
            ),
            text_response("Thank you Margaret. What can I do for you?"),
        ],
        when=when,
    )
    agent.say("Margaret Hollis, twelfth of March 1958, WA1 2NF")

    # Turn two: work out the type, then find real slots.
    slots = agent.tools.find_available_slots("NHS_EXAM", limit=3)["slots"]
    assert slots

    agent.client.responses = [
        tool_response(
            [
                ("recommend_appointment_type", {"purpose": "check_up"}),
                ("find_available_slots", {"appointment_type": "NHS_EXAM"}),
            ]
        ),
        text_response("I can offer you a few times."),
    ]
    turn = agent.say("I'd like a check up please")
    assert turn.tools_used == ["recommend_appointment_type", "find_available_slots"]

    # Turn three: book one.
    agent.client.responses = [
        tool_response(
            [
                (
                    "book_appointment",
                    {"slot_ref": slots[0]["slot_ref"], "appointment_type": "NHS_EXAM"},
                )
            ]
        ),
        text_response("That is booked for you."),
    ]
    agent.say("The first one please")

    booked = conn.execute(
        "SELECT * FROM appointments WHERE patient_id = ? AND booked_via = 'sophia'",
        (row["id"],),
    ).fetchall()
    assert len(booked) == 1
    assert booked[0]["status"] == "booked"


def test_a_cancellation_needs_the_check_step_first(conn):
    """
    Even if the model tries to skip straight to cancelling, the tool
    refuses without an explicit confirmation.
    """
    appointment = conn.execute(
        "SELECT a.* FROM appointments a JOIN patients p ON p.id = a.patient_id "
        "WHERE p.code = 'okafor' AND a.status = 'booked' LIMIT 1"
    ).fetchone()
    row = conn.execute("SELECT * FROM patients WHERE code = 'okafor'").fetchone()
    when = clock.from_db(appointment["start_time"]) - timedelta(hours=5)

    agent = agent_with(
        conn,
        [
            tool_response(
                [
                    (
                        "verify_patient",
                        {
                            "full_name": row["full_name"],
                            "dob": row["dob"],
                            "postcode": row["postcode"],
                        },
                    ),
                    (
                        "cancel_appointment",
                        {"appointment_id": appointment["id"], "caller_confirmed": False},
                    ),
                ]
            ),
            text_response("Before I cancel, I should explain something."),
        ],
        when=when,
    )

    turn = agent.say(f"Daniel Okafor, {row['dob']}, {row['postcode']}, cancel my appointment")

    still_booked = conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment["id"],)
    ).fetchone()
    assert still_booked["status"] == "booked"

    assert "not confirmed" in turn.result_for("cancel_appointment")["error"]


# ---------------------------------------------------------------------------
# Claims that contradict the call state never reach the caller
# ---------------------------------------------------------------------------


def test_a_false_claim_of_verification_is_replaced(conn):
    """
    On a live phone test Sophia said "I've got you verified now" when no
    identity check had run, for details matching no patient. The prompt
    already forbade it. The reply is now checked against the call state.
    """
    agent = agent_with(
        conn,
        [text_response("Sorry about that, Vandana, I've got you verified now. How can I help?")],
    )
    turn = agent.say("five two nine")

    assert agent.tools.verified_patient_id is None
    assert "verified" not in turn.reply.lower()
    assert "date of birth" in turn.reply
    assert turn.corrected_claim and "verified" in turn.corrected_claim
    assert agent.messages[-1]["content"] == turn.reply


def test_a_true_verification_is_left_alone(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    agent = agent_with(
        conn,
        [
            tool_response([("verify_patient", {
                "full_name": row["full_name"], "dob": row["dob"], "postcode": row["postcode"],
            })]),
            text_response("Thank you, I've found your record."),
        ],
    )
    turn = agent.say(f"Margaret Hollis, {row['dob']}, {row['postcode']}")

    assert turn.reply == "Thank you, I've found your record."
    assert turn.corrected_claim is None


def test_saying_a_record_cannot_be_found_is_not_mistaken_for_a_claim(conn):
    agent = agent_with(conn, [text_response("I'm sorry, I can't find a record with those details.")])
    turn = agent.say("Nobody Real")
    assert turn.corrected_claim is None


def test_a_false_booking_claim_inside_a_booking_is_replaced(conn):
    row = conn.execute("SELECT * FROM patients WHERE code = 'hollis'").fetchone()
    agent = agent_with(conn, [])
    agent.tools.verify_patient(row["full_name"], row["dob"], row["postcode"])
    agent.tools.find_available_slots("NHS_EXAM", limit=3)

    agent.client.responses = [text_response("That's all booked for Monday at ten past ten.")]
    turn = agent.say("the first one")

    assert not agent.tools.bookings_made
    assert "booked" not in turn.reply.lower()
    assert turn.corrected_claim


def test_describing_an_existing_appointment_is_not_treated_as_a_false_booking(conn):
    """Outside a booking flow, "your appointment is confirmed" can be true."""
    agent = agent_with(conn, [text_response("Your appointment is confirmed for Monday at 10:20.")])
    turn = agent.say("when is my appointment")
    assert turn.corrected_claim is None


def test_the_voice_connection_can_be_used_from_another_thread(tmp_path):
    """
    Every voice call opened the database on the event loop thread and used
    it from worker threads, so every database tool raised ProgrammingError.
    """
    import threading
    from sophia import db

    connection = db.reset_database(tmp_path / "threads.db")
    connection.close()
    shared = db.connect(tmp_path / "threads.db", shared_across_threads=True)

    outcome = {}

    def use_it():
        try:
            outcome["n"] = shared.execute("SELECT COUNT(*) AS n FROM patients").fetchone()["n"]
        except Exception as error:  # noqa: BLE001
            outcome["error"] = error

    worker = threading.Thread(target=use_it)
    worker.start()
    worker.join()
    shared.close()

    assert "error" not in outcome, outcome.get("error")
    assert outcome["n"] > 0


def test_an_interrupted_reply_is_marked_in_the_history(conn):
    agent = agent_with(conn, [text_response("The fee is twenty seven pounds ninety.")])
    agent.say("how much is a check up")

    agent.note_interrupted("The fee is twenty seven pounds ninety.")

    assert "interrupted" in agent.messages[-1]["content"]
    assert agent.messages[-1]["content"].startswith("The fee is twenty seven pounds ninety.")


def test_each_turn_records_which_model_answered(conn):
    """With a fallback chain, the eval report needs to know who answered."""
    client = FakeClient([tool_response([("search_practice_info", {"question": "hours"})]), text_response("Eight till six.")])
    served = iter(["gemini:primary", "gemini:backup"])
    original = client._create

    def create(**kwargs):
        client.served_by = next(served)
        return original(**kwargs)

    client.chat.completions.create = create
    agent = SophiaAgent(conn, now=a_weekday_at(10), client=client)
    record = agent.say("What are your opening hours?")
    assert record.models == ["gemini:primary", "gemini:backup"]


def test_a_single_fixed_model_records_nothing(conn):
    record = agent_with(conn, [text_response("Hello.")]).say("Hello")
    assert record.models == []


def test_the_call_state_says_when_a_new_patient_was_registered(conn):
    """Otherwise the model sees a verified caller it never verified, and may try again."""
    agent = agent_with(conn, [])
    agent.tools.caller_heard = ["Priya Sharma", "fourth of May nineteen ninety", "W A 1 3 B X", "07700 900123"]
    assert agent.tools.register_new_patient("Priya Sharma", "1990-05-04", "WA1 3BX", "07700 900123")["registered"]

    state = agent._state_block()
    assert "New patient registered on this call: Priya Sharma" in state
    assert "Verified caller" not in state


# ---------------------------------------------------------------------------
# Claims checked against what was looked up this turn
# ---------------------------------------------------------------------------
#
# From a tester's call: "nothing between 2 and 5 tomorrow" and "no lunchtime
# appointments" with no search at all, and "we do offer Invisalign" with no
# lookup, when it is nowhere in the practice's information.


def test_an_availability_claim_without_a_search_is_sent_back_to_check(conn):
    agent = agent_with(conn, [
        text_response("I'm afraid we don't have any appointments between two and five tomorrow."),
        tool_response([("find_available_slots", {"appointment_type": "NHS_EXAM", "after_time": "14:00"})]),
        text_response("Tomorrow I have 2:30 pm with Dr Helen Whitfield."),
    ])
    record = agent.say("Anytime between 2 and 5 tomorrow?")

    assert record.reply == "Tomorrow I have 2:30 pm with Dr Helen Whitfield."
    assert "find_available_slots" in record.tools_used
    assert record.unchecked_claim.startswith("I'm afraid we don't have")


def test_a_claim_still_unchecked_after_one_reminder_is_not_spoken(conn):
    agent = agent_with(conn, [
        text_response("We only have 8 am slots."),
        text_response("Sorry, we only have 8 am slots."),
    ])
    record = agent.say("Can I come at 3pm?")

    assert "8 am" not in record.reply
    assert record.corrected_claim == "Sorry, we only have 8 am slots."


def test_the_reminder_to_check_is_not_left_in_the_conversation(conn):
    agent = agent_with(conn, [
        text_response("We don't have any appointments then."),
        tool_response([("find_available_slots", {"appointment_type": "NHS_EXAM"})]),
        text_response("I have Monday at 9 am."),
    ])
    agent.say("Anything Friday?")
    assert not any("CHECK FIRST" in str(m.get("content")) for m in agent.messages)


def test_an_availability_answer_after_a_search_is_left_alone(conn):
    agent = agent_with(conn, [
        tool_response([("find_available_slots", {"appointment_type": "NHS_EXAM"})]),
        text_response("I'm sorry, there are no appointments before Monday."),
    ])
    record = agent.say("Anything this week?")
    assert record.reply == "I'm sorry, there are no appointments before Monday."
    assert record.unchecked_claim is None


def test_a_practice_fact_without_a_lookup_is_sent_back_to_check(conn):
    agent = agent_with(conn, [
        text_response("We do offer Invisalign."),
        tool_response([("search_practice_info", {"question": "Do you offer Invisalign?"})]),
        text_response("I don't have information about Invisalign, so let me take a message."),
    ])
    record = agent.say("Do you offer Invisalign?")

    assert "search_practice_info" in record.tools_used
    assert "offer Invisalign" not in record.reply


def test_saying_nhs_or_private_needs_a_lookup(conn):
    agent = agent_with(conn, [
        text_response("We are a private practice."),
        tool_response([("search_practice_info", {"question": "NHS or private?"})]),
        text_response("We see both NHS and private patients."),
    ])
    assert agent.say("Are you NHS or private?").reply == "We see both NHS and private patients."


# ---------------------------------------------------------------------------
# What the call state remembers
# ---------------------------------------------------------------------------


def test_999_advice_is_not_repeated_in_every_reply(conn):
    """A tester heard "call 999" in ten replies in a row, including about parking."""
    agent = agent_with(conn, [text_response("If you have difficulty breathing, call 999. Have you been here before?")])
    agent.say("My face is really swollen")
    assert "already mentioned 999" in agent._state_block()


def test_the_call_state_names_the_patient_and_their_bookings(conn):
    agent = agent_with(conn, [])
    row = conn.execute("SELECT * FROM patients WHERE code = 'okafor'").fetchone()
    agent.tools.caller_heard = None
    agent.tools.verify_patient(row["full_name"], row["dob"], row["postcode"])
    agent.now = clock.combine(clock.today(), time(0, 1))

    stage = agent._call_stage()
    assert "Daniel Okafor" in stage
    assert "upcoming appointment" in stage
    assert "Do not ask whether they have been here before" in stage


def test_the_call_state_says_a_new_caller_must_be_registered(conn):
    agent = agent_with(conn, [text_response("Have you been a patient with us before?"), text_response("Welcome.")])
    agent.say("I need an appointment")
    agent.say("nope")
    stage = agent._call_stage()
    assert "NEW to the practice" in stage and "Never use verify_patient" in stage


def test_recent_tool_results_are_kept_between_turns(conn):
    """A tester found Sophia forgetting, two turns later, the fee she had just looked up."""
    agent = agent_with(conn, [
        tool_response([("search_practice_info", {"question": "new patient exam fee"})]),
        text_response("A new patient examination is eighty five pounds."),
        text_response("Could I take your date of birth?"),
    ])
    agent.say("How much is a new patient exam?")
    agent.say("I'm new")
    roles = [m["role"] for m in agent.messages]
    assert "tool" in roles
    assert any(m.get("tool_calls") for m in agent.messages)


def test_old_tool_results_are_dropped_but_what_was_said_is_kept(conn):
    from sophia.agent_text import KEEP_TOOL_RESULTS_TURNS

    turns = KEEP_TOOL_RESULTS_TURNS + 2
    responses = []
    for n in range(turns):
        responses += [tool_response([("search_practice_info", {"question": f"q{n}"})]), text_response(f"answer {n}")]
    agent = agent_with(conn, responses)
    for n in range(turns):
        agent.say(f"question {n}")

    tool_messages = [m for m in agent.messages if m["role"] == "tool"]
    assert len(tool_messages) == KEEP_TOOL_RESULTS_TURNS
    said = [m["content"] for m in agent.messages if m["role"] == "user"]
    assert said == [f"question {n}" for n in range(turns)]
    # Every tool result still follows the message that asked for it.
    for index, message in enumerate(agent.messages):
        if message["role"] == "tool":
            assert agent.messages[index - 1]["role"] in ("assistant", "tool")


def test_the_call_state_remembers_the_name_the_caller_gave(conn):
    agent = agent_with(conn, [text_response("Is it urgent?"), text_response("Have you been here before?")])
    agent.say("Hi Sophia, my name is Mihir Gambhire, I need an appointment")
    agent.say("No it's not urgent")
    state = agent._state_block()
    assert "already said their name: Mihir Gambhire" in state
    assert "Just to confirm, your name is Mihir Gambhire?" in state


@pytest.mark.parametrize(
    "said, reply, expected",
    [
        ("No, that's all, thank you", "Thank you for calling, Mihir. Goodbye.", True),
        ("nope thanks bye", "Have a lovely day, goodbye!", True),
        ("no thanks", "No problem. Is there anything else I can help with?", False),
        ("What time do you close?", "We close at six. Have a lovely day!", False),
        ("that's all", "Before you go, could I take your phone number?", False),
    ],
)
def test_a_call_is_over_only_when_both_sides_have_finished(said, reply, expected):
    from sophia.agent_text import call_is_over

    assert call_is_over(said, reply) is expected


@pytest.mark.parametrize(
    "said, reply, last_reply, expected",
    [
        ("no", "Thank you for calling, Mihir. Goodbye.", "Is there anything else I can help you with today, Mihir?", True),
        ("nope.", "Take care, bye now.", "Anything else I can help with?", True),
        ("no", "Thank you for calling, Mihir. Goodbye.", "Could you tell me again what you'd like to know?", True),
        ("no", "No problem, have a lovely day.", "Would you like to book it?", False),
    ],
)
def test_a_bare_no_ends_the_call_when_it_answers_anything_else_or_gets_a_goodbye(said, reply, last_reply, expected):
    """A tester answered with "no", heard goodbye, and the line stayed open."""
    from sophia.agent_text import call_is_over

    assert call_is_over(said, reply, last_reply) is expected


def test_the_turn_record_says_when_the_call_is_over(conn):
    agent = agent_with(conn, [text_response("Thank you for calling. Goodbye.")])
    assert agent.say("No, that's everything, thanks").ends_call is True


def test_a_name_is_not_confirmed_straight_after_it_was_given(conn):
    """"Just to confirm, your name is Mihir Gambhire?" right after he said it."""
    agent = agent_with(conn, [text_response("Could I take your full name?"), text_response("And your date of birth?"),
                              text_response("And your postcode?")])
    agent.say("I want to cancel my appointment")
    agent.say("Mihir Gambhire")
    # Asked about in the turn after, the name is known and can be confirmed.
    assert agent.tools.caller_name == "Mihir Gambhire"
    agent.say("13 April 2003")
    assert "already said their name: Mihir Gambhire" in agent._state_block()


def test_the_details_sophia_asked_for_are_remembered(conn):
    agent = agent_with(conn, [text_response("Could you tell me your date of birth and postcode?")])
    agent.say("I want to cancel my appointment")
    assert agent.tools.details_asked_for == {"date of birth", "postcode"}


def test_saying_a_real_person_will_answer_must_be_looked_up():
    """Sophia told a caller "a real person from our reception team answers", from nothing."""
    from sophia.agent_text import _CLAIMS_PRACTICE_FACT

    assert _CLAIMS_PRACTICE_FACT.search("a real person from our reception team answers the phone")
    assert _CLAIMS_PRACTICE_FACT.search("someone will answer during opening hours")
    assert not _CLAIMS_PRACTICE_FACT.search("No, I'm not a real person, I'm the practice's AI assistant.")


def test_who_answers_the_phone_is_in_the_practice_information():
    from sophia import knowledge

    found = knowledge.answer("is there gonna be a real person or AI if I call reception?")
    assert found["found"]
    assert any("real person" in extract["text"] for extract in found["extracts"])


def test_a_reply_does_not_start_with_stray_punctuation():
    from sophia.agent_text import plain_text

    assert plain_text(". Our reception staff answer the phone.") == "Our reception staff answer the phone."


def test_an_invented_call_back_habit_must_be_looked_up():
    from sophia.agent_text import _CLAIMS_PRACTICE_FACT

    assert _CLAIMS_PRACTICE_FACT.search("they will usually try calling you again later")
    assert not _CLAIMS_PRACTICE_FACT.search("I have taken that down for the team, and they will call you back.")


def test_a_guessed_call_back_habit_is_replaced_with_what_is_known():
    """"They will usually try calling you back again later", said straight after a lookup that said otherwise."""
    from sophia.agent_text import CALL_BACK_ANSWER, replace_call_back_guess

    reply = ("If they can't reach you, they will usually try calling you back again later. "
             "Is there anything else I can help with?")
    fixed = replace_call_back_guess(reply)
    assert "usually" not in fixed
    assert CALL_BACK_ANSWER in fixed and fixed.endswith("Is there anything else I can help with?")
    assert replace_call_back_guess("I have taken that down, and they will call you back.") is None


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("I have taken that down for the team, Mihir. Thank you for calling, have a lovely day. Goodbye.", True),
        ("Before we say goodbye, could I take your phone number?", False),
        ("Thank you for calling. Bye for now!", True),
    ],
)
def test_sophia_saying_goodbye_ends_the_call_on_its_own(reply, expected):
    """She said goodbye after taking his number, and the line stayed open."""
    from sophia.agent_text import call_is_over

    assert call_is_over("9307799220", reply) is expected
