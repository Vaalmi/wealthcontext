"""Durable conversations with current authorization at every redisclosure boundary.

Stored messages reference immutable governed results. Provider views are rebuilt,
never cached: revoked data and answers become paired, content-free tombstones.
"""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, JsonValue, TypeAdapter

from wealthcontext.chat_contracts import ChatHistory, HistoryMessage
from wealthcontext.clock import utc_timestamp
from wealthcontext.context_service import DENIAL, ContextService, Operation, RequestFailure
from wealthcontext.contracts import Contract, Envelope, Error, Identifier
from wealthcontext.conversation_integrity import (
    conversation_owner,
    record_message,
    verify_messages,
)
from wealthcontext.database import write_transaction
from wealthcontext.identity import Principal
from wealthcontext.storage import canonical, digest, identifier, read_snapshot

TOMBSTONE = "[REDACTED: NOT_AUTHORIZED]"

# The seven public data tools plus the harness's terminal submission. Inspectors,
# conversation operations and internal service actions are never model tools.
MODEL_TOOLS = frozenset({
    "get_household_snapshot", "get_portfolio_exposure", "get_concentrated_positions",
    "get_data_provenance", "search_households", "get_metric_definition",
    "get_data_quality_report", "submit_answer",
})


class UserMessage(Contract):
    content: str = Field(min_length=1, max_length=16000)


class ToolExchange(Contract):
    call_id: Identifier
    name: Identifier
    arguments: dict[str, JsonValue]
    result_id: Identifier


class ToolResultMessage(Contract):
    call_id: Identifier
    result_id: Identifier
    dependency_result_ids: list[Identifier] | None = None


class ToolCall(Contract):
    call_id: Identifier
    name: Identifier
    arguments: dict[str, JsonValue]


class ToolGroup(Contract):
    calls: list[ToolExchange] = Field(min_length=1, max_length=8)
    context_result_ids: list[Identifier] = Field(default_factory=list)


class AnswerMessage(Contract):
    result_id: Identifier
    cited_result_ids: list[Identifier]
    context_result_ids: list[Identifier] = Field(default_factory=list)


