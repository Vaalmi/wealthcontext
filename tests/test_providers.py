"""Official-SDK method payloads with fake clients; no live account/provider coverage."""

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import anyio
import httpx
import httpx2
import openai
import pytest
from google.genai import errors as google_errors

from wealthcontext.provider_contracts import ProviderFailure, ProviderRequest, ToolSpec
from wealthcontext.providers import (
    TRANSPORT_RETRIES,
    TRANSPORT_RETRY_DELAYS,
    AnthropicProvider,
    GoogleProvider,
    OpenAIProvider,
    create_provider,
    provider_configuration,
)


class Endpoint:
    def __init__(self, response: Any):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def request(*, terminal_only=False, private=False):
    return ProviderRequest(
        system_prompt="Trusted harness instructions.",
        messages=[
            {"role": "user", "content": "Report governed facts."},
            {"role": "assistant", "tool_calls": [{"id": "call_data", "name": "snapshot",
                                                     "arguments": {} if not private else
                                                     {"synthetic_private": "old-private-figure"}}]},
            {"role": "tool", "tool_call_id": "call_data", "content":
             "old-private-figure" if private else "[REDACTED: NOT_AUTHORIZED]"},
        ],
        tools=[ToolSpec(name="submit_answer", description="Terminal frozen answer.",
                        input_schema={"type": "object",
                                      "properties": {"answer": {"type": "string"}},
                                      "required": ["answer"], "additionalProperties": False})],
        terminal_only=terminal_only,
    )


