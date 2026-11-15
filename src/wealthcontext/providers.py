"""Thin official SDK calls. Each inference starts from freshly authorized transcript data.

Fresh inference is our context-format choice, not a provider requirement: native
continuations require opaque reasoning replay, which conflicts with editable
authorization tombstones. No provider history or server-side conversation is reused.
"""

import json
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import anthropic
import anyio
import httpx
import httpx2
import openai
from google import genai
from google.genai import errors as google_errors
from google.genai import types as google_types
from pydantic import JsonValue, TypeAdapter, ValidationError

from wealthcontext.provider_contracts import (
    MAX_TRANSPORT_RETRIES,
    ProviderFailure,
    ProviderName,
    ProviderRequest,
    ProviderTurn,
    ToolCall,
)

DEFAULT_MODELS: dict[ProviderName, str] = {
    "anthropic": "claude-opus-5-5",
    "openai": "gpt-6-luna",
    "google": "gemini-3.8-flash",
}
# Reasoning tokens count against the Responses API output limit, so OpenAI gets the
# configuration maximum; the other adapters keep the earlier default.
DEFAULT_MAX_OUTPUT_TOKENS: dict[ProviderName, int] = {
    "anthropic": 8192, "openai": 16384, "google": 8192,
}
# Owner decision 2026-11-08: live OpenAI runs use reasoning effort "high" or lower. Amended
# 2026-11-13 ("Try sol on xhigh", then "Allow xhigh, then run"): "xhigh" is allowed too, for
# the scored gate run of GPT-6.1 Sol. The default stays "high".
OPENAI_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
DEFAULT_OPENAI_REASONING_EFFORT = "high"
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
# Owner decision 2026-11-05 (ADR 0008): at most two transport retries per provider call,
# only for rate limits (429), server errors (5xx) and connection failures. SDK retry
# loops stay disabled; this bounded loop runs inside the harness per-call timeout.
TRANSPORT_RETRIES = MAX_TRANSPORT_RETRIES
TRANSPORT_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0)


def _status_retryable(status: object) -> bool:
    return type(status) is int and (status == 429 or 500 <= status <= 599)


def _anthropic_retryable(exc: Exception) -> bool:
    return isinstance(exc, anthropic.APIConnectionError) or (
        isinstance(exc, anthropic.APIStatusError) and _status_retryable(exc.status_code))


def _openai_retryable(exc: Exception) -> bool:
    return isinstance(exc, openai.APIConnectionError) or (
        isinstance(exc, openai.APIStatusError) and _status_retryable(exc.status_code))


# google-genai depends on httpx and sends through it when installed, otherwise through the
# httpx2 fork the project declares (and the Anthropic and OpenAI SDKs use); classes differ.
_HTTP_TRANSPORT_ERRORS = (httpx.TransportError, httpx2.TransportError)


def _google_retryable(exc: Exception) -> bool:
    return isinstance(exc, _HTTP_TRANSPORT_ERRORS) or (
        isinstance(exc, google_errors.APIError) and _status_retryable(exc.code))


class _TransportRetries:
    """Bounded transport retries; the count is visible even when the harness times out."""

    def __init__(self, delays: tuple[float, ...]):
        if len(delays) != TRANSPORT_RETRIES or any(delay < 0 for delay in delays):
            raise ValueError("retry delays must match the transport retry bound")
        self.delays = delays
        self.last_transport_retries = 0

    async def _send(self, call: Callable[[], Awaitable[Any]],
                    retryable: Callable[[Exception], bool]) -> Any:
        self.last_transport_retries = 0
        while True:
            try:
                return await call()
            except Exception as exc:
                if (self.last_transport_retries >= TRANSPORT_RETRIES
                        or not retryable(exc)):
                    raise ProviderFailure("PROVIDER_CALL_FAILED") from None
            await anyio.sleep(self.delays[self.last_transport_retries])
            self.last_transport_retries += 1


