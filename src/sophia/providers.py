"""
Model providers, behind one interface.

Sophia's conversation loop should not know or care which company is
answering. It builds OpenAI style messages, sends OpenAI style tool
schemas, and reads back `.choices[0].message`. Groq speaks that natively.
Gemini does not, so this module translates.

WHY BOTH
========
Groq is fast, and for a voice agent that matters more than almost
anything: a turn comes back in well under a second. But its free tier
allows 8,000 tokens per minute, and a tool calling agent resends the
system prompt and every tool schema on every round trip. Two or three
tool calls in one turn can exhaust the whole minute, after which the SDK
sits in backoff and the caller hears nothing for the best part of a
minute. That was measured, not guessed. See the engineering log.

Gemini's free tier is far more generous on tokens, so it can carry a long
tool heavy conversation without stalling. It is the default. Groq is the
last model in the fallback chain, further down this file.

WHAT ACTUALLY DIFFERS
=====================
Four things, all handled below:

  1. Gemini takes the system prompt as a separate `system_instruction`
     rather than as a message. Sophia sends more than one system message,
     the standing prompt plus a per turn state block, so they are joined.
  2. Roles are "user" and "model", not "user" and "assistant".
  3. A tool result is a function_response part, not a message with a role
     of "tool".
  4. Gemini does not give tool calls an id. OpenAI style pairing needs
     one, so synthetic ids are generated and matched by position.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from loguru import logger

from . import config


class ProviderError(RuntimeError):
    """Raised when a provider cannot be built, with something actionable to do."""


# ---------------------------------------------------------------------------
# The shape the agent expects back
# ---------------------------------------------------------------------------


@dataclass
class _FunctionCall:
    name: str
    arguments: str  # JSON string, as the OpenAI style expects


@dataclass
class _ToolCall:
    id: str
    function: _FunctionCall
    type: str = "function"


@dataclass
class _Message:
    content: str | None = None
    tool_calls: list[_ToolCall] | None = None


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list[_Choice] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def _to_gemini_schema(openai_parameters: dict) -> dict | None:
    """
    Convert an OpenAI style parameter block to what Gemini accepts.

    Gemini rejects a declaration whose parameter object has no properties,
    so a tool that takes no arguments must omit the block entirely rather
    than send an empty one.
    """
    properties = openai_parameters.get("properties") or {}
    if not properties:
        return None

    cleaned: dict[str, dict] = {}
    for name, spec in properties.items():
        entry = {"type": spec.get("type", "string").upper()}
        if spec.get("description"):
            entry["description"] = spec["description"]
        if spec.get("enum"):
            entry["enum"] = list(spec["enum"])
        cleaned[name] = entry

    schema: dict = {"type": "OBJECT", "properties": cleaned}
    required = [name for name in openai_parameters.get("required", []) if name in cleaned]
    if required:
        schema["required"] = required
    return schema


def _to_gemini_tools(tool_schemas: list[dict]):
    from google.genai import types

    declarations = []
    for schema in tool_schemas:
        function = schema["function"]
        declaration: dict = {
            "name": function["name"],
            "description": function.get("description", ""),
        }
        parameters = _to_gemini_schema(function.get("parameters") or {})
        if parameters:
            declaration["parameters"] = parameters
        declarations.append(types.FunctionDeclaration(**declaration))

    return [types.Tool(function_declarations=declarations)]


def _to_gemini_contents(
    messages: list[dict],
    call_names: dict[str, str],
    model_turns: dict[str, object],
):
    """
    Translate the conversation, and pull the system messages out of it.

    call_names maps a tool_call id to its function name, because a tool
    result message carries the id but Gemini needs the name.

    model_turns holds the Content objects Gemini actually returned. Gemini
    3 attaches a thought_signature to function call parts, an encrypted
    record of its own reasoning, and the documented rule for stateless
    conversations is that these must be resent exactly as received. A
    rebuilt part looks equivalent and is rejected with:

        Function call is missing a thought_signature in functionCall parts

    So an assistant turn that made tool calls is replayed verbatim from
    this cache rather than reconstructed from the OpenAI style message.
    Tool calls with nothing cached were made by another provider, and are
    described in text instead, see below.
    """
    from google.genai import types

    system_parts: list[str] = []
    contents = []
    told_as_text: set[str] = set()

    for message in messages:
        role = message.get("role")

        if role == "system":
            if message.get("content"):
                system_parts.append(message["content"])
            continue

        if role == "user":
            contents.append(
                types.Content(role="user", parts=[types.Part(text=message["content"])])
            )
            continue

        if role == "assistant":
            tool_calls = message.get("tool_calls") or []

            # Replay Gemini's own Content when we have it, so the thought
            # signatures survive. Anything else is rejected.
            if tool_calls:
                original = model_turns.get(tool_calls[0]["id"])
                for call in tool_calls:
                    call_names[call["id"]] = call["function"]["name"]
                if original is not None:
                    contents.append(original)
                    continue

            # No signature for these calls, because Gemini did not make them:
            # another provider in the fallback chain did. Rebuilt function
            # call parts are rejected outright, which ended a live call when
            # Groq ran out of tokens and Gemini could not take over. The
            # signature check applies to function call parts only, so the
            # calls and their results are told to Gemini as plain text.
            parts = []
            if message.get("content"):
                parts.append(types.Part(text=message["content"]))
            for call in tool_calls:
                told_as_text.add(call["id"])
                parts.append(
                    types.Part(
                        text=f"(I called {call['function']['name']} with {call['function']['arguments'] or '{}'}.)"
                    )
                )
            if parts:
                contents.append(types.Content(role="model", parts=parts))
            continue

        if role == "tool":
            name = message.get("name") or call_names.get(message.get("tool_call_id", ""), "tool")
            if message.get("tool_call_id") in told_as_text:
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part(text=f"(Result of {name}: {message.get('content') or '{}'})")],
                    )
                )
                continue
            try:
                payload = json.loads(message.get("content") or "{}")
            except json.JSONDecodeError:
                payload = {"result": message.get("content")}
            if not isinstance(payload, dict):
                payload = {"result": payload}
            contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_function_response(name=name, response=payload)],
                )
            )

    return "\n\n".join(system_parts), contents


class _GeminiCompletions:
    """Presents Gemini through the same create() call the agent already uses."""

    def __init__(self, client):
        self._client = client
        self._call_names: dict[str, str] = {}
        # tool_call id -> the Content object Gemini returned, kept whole so
        # its thought signatures can be replayed untouched.
        self._model_turns: dict[str, object] = {}

    def create(self, **kwargs) -> _Response:
        from google.genai import types

        system_instruction, contents = _to_gemini_contents(
            kwargs["messages"], self._call_names, self._model_turns
        )

        gen_config: dict = {
            "temperature": kwargs.get("temperature", 0.3),
            "max_output_tokens": kwargs.get("max_completion_tokens", 500),
        }
        if system_instruction:
            gen_config["system_instruction"] = system_instruction
        if kwargs.get("tools"):
            gen_config["tools"] = _to_gemini_tools(kwargs["tools"])

        response = self._generate_with_retry(
            model=kwargs["model"],
            contents=contents,
            config=types.GenerateContentConfig(**gen_config),
        )

        return self._to_openai_shape(response)

    # Waits between attempts. Short enough that a caller on a live call is
    # not left in silence for long, long enough to ride out a burst.
    RETRY_DELAYS_SECS = (1.5, 4.0, 9.0)

    def _generate_with_retry(self, **request):
        """
        Retry a request that failed for reasons worth retrying.

        Gemini returned 503 "high demand" for several models during testing,
        and the free tier answers 429 when requests arrive too quickly. Both
        are transient, and the request is safe to repeat because nothing has
        happened yet: tools only run after a successful response. Anything
        else, like a bad request, fails immediately.
        """
        import time

        from google.genai import errors

        for attempt, delay in enumerate((*self.RETRY_DELAYS_SECS, None)):
            try:
                return self._client.models.generate_content(**request)
            except (errors.ServerError, errors.ClientError) as failure:
                status = getattr(failure, "code", None)
                retryable = isinstance(failure, errors.ServerError) or status == 429
                # A daily quota is also a 429, and no wait short of the
                # daily reset helps. Retrying only makes the caller wait.
                if is_daily_quota(failure):
                    retryable = False
                if not retryable or delay is None:
                    raise
                time.sleep(delay)

    def _to_openai_shape(self, response) -> _Response:
        text_parts: list[str] = []
        tool_calls: list[_ToolCall] = []

        candidates = getattr(response, "candidates", None) or []
        if candidates:
            content = getattr(candidates[0], "content", None)
            for part in (getattr(content, "parts", None) or []):
                if getattr(part, "text", None):
                    text_parts.append(part.text)

                call = getattr(part, "function_call", None)
                if call is not None and getattr(call, "name", None):
                    # Gemini gives no call id, so one is invented. It only
                    # has to be unique within this conversation, because
                    # its single job is to pair a result back to a call.
                    call_id = f"gemini_call_{len(self._call_names)}_{call.name}"
                    self._call_names[call_id] = call.name
                    tool_calls.append(
                        _ToolCall(
                            id=call_id,
                            function=_FunctionCall(
                                name=call.name,
                                arguments=json.dumps(dict(call.args or {})),
                            ),
                        )
                    )

        # Keep the raw Content so the next request can replay it with its
        # thought signatures intact.
        if tool_calls and candidates:
            for call in tool_calls:
                self._model_turns[call.id] = candidates[0].content

        return _Response(
            choices=[
                _Choice(
                    message=_Message(
                        content="".join(text_parts) or None,
                        tool_calls=tool_calls or None,
                    )
                )
            ]
        )


class GeminiClient:
    """A Groq shaped wrapper around the Google GenAI SDK."""

    def __init__(self, api_key: str):
        try:
            from google import genai
        except ImportError as missing:
            raise ProviderError(
                "The Gemini provider needs the google-genai package. "
                "Run: pip install -r requirements.txt"
            ) from missing

        self._client = genai.Client(api_key=api_key)
        self.chat = SimpleNamespace(completions=_GeminiCompletions(self._client))


# ---------------------------------------------------------------------------
# Falling back when a model is out of quota or unavailable
# ---------------------------------------------------------------------------
#
# WHY
# ===
# The Gemini free tier allows 500 requests a day per model. A day of phone
# testing plus one full evaluation used all of them, and from then until
# the reset every caller heard an apology. Each model has its own quota,
# so the answer is a list of models tried in order, not more accounts:
# spreading one app across accounts to get round a limit is against the
# API terms, and is not something to put in a public repo.
#
# The rules are about what a caller on the line experiences:
#   - a daily quota moves on immediately and rests that model until the
#     reset, because retrying it only adds silence
#   - a per minute limit or a 503 rests the model briefly and moves on
#   - once a model is resting, every call skips it without asking again,
#     so only the first unlucky request pays for finding out
#   - anything that looks like a bug, a bad request, is raised as before,
#     because silently trying another model would hide it


# Seconds a model is left alone after each kind of failure.
REST_AFTER_OVERLOAD_SECS = 30.0
REST_AFTER_RATE_LIMIT_SECS = 60.0
REST_AFTER_NETWORK_SECS = 15.0
REST_WHEN_UNAVAILABLE_SECS = 3600.0

_NETWORK_FAILURES = {
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "RemoteProtocolError", "TimeoutException", "APIConnectionError", "APITimeoutError",
}


def _status_of(failure) -> int | None:
    """The HTTP status, whichever SDK raised it. Gemini says code, Groq status_code."""
    for name in ("code", "status_code"):
        value = getattr(failure, name, None)
        if isinstance(value, int):
            return value
    return None


def is_daily_quota(failure) -> bool:
    """
    True for a 429 that is a per day limit rather than a per minute one.

    They arrive as the same status. Gemini names the quota in the body,
    GenerateRequestsPerDayPerProjectPerModel-FreeTier, and Groq says
    "per day" in its message. Only this tells them apart.
    """
    text = str(failure)
    return _status_of(failure) == 429 and ("PerDay" in text or "per day" in text.lower())


def seconds_until_gemini_reset(now: datetime | None = None) -> float:
    """
    Seconds until Gemini's daily quotas reset, at midnight Pacific time.

    Both ends are converted to UTC before subtracting. Python subtracts two
    datetimes that share a tzinfo as wall clock times, which is the British
    Summer Time bug in the engineering log; on the night the clocks change,
    this would be an hour out.
    """
    pacific = ZoneInfo("America/Los_Angeles")
    now = (now or datetime.now(timezone.utc)).astimezone(pacific)
    reset = datetime.combine(now.date() + timedelta(days=1), dt_time(0), tzinfo=pacific)
    elapsed = reset.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return max(60.0, elapsed.total_seconds())


def _suggested_wait(failure) -> float | None:
    """The wait the provider asked for, if it said."""
    match = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+(?:\.\d+)?)s", str(failure))
    if match:
        return float(match.group(1))
    response = getattr(failure, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        return float(headers.get("retry-after")) if headers.get("retry-after") else None
    except (TypeError, ValueError):
        return None


@dataclass
class Verdict:
    """What to do about one failed request."""

    move_on: bool
    rest_seconds: float = 0.0
    reason: str = ""


def judge_failure(failure, provider: str) -> Verdict:
    """Decide whether a failure is the model's problem or the request's."""
    status = _status_of(failure)
    name = type(failure).__name__
    text = str(failure)

    if status == 429:
        if is_daily_quota(failure):
            wait = seconds_until_gemini_reset() if provider == "gemini" else REST_WHEN_UNAVAILABLE_SECS
            return Verdict(True, wait, "429 daily quota used up")
        return Verdict(True, _suggested_wait(failure) or REST_AFTER_RATE_LIMIT_SECS, "429 rate limited")
    if (status is not None and status >= 500) or name in ("ServerError", "InternalServerError"):
        return Verdict(True, REST_AFTER_OVERLOAD_SECS, f"{status or 503} overloaded")
    if name in _NETWORK_FAILURES:
        return Verdict(True, REST_AFTER_NETWORK_SECS, "network error")
    if status == 400 and "thought_signature" in text:
        # A rejected thought signature. Another provider's tool calls are
        # now sent as text so this should not happen, but if it does the
        # model itself is fine: skip it for this request, do not rest it.
        return Verdict(True, 0.0, "cannot continue another provider's tool calls")
    if status in (401, 403, 404):
        return Verdict(True, REST_WHEN_UNAVAILABLE_SECS, f"{status} unavailable to this key")
    return Verdict(False)


class ModelHealth:
    """
    Which models are resting, shared by every call in the process.

    Shared so that when one caller finds a model out of quota, the next
    caller does not pay for finding out again.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._resting: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def rest(self, label: str, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._resting[label] = (self._clock() + seconds, reason)

    def why_resting(self, label: str) -> str | None:
        """None if the model is available, otherwise why it is not."""
        with self._lock:
            entry = self._resting.get(label)
            if entry is None:
                return None
            until, reason = entry
            remaining = until - self._clock()
            if remaining <= 0:
                del self._resting[label]
                return None
            return f"{reason}, back in {remaining:.0f}s"

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            labels = list(self._resting)
        return {label: why for label in labels if (why := self.why_resting(label))}

    def clear(self) -> None:
        with self._lock:
            self._resting.clear()


HEALTH = ModelHealth()


class ModelsUnavailable(ProviderError):
    """Every model in the chain failed or is resting."""

    def __init__(self, reasons: list[tuple[str, str]]):
        self.reasons = reasons
        detail = "; ".join(f"{label}: {why}" for label, why in reasons)
        # Worded so the eval runner's quota checks still read it: a 429,
        # and PerDay only when every model is out for the day.
        all_daily = bool(reasons) and all("daily quota" in why for _, why in reasons)
        kind = "PerDay, every model is out for the day" if all_daily else "try again shortly"
        super().__init__(f"429 RESOURCE_EXHAUSTED ({kind}). No model available: {detail}")


class FallbackClient:
    """
    Tries each model in the chain until one answers.

    Presents the same chat.completions.create() as a single client, so the
    agent does not know it exists. One client per provider is kept for the
    whole call, which matters for Gemini: its cache of model turns, with
    their thought signatures, is shared by every Gemini model in the chain.
    Google documents that when switching models within a session, the
    previous model's thought blocks are resent and the backend manages
    compatibility.
    """

    def __init__(self, chain: list[tuple[str, str]], health: ModelHealth | None = None, factory=None):
        if not chain:
            raise ProviderError("The model chain is empty.")
        self.chain = list(chain)
        self.health = health or HEALTH
        self._factory = factory or self._build_for_chain
        self._clients: dict[str, object] = {}
        # Which model answered the most recent request, as "provider:model".
        self.served_by: str | None = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def _build_for_chain(self, provider: str):
        """
        A client that fails fast, because there is somewhere else to go.

        The SDKs' own retries are what made Groq stall for a minute, and on
        a live call moving to the next model is quicker than waiting.
        """
        client = build_single_client(provider)
        if provider == "gemini":
            client.chat.completions.RETRY_DELAYS_SECS = ()
        elif provider == "groq":
            client = client.with_options(max_retries=0)
        return client

    def _client(self, provider: str):
        if provider not in self._clients:
            self._clients[provider] = self._factory(provider)
        return self._clients[provider]

    def create(self, **request):
        reasons: list[tuple[str, str]] = []
        for provider, model in self.chain:
            label = f"{provider}:{model}"
            resting = self.health.why_resting(label)
            if resting:
                reasons.append((label, resting))
                continue

            attempt = dict(request, model=model)
            if provider == "groq" and config.LLM.reasoning_effort:
                attempt["reasoning_effort"] = config.LLM.reasoning_effort
            else:
                attempt.pop("reasoning_effort", None)

            try:
                response = self._client(provider).chat.completions.create(**attempt)
            except Exception as failure:  # noqa: BLE001, judged just below
                verdict = judge_failure(failure, provider)
                if not verdict.move_on:
                    raise
                self.health.rest(label, verdict.rest_seconds, verdict.reason)
                reasons.append((label, verdict.reason))
                logger.warning(f"Model {label} failed ({verdict.reason}), trying the next one")
                continue

            if self.served_by and self.served_by != label:
                logger.info(f"Now answering with {label}")
            self.served_by = label
            return response

        raise ModelsUnavailable(reasons)


# ---------------------------------------------------------------------------
# Choosing one
# ---------------------------------------------------------------------------


def build_client(provider: str | None = None):
    """
    Build the client Sophia talks through.

    With fallbacks configured, a FallbackClient over the whole chain.
    Naming a provider asks for that provider alone, with no fallback.
    """
    if provider is None:
        chain = config.LLM.chain()
        if len(chain) > 1:
            build_single_client(config.LLM.provider)  # fail now on a missing primary key
            return FallbackClient(chain)
    return build_single_client(provider or config.LLM.provider)


def build_single_client(provider: str):
    """
    Build the client for one provider.

    Errors here say what to do about them, because the most common cause
    is a missing key and the least helpful response is a stack trace.
    """

    if provider == "gemini":
        if not config.LLM.gemini_api_key:
            raise ProviderError(
                "LLM_PROVIDER is gemini but GEMINI_API_KEY is empty in .env. "
                "Get a free key at https://aistudio.google.com/apikey"
            )
        return GeminiClient(config.LLM.gemini_api_key)

    if provider == "groq":
        if not config.LLM.groq_api_key:
            raise ProviderError(
                "No GROQ_API_KEY found. Copy .env.example to .env and add a key "
                "from https://console.groq.com, which is free and needs no card."
            )
        try:
            from groq import Groq
        except ImportError as missing:
            raise ProviderError(
                "The Groq provider needs the groq package. "
                "Run: pip install -r requirements.txt"
            ) from missing
        return Groq(api_key=config.LLM.groq_api_key)

    raise ProviderError(
        f"Unknown LLM_PROVIDER '{provider}'. Use 'groq' or 'gemini'."
    )