def adapter_case(provider, *, malformed=False):
    args = [] if malformed else {"answer": "Safe placeholder"}
    if provider == "anthropic":
        endpoint = Endpoint({
            "id": "msg_1", "model": "claude-sonnet-5", "stop_reason": "tool_use",
            "content": [{"type": "thinking", "signature": "opaque-old-reasoning"},
                        {"type": "tool_use", "id": "call_submit", "name": "submit_answer",
                         "input": args}],
            "usage": {"input_tokens": 17, "output_tokens": 5},
        })
        return (
            AnthropicProvider(SimpleNamespace(messages=endpoint), model="claude-sonnet-5"),
            endpoint,
        )
    if provider == "openai":
        endpoint = Endpoint({
            "id": "resp_1", "model": "gpt-6-luna", "status": "completed",
            "output": [{"type": "reasoning", "encrypted_content": "opaque-old-reasoning"},
                       {"type": "function_call", "call_id": "call_submit", "name": "submit_answer",
                        "arguments": json.dumps(args)}],
            "usage": {"input_tokens": 17, "output_tokens": 5},
        })
        return OpenAIProvider(SimpleNamespace(responses=endpoint)), endpoint
    endpoint = Endpoint({
        "id": "interaction_1", "model": "gemini-3.8-flash", "status": "requires_action",
        "steps": [{"type": "thought", "signature": "opaque-old-reasoning"},
                  {"type": "function_call", "id": "call_submit", "name": "submit_answer",
                   "arguments": args}],
        "usage": {"total_input_tokens": 17, "total_output_tokens": 5},
    })
    return GoogleProvider(SimpleNamespace(aio=SimpleNamespace(interactions=endpoint))), endpoint


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_native_sdk_payload_and_parsing_are_stateless_and_manual(provider):
    adapter, endpoint = adapter_case(provider)
    first = await adapter.complete(request(private=True))
    assert first.calls[0].name == "submit_answer"
    assert first.calls[0].id == "call_submit"
    assert first.calls[0].arguments == {"answer": "Safe placeholder"}
    assert first.usage and first.response_id
    assert first.request_id is None  # Response IDs are not mislabeled as HTTP request IDs.
    assert "opaque-old-reasoning" not in first.model_dump_json()
    await adapter.complete(request(terminal_only=True))
    old, current = endpoint.calls
    assert "old-private-figure" in json.dumps(old)
    assert "old-private-figure" not in json.dumps(current)
    assert "[REDACTED: NOT_AUTHORIZED]" in json.dumps(current)
    assert all(key not in current for key in (
        "previous_response_id", "previous_interaction_id", "conversation", "response_format",
    ))
    assert "opaque-old-reasoning" not in json.dumps(current)
    assert len(current["messages"] if "messages" in current else current["input"]) == 1
    if provider == "anthropic":
        assert current["system"] == request().system_prompt
        assert current["thinking"] == {"type": "disabled"}
        assert current["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
        assert current["tools"][0]["input_schema"] == request().tools[0].input_schema
    elif provider == "openai":
        assert current["store"] is False and current["parallel_tool_calls"] is False
        assert current["reasoning"] == {"effort": "high"}
        assert current["max_output_tokens"] == 16384
        assert current["tool_choice"] == {"type": "function", "name": "submit_answer"}
        assert current["tools"][0]["strict"] is False
        assert current["tools"][0]["parameters"] == request().tools[0].input_schema
    else:
        assert current["store"] is False and current["automatic_continuation"] is False
        assert current["generation_config"]["tool_choice"] == "any"
        assert current["generation_config"]["thinking_level"] == "low"
        assert current["tools"][0]["parameters"] == request().tools[0].input_schema


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_malformed_native_arguments_fail_before_business_execution(provider):
    adapter, _ = adapter_case(provider, malformed=True)
    with pytest.raises(ProviderFailure, match="MALFORMED_TOOL_ARGUMENTS"):
        await adapter.complete(request())


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_provider_exception_details_are_not_recorded_as_failure_messages(provider):
    adapter, endpoint = adapter_case(provider)
    endpoint.response = RuntimeError("private-provider-body-and-credential")
    with pytest.raises(ProviderFailure) as failure:
        await adapter.complete(request())
    assert str(failure.value) == "PROVIDER_CALL_FAILED"


@pytest.mark.anyio
@pytest.mark.parametrize("model,thinking", [
    ("claude-sonnet-5-5", {"type": "between_tools"}),
    ("claude-opus-5-5", {"type": "adaptive", "display": "omitted"}),
])
async def test_documented_new_claude_models_do_not_receive_unsupported_forced_tools(
    model, thinking,
):
    _, endpoint = adapter_case("anthropic")
    adapter = AnthropicProvider(SimpleNamespace(messages=endpoint), model=model)
    await adapter.complete(request(terminal_only=True))
    assert endpoint.calls[0]["thinking"] == thinking
    assert endpoint.calls[0]["tool_choice"]["type"] == "auto"


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
def test_factory_disables_sdk_retry_loops_and_preserves_optional_workspace_only_in_header(
    monkeypatch, provider,
):
    captured = []

    def client(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("wealthcontext.providers.anthropic.AsyncAnthropic", client)
    monkeypatch.setattr("wealthcontext.providers.openai.AsyncOpenAI", client)
    monkeypatch.setattr("wealthcontext.providers.genai.Client", client)
    adapter = create_provider(provider, api_key="synthetic-key-not-sent",
                              workspace_id="synthetic-workspace", timeout_seconds=7)
    assert "synthetic-key" not in json.dumps(adapter.settings)
    assert "synthetic-workspace" not in json.dumps(adapter.settings)
    # SDK-managed retry loops stay off; the adapter's own bounded loop is recorded.
    assert adapter.settings["sdk_retries"] == 0
    assert adapter.settings["transport_retries"] == TRANSPORT_RETRIES == 2
    assert adapter.delays == TRANSPORT_RETRY_DELAYS
    if provider == "google":
        assert captured[0]["http_options"].retry_options.attempts == 1
        assert captured[0]["http_options"].timeout == 7000
    else:
        assert captured[0]["max_retries"] == 0 and captured[0]["timeout"] == 7
        if provider == "anthropic":
            assert captured[0]["default_headers"] == {
                "anthropic-workspace-id": "synthetic-workspace",
            }


def _tool_item(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = {"anthropic": "content", "openai": "output", "google": "steps"}[provider]
    return payload[key][1]


def _break_response(provider: str, payload: dict[str, Any], defect: str) -> None:
    key = {"anthropic": "content", "openai": "output", "google": "steps"}[provider]
    item = _tool_item(provider, payload)
    if defect == "missing_arguments":
        del item[{"anthropic": "input"}.get(provider, "arguments")]
    elif defect == "missing_name":
        del item["name"]
    elif defect == "missing_text":
        payload[key].append({
            "anthropic": {"type": "text"},
            "openai": {"type": "message", "content": [{"type": "output_text"}]},
            "google": {"type": "model_output", "content": [{"type": "text"}]},
        }[provider])
    elif defect == "none_container":
        payload[key] = None
    elif defect == "non_iterable_container":
        payload[key] = 7
    elif defect == "non_mapping_item":
        payload[key] = ["not-a-block"]
    elif defect == "invalid_id":
        item["call_id" if provider == "openai" else "id"] = "bad id"
    elif defect == "empty_id":
        item["call_id" if provider == "openai" else "id"] = ""
    elif defect == "nan_arguments":
        if provider == "openai":
            item["arguments"] = '{"answer": NaN}'
        else:
            item["input" if provider == "anthropic" else "arguments"] = {"answer": float("nan")}
    elif defect == "infinity_arguments":
        if provider == "openai":
            item["arguments"] = '{"answer": "ok", "nested": {"value": -Infinity}}'
        else:
            item["input" if provider == "anthropic" else "arguments"] = {
                "answer": "ok", "nested": [float("inf")],
            }
    else:
        raise AssertionError(defect)


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
@pytest.mark.parametrize("defect,code", [
    ("missing_arguments", "MALFORMED_PROVIDER_RESPONSE"),
    ("missing_name", "MALFORMED_PROVIDER_RESPONSE"),
    ("missing_text", "MALFORMED_PROVIDER_RESPONSE"),
    ("none_container", "MALFORMED_PROVIDER_RESPONSE"),
    ("non_iterable_container", "MALFORMED_PROVIDER_RESPONSE"),
    ("non_mapping_item", "MALFORMED_PROVIDER_RESPONSE"),
    ("invalid_id", "MALFORMED_PROVIDER_RESPONSE"),
    ("empty_id", "MALFORMED_PROVIDER_RESPONSE"),
    ("nan_arguments", "MALFORMED_TOOL_ARGUMENTS"),
    ("infinity_arguments", "MALFORMED_TOOL_ARGUMENTS"),
])
async def test_malformed_response_structures_are_not_reported_as_call_failures(
    provider, defect, code,
):
    adapter, endpoint = adapter_case(provider)
    _break_response(provider, endpoint.response, defect)
    with pytest.raises(ProviderFailure) as failure:
        await adapter.complete(request())
    assert failure.value.code == code
    assert len(endpoint.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_non_mapping_response_is_malformed_not_a_call_failure(provider):
    adapter, endpoint = adapter_case(provider)
    endpoint.response = ["not", "a", "response"]
    with pytest.raises(ProviderFailure) as failure:
        await adapter.complete(request())
    assert failure.value.code == "MALFORMED_PROVIDER_RESPONSE"


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_sdk_exception_alone_is_a_call_failure(provider):
    adapter, endpoint = adapter_case(provider)
    endpoint.response = KeyError("sdk-internal-key")
    with pytest.raises(ProviderFailure) as failure:
        await adapter.complete(request())
    assert failure.value.code == "PROVIDER_CALL_FAILED"


@pytest.mark.anyio
@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
async def test_anthropic_output_limit_is_a_typed_failure(stop_reason):
    adapter, endpoint = adapter_case("anthropic")
    endpoint.response["stop_reason"] = stop_reason
    with pytest.raises(ProviderFailure) as failure:
        await adapter.complete(request())
    assert failure.value.code == "PROVIDER_OUTPUT_LIMIT"


@pytest.mark.anyio
@pytest.mark.parametrize("provider,status", [
    ("openai", "failed"), ("openai", "incomplete"), ("openai", "cancelled"),
    ("google", "failed"), ("google", "incomplete"), ("google", "cancelled"),
    ("google", "requires_continuation"),
])
async def test_incomplete_provider_status_is_a_typed_failure(provider, status):
    adapter, endpoint = adapter_case(provider)
    endpoint.response["status"] = status
    with pytest.raises(ProviderFailure) as failure:
        await adapter.complete(request())
    assert failure.value.code == "PROVIDER_OUTPUT_INCOMPLETE"


@pytest.mark.parametrize("provider,key_name", [
    ("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY"),
    ("google", "GEMINI_API_KEY"),
])
def test_factory_without_credentials_fails_before_client_construction(
    monkeypatch, provider, key_name,
):
    monkeypatch.delenv(key_name, raising=False)

    def client(**_kwargs):
        raise AssertionError("no SDK client without credentials")

    monkeypatch.setattr("wealthcontext.providers.anthropic.AsyncAnthropic", client)
    monkeypatch.setattr("wealthcontext.providers.openai.AsyncOpenAI", client)
    monkeypatch.setattr("wealthcontext.providers.genai.Client", client)
    with pytest.raises(ProviderFailure) as failure:
        create_provider(provider)
    assert failure.value.code == "MISSING_PROVIDER_CREDENTIALS"
    with pytest.raises(ProviderFailure) as failure:
        create_provider(provider, api_key="")
    assert failure.value.code == "MISSING_PROVIDER_CREDENTIALS"


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
@pytest.mark.parametrize("limit", [True, False, 0, -1, 16385, 8192.0, "8192"])
def test_configuration_rejects_bool_non_int_and_out_of_range_output_limits(provider, limit):
    with pytest.raises(ValueError, match="max_output_tokens"):
        provider_configuration(provider, max_output_tokens=limit)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
@pytest.mark.parametrize("limit", [1, 16384])
def test_configuration_accepts_output_limit_bounds(provider, limit):
    _, settings = provider_configuration(provider, model="synthetic-model",
                                         max_output_tokens=limit)
    assert settings["max_output_tokens"] == limit


@pytest.mark.anyio
@pytest.mark.parametrize("model", [
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-mythos-5-1",
    "claude-fable-5-5", "claude-mythos-6", "claude-opus-5-5-synthetic", "claude-sonnet-5",
    "claude-haiku-4-5",
])
async def test_forced_tool_choice_is_never_combined_with_enabled_thinking(model):
    _, endpoint = adapter_case("anthropic")
    adapter = AnthropicProvider(SimpleNamespace(messages=endpoint), model=model)
    await adapter.complete(request(terminal_only=True))
    sent = endpoint.calls[0]
    assert sent["thinking"] == adapter.settings["thinking"]
    assert sent["tool_choice"]["type"] == adapter.settings["tool_choice"]
    if sent["tool_choice"]["type"] == "any":
        assert sent["thinking"] == {"type": "disabled"}
    else:
        assert sent["thinking"]["type"] != "disabled"


def test_default_openai_model_uses_luna_at_high_effort_with_the_output_maximum(monkeypatch):
    # Owner decision 2026-11-08: live OpenAI runs use gpt-6-luna at effort "high" or lower.
    monkeypatch.delenv("WEALTHCONTEXT_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("WEALTHCONTEXT_OPENAI_REASONING_EFFORT", raising=False)
    model, settings = provider_configuration("openai")
    assert model == "gpt-6-luna"
    assert settings["reasoning_effort"] == "high"
    assert settings["max_output_tokens"] == 16384
    assert provider_configuration("anthropic")[1]["max_output_tokens"] == 8192
    assert provider_configuration("google")[1]["max_output_tokens"] == 8192


@pytest.mark.anyio
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
async def test_openai_reasoning_effort_is_configured_recorded_and_sent(monkeypatch, effort):
    monkeypatch.setenv("WEALTHCONTEXT_OPENAI_REASONING_EFFORT", effort)
    _, settings = provider_configuration("openai")
    assert settings["reasoning_effort"] == effort
    adapter = create_provider("openai", api_key="synthetic-test-key")
    assert isinstance(adapter, OpenAIProvider) and adapter.settings == settings
    _, endpoint = adapter_case("openai")
    adapter.client = SimpleNamespace(responses=endpoint, close=adapter.client.close)
    await adapter.complete(request())
    assert endpoint.calls[0]["reasoning"] == {"effort": effort}
    await adapter.aclose()


# Owner amendment 2026-11-13: "xhigh" moved from the rejected list to the accepted one.
@pytest.mark.parametrize("effort", ["none", "minimal", "max", "XHIGH", "HIGH", " high"])
def test_openai_reasoning_effort_above_xhigh_or_unknown_is_rejected(monkeypatch, effort):
    monkeypatch.setenv("WEALTHCONTEXT_OPENAI_REASONING_EFFORT", effort)
    with pytest.raises(ValueError, match="reasoning effort"):
        provider_configuration("openai")
    with pytest.raises(ValueError, match="reasoning effort"):
        create_provider("openai", api_key="synthetic-test-key")


def test_default_anthropic_model_keeps_adaptive_thinking_with_auto_tools():
    model, settings = provider_configuration("anthropic", model="claude-opus-5-5")
    assert model == "claude-opus-5-5"
    assert settings["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert settings["tool_choice"] == "auto"


# Owner decision 2026-11-05 (ADR 0008): up to two transport retries, only for 429, 5xx
# and connection failures, inside the harness per-call timeout.
# The Anthropic and OpenAI SDKs send through httpx2; google-genai through httpx.
_HTTP_REQUEST = httpx2.Request("POST", "https://provider.invalid/v1/synthetic")


def _status(provider: str, code: int) -> Exception:
    response = httpx2.Response(code, request=_HTTP_REQUEST)
    if provider == "anthropic":
        return anthropic.APIStatusError("synthetic", response=response, body=None)
    if provider == "openai":
        return openai.APIStatusError("synthetic", response=response, body=None)
    return google_errors.APIError(code, {"error": {"code": code, "message": "synthetic"}})


def _connection(provider: str) -> list[Exception]:
    failures: list[Exception]
    if provider == "anthropic":
        failures = [anthropic.APIConnectionError(request=_HTTP_REQUEST),
                    anthropic.APITimeoutError(request=_HTTP_REQUEST)]
        return failures
    if provider == "openai":
        failures = [openai.APIConnectionError(request=_HTTP_REQUEST),
                    openai.APITimeoutError(request=_HTTP_REQUEST)]
        return failures
    return [httpx.ConnectError("synthetic"), httpx.ReadTimeout("synthetic"),
            httpx.RemoteProtocolError("synthetic"), httpx2.ConnectError("synthetic")]


def _retryable(provider: str) -> list[Exception]:
    return [*_connection(provider), *(_status(provider, code) for code in (429, 500, 503, 529))]


def _not_retryable(provider: str) -> list[Exception]:
    return [*(_status(provider, code) for code in (400, 401, 403, 404, 408, 409, 422)),
            RuntimeError("synthetic"), KeyError("synthetic")]


class Sequence:
    """Raises or returns each scripted outcome in turn, recording every SDK call."""

    def __init__(self, outcomes: list[Any]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def retry_case(provider: str, outcomes: list[Any], delays=(0.0, 0.0)):
    adapter, endpoint = adapter_case(provider)
    sequence = Sequence([endpoint.response if item == "ok" else item for item in outcomes])
    if provider == "anthropic":
        adapter.client = SimpleNamespace(messages=sequence)
    elif provider == "openai":
        adapter.client = SimpleNamespace(responses=sequence)
    else:
        adapter.client = SimpleNamespace(aio=SimpleNamespace(interactions=sequence))
    adapter.delays = delays
    return adapter, sequence


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_each_retryable_transport_failure_is_retried_and_counted(provider):
    for failure in _retryable(provider):
        adapter, sequence = retry_case(provider, [failure, "ok"])
        turn = await adapter.complete(request())
        assert turn.calls[0].name == "submit_answer", failure
        assert adapter.last_transport_retries == 1, failure
        assert len(sequence.calls) == 2 and sequence.calls[0] == sequence.calls[1]


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_transport_retries_stop_after_two_and_fail_sanitized(provider):
    first, *_ = _retryable(provider)
    adapter, sequence = retry_case(provider, [first, _status(provider, 503), first, "ok"])
    with pytest.raises(ProviderFailure) as failure:
        await adapter.complete(request())
    assert failure.value.code == "PROVIDER_CALL_FAILED"
    assert str(failure.value) == "PROVIDER_CALL_FAILED" and failure.value.__cause__ is None
    assert len(sequence.calls) == 3 and adapter.last_transport_retries == 2
    adapter, sequence = retry_case(provider, [first, first, "ok"])
    await adapter.complete(request())
    assert len(sequence.calls) == 3 and adapter.last_transport_retries == 2


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_other_failures_are_never_retried(provider):
    for failure in _not_retryable(provider):
        adapter, sequence = retry_case(provider, [failure, "ok"])
        with pytest.raises(ProviderFailure) as raised:
            await adapter.complete(request())
        assert raised.value.code == "PROVIDER_CALL_FAILED", failure
        assert len(sequence.calls) == 1 and adapter.last_transport_retries == 0, failure
    # A received but malformed response is not a transport failure either.
    adapter, sequence = retry_case(provider, [["not", "a", "response"], "ok"])
    with pytest.raises(ProviderFailure, match="MALFORMED_PROVIDER_RESPONSE"):
        await adapter.complete(request())
    assert len(sequence.calls) == 1 and adapter.last_transport_retries == 0
    # A retryable failure followed by a non-retryable one stops at once.
    adapter, sequence = retry_case(provider, [_status(provider, 429), _status(provider, 400),
                                              "ok"])
    with pytest.raises(ProviderFailure, match="PROVIDER_CALL_FAILED"):
        await adapter.complete(request())
    assert len(sequence.calls) == 2 and adapter.last_transport_retries == 1


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_retry_count_is_per_call_and_backoff_uses_the_fixed_delays(provider, monkeypatch):
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("wealthcontext.providers.anyio.sleep", sleep)
    failure = _status(provider, 500)
    adapter, sequence = retry_case(provider, [failure, failure, "ok", "ok"],
                                   delays=TRANSPORT_RETRY_DELAYS)
    await adapter.complete(request())
    assert adapter.last_transport_retries == 2 and slept == [0.5, 1.0]
    await adapter.complete(request())
    assert adapter.last_transport_retries == 0 and slept == [0.5, 1.0]
    assert len(sequence.calls) == 4


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_an_outer_deadline_cancels_retries_and_keeps_the_count(provider):
    first, *_ = _retryable(provider)
    adapter, sequence = retry_case(provider, [first, first, "ok"], delays=(0.0, 30.0))
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.2):
            await adapter.complete(request())
    assert len(sequence.calls) == 2 and adapter.last_transport_retries == 1


@pytest.mark.parametrize("delays", [(), (0.5,), (0.5, 1.0, 2.0), (-1.0, 0.5)])
def test_retry_delays_must_match_the_bound(delays):
    with pytest.raises(ValueError):
        AnthropicProvider(None, retry_delays=delays)
