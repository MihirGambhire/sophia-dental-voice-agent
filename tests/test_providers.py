"""
Tests for the provider translation layer.

The agent speaks one dialect. Groq understands it natively, Gemini does
not, so providers.py translates. Everything here tests that translation
without touching a network, because a mistranslation is a silent bug:
the call succeeds, the model simply never sees what you meant to send.
"""

import json
from types import SimpleNamespace

import pytest

from sophia import providers
from sophia.providers import ProviderError
from sophia.schemas import TOOL_SCHEMAS


# ---------------------------------------------------------------------------
# Tool schema conversion
# ---------------------------------------------------------------------------


def test_a_tool_with_no_parameters_sends_no_parameter_block():
    """
    Gemini rejects a declaration whose parameter object is empty, so the
    block has to be omitted rather than sent as {}.
    """
    assert providers._to_gemini_schema({"type": "object", "properties": {}}) is None
    assert providers._to_gemini_schema({}) is None


def test_types_are_upper_cased_for_gemini():
    schema = providers._to_gemini_schema(
        {"properties": {"count": {"type": "integer"}, "name": {"type": "string"}}}
    )
    assert schema["type"] == "OBJECT"
    assert schema["properties"]["count"]["type"] == "INTEGER"
    assert schema["properties"]["name"]["type"] == "STRING"


def test_enums_and_descriptions_survive():
    schema = providers._to_gemini_schema(
        {
            "properties": {
                "purpose": {
                    "type": "string",
                    "enum": ["check_up", "urgent"],
                    "description": "what they want",
                }
            },
            "required": ["purpose"],
        }
    )
    assert schema["properties"]["purpose"]["enum"] == ["check_up", "urgent"]
    assert schema["properties"]["purpose"]["description"] == "what they want"
    assert schema["required"] == ["purpose"]


def test_required_names_that_are_not_declared_are_dropped():
    """A required field with no property would be rejected outright."""
    schema = providers._to_gemini_schema(
        {"properties": {"a": {"type": "string"}}, "required": ["a", "ghost"]}
    )
    assert schema["required"] == ["a"]


def test_every_real_tool_converts_without_error():
    for schema in TOOL_SCHEMAS:
        providers._to_gemini_schema(schema["function"].get("parameters") or {})


# ---------------------------------------------------------------------------
# Conversation translation
# ---------------------------------------------------------------------------


def translate(messages, model_turns=None):
    return providers._to_gemini_contents(messages, {}, model_turns or {})


def test_system_messages_are_pulled_out_and_joined():
    """
    Sophia sends two: the standing prompt and a per turn state block.
    Gemini has one system_instruction, so they are joined rather than one
    silently winning.
    """
    system, contents = translate(
        [
            {"role": "system", "content": "You are Sophia."},
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "CALL STATE: verified"},
        ]
    )
    assert "You are Sophia." in system
    assert "CALL STATE: verified" in system
    assert len(contents) == 1


def test_assistant_becomes_model():
    _, contents = translate(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    )
    assert [content.role for content in contents] == ["user", "model"]


def test_a_tool_result_becomes_a_function_response():
    _, contents = translate(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "get_fee", "arguments": '{"appointment_type":"NHS_EXAM"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "get_fee",
                "content": json.dumps({"fee": "£27.90"}),
            },
        ]
    )
    last = contents[-1]
    assert last.parts[0].function_response is not None
    assert last.parts[0].function_response.name == "get_fee"


def test_a_tool_result_that_is_not_json_is_still_sent():
    _, contents = translate(
        [{"role": "tool", "tool_call_id": "c1", "name": "get_fee", "content": "not json"}]
    )
    assert contents[-1].parts[0].function_response is not None


def test_a_tool_result_that_is_a_bare_list_is_wrapped():
    """Gemini requires an object, so a list has to be boxed."""
    _, contents = translate(
        [{"role": "tool", "tool_call_id": "c1", "name": "x", "content": "[1, 2, 3]"}]
    )
    response = contents[-1].parts[0].function_response
    assert isinstance(response.response, dict)


# ---------------------------------------------------------------------------
# Thought signatures, which is where this first broke
# ---------------------------------------------------------------------------


def test_a_remembered_model_turn_is_replayed_verbatim():
    """
    Gemini 3 attaches an encrypted thought_signature to function call
    parts, and rejects a rebuilt part that lacks one:

        Function call is missing a thought_signature in functionCall parts

    So the Content Gemini returned must be replayed as the same object,
    not reconstructed from the OpenAI style message.
    """
    original = SimpleNamespace(role="model", parts=["the original, signature intact"])
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_fee", "arguments": "{}"}}
        ],
    }

    _, contents = translate([assistant], model_turns={"c1": original})

    assert contents[0] is original