def transcript(request: ProviderRequest) -> str:
    """Keep every authorized pair/tombstone; never summarize or replay opaque native state."""
    return json.dumps({"authorized_conversation": request.messages,
                       "terminal_only": request.terminal_only},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _mapping(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump(mode="json")
    raise ProviderFailure("MALFORMED_PROVIDER_RESPONSE")


def _reject_constant(_constant: str) -> None:
    raise ValueError("non-finite JSON number")


def _arguments(value: Any) -> dict[str, JsonValue]:
    """Strict JSON object: NaN/Infinity are rejected from strings and decoded mappings alike."""
    try:
        if isinstance(value, str):
            decoded = json.loads(value, parse_constant=_reject_constant)
        else:
            decoded = json.loads(json.dumps(value, allow_nan=False))
        return _JSON_OBJECT.validate_python(decoded)
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise ProviderFailure("MALFORMED_TOOL_ARGUMENTS") from None


def _parse_response(parser: Callable[[Any], ProviderTurn], response: Any) -> ProviderTurn:
    """Failure boundary for a received response: its shape, never the SDK call, failed."""
    try:
        return parser(response)
    except ProviderFailure:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
        raise ProviderFailure("MALFORMED_PROVIDER_RESPONSE") from None


def _turn(
    calls: list[ToolCall], text: list[str], payload: dict, model: str, response: Any,
) -> ProviderTurn:
    try:
        return ProviderTurn(
            calls=calls, text="\n".join(text) or None, model=payload.get("model") or model,
            request_id=getattr(response, "_request_id", None) or payload.get("request_id"),
            response_id=payload.get("id"), usage=payload.get("usage") or {},
        )
    except (ValueError, TypeError):
        raise ProviderFailure("MALFORMED_PROVIDER_RESPONSE") from None


class AnthropicProvider(_TransportRetries):
    provider = "anthropic"

    def __init__(self, client: Any, model: str = DEFAULT_MODELS["anthropic"], *,
                 max_output_tokens: int = 8192,
                 retry_delays: tuple[float, ...] = TRANSPORT_RETRY_DELAYS):
        super().__init__(retry_delays)
        self.client, self.model = client, model
        self.thinking: dict[str, JsonValue]
        if "sonnet-5-5" in model:
            self.thinking = {"type": "between_tools"}
        elif any(name in model for name in ("opus-5-5", "fable", "mythos")):
            self.thinking = {"type": "adaptive", "display": "omitted"}
        else:
            self.thinking = {"type": "disabled"}
        # Forced tool choice is rejected alongside thinking, so derive it from thinking.
        self.force_tools = self.thinking["type"] == "disabled"
        self.settings: dict[str, JsonValue] = {
            "max_output_tokens": max_output_tokens, "thinking": self.thinking,
            "tool_choice": "any" if self.force_tools else "auto", "sdk_retries": 0,
            "transport_retries": TRANSPORT_RETRIES,
            "context_mode": "fresh_authorized_transcript",
        }

    async def aclose(self) -> None:
        await self.client.close()

    async def complete(self, request: ProviderRequest) -> ProviderTurn:
        response = await self._send(lambda: self.client.messages.create(
            model=self.model, max_tokens=self.settings["max_output_tokens"],
            thinking=self.thinking,
            tools=[{"name": tool.name, "description": tool.description,
                    "input_schema": tool.input_schema} for tool in request.tools],
            tool_choice={"type": "any" if self.force_tools else "auto",
                         "disable_parallel_tool_use": True},
            system=request.system_prompt,
            messages=[{"role": "user", "content": transcript(request)}],
        ), _anthropic_retryable)
        return _parse_response(self._parse, response)

    def _parse(self, response: Any) -> ProviderTurn:
        payload = _mapping(response)
        if payload.get("stop_reason") in {"max_tokens", "model_context_window_exceeded"}:
            raise ProviderFailure("PROVIDER_OUTPUT_LIMIT")
        calls, text = [], []
        for block in payload.get("content", []):
            if block.get("type") == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"],
                                      arguments=_arguments(block["input"])))
            elif block.get("type") == "text":
                text.append(block["text"])
        return _turn(calls, text, payload, self.model, response)


