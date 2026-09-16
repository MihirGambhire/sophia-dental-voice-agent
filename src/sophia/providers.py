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
tool heavy conversation without stalling. It is the safer choice for a
live demo. Groq stays the default for latency when the budget allows.

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
from dataclasses import dataclass, field
from types import SimpleNamespace

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
    """
    from google.genai import types

    system_parts: list[str] = []
    contents = []

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

            parts = []
            if message.get("content"):
                parts.append(types.Part(text=message["content"]))
            for call in tool_calls:
                try:
                    arguments = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                parts.append(
                    types.Part.from_function_call(
                        name=call["function"]["name"], args=arguments
                    )
                )
            if parts:
                contents.append(types.Content(role="model", parts=parts))
            continue

        if role == "tool":
            name = message.get("name") or call_names.get(message.get("tool_call_id", ""), "tool")
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

        response = self._client.models.generate_content(
            model=kwargs["model"],
            contents=contents,
            config=types.GenerateContentConfig(**gen_config),
        )

        return self._to_openai_shape(response)

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
# Choosing one
# ---------------------------------------------------------------------------


def build_client(provider: str | None = None):
    """
    Build the client for the configured provider.

    Errors here say what to do about them, because the most common cause
    is a missing key and the least helpful response is a stack trace.
    """
    provider = provider or config.LLM.provider

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
