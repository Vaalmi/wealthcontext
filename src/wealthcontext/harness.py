"""Bounded verified tool runtime; the legacy scripted demo is never a live evaluation."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import anyio
from pydantic import JsonValue

from wealthcontext.clock import Clock, SystemClock
from wealthcontext.context_service import ContextService, RequestFailure
from wealthcontext.contracts import Answer
from wealthcontext.conversation import ConversationManager
from wealthcontext.identity import Principal
from wealthcontext.mcp_server import ARGUMENT_MODELS
from wealthcontext.provider_contracts import (
    HarnessLimits,
    HarnessRun,
    Provider,
    ProviderFailure,
    ProviderRequest,
    RunMode,
    ToolCall,
    ToolSpec,
    TurnRecord,
)
from wealthcontext.storage import identifier
from wealthcontext.verifier import TEMPLATE, deterministic_fallback

_ROOT = Path(__file__).resolve().parents[2]
ANSWER_SCHEMA_SHA256 = "eec9fdb90ade138230bbf19b01fc6e82b9916d9fc60cb9dfffab980baf3ad498"
_DESCRIPTIONS = {
    "get_household_snapshot": "Household assets, components, scope and reconciliation at cutoffs.",
    "get_portfolio_exposure": "Ranked issuer/security exposure and its valuation denominator.",
    "get_concentrated_positions": "Issuer breaches and every indeterminate entitled household.",
    "get_data_provenance": "Resolve an opaque source handle under current authorization.",
    "search_households": "Find currently entitled household IDs by a bounded name query.",
    "get_metric_definition": "Read the governed formula, inputs, rounding and limitations.",
    "get_data_quality_report": "Inspect received-input quality under the caller's PII projection.",
}


def tool_specs() -> list[ToolSpec]:
    tools = [ToolSpec(name=name, description=_DESCRIPTIONS[name],
                      input_schema=model.model_json_schema())
             for name, model in ARGUMENT_MODELS.items()]
    tools.append(ToolSpec(
        name="submit_answer", description="Terminal: submit the frozen Answer for verification.",
        input_schema=Answer.model_json_schema(),
    ))
    return tools


def current_results(prepared: dict) -> dict[str, dict]:
    """Only current authorized tool envelopes are eligible for deterministic fallback."""
    results = {}
    calls = {}
    for message in prepared["data"]["messages"]:
        if message.get("role") == "assistant":
            calls.update({call["id"]: call["name"] for call in message.get("tool_calls", [])})
        if message.get("role") != "tool":
            continue
        if calls.get(message.get("tool_call_id")) == "submit_answer":
            continue  # Verification failures are diagnostics, not business fallback evidence.
        try:
            envelope = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            continue  # Authorization tombstones intentionally contain no JSON result.
        if isinstance(envelope, dict) and isinstance(envelope.get("result_id"), str):
            results[envelope["result_id"]] = envelope
    return results


async def run_answer(
    service: ContextService, manager: ConversationManager, principal: Principal, clock: Clock,
    question: str, adapter: Provider, *, conversation_id: str | None = None,
    mode: RunMode = "synthetic_test", limits: HarnessLimits | None = None,
    evidence_clock: Clock | None = None,
    expected_prompt_sha256: str | None = None,
) -> HarnessRun:
    """Bounded native-tool loop; every provider sample and final display reauthorize anew."""
    limits = limits or HarnessLimits()
    prompt = (_ROOT / "prompts/harness_system.md").read_text()
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    if expected_prompt_sha256 is not None and prompt_hash != expected_prompt_sha256:
        raise ValueError("Harness prompt changed before inference")
    schema_hash = hashlib.sha256((_ROOT / "schemas/v1/answer.json").read_bytes()).hexdigest()
    if schema_hash != ANSWER_SCHEMA_SHA256:
        raise ValueError("Answer contract changed after the harness prompt was frozen")
    evidence_clock = evidence_clock or SystemClock()
    started = evidence_clock.now()
    authorization_started = clock.now()
    conversation_id = conversation_id or manager.start(principal, authorization_started)
    run = HarnessRun(
        run_id=identifier("run"), conversation_id=conversation_id, provider=adapter.provider,
        principal_id=principal.id, principal_role=principal.role,
        model=adapter.model, settings=adapter.settings, mode=mode, started_at=started,
        completed_at=started, prompt_sha256=prompt_hash,
        authorization_started_at=authorization_started,
        schema_sha256=schema_hash,
    )
    specifications = tool_specs()
    data_names = {tool.name for tool in specifications if tool.name != "submit_answer"}
    seen_calls: set[str] = set()

    async def prepare() -> dict:
        with anyio.fail_after(limits.per_call_timeout_seconds):
            prepared = await manager.prepare_turn(principal, clock.now(), conversation_id)
        if prepared["status"] == "ERROR":
            run.final_result = prepared
            raise ProviderFailure(prepared["error"]["code"])
        if "authorized_result_ids" not in prepared["data"]:
            raise ProviderFailure("INVALID_AUTHORIZED_CONTEXT")
        return prepared

    async def execute(calls: list[ToolCall], prepared: dict) -> list[dict]:
        with anyio.fail_after(limits.per_call_timeout_seconds):
            return await manager.execute_tool_group(
                principal, clock.now(), conversation_id,
                [{"call_id": call.id, "name": call.name, "arguments": call.arguments}
                 for call in calls],
                context_result_ids=prepared["data"]["authorized_result_ids"],
            )

    async def finish(result: dict, prepared: dict) -> bool:
        manager.append_answer(principal, clock.now(), conversation_id, result["result_id"],
                              context_result_ids=prepared["data"]["authorized_result_ids"])
        with anyio.fail_after(limits.per_call_timeout_seconds):
            rendered = await manager.render_history(principal, clock.now(), conversation_id)
        run.rendered_context = rendered
        if rendered["status"] == "ERROR":
            run.final_result = rendered
            return False
        answers = rendered["data"]["answers"]
        if not answers or answers[-1]["redacted"]:
            run.final_result = None
            return False
        # The renderer supplies the freshly resolved answer; never return stale provider prose.
        run.final_result = {**result, "data": answers[-1]["data"]}
        run.final_valid = True
        return True

    async def fallback() -> None:
        prepared = await prepare()
        candidate = deterministic_fallback(current_results(prepared)).model_dump(mode="json")
        run.fallback = True
        call = ToolCall(id=identifier("fallback"), name="submit_answer", arguments=candidate)
        result = (await execute([call], prepared))[0]
        run.terminal_attempts.append({"kind": "fallback", "candidate": candidate, "result": result})
        if result["status"] == "COMPLETE":
            if not await finish(result, prepared):
                run.failure = "FALLBACK_REVOKED_OR_UNAVAILABLE"
        else:
            run.final_result = result
            run.failure = "FALLBACK_VERIFICATION_FAILED"

    async def loop() -> None:
        manager.add_user(principal, clock.now(), conversation_id, question)
        terminal_attempts = 0
        while True:
            prepared = await prepare()
            # Call ids are unique across the whole conversation, not just this run;
            # prepared context keeps every earlier call id, including redacted ones.
            seen_calls.update(call["id"] for message in prepared["data"]["messages"]
                              if message.get("role") == "assistant"
                              for call in message.get("tool_calls", []))
            terminal_only =terminal_attempts > 0 or run.data_tool_calls >= limits.max_data_calls
            request = ProviderRequest(
                system_prompt=prompt, messages=prepared["data"]["messages"],
                tools=[tool for tool in specifications
                       if not terminal_only or tool.name == "submit_answer"],
                terminal_only=terminal_only,
                authorized_result_ids=prepared["data"]["authorized_result_ids"],
            )
            turn = TurnRecord(request=request)
            run.turns.append(turn)
            try:
                try:
                    with anyio.fail_after(limits.per_call_timeout_seconds):
                        response = await adapter.complete(request)
                finally:
                    # Recorded on success, failure and timeout alike (PLAN section 13).
                    turn.transport_retries = getattr(adapter, "last_transport_retries", 0)
                    run.transport_retries += turn.transport_retries
                turn.response = response
                run.model = response.model
            except TimeoutError:
                turn.error = "PROVIDER_TIMEOUT"
                run.failure = turn.error
                break
            except ProviderFailure as exc:
                turn.error = exc.code
                run.failure = exc.code
                break
            calls = response.calls
            if not calls:
                run.failure = turn.error = "NATIVE_TOOL_CALL_REQUIRED"
                break
            if (len({call.id for call in calls}) != len(calls)
                    or any(call.id in seen_calls for call in calls)):
                run.failure = turn.error = "DUPLICATE_TOOL_CALL_ID"
                break
            seen_calls.update(call.id for call in calls)
            terminal = [call for call in calls if call.name == "submit_answer"]
            if terminal:
                if len(calls) != 1:
                    run.failure = turn.error = "MIXED_TERMINAL_TOOL_CALL"
                    break
                terminal_attempts += 1
                if terminal_attempts == 2:
                    run.retries = 1
                # Recorded before verification runs, so a submission cut short by a
                # timeout or request failure still counts as an attempt (and the second
                # as the answer retry). Its result stays None: no governed result exists.
                attempt: dict[str, JsonValue] = {
                    "kind": "provider", "candidate": terminal[0].arguments, "result": None,
                }
                run.terminal_attempts.append(attempt)
                try:
                    result = (await execute(calls, prepared))[0]
                except RequestFailure as exc:
                    run.failure = turn.error = exc.error.code
                    break
                except TimeoutError:
                    run.failure = turn.error = "TERMINAL_TIMEOUT"
                    break
                turn.envelopes = [result]
                valid = result["status"] == "COMPLETE"
                attempt["result"] = result
                if terminal_attempts == 1:
                    run.first_pass_valid = valid
                if valid:
                    if await finish(result, prepared):
                        return
                    run.failure = "ANSWER_REVOKED_OR_UNAVAILABLE"
                    break
                if result["error"]["code"] != "INVALID_ARGUMENT":
                    run.failure = result["error"]["code"]
                    break
                if terminal_attempts == 2:
                    run.failure = "VERIFICATION_FAILED"
                    break
                # The paired terminal error carries structured diagnostics. The next
                # provider turn must prepare current context again before the correction.
                continue
            if terminal_only or any(call.name not in data_names for call in calls):
                run.failure = turn.error = "UNEXPECTED_DATA_TOOL"
                break
            if run.data_tool_calls + len(calls) > limits.max_data_calls:
                run.failure = turn.error = "DATA_TOOL_BUDGET_EXCEEDED"
                break
            run.data_tool_calls += len(calls)
            try:
                turn.envelopes = await execute(calls, prepared)
            except RequestFailure as exc:
                run.failure = turn.error = exc.error.code
                break
            except TimeoutError:
                run.failure = turn.error = "DATA_TOOL_TIMEOUT"
                break
        await fallback()

    with service.tracer.start_as_current_span("agent", attributes={
        "gen_ai.operation.name": "invoke_agent", "gen_ai.provider.name": adapter.provider,
        "gen_ai.request.model": adapter.model, "wealthcontext.mode": mode,
    }):
        try:
            with anyio.fail_after(limits.total_timeout_seconds):
                await loop()
        except TimeoutError:
            run.failure = "TOTAL_OR_TOOL_TIMEOUT"
            run.final_valid = False
            run.final_result = None
        except ProviderFailure as exc:
            run.failure = exc.code
            run.final_valid = False
        except Exception:
            run.failure = "HARNESS_FAILED"
            run.final_valid = False
            run.final_result = None
        finally:
            run.completed_at = evidence_clock.now()
            run.authorization_completed_at = clock.now()
    return run


async def one_claim_demo(service: ContextService, principal: Principal, now: datetime) -> dict:
    with service.tracer.start_as_current_span("agent", attributes={
        "gen_ai.operation.name": "invoke_agent", "wealthcontext.mode": "scripted_p1",
    }):
        manager = ConversationManager(service)
        conversation = manager.start(principal, now)
        manager.add_user(principal, now, conversation,
                         "Show household assets in included accounts.")
        prepared = await manager.prepare_turn(principal, now, conversation)
        if prepared["status"] == "ERROR":
            return prepared
        result = await manager.execute_tool(
            principal, now, conversation, "scripted_snapshot", "get_household_snapshot",
            {"household_id": "WC-HH-0001", "as_of": "2026-09-30"},
        )
        if result["status"] != "COMPLETE":
            return result
        candidate = {
            "answer_markdown": TEMPLATE,
            "claims": [{"id": "c1", "result_id": result["result_id"],
                        "entity": {"type": "household", "id": "WC-HH-0001"},
                        "field": "assets_usd", "household_id": "WC-HH-0001",
                        "valuation_date": "2026-09-30", "unit": "USD", "format": "value_only"}],
            "predicates": [], "caveats": [], "refusal": None,
        }
        answer = await service.execute(principal, now, "submit_answer", candidate, conversation)
        if answer["status"] == "COMPLETE":
            manager.append_answer(principal, now, conversation, answer["result_id"])
        return answer