class OpenAIProvider(_TransportRetries):
    provider = "openai"

    def __init__(self, client: Any, model: str = DEFAULT_MODELS["openai"], *,
                 max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS["openai"],
                 reasoning_effort: str = DEFAULT_OPENAI_REASONING_EFFORT,
                 retry_delays: tuple[float, ...] = TRANSPORT_RETRY_DELAYS):
        super().__init__(retry_delays)
        if reasoning_effort not in OPENAI_REASONING_EFFORTS:
            raise ValueError("OpenAI reasoning effort must be low, medium, high or xhigh")
        self.client, self.model, self.reasoning_effort = client, model, reasoning_effort
        self.settings: dict[str, JsonValue] = {
            "max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort,
            "store": False, "sdk_retries": 0, "transport_retries": TRANSPORT_RETRIES,
            "context_mode": "fresh_authorized_transcript",
        }

    async def aclose(self) -> None:
        await self.client.close()

    async def complete(self, request: ProviderRequest) -> ProviderTurn:
        response = await self._send(lambda: self.client.responses.create(
            model=self.model, max_output_tokens=self.settings["max_output_tokens"],
            reasoning={"effort": self.reasoning_effort}, store=False, parallel_tool_calls=False,
            instructions=request.system_prompt,
            input=[{"role": "user", "content": transcript(request)}],
            tools=[{"type": "function", "name": tool.name, "description": tool.description,
                    "parameters": tool.input_schema, "strict": False}
                   for tool in request.tools],
            tool_choice=({"type": "function", "name": "submit_answer"}
                         if request.terminal_only else "required"),
        ), _openai_retryable)
        return _parse_response(self._parse, response)

    def _parse(self, response: Any) -> ProviderTurn:
        payload = _mapping(response)
        if payload.get("status") in {"failed", "incomplete", "cancelled"}:
            raise ProviderFailure("PROVIDER_OUTPUT_INCOMPLETE")
        calls, text = [], []
        for item in payload.get("output", []):
            if item.get("type") == "function_call":
                calls.append(ToolCall(id=item["call_id"], name=item["name"],
                                      arguments=_arguments(item["arguments"])))
            elif item.get("type") == "message":
                text.extend(part["text"] for part in item.get("content", [])
                            if part.get("type") == "output_text")
        return _turn(calls, text, payload, self.model, response)


class GoogleProvider(_TransportRetries):
    provider = "google"

    def __init__(self, client: Any, model: str = DEFAULT_MODELS["google"], *,
                 max_output_tokens: int = 8192,
                 retry_delays: tuple[float, ...] = TRANSPORT_RETRY_DELAYS):
        super().__init__(retry_delays)
        self.client, self.model = client, model
        self.settings: dict[str, JsonValue] = {
            "max_output_tokens": max_output_tokens, "thinking_level": "low", "store": False,
            "automatic_continuation": False, "sdk_retries": 0,
            "transport_retries": TRANSPORT_RETRIES,
            "context_mode": "fresh_authorized_transcript",
        }

    async def aclose(self) -> None:
        await self.client.aio.aclose()
        self.client.close()

    async def complete(self, request: ProviderRequest) -> ProviderTurn:
        response = await self._send(lambda: self.client.aio.interactions.create(
            model=self.model, store=False, automatic_continuation=False,
            system_instruction=request.system_prompt,
            input=[{"type": "user_input", "content": [{"type": "text",
                                                        "text": transcript(request)}]}],
            tools=[{"type": "function", "name": tool.name, "description": tool.description,
                    "parameters": tool.input_schema} for tool in request.tools],
            generation_config={"max_output_tokens": self.settings["max_output_tokens"],
                               "thinking_level": "low", "tool_choice": "any"},
        ), _google_retryable)
        return _parse_response(self._parse, response)

    def _parse(self, response: Any) -> ProviderTurn:
        payload = _mapping(response)
        if payload.get("status") in {
            "failed", "incomplete", "cancelled", "requires_continuation",
        }:
            raise ProviderFailure("PROVIDER_OUTPUT_INCOMPLETE")
        calls, text = [], []
        for step in payload.get("steps", []):
            if step.get("type") == "function_call":
                calls.append(ToolCall(id=step["id"], name=step["name"],
                                      arguments=_arguments(step["arguments"])))
            elif step.get("type") == "model_output":
                text.extend(part["text"] for part in step.get("content", [])
                            if part.get("type") == "text")
        return _turn(calls, text, payload, self.model, response)