class ConversationManager:
    def __init__(self, service: ContextService):
        self.service = service

    def start(self, principal: Principal, now: datetime) -> str:
        # Persona selection always calls start; a conversation's owner is immutable.
        return self.service.start_conversation(principal, now)

    @staticmethod
    def _owner(connection: sqlite3.Connection, principal: Principal, conversation_id: str) -> None:
        if conversation_owner(connection, conversation_id) != principal.id:
            raise RequestFailure("NOT_AUTHORIZED", DENIAL)
        verify_messages(connection, conversation_id, principal.id)

    @staticmethod
    def _append(
        connection: sqlite3.Connection, conversation_id: str, now: datetime,
        kind: str, payload: dict, result_ids: list[str],
    ) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        text = canonical(payload)
        connection.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?)", (
            conversation_id, sequence, kind, text, digest(text), utc_timestamp(now),
        ))
        for result_id in sorted(set(result_ids)):
            connection.execute("INSERT INTO conversation_message_results VALUES (?,?,?)", (
                conversation_id, sequence, result_id,
            ))
        owner = conversation_owner(connection, conversation_id)
        if owner is None:
            raise sqlite3.DatabaseError("Conversation owner unavailable")
        record_message(connection, conversation_id, owner, sequence, kind, digest(text),
                       utc_timestamp(now), result_ids)

    def add_user(
        self, principal: Principal, now: datetime, conversation_id: str, text: str,
    ) -> None:
        message = UserMessage(content=text)
        with write_transaction(self.service.database) as connection:
            self._owner(connection, principal, conversation_id)
            self._append(connection, conversation_id, now, "user", message.model_dump(), [])

    async def execute_tool(
        self, principal: Principal, now: datetime, conversation_id: str,
        call_id: str, name: str, arguments: dict,
    ) -> dict:
        return (await self.execute_tool_group(
            principal, now, conversation_id,
            [{"call_id": call_id, "name": name, "arguments": arguments}],
        ))[0]

    @staticmethod
    def _owned_results(
        connection: sqlite3.Connection, principal: Principal, conversation_id: str,
        result_ids: list[str],
    ) -> None:
        for result_id in result_ids:
            owner = connection.execute(
                "SELECT principal_id,conversation_id FROM results WHERE id=?", (result_id,),
            ).fetchone()
            if not owner or tuple(owner) != (principal.id, conversation_id):
                raise RequestFailure("NOT_AUTHORIZED", DENIAL)
            issued = ContextService.result_issuance(connection, result_id)
            if (issued.principal_id != principal.id
                    or issued.conversation_id != conversation_id):
                raise RequestFailure("NOT_AUTHORIZED", DENIAL)
            ContextService.result_scopes(connection, result_id)

    @staticmethod
    def _check_call_ids(
        connection: sqlite3.Connection, conversation_id: str, call_ids: set[str],
    ) -> None:
        for row in connection.execute(
            "SELECT payload,payload_hash FROM conversation_messages WHERE conversation_id=? "
            "AND kind='assistant_call'", (conversation_id,),
        ):
            if digest(row[0]) != row[1]:
                raise RequestFailure("INTERNAL", "Conversation evidence unavailable", True)
            payload = json.loads(row[0])
            calls = payload.get("calls", [payload])
            if any(call.get("call_id") in call_ids for call in calls):
                raise ValueError("tool call id already belongs to this conversation")

    async def execute_tool_group(
        self, principal: Principal, now: datetime, conversation_id: str, calls: list[dict],
        *, context_result_ids: list[str] | None = None,
    ) -> list[dict]:
        """Execute read-only calls sequentially; persist the complete exchange atomically.

        The caller passes every result exposed in the fresh provider context, not
        only references the model chose to cite. Those dependencies taint arguments
        and diagnostics as well as successful data when a grant is later revoked.
        """
        drafts = TypeAdapter(list[ToolCall]).validate_python(calls)
        context_ids = TypeAdapter(list[Identifier]).validate_python(context_result_ids or [])
        call_ids = {call.call_id for call in drafts}
        if not 1 <= len(drafts) <= 8:
            raise ValueError("a tool group requires between one and eight calls")
        if len(call_ids) != len(drafts):
            raise ValueError("duplicate tool call id in group")
        if any(call.name not in MODEL_TOOLS for call in drafts):
            raise ValueError("operation is not an allowed model tool")
        if any(call.name == "submit_answer" for call in drafts) and len(drafts) != 1:
            raise ValueError("submit_answer must be the sole call in its group")
        if len(set(context_ids)) != len(context_ids):
            raise ValueError("context result ids must be unique")
        context_ids.sort()
        with read_snapshot(self.service.database) as connection:
            self._owner(connection, principal, conversation_id)
            self._owned_results(connection, principal, conversation_id, context_ids)
            self._check_call_ids(connection, conversation_id, call_ids)
        if context_ids:
            current = await self.prepare_turn(principal, now, conversation_id)
            if current["status"] != "COMPLETE":
                error = Error.model_validate(current["error"])
                raise RequestFailure(error.code, error.message, error.retryable)
            if not set(context_ids) <= set(current["data"]["authorized_result_ids"]):
                raise RequestFailure("NOT_AUTHORIZED", DENIAL)
        results = [await self.service.execute(
            principal, now, call.name, call.arguments, conversation_id,
        ) for call in drafts]
        exchanges = [ToolExchange(**call.model_dump(), result_id=result["result_id"])
                     for call, result in zip(drafts, results, strict=True)]
        group = ToolGroup(calls=exchanges, context_result_ids=context_ids)
        dependencies = sorted({*context_ids, *(result["result_id"] for result in results)})
        with write_transaction(self.service.database) as connection:
            self._owner(connection, principal, conversation_id)
            self._check_call_ids(connection, conversation_id, call_ids)
            self._owned_results(connection, principal, conversation_id, context_ids)
            for result in results:
                persisted = connection.execute(
                    "SELECT payload_hash FROM results WHERE id=? "
                    "AND conversation_id=? AND principal_id=?",
                    (result["result_id"], conversation_id, principal.id),
                ).fetchone()
                if not persisted or persisted[0] != digest(canonical(result)):
                    raise RequestFailure("INTERNAL", "Response evidence unavailable", True)
                self.service.result_scopes(connection, result["result_id"])
            self._append(connection, conversation_id, now, "assistant_call",
                         group.model_dump(mode="json"), dependencies)
            for call in exchanges:
                self._append(connection, conversation_id, now, "tool_result", ToolResultMessage(
                    call_id=call.call_id, result_id=call.result_id,
                    dependency_result_ids=dependencies,
                ).model_dump(), dependencies)
        return results

    def append_answer(
        self, principal: Principal, now: datetime, conversation_id: str, result_id: str,
        *, context_result_ids: list[str] | None = None,
    ) -> None:
        """Retain only a server-verified answer; render_history rechecks it before display."""
        with write_transaction(self.service.database) as connection:
            self._owner(connection, principal, conversation_id)
            row = connection.execute(
                "SELECT payload,payload_hash FROM results WHERE id=? AND principal_id=? "
                "AND conversation_id=?", (result_id, principal.id, conversation_id),
            ).fetchone()
            if not row or digest(row[0]) != row[1]:
                raise RequestFailure("NOT_AUTHORIZED", DENIAL)
            self.service.result_scopes(connection, result_id)
            result = json.loads(row[0])
            data = result.get("data")
            if (result.get("status") != "COMPLETE" or not isinstance(data, dict)
                    or not isinstance(data.get("markdown"), str)
                    or not isinstance(data.get("claims"), list)):
                raise ValueError("only a verified answer result can be appended")
            access = connection.execute(
                "SELECT action FROM result_access WHERE result_id=?", (result_id,),
            ).fetchone()
            if not access or access[0] != "verify_claim":
                raise ValueError("result is not a verified answer")
            cited = {item["result_id"] for item in data["claims"]}
            cited.update(item["result_id"] for item in data.get("predicates", []))
            if data.get("refusal") is not None:
                cited.add(data["refusal"]["result_id"])
            if "cited_result_ids" in data and set(data["cited_result_ids"]) != cited:
                raise ValueError("verified answer citations do not match its rendered references")
            context_ids = TypeAdapter(list[Identifier]).validate_python(context_result_ids or [])
            if len(set(context_ids)) != len(context_ids):
                raise ValueError("context result ids must be unique")
            self._owned_results(connection, principal, conversation_id, [*cited, *context_ids])
            message = AnswerMessage(result_id=result_id, cited_result_ids=sorted(cited),
                                    context_result_ids=sorted(context_ids))
            self._append(connection, conversation_id, now, "assistant_answer",
                         message.model_dump(), [result_id, *cited, *context_ids])

    async def prepare_turn(
        self, principal: Principal, now: datetime, conversation_id: str,
    ) -> dict:
        return await self._view(principal, now, conversation_id, "provider_context")

    async def render_history(
        self, principal: Principal, now: datetime, conversation_id: str,
        *, display_only: bool = False,
    ) -> dict:
        return await self._view(principal, now, conversation_id,
                                "browser_history" if display_only else "answer_render")

    async def _view(
        self, principal: Principal, now: datetime, conversation_id: str,
        purpose: Literal["provider_context", "answer_render", "browser_history"],
    ) -> dict:
        with self.service.tracer.start_as_current_span(purpose) as span:
            operation = Operation(
                principal, now.astimezone(UTC), format(span.get_span_context().trace_id, "032x"),
                identifier("r"), "conversation_context", conversation_id,
            )
            operation.origin_action = "conversation_context"
            data, error = None, None
            try:
                with read_snapshot(self.service.database) as connection:
                    await self.service.authorize(
                        operation, connection, "conversation_preflight", [],
                    )
                    try:
                        self._owner(connection, principal, conversation_id)
                    except RequestFailure:
                        self.service.deny_locally(operation, "CONVERSATION_OWNER_MISMATCH")
                    rows = connection.execute(
                        "SELECT sequence,kind,payload,payload_hash FROM conversation_messages "
                        "WHERE conversation_id=? ORDER BY sequence", (conversation_id,),
                    ).fetchall()
                    dependencies: dict[int, set[str]] = {}
                    for row in connection.execute(
                        "SELECT sequence,result_id FROM conversation_message_results "
                        "WHERE conversation_id=?", (conversation_id,),
                    ):
                        dependencies.setdefault(row[0], set()).add(row[1])
                    records = []
                    for row in rows:
                        if digest(row["payload"]) != row["payload_hash"]:
                            raise ValueError("message evidence hash mismatch")
                        raw = json.loads(row["payload"])
                        produced: set[str] = set()
                        if row["kind"] == "user":
                            payload = UserMessage.model_validate(raw).model_dump()
                            expected = set()
                        elif row["kind"] == "assistant_call":
                            # A group uses the existing storage kind. Legacy single
                            # exchanges remain readable without a storage migration.
                            if "calls" in raw:
                                group = ToolGroup.model_validate(raw)
                                calls, context = group.calls, group.context_result_ids
                            else:
                                calls, context = [ToolExchange.model_validate(raw)], []
                            if (len({call.call_id for call in calls}) != len(calls)
                                    or len(set(context)) != len(context)
                                    or any(call.name not in MODEL_TOOLS for call in calls)
                                    or (len(calls) > 1 and any(call.name == "submit_answer"
                                                              for call in calls))):
                                raise ValueError("invalid stored tool group")
                            produced = {call.result_id for call in calls}
                            if len(produced) != len(calls):
                                raise ValueError("tool group reuses result identity")
                            expected = produced | set(context)
                            payload = {"calls": [call.model_dump() for call in calls]}
                        elif row["kind"] == "tool_result":
                            tool = ToolResultMessage.model_validate(raw)
                            bound = tool.dependency_result_ids
                            if bound is not None and len(set(bound)) != len(bound):
                                raise ValueError("duplicate result dependency")
                            expected = set(bound) if bound is not None else {tool.result_id}
                            if tool.result_id not in expected:
                                raise ValueError("tool result omitted its own dependency")
                            produced = {tool.result_id}
                            payload = tool.model_dump()
                        elif row["kind"] == "assistant_answer":
                            answer = AnswerMessage.model_validate(raw)
                            if (len(set(answer.cited_result_ids)) != len(answer.cited_result_ids)
                                    or len(set(answer.context_result_ids))
                                    != len(answer.context_result_ids)):
                                raise ValueError("duplicate answer dependency")
                            expected = {answer.result_id, *answer.cited_result_ids,
                                        *answer.context_result_ids}
                            produced = {answer.result_id}
                            payload = answer.model_dump()
                        else:
                            raise ValueError("unknown conversation message")
                        refs = dependencies.get(row["sequence"], set())
                        if refs != expected:
                            raise ValueError("message result dependencies do not match")
                        records.append({"kind": row["kind"], "sequence": row["sequence"],
                                        "payload": payload, "refs": refs, "produced": produced})
                    resolved: dict[str, dict] = {}
                    denied: set[str] = set()
                    for result_id in sorted(set().union(*dependencies.values())):
                        issued = self.service.result_issuance(connection, result_id)
                        if (issued.principal_id != principal.id
                                or issued.conversation_id != conversation_id):
                            raise sqlite3.DatabaseError("Conversation result origin mismatch")
                        try:
                            resolved[result_id] = await self.service.resolve_result(
                                operation, connection, result_id,
                            )
                        except RequestFailure as exc:
                            if exc.error.code != "NOT_AUTHORIZED":
                                raise
                            denied.add(result_id)
                    # Error envelopes can have no financial scope, yet their
                    # diagnostics/arguments can repeat a revoked figure. Propagate
                    # taint through the complete group dependency graph first.
                    changed = True
                    while changed:
                        before = len(denied)
                        for record in records:
                            if record["refs"] & denied:
                                denied.update(record["produced"])
                        changed = len(denied) != before
                    # Group dependencies may taint otherwise authorized results.
                    # Check only the complete scope of results actually disclosed.
                    operation.scopes = sorted({
                        household for result_id in set(resolved) - denied
                        for household in self.service.result_scopes(connection, result_id)
                    })
                    await self.service.authorize(
                        operation, connection, "conversation_context", operation.scopes,
                    )
                    messages, answers = [], []
                    pending: list[tuple[str, str]] = []
                    pending_dependencies: set[str] = set()
                    seen_calls: set[str] = set()
                    for record in records:
                        payload, refs = record["payload"], record["refs"]
                        redacted = bool(refs & denied)
                        if record["kind"] == "user":
                            if pending:
                                raise ValueError("missing paired tool result")
                            messages.append({"role": "user", "content": payload["content"]})
                        elif record["kind"] == "assistant_call":
                            if pending:
                                raise ValueError("missing paired tool result")
                            tool_calls = []
                            for call in payload["calls"]:
                                if call["call_id"] in seen_calls:
                                    raise ValueError("duplicate tool call identity")
                                pending.append((call["call_id"], call["result_id"]))
                                seen_calls.add(call["call_id"])
                                tool_calls.append({"id": call["call_id"], "name": call["name"],
                                                   "arguments": {} if (
                                                       redacted
                                                       or resolved[call["result_id"]]["status"]
                                                       == "ERROR"
                                                       or not self.service.result_scopes(
                                                           connection, call["result_id"],
                                                       )) else call["arguments"]})
                            pending_dependencies = refs
                            messages.append({"role": "assistant", "tool_calls": tool_calls})
                        elif record["kind"] == "tool_result":
                            if (not pending or pending.pop(0)
                                    != (payload["call_id"], payload["result_id"])
                                    or refs != pending_dependencies):
                                raise ValueError("tool result has no matching call")
                            messages.append({"role": "tool", "tool_call_id": payload["call_id"],
                                             "content": TOMBSTONE if redacted else
                                             canonical(resolved[payload["result_id"]])})
                        else:
                            if pending:
                                raise ValueError("missing paired tool result")
                            answer_data = (None if redacted else
                                           resolved[payload["result_id"]]["data"])
                            if not redacted and not isinstance(answer_data, dict):
                                raise ValueError("verified answer data is unavailable")
                            content = (answer_data["markdown"] if answer_data is not None
                                       else TOMBSTONE)
                            messages.append({"role": "assistant", "content": content})
                            answers.append({"sequence": record["sequence"], "content": content,
                                            "redacted": redacted, "data": answer_data,
                                            "result_id": payload["result_id"]})
                    if pending:
                        raise ValueError("missing paired tool result")
                    data = {"conversation_id": conversation_id, "purpose": purpose,
                            "messages": messages, "answers": answers,
                            "authorized_result_ids": sorted(set(resolved) - denied),
                            "redacted_result_ids": sorted(denied), "message_count": len(messages)}
                    if purpose == "browser_history":
                        # Project before recording, so the exact browser response
                        # has its own durable hash and never contains model context.
                        current_answers = iter(answers)
                        display = []
                        for message in messages:
                            if message["role"] == "user":
                                display.append(HistoryMessage(role="user",
                                                              content=message["content"]))
                            elif message["role"] == "assistant" and "content" in message:
                                answer = next(current_answers)
                                display.append(HistoryMessage(
                                    role="assistant", content=answer["content"],
                                    redacted=answer["redacted"], result_id=answer["result_id"],
                                    data=answer["data"],
                                ))
                        data = ChatHistory(conversation_id=conversation_id,
                                           messages=display).model_dump(mode="json")
            except RequestFailure as exc:
                error = exc.error
            except (sqlite3.Error, ValueError, KeyError, TypeError):
                error = Error(code="INTERNAL", message="Conversation evidence unavailable",
                              retryable=True)
            envelope = Envelope[dict[str, JsonValue]](
                trace_id=operation.trace_id, result_id=operation.result_id,
                status="ERROR" if error else "COMPLETE", as_of=None, valuation_date=None,
                known_as_of=None, calculated_at=operation.now,
                entitlement_revision=operation.revision, policy_revision=operation.policy_revision,
                data=None if error else data, error=error,
            ).model_dump(mode="json")
            try:
                self.service.record(operation, envelope, None)
            except (sqlite3.Error, ValueError):
                envelope.update(status="ERROR", data=None, error=Error(
                    code="INTERNAL", message="Response evidence unavailable", retryable=True,
                ).model_dump())
            return envelope