def test_without_a_remembered_turn_it_falls_back_to_rebuilding():
    """
    A first request, or a conversation resumed from nothing, has no cache.
    Rebuilding is better than failing, and Gemini only objects once a
    signature has been issued.
    """
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_fee", "arguments": "{}"}}
        ],
    }
    _, contents = translate([assistant], model_turns={})

    assert contents[0].role == "model"
    assert contents[0].parts[0].function_call.name == "get_fee"


def test_malformed_tool_arguments_do_not_break_translation():
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_fee", "arguments": "{broken"}}
        ],
    }
    _, contents = translate([assistant], model_turns={})
    assert contents[0].parts[0].function_call.args == {}


# ---------------------------------------------------------------------------
# Reading the response back
# ---------------------------------------------------------------------------


def fake_gemini_response(parts):
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))]
    )


def completions():
    return providers._GeminiCompletions(client=None)


def test_a_text_reply_is_read_back():
    response = completions()._to_openai_shape(
        fake_gemini_response([SimpleNamespace(text="We close at six.", function_call=None)])
    )
    message = response.choices[0].message
    assert message.content == "We close at six."
    assert message.tool_calls is None


def test_a_function_call_is_read_back_in_openai_shape():
    part = SimpleNamespace(
        text=None,
        function_call=SimpleNamespace(name="get_fee", args={"appointment_type": "NHS_EXAM"}),
    )
    response = completions()._to_openai_shape(fake_gemini_response([part]))
    call = response.choices[0].message.tool_calls[0]

    assert call.function.name == "get_fee"
    assert json.loads(call.function.arguments) == {"appointment_type": "NHS_EXAM"}
    assert call.id


def test_several_function_calls_get_distinct_ids():
    """Ids are invented here, and pairing a result to a call depends on them."""
    parts = [
        SimpleNamespace(text=None, function_call=SimpleNamespace(name="a", args={})),
        SimpleNamespace(text=None, function_call=SimpleNamespace(name="b", args={})),
    ]
    response = completions()._to_openai_shape(fake_gemini_response(parts))
    ids = [call.id for call in response.choices[0].message.tool_calls]

    assert len(set(ids)) == 2


def test_text_and_a_function_call_together_both_survive():
    parts = [
        SimpleNamespace(text="Let me check.", function_call=None),
        SimpleNamespace(text=None, function_call=SimpleNamespace(name="get_fee", args={})),
    ]
    response = completions()._to_openai_shape(fake_gemini_response(parts))
    message = response.choices[0].message

    assert message.content == "Let me check."
    assert len(message.tool_calls) == 1


def test_an_empty_response_does_not_crash():
    response = completions()._to_openai_shape(SimpleNamespace(candidates=[]))
    assert response.choices[0].message.content is None


def test_the_model_turn_is_remembered_for_replay():
    part = SimpleNamespace(text=None, function_call=SimpleNamespace(name="get_fee", args={}))
    raw = fake_gemini_response([part])
    completion = completions()
    response = completion._to_openai_shape(raw)

    call_id = response.choices[0].message.tool_calls[0].id
    assert completion._model_turns[call_id] is raw.candidates[0].content


# ---------------------------------------------------------------------------
# Choosing a provider
# ---------------------------------------------------------------------------


def test_an_unknown_provider_says_what_is_valid():
    with pytest.raises(ProviderError, match="groq.*gemini|gemini.*groq"):
        providers.build_client("wishful-thinking")


def with_no_keys(monkeypatch):
    """
    Swap in a settings object with empty keys.

    LLMSettings is frozen, so individual fields cannot be patched. The
    whole object is replaced instead.
    """
    from sophia.config import LLMSettings

    monkeypatch.setattr(
        providers.config, "LLM", LLMSettings(provider="groq", groq_api_key="", gemini_api_key="")
    )


def test_a_missing_gemini_key_says_where_to_get_one(monkeypatch):
    with_no_keys(monkeypatch)
    with pytest.raises(ProviderError, match="aistudio"):
        providers.build_client("gemini")


def test_a_missing_groq_key_says_where_to_get_one(monkeypatch):
    with_no_keys(monkeypatch)
    with pytest.raises(ProviderError, match="console.groq.com"):
        providers.build_client("groq")


def test_api_keys_never_appear_in_a_repr():
    """
    A dataclass repr includes every field unless told otherwise, so a
    traceback that touched config printed both keys in full. This is the
    guard against that coming back.
    """
    text = repr(providers.config.LLM) + repr(providers.config.SPEECH)

    for secret in (
        providers.config.LLM.groq_api_key,
        providers.config.LLM.gemini_api_key,
        providers.config.SPEECH.deepgram_api_key,
    ):
        if secret:
            assert secret not in text, "an API key is exposed in a repr"
