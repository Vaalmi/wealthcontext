"""Authenticated browser adapter over the bounded harness and current history view."""

import inspect
import sqlite3
from collections.abc import Callable
from datetime import UTC

import anyio
from pydantic import JsonValue

from wealthcontext.chat_contracts import (
    ChatAnswer,
    ChatArguments,
    ChatRunResponse,
)
from wealthcontext.clock import Clock
from wealthcontext.context_service import ContextService, Operation
from wealthcontext.contracts import Envelope, Error
from wealthcontext.conversation import ConversationManager
from wealthcontext.harness import run_answer
from wealthcontext.identity import Principal
from wealthcontext.provider_contracts import Provider, ProviderFailure, ProviderName
from wealthcontext.storage import identifier

ProviderFactory = Callable[[ProviderName], Provider]
# The closed set of sanitized codes a live chat run emits. Anything else,
# including replay-only codes, is reported as HARNESS_FAILED.
_FAILURES = frozenset({
    # Provider adapters.
    "MISSING_PROVIDER_CREDENTIALS", "PROVIDER_CALL_FAILED", "MALFORMED_PROVIDER_RESPONSE",
    "MALFORMED_TOOL_ARGUMENTS", "PROVIDER_OUTPUT_LIMIT", "PROVIDER_OUTPUT_INCOMPLETE",
    # Harness classifications.
    "PROVIDER_TIMEOUT", "NATIVE_TOOL_CALL_REQUIRED", "DUPLICATE_TOOL_CALL_ID",
    "MIXED_TERMINAL_TOOL_CALL", "TERMINAL_TIMEOUT", "ANSWER_REVOKED_OR_UNAVAILABLE",
    "VERIFICATION_FAILED", "UNEXPECTED_DATA_TOOL", "DATA_TOOL_BUDGET_EXCEEDED",
    "DATA_TOOL_TIMEOUT", "TOTAL_OR_TOOL_TIMEOUT", "HARNESS_FAILED",
    "FALLBACK_REVOKED_OR_UNAVAILABLE", "FALLBACK_VERIFICATION_FAILED",
    "INVALID_AUTHORIZED_CONTEXT",
    # Envelope codes that reach run.failure from context, tool groups or a terminal.
    "NOT_AUTHORIZED", "POLICY_UNAVAILABLE", "INTERNAL",
})


def _failure(code: str | None) -> str | None:
    return code if code is None or code in _FAILURES else "HARNESS_FAILED"


def _unavailable(
    service: ContextService, principal: Principal, clock: Clock, conversation_id: str,
    failure: str,
) -> dict:
    """Record sanitized configuration/runtime failure without an SDK exception body."""
    with service.tracer.start_as_current_span("harness_http_failure") as span:
        operation = Operation(
            principal, clock.now().astimezone(UTC),
            format(span.get_span_context().trace_id, "032x"), identifier("r"),
            "harness_answer", conversation_id, origin_action="error_result",
            projection="no_pii" if principal.role == "ops" else "full",
        )
        operation.decisions.append({"action": "harness_answer", "resources": [],
                                    "allow": False, "reason": failure,
                                    "policy_revision": None})
        envelope = Envelope[dict[str, JsonValue]](
            trace_id=operation.trace_id, result_id=operation.result_id,
            status="ERROR", as_of=None, valuation_date=None, known_as_of=None,
            calculated_at=operation.now, entitlement_revision=None, policy_revision=None,
            data=None, error=Error(code="INTERNAL", message="Chat provider is unavailable",
                                   retryable=True, details={"failure": failure}),
        ).model_dump(mode="json")
        try:
            service.record(operation, envelope, None)
        except (sqlite3.Error, ValueError):
            envelope["error"] = Error(code="INTERNAL", message="Response evidence unavailable",
                                      retryable=True).model_dump(mode="json")
        return envelope


async def display_history(
    manager: ConversationManager, principal: Principal, clock: Clock, conversation_id: str,
) -> dict:
    return await manager.render_history(principal, clock.now(), conversation_id, display_only=True)


async def answer_chat(
    service: ContextService, principal: Principal, clock: Clock, arguments: ChatArguments,
    provider_factory: ProviderFactory,
) -> dict:
    manager = ConversationManager(service)
    conversation_id = arguments.conversation_id or manager.start(principal, clock.now())
    preflight = await manager.prepare_turn(principal, clock.now(), conversation_id)
    if preflight["status"] == "ERROR":
        return preflight
    # No client construction or provider invocation before owner and policy checks.
    try:
        adapter = provider_factory(arguments.provider)
    except ProviderFailure as exc:
        return _unavailable(service, principal, clock, conversation_id,
                            _failure(exc.code) or "HARNESS_FAILED")
    except Exception:
        return _unavailable(service, principal, clock, conversation_id, "HARNESS_FAILED")
    try:
        run = await run_answer(
            service, manager, principal, clock, arguments.request_text(), adapter,
            conversation_id=conversation_id, mode="live_chat",
        )
    except Exception:
        return _unavailable(service, principal, clock, conversation_id, "HARNESS_FAILED")
    finally:
        close = getattr(adapter, "aclose", None)
        if close is not None:
            try:
                with anyio.move_on_after(5):
                    pending = close()
                    if inspect.isawaitable(pending):
                        await pending
            except Exception:
                pass  # Client cleanup cannot disclose SDK errors or an old answer.
    # Reauthorize after the entire run (and client cleanup), including failures.
    current = await manager.render_history(principal, clock.now(), conversation_id)
    if current["status"] == "ERROR":
        return current
    final = None
    if run.final_valid and run.final_result is not None:
        result_id = run.final_result.get("result_id")
        for answer in current["data"]["answers"]:
            if answer["result_id"] == result_id and not answer["redacted"]:
                final = ChatAnswer(result_id=answer["result_id"], data=answer["data"])
                break
    failure = _failure(run.failure)
    if run.final_valid and final is None:
        failure = "ANSWER_REVOKED_OR_UNAVAILABLE"
    return ChatRunResponse(
        run_id=run.run_id, conversation_id=conversation_id, provider=arguments.provider,
        model=run.model, first_pass_valid=run.first_pass_valid, final_valid=final is not None,
        retries=run.retries, fallback=run.fallback, failure=failure, final_result=final,
    ).model_dump(mode="json")
