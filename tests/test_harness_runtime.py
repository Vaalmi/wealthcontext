"""Bounded synthetic provider turns through the real service, verifier and conversation store."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import anyio
import httpx2
import pytest
from pydantic import JsonValue

from wealthcontext.clock import utc_timestamp
from wealthcontext.context_service import ContextService, RequestFailure
from wealthcontext.conversation import ConversationManager
from wealthcontext.database import migrate, write_transaction
from wealthcontext.evaluation import freeze_manifest, load_cases, trial_from_harness
from wealthcontext.evaluation_contracts import EvaluationRun
from wealthcontext.evaluation_store import (
    create_evaluation_run,
    evaluation_summaries,
    record_trial,
)
from wealthcontext.harness import current_results, run_answer
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyDecision
from wealthcontext.provider_contracts import (
    HarnessLimits,
    HarnessRun,
    ProviderFailure,
    ProviderTurn,
    ToolCall,
)
from wealthcontext.providers import AnthropicProvider, GoogleProvider, OpenAIProvider
from wealthcontext.storage import migrate_business, read_snapshot, seed_vertical_slice
from wealthcontext.telemetry import create_provider
from wealthcontext.verifier import TEMPLATE

NOW = datetime(2035, 1, 1, 15, tzinfo=UTC)
PRINCIPAL = Principal("ADV-01", "advisor")
ARGS: dict[str, JsonValue] = {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}
REVISION = "sha256:" + "a" * 64
ROOT = Path(__file__).parents[1]
ACCEPTANCE = ROOT / "acceptance"


class TickClock:
    def __init__(self):
        self.instant = NOW

    def now(self):
        self.instant += timedelta(microseconds=1)
        return self.instant


class TestPolicy(PolicyClient):
    __test__ = False

    def __init__(self):
        super().__init__("http://unused.invalid", REVISION)

    async def decide(self, principal, action, resources, facts, revision, request_business_date):
        covered = {fact["object"] for fact in facts
                   if fact["kind"] == "advisor_covers" and fact["subject"] == principal.id}
        allow = not resources or set(resources) <= covered
        return PolicyDecision(allow=allow, allowed_ids=resources if allow else [],
                              projection="full",
                              reason="CURRENT_GRANT" if allow else "NO_CURRENT_GRANT",
                              policy_revision=REVISION, entitlement_revision=revision)


class Scripted:
    provider = "synthetic"
    model = "synthetic-script-fixture"
    settings: dict[str, JsonValue] = {"transport": "synthetic_test"}

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request.model_copy(deep=True))
        if not self.steps:
            raise ProviderFailure("SYNTHETIC_STEPS_EXHAUSTED")
        step = self.steps.pop(0)
        result = step(request)
        if hasattr(result, "__await__"):
            result = await result
        return result


def turn(*calls, text=None):
    return ProviderTurn(calls=list(calls), model=Scripted.model, text=text)


def fetch(call_id="fetch"):
    return lambda _: turn(ToolCall(id=call_id, name="get_household_snapshot", arguments=ARGS))


def candidate(request, *, invalid=False):
    prepared = {"data": {"messages": request.messages}}
    financial = next(value for value in current_results(prepared).values()
                     if value.get("data", {}).get("assets_usd") == "3000.00")
    return {
        "answer_markdown": "Household assets are 3000.00." if invalid else TEMPLATE,
        "claims": [{"id": "c1", "result_id": financial["result_id"],
                    "entity": {"type": "household", "id": "WC-HH-0001"}, "field": "assets_usd",
                    "household_id": "WC-HH-0001", "valuation_date": "2026-09-30", "unit": "USD",
                    "format": "value_only"}],
        "predicates": [], "caveats": [], "refusal": None,
    }


def submit(call_id="submit", *, invalid=False):
    return lambda request: turn(ToolCall(id=call_id, name="submit_answer",
                                         arguments=candidate(request, invalid=invalid)),
                                text="Unverified private-provider prose 999999.")


@pytest.fixture
def runtime(tmp_path):
    database = tmp_path / "harness.sqlite"
    migrate(database)
    migrate_business(database)
    seed_vertical_slice(database, NOW)
    provider = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, TestPolicy(), provider.get_tracer("runtime"))
    yield service, ConversationManager(service), TickClock()
    provider.shutdown()


def revoke(service):
    with write_transaction(service.database) as connection:
        revision = connection.execute(
            "SELECT MAX(revision)+1 FROM entitlement_revisions",
        ).fetchone()[0]
        connection.execute("INSERT INTO entitlement_revisions VALUES (?,?)",
                           (revision, utc_timestamp(NOW)))


def final_data(run: HarnessRun) -> dict[str, Any]:
    assert run.final_result is not None
    data = run.final_result["data"]
    assert isinstance(data, dict)
    return data


@pytest.mark.anyio
async def test_valid_native_terminal_records_rendered_answer_and_exact_run_metadata(runtime):
    service, manager, clock = runtime
    adapter = Scripted([fetch(), submit()])
    result = await run_answer(service, manager, PRINCIPAL, clock, "Show household assets.", adapter)
    assert result.first_pass_valid and result.final_valid, result
    assert not result.fallback and result.retries == 0 and result.data_tool_calls == 1
    assert final_data(result)["markdown"].startswith(
        "Household assets in included accounts: USD 3000.00.",
    )
    assert "> Source:" in final_data(result)["markdown"]
    assert "999999" not in json.dumps(result.final_result)
    assert result.mode == "synthetic_test" and result.model == Scripted.model
    assert len(result.prompt_sha256) == len(result.schema_sha256) == 64
    assert result.completed_at > result.started_at
    assert result.authorization_started_at is not None
    assert result.authorization_started_at.year == 2035
    assert result.started_at != result.authorization_started_at
    assert adapter.requests[1].authorized_result_ids
    assert len(result.turns) == 2 and len(result.terminal_attempts) == 1
    with read_snapshot(service.database) as connection:
        rows = connection.execute(
            "SELECT kind,payload FROM conversation_messages WHERE conversation_id=?",
            (result.conversation_id,),
        ).fetchall()
        assert [row["kind"] for row in rows] == [
            "user", "assistant_call", "tool_result", "assistant_call", "tool_result",
            "assistant_answer",
        ]
        assert json.loads(rows[3]["payload"])["context_result_ids"]


@pytest.mark.anyio
async def test_verification_gets_one_structured_retry_from_fresh_context(runtime):
    service, manager, clock = runtime

    def corrected(request):
        assert request.terminal_only
        assert [tool.name for tool in request.tools] == ["submit_answer"]
        assert "diagnostics" in json.dumps(request.messages)
        assert "Unverified private-provider prose" not in json.dumps(request.messages)
        return submit("corrected")(request)

    adapter = Scripted([fetch(), submit("invalid", invalid=True), corrected])
    result = await run_answer(service, manager, PRINCIPAL, clock, "Show household assets.", adapter)
    assert not result.first_pass_valid and result.final_valid, result
    assert result.retries == 1 and not result.fallback
    assert len(result.terminal_attempts) == 2
    rejected = cast(dict[str, Any], result.terminal_attempts[0]["result"])
    assert rejected["error"]["details"]["diagnostics"]


@pytest.mark.anyio
async def test_expected_prompt_guard_precedes_inference_and_conversation_creation(runtime):
    service, manager, clock = runtime
    adapter = Scripted([])
    with pytest.raises(ValueError, match="prompt changed"):
        await run_answer(service, manager, PRINCIPAL, clock, "Show facts.", adapter,
                         expected_prompt_sha256="b" * 64)
    assert not adapter.requests
    with read_snapshot(service.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


@pytest.mark.anyio
async def test_second_invalid_answer_uses_identical_platform_verification_for_fallback(runtime):
    service, manager, clock = runtime
    adapter = Scripted([fetch(), submit("bad_1", invalid=True), submit("bad_2", invalid=True)])
    result = await run_answer(service, manager, PRINCIPAL, clock, "Show household assets.", adapter)
    assert result.fallback and result.final_valid and not result.first_pass_valid, result
    assert result.retries == 1 and len(adapter.requests) == 3
    assert len(result.terminal_attempts) == 3
    assert result.terminal_attempts[-1]["kind"] == "fallback"
    assert final_data(result)["mode"] == "verified"
    assert "3000.00" in final_data(result)["markdown"]


@pytest.mark.anyio
async def test_eight_data_calls_force_terminal_only_and_extra_group_never_executes(runtime):
    service, manager, clock = runtime

    def finish(request):
        assert request.terminal_only and len(request.tools) == 1
        return submit()(request)

    adapter = Scripted([*(fetch(f"fetch_{n}") for n in range(8)), finish])
    result = await run_answer(service, manager, PRINCIPAL, clock,
                              "Retrieve received facts.", adapter)
    assert result.data_tool_calls == 8 and result.final_valid, result
    assert len(adapter.requests) == 9
    malicious = Scripted([lambda _: turn(*(ToolCall(
        id=f"many_{n}", name="get_household_snapshot", arguments=ARGS,
    ) for n in range(9)))])
    rejected = await run_answer(service, manager, PRINCIPAL, clock, "Retrieve facts.", malicious)
    assert rejected.failure == "DATA_TOOL_BUDGET_EXCEEDED"
    assert rejected.data_tool_calls == 0 and rejected.fallback and rejected.final_valid


@pytest.mark.anyio
@pytest.mark.parametrize("response,failure", [
    (turn(text="Unverified assets 999999."), "NATIVE_TOOL_CALL_REQUIRED"),
    (turn(ToolCall(id="hidden", name="unknown_business_tool", arguments={})),
     "UNEXPECTED_DATA_TOOL"),
    (turn(ToolCall(id="terminal", name="submit_answer", arguments={}),
          ToolCall(id="data", name="get_household_snapshot", arguments=ARGS)),
     "MIXED_TERMINAL_TOOL_CALL"),
])
async def test_unverified_text_unknown_tools_and_mixed_terminal_are_never_dispatched(
    runtime, response, failure,
):
    service, manager, clock = runtime
    result = await run_answer(service, manager, PRINCIPAL, clock, "Show facts.",
                              Scripted([lambda _: response]))
    assert result.failure == failure and result.data_tool_calls == 0
    assert result.final_valid and result.fallback
    assert "999999" not in json.dumps(result.final_result)


@pytest.mark.anyio
async def test_call_id_reused_from_an_earlier_turn_is_classified_and_falls_back(runtime):
    """ARGON-P5-HARNESS-R1-001: conversation-wide call ids are checked before dispatch."""
    service, manager, clock = runtime
    first = await run_answer(service, manager, PRINCIPAL, clock, "Show household assets.",
                             Scripted([fetch("call_1"), submit("call_2")]))
    assert first.final_valid and not first.fallback, first
    adapter = Scripted([fetch("call_1")])
    second = await run_answer(service, manager, PRINCIPAL, clock, "Show household assets again.",
                              adapter, conversation_id=first.conversation_id)
    assert second.failure == "DUPLICATE_TOOL_CALL_ID", second.failure
    assert second.turns[-1].error == "DUPLICATE_TOOL_CALL_ID"
    assert second.data_tool_calls == 0 and len(adapter.requests) == 1
    assert second.fallback and second.final_valid
    assert final_data(second)["mode"] == "verified"
    assert "3000.00" in final_data(second)["markdown"]


@pytest.mark.anyio
async def test_provider_and_total_timeouts_are_bounded_without_api_retries(runtime):
    service, manager, clock = runtime

    async def slow(_request):
        await anyio.sleep(1)

    provider_timeout = await run_answer(service, manager, PRINCIPAL, clock, "Show facts.",
                                        Scripted([slow]), limits=HarnessLimits(
                                            per_call_timeout_seconds=0.05, total_timeout_seconds=1,
                                        ))
    assert provider_timeout.failure == "PROVIDER_TIMEOUT"
    assert provider_timeout.fallback and provider_timeout.final_valid
    total_timeout = await run_answer(service, manager, PRINCIPAL, clock, "Show facts.",
                                     Scripted([slow]), limits=HarnessLimits(
                                         per_call_timeout_seconds=1, total_timeout_seconds=0.05,
                                     ))
    assert total_timeout.failure == "TOTAL_OR_TOOL_TIMEOUT"
    assert not total_timeout.final_valid and total_timeout.final_result is None


@pytest.mark.anyio
async def test_revocation_before_retry_removes_old_results_and_rejected_private_arguments(runtime):
    service, _, clock = runtime

    class RevokingManager(ConversationManager):
        async def execute_tool_group(self, principal, now, conversation_id, calls, **kwargs):
            results = await super().execute_tool_group(
                principal, now, conversation_id, calls, **kwargs,
            )
            if calls[0]["name"] == "submit_answer" and results[0]["status"] == "ERROR":
                revoke(self.service)
            return results

    def retry(request):
        content = json.dumps(request.messages)
        assert "3000.00" not in content and "999999" not in content
        assert "[REDACTED: NOT_AUTHORIZED]" in content
        assert not request.authorized_result_ids
        return turn(ToolCall(id="safe_retry", name="submit_answer", arguments={
            "answer_markdown": "Verified data is unavailable.", "claims": [], "predicates": [],
            "caveats": [], "refusal": None,
        }))

    adapter = Scripted([fetch(), submit("private_bad", invalid=True), retry])
    result = await run_answer(service, RevokingManager(service), PRINCIPAL, clock,
                              "Show household assets.", adapter)
    assert result.retries == 1 and result.final_valid, result
    assert "3000.00" not in json.dumps(result.final_result)


@pytest.mark.anyio
async def test_revocation_during_provider_call_cannot_release_a_cached_terminal(runtime):
    service, manager, clock = runtime

    def terminal_after_revocation(request):
        response = submit()(request)
        revoke(service)
        return response

    result = await run_answer(service, manager, PRINCIPAL, clock, "Show assets.",
                              Scripted([fetch(), terminal_after_revocation]))
    assert result.failure == "NOT_AUTHORIZED"
    assert result.fallback and result.final_valid, result
    assert "3000.00" not in json.dumps(result.final_result)


@pytest.mark.anyio
async def test_revocation_before_final_render_uses_current_fallback_not_verified_old_answer(
    runtime,
):
    service, _, clock = runtime

    class RevokingRenderManager(ConversationManager):
        revoked = False

        def append_answer(self, principal, now, conversation_id, result_id, **kwargs):
            super().append_answer(principal, now, conversation_id, result_id, **kwargs)
            if not self.revoked:
                self.revoked = True
                revoke(self.service)

    result = await run_answer(service, RevokingRenderManager(service), PRINCIPAL, clock,
                              "Show assets.", Scripted([fetch(), submit()]))
    assert result.first_pass_valid and result.fallback and result.final_valid, result
    assert "3000.00" not in json.dumps(result.final_result)


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_native_sdk_retry_payload_contains_only_fresh_post_revocation_context(
    runtime, provider,
):
    service, _, clock = runtime

    class RevokingManager(ConversationManager):
        async def execute_tool_group(self, principal, now, conversation_id, calls, **kwargs):
            results = await super().execute_tool_group(
                principal, now, conversation_id, calls, **kwargs,
            )
            if calls[0]["name"] == "submit_answer" and results[0]["status"] == "ERROR":
                revoke(self.service)
            return results

    class NativeEndpoint:
        calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if provider == "anthropic":
                text = kwargs["messages"][0]["content"]
            elif provider == "openai":
                text = kwargs["input"][0]["content"]
            else:
                text = kwargs["input"][0]["content"][0]["text"]
            messages = json.loads(text)["authorized_conversation"]
            if len(self.calls) == 1:
                name, args = "get_household_snapshot", ARGS
            elif len(self.calls) == 2:
                name, args = "submit_answer", candidate(SimpleNamespace(messages=messages),
                                                        invalid=True)
            else:
                assert "3000.00" not in text
                assert "[REDACTED: NOT_AUTHORIZED]" in text
                assert all(key not in kwargs for key in (
                    "previous_response_id", "previous_interaction_id", "conversation",
                ))
                name, args = "submit_answer", {
                    "answer_markdown": "Verified data is unavailable.", "claims": [],
                    "predicates": [], "caveats": [], "refusal": None,
                }
            call_id = f"native_{len(self.calls)}"
            identity = {"id": f"response_{len(self.calls)}", "model": kwargs["model"]}
            if provider == "anthropic":
                return {**identity, "stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": call_id, "name": name, "input": args},
                ]}
            if provider == "openai":
                return {**identity, "status": "completed", "output": [
                    {"type": "function_call", "call_id": call_id, "name": name,
                     "arguments": json.dumps(args)},
                ]}
            return {**identity, "status": "requires_action", "steps": [
                {"type": "function_call", "id": call_id, "name": name, "arguments": args},
            ]}

    endpoint = NativeEndpoint()
    if provider == "anthropic":
        adapter = AnthropicProvider(SimpleNamespace(messages=endpoint))
    elif provider == "openai":
        adapter = OpenAIProvider(SimpleNamespace(responses=endpoint))
    else:
        adapter = GoogleProvider(SimpleNamespace(aio=SimpleNamespace(interactions=endpoint)))
    result = await run_answer(service, RevokingManager(service), PRINCIPAL, clock,
                              "Show household assets.", adapter)
    assert result.final_valid and result.retries == 1 and not result.fallback, result
    assert len(endpoint.calls) == 3
    assert "3000.00" not in json.dumps(result.final_result)


class Retrying(Scripted):
    """Reports a transport-retry count per call, as the real adapters do."""

    def __init__(self, steps, retries):
        super().__init__(steps)
        self.counts = list(retries)
        self.last_transport_retries = 0

    async def complete(self, request):
        self.last_transport_retries = self.counts.pop(0)
        return await super().complete(request)


@pytest.mark.anyio
async def test_transport_retries_are_recorded_per_turn_and_per_run(runtime):
    service, manager, clock = runtime
    result = await run_answer(service, manager, PRINCIPAL, clock, "Show household assets.",
                              Retrying([fetch(), submit()], [1, 2]))
    assert result.final_valid and result.retries == 0
    assert [turn.transport_retries for turn in result.turns] == [1, 2]
    assert result.transport_retries == 3

    def failed(_request):
        raise ProviderFailure("PROVIDER_CALL_FAILED")

    failure = await run_answer(service, manager, PRINCIPAL, clock, "Show facts.",
                               Retrying([fetch(), failed], [0, 2]))
    assert failure.failure == "PROVIDER_CALL_FAILED" and failure.fallback
    assert [turn.transport_retries for turn in failure.turns] == [0, 2]
    assert failure.transport_retries == 2
    # Adapters without the attribute (scripted fixtures, replay) record zero.
    plain = await run_answer(service, manager, PRINCIPAL, clock, "Show facts.",
                             Scripted([fetch(), submit()]))
    assert plain.transport_retries == 0 and all(not t.transport_retries for t in plain.turns)


@pytest.mark.anyio
async def test_real_adapter_retries_stay_inside_the_per_call_timeout(runtime):
    service, manager, clock = runtime
    request = httpx2.Request("POST", "https://provider.invalid/v1/messages")

    class Unreachable:
        calls = 0

        async def create(self, **_kwargs):
            Unreachable.calls += 1
            raise anthropic.APIConnectionError(request=request)

    adapter = AnthropicProvider(SimpleNamespace(messages=Unreachable()), "claude-sonnet-5",
                                retry_delays=(0.0, 30.0))
    result = await run_answer(service, manager, PRINCIPAL, clock, "Show facts.", adapter,
                              limits=HarnessLimits(per_call_timeout_seconds=0.2,
                                                   total_timeout_seconds=5))
    assert result.failure == "PROVIDER_TIMEOUT" and result.fallback
    assert Unreachable.calls == 2
    assert result.turns[0].error == "PROVIDER_TIMEOUT"
    assert result.turns[0].transport_retries == 1 and result.transport_retries == 1


class AnthropicScripted(Scripted):
    provider = "anthropic"


@pytest.mark.anyio
@pytest.mark.parametrize("cut", ["per_call_timeout", "request_failure", "total_timeout"])
async def test_a_retry_cut_short_in_verification_still_counts_through_store_and_summary(
    runtime, cut,
):
    """SOL-C-R1-RETRY-001: the second native submission is an answer retry even when its
    verification never returns, and the trial, store and summary all count it."""
    service, manager, clock = runtime
    original = manager.execute_tool_group

    async def execute_tool_group(principal, now, conversation_id, calls, **kwargs):
        if [call["call_id"] for call in calls] == ["corrected"]:
            if cut == "request_failure":
                raise RequestFailure("INTERNAL", "Synthetic verification failure.")
            await anyio.sleep(30)
        return await original(principal, now, conversation_id, calls, **kwargs)

    manager.execute_tool_group = execute_tool_group  # type: ignore[method-assign]
    limits = (HarnessLimits(per_call_timeout_seconds=30, total_timeout_seconds=3)
              if cut == "total_timeout" else HarnessLimits(per_call_timeout_seconds=1))
    adapter = AnthropicScripted([fetch(), submit("invalid", invalid=True), submit("corrected")])
    result = await run_answer(service, manager, PRINCIPAL, clock, "Show household assets.",
                              adapter, limits=limits)
    assert result.retries == 1 and not result.first_pass_valid
    # A per-call cut-off or request failure leaves time for the verified fallback.
    assert result.fallback == (cut != "total_timeout")
    assert result.failure == {"per_call_timeout": "TERMINAL_TIMEOUT",
                              "request_failure": "INTERNAL",
                              "total_timeout": "TOTAL_OR_TOOL_TIMEOUT"}[cut]
    provider = [item for item in result.terminal_attempts if item["kind"] == "provider"]
    assert len(provider) == 2
    # The cut-short submission keeps its candidate but has no governed result.
    assert provider[1]["result"] is None and provider[1]["candidate"]
    assert result.turns[-1].envelopes == [] and len(result.turns) == 3

    case = next(item for item in load_cases(ACCEPTANCE) if item.id == "F01")
    evaluation = EvaluationRun.model_validate({
        "id": "eval_retry", "provider": "anthropic", "model": adapter.model,
        "mode": "synthetic_test", "status": "running", "recorded_at": result.started_at,
        "case_set_sha256": freeze_manifest(ACCEPTANCE)["case_set_sha256"],
        "prompt_sha256": result.prompt_sha256, "schema_sha256": result.schema_sha256,
        "settings": result.settings,
    })
    recorded = trial_from_harness(evaluation, case, 1, result)
    assert recorded.retries == 1 == recorded.transcript["retries"]
    assert [item.valid for item in recorded.attempts] == [False, False]
    create_evaluation_run(service.database, evaluation, repository=ROOT,
                          acceptance_directory=ACCEPTANCE)
    record_trial(service.database, recorded)
    with read_snapshot(service.database) as connection:
        (report,) = evaluation_summaries(connection, result.completed_at + timedelta(days=1))
    assert report.retry_count == 1
