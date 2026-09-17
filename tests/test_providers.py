"""
Tests for the provider translation layer.

The agent speaks one dialect. Groq understands it natively, Gemini does
not, so providers.py translates. Everything here tests that translation
without touching a network, because a mistranslation is a silent bug:
the call succeeds, the model simply never sees what you meant to send.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sophia import providers
from sophia.providers import ProviderError
from sophia.schemas import TOOL_SCHEMAS

# The quota tests check the error wording against the eval runner's own
# checks, which live outside src.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    """For a call Gemini made itself, so its turn is in the replay cache."""
    original = SimpleNamespace(role="model", parts=["gemini's own call"])
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
        ],
        model_turns={"c1": original},
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


def test_another_providers_tool_calls_are_told_as_text():
    """
    Tool calls with nothing in the cache were made by another model in the
    fallback chain. This used to rebuild them as function call parts, on
    the belief that Gemini only objects once it has issued a signature. A
    live call proved otherwise: Groq ran out of tokens on turn two,
    Gemini rejected the rebuilt calls with a 400, and the call ended.
    Text parts carry no signature to check.
    """
    _, contents = translate(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "g1", "type": "function", "function": {"name": "verify_patient", "arguments": '{"full_name": "Margaret Hollis"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "g1", "name": "verify_patient", "content": '{"verified": true}'},
        ],
        model_turns={},
    )

    assert contents[0].role == "model"
    assert contents[0].parts[0].function_call is None
    assert "verify_patient" in contents[0].parts[0].text and "Margaret Hollis" in contents[0].parts[0].text
    assert contents[1].role == "user"
    assert contents[1].parts[0].function_response is None
    assert '"verified": true' in contents[1].parts[0].text


def test_malformed_tool_arguments_do_not_break_translation():
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_fee", "arguments": "{broken"}}
        ],
    }
    _, contents = translate([assistant], model_turns={})
    assert "get_fee" in contents[0].parts[0].text


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


# ---------------------------------------------------------------------------
# Falling back between models
# ---------------------------------------------------------------------------


class FakeApiError(Exception):
    """Shaped like the SDK errors: a status on .code, the body in the message."""

    def __init__(self, code, message=""):
        super().__init__(f"{code} {message}")
        self.code = code


class ServerError(FakeApiError):
    """Named like google.genai.errors.ServerError, which is matched by name."""


DAILY = FakeApiError(429, "RESOURCE_EXHAUSTED {'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}")
PER_MINUTE = FakeApiError(429, "RESOURCE_EXHAUSTED {'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier', 'retryDelay': '31s'}")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ScriptedProvider:
    """A provider whose models fail or answer as scripted, recording every request."""

    def __init__(self, outcomes):
        self.outcomes = outcomes  # model -> exception, or a reply string
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        outcome = self.outcomes[request["model"]]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome, tool_calls=None))])


def chain_client(outcomes_by_provider, chain, clock=None):
    fakes = {name: ScriptedProvider(outcomes) for name, outcomes in outcomes_by_provider.items()}
    health = providers.ModelHealth(clock=clock or FakeClock())
    client = providers.FallbackClient(chain, health=health, factory=lambda name: fakes[name])
    return client, fakes, health


CHAIN = [("gemini", "primary"), ("gemini", "backup"), ("groq", "last")]


def ask(client):
    return client.chat.completions.create(model="ignored", messages=[], reasoning_effort="low")


def test_a_healthy_primary_answers_and_nothing_else_is_asked():
    client, fakes, _ = chain_client({"gemini": {"primary": "hello", "backup": "no"}, "groq": {}}, CHAIN)
    assert ask(client).choices[0].message.content == "hello"
    assert [r["model"] for r in fakes["gemini"].requests] == ["primary"]
    assert client.served_by == "gemini:primary"


def test_a_daily_quota_moves_on_and_is_not_asked_again():
    """The first unlucky request finds out. Every later one goes straight past."""
    client, fakes, health = chain_client({"gemini": {"primary": DAILY, "backup": "hi"}, "groq": {}}, CHAIN)
    assert ask(client).choices[0].message.content == "hi"
    assert ask(client).choices[0].message.content == "hi"
    assert [r["model"] for r in fakes["gemini"].requests] == ["primary", "backup", "backup"]
    assert "daily quota" in health.why_resting("gemini:primary")
    assert client.served_by == "gemini:backup"


def test_a_per_minute_limit_rests_the_model_only_as_long_as_asked():
    clock = FakeClock()
    outcomes = {"gemini": {"primary": PER_MINUTE, "backup": "hi"}, "groq": {}}
    client, fakes, health = chain_client(outcomes, CHAIN, clock=clock)
    ask(client)
    assert health.why_resting("gemini:primary")

    clock.now += 32
    fakes["gemini"].outcomes["primary"] = "back"
    assert ask(client).choices[0].message.content == "back"


def test_an_overloaded_model_is_rested_briefly():
    client, _, health = chain_client(
        {"gemini": {"primary": ServerError(503, "UNAVAILABLE"), "backup": "hi"}, "groq": {}}, CHAIN
    )
    ask(client)
    assert "overloaded" in health.why_resting("gemini:primary")


def test_a_bad_request_is_raised_not_hidden_behind_another_model():
    """Trying the next model would make a real bug look like a quota problem."""
    client, fakes, _ = chain_client(
        {"gemini": {"primary": FakeApiError(400, "INVALID_ARGUMENT"), "backup": "hi"}, "groq": {}}, CHAIN
    )
    with pytest.raises(FakeApiError):
        ask(client)
    assert [r["model"] for r in fakes["gemini"].requests] == ["primary"]


def test_a_missing_thought_signature_skips_the_model_without_resting_it():
    """
    After Groq has answered with tool calls, Gemini cannot continue that
    call. That says nothing about Gemini for any other caller.
    """
    signature = FakeApiError(400, "Function call is missing a thought_signature in functionCall parts")
    client, _, health = chain_client({"gemini": {"primary": signature, "backup": signature}, "groq": {"last": "ok"}}, CHAIN)
    assert ask(client).choices[0].message.content == "ok"
    assert health.why_resting("gemini:primary") is None


def test_reasoning_effort_only_reaches_groq():
    """Groq needs it to protect its token cap; Gemini does not take it."""
    client, fakes, _ = chain_client({"gemini": {"primary": DAILY, "backup": DAILY}, "groq": {"last": "ok"}}, CHAIN)
    ask(client)
    assert all("reasoning_effort" not in r for r in fakes["gemini"].requests)
    assert "reasoning_effort" in fakes["groq"].requests[0]


def test_when_every_model_is_out_for_the_day_the_error_says_so():
    """Worded so the eval runner stops instead of retrying for hours."""
    client, _, _ = chain_client({"gemini": {"primary": DAILY, "backup": DAILY}, "groq": {"last": DAILY}}, CHAIN)
    with pytest.raises(providers.ModelsUnavailable) as raised:
        ask(client)
    from evals.framework import is_daily_quota

    assert is_daily_quota(str(raised.value))


def test_a_mix_of_failures_is_not_reported_as_a_daily_limit():
    client, _, _ = chain_client(
        {"gemini": {"primary": DAILY, "backup": ServerError(503, "UNAVAILABLE")}, "groq": {"last": DAILY}}, CHAIN
    )
    with pytest.raises(providers.ModelsUnavailable) as raised:
        ask(client)
    from evals.framework import is_daily_quota, is_rate_limited

    assert is_rate_limited(str(raised.value))
    assert not is_daily_quota(str(raised.value))


def test_the_daily_reset_is_midnight_pacific_across_a_clock_change():
    """
    3 November 2024: US clocks went back an hour at 2am Pacific. From 8pm
    UTC on the 2nd, 1pm PDT, to midnight is an ordinary 11 hours. From just
    after midnight on the 3rd to the next midnight is 25 hours, because
    that day had an extra hour. Wall clock subtraction would say 24.
    """
    from datetime import datetime, timezone

    assert providers.seconds_until_gemini_reset(datetime(2024, 11, 2, 20, 0, tzinfo=timezone.utc)) == 11 * 3600
    assert providers.seconds_until_gemini_reset(datetime(2024, 11, 3, 7, 0, 1, tzinfo=timezone.utc)) == 25 * 3600 - 1


def test_the_chain_starts_with_the_primary_and_skips_what_it_cannot_use(monkeypatch):
    from sophia.config import LLMSettings

    settings = LLMSettings(
        provider="gemini", gemini_model="a", gemini_api_key="g", groq_api_key="",
        fallbacks="gemini:a, gemini:b ,groq:c,mystery:d,gemini:",
    )
    assert settings.chain() == [("gemini", "a"), ("gemini", "b")]
    assert LLMSettings(provider="gemini", gemini_model="a", gemini_api_key="g", fallbacks="none").chain() == [("gemini", "a")]


def test_the_gemini_adapter_does_not_retry_a_daily_quota(monkeypatch):
    """Retrying cannot help before the reset. It only leaves a caller in silence."""
    from google.genai import errors

    daily = errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
                                               "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}})
    attempts = []

    def generate_content(**request):
        attempts.append(request)
        raise daily

    fake = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    completions = providers._GeminiCompletions(fake)
    monkeypatch.setattr("time.sleep", lambda seconds: pytest.fail("slept before failing a daily quota"))
    with pytest.raises(errors.ClientError):
        completions._generate_with_retry(model="m", contents=[], config=None)
    assert len(attempts) == 1


def test_calls_share_the_sdk_client_but_not_the_conversation():
    """A new SDK client per call cost 2.2 seconds before every greeting."""
    first = providers.GeminiClient("test-key-shared")
    second = providers.GeminiClient("test-key-shared")
    assert first._client is second._client
    assert first.chat.completions is not second.chat.completions


def test_a_reply_in_two_parts_is_joined_with_a_space_and_thoughts_are_dropped():
    response = fake_gemini_response([
        SimpleNamespace(text="Let me reason about this.", thought=True, function_call=None),
        SimpleNamespace(text="Could you confirm your postcode?", thought=False, function_call=None),
        SimpleNamespace(text="Margaret, may I have it please?", thought=None, function_call=None),
    ])
    message = completions()._to_openai_shape(response).choices[0].message
    assert message.content == "Could you confirm your postcode? Margaret, may I have it please?"