def _openai_reasoning_effort() -> str:
    return (os.environ.get("WEALTHCONTEXT_OPENAI_REASONING_EFFORT")
            or DEFAULT_OPENAI_REASONING_EFFORT)


def provider_configuration(
    provider: ProviderName = "anthropic", *, model: str | None = None,
    max_output_tokens: int | None = None,
) -> tuple[str, dict[str, JsonValue]]:
    """Compute exact public metadata without SDK clients, credentials or network access."""
    if max_output_tokens is None:
        max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS[provider]
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 16384:
        raise ValueError("max_output_tokens must be between 1 and 16384")
    resolved = (model or os.environ.get(f"WEALTHCONTEXT_{provider.upper()}_MODEL")
                or DEFAULT_MODELS[provider])
    # Constructors only retain the injected client; settings have one source of truth.
    configured: AnthropicProvider | OpenAIProvider | GoogleProvider
    if provider == "openai":
        configured = OpenAIProvider(None, resolved, max_output_tokens=max_output_tokens,
                                    reasoning_effort=_openai_reasoning_effort())
    else:
        implementation = {"anthropic": AnthropicProvider, "google": GoogleProvider}[provider]
        configured = implementation(None, resolved, max_output_tokens=max_output_tokens)
    return resolved, _JSON_OBJECT.validate_python(configured.settings)


def create_provider(
    provider: ProviderName = "anthropic", *, model: str | None = None, api_key: str | None = None,
    workspace_id: str | None = None, timeout_seconds: float = 30,
    max_output_tokens: int | None = None,
) -> AnthropicProvider | OpenAIProvider | GoogleProvider:
    """Construct only; no model request, .env loading, credential logging or SDK retry loops."""
    if max_output_tokens is None:
        max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS[provider]
    model, _ = provider_configuration(provider, model=model, max_output_tokens=max_output_tokens)
    key_name = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                "google": "GEMINI_API_KEY"}[provider]
    key = api_key or os.environ.get(key_name)
    if not key:
        raise ProviderFailure("MISSING_PROVIDER_CREDENTIALS")
    if provider == "anthropic":
        workspace_id = workspace_id or os.environ.get("ANTHROPIC_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else {}
        return AnthropicProvider(anthropic.AsyncAnthropic(
            api_key=key, max_retries=0, timeout=timeout_seconds, default_headers=headers,
        ), model, max_output_tokens=max_output_tokens)
    if provider == "openai":
        return OpenAIProvider(openai.AsyncOpenAI(api_key=key, max_retries=0,
                                                timeout=timeout_seconds), model,
                              max_output_tokens=max_output_tokens,
                              reasoning_effort=_openai_reasoning_effort())
    return GoogleProvider(genai.Client(api_key=key, http_options=google_types.HttpOptions(
        timeout=int(timeout_seconds * 1000),
        retry_options=google_types.HttpRetryOptions(attempts=1),
    )), model, max_output_tokens=max_output_tokens)
