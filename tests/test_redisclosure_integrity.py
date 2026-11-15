"""Original scopes and failed arguments cannot disappear at redisclosure boundaries."""

import json
from datetime import UTC, datetime

import pytest

from wealthcontext.context_service import ContextService, Operation
from wealthcontext.contracts import Envelope
from wealthcontext.conversation import ConversationManager, ToolExchange, ToolResultMessage
from wealthcontext.database import migrate, write_transaction
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyDecision
from wealthcontext.storage import (
    canonical,
    identifier,
    migrate_business,
    read_snapshot,
    seed_vertical_slice,
)
from wealthcontext.telemetry import create_provider

NOW = datetime(2035, 1, 1, tzinfo=UTC)
PRINCIPAL = Principal("ADV-01", "advisor")
HH = "WC-HH-0001"
OTHER = "WC-HH-0002"
REVISION = "sha256:" + "a" * 64
ARGS = {"household_id": HH, "as_of": "2026-09-30"}


class CurrentPolicy(PolicyClient):
    def __init__(self):
        super().__init__("http://unused.invalid", REVISION)
        self.allowed = {HH, OTHER}

    async def decide(self, principal, action, resources, facts, revision, request_business_date):
        allowed = set(resources) <= self.allowed
        return PolicyDecision(allow=allowed, allowed_ids=resources if allowed else [],
                              reason="CURRENT_GRANT" if allowed else "NO_CURRENT_GRANT",
                              projection="full", policy_revision=REVISION,
                              entitlement_revision=revision)


@pytest.fixture
def runtime(tmp_path):
    database = tmp_path / "scope.sqlite"
    migrate(database)
    migrate_business(database)
    seed_vertical_slice(database, NOW)
    policy = CurrentPolicy()
    telemetry = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, policy, telemetry.get_tracer("scope_integrity"))
    yield service, ConversationManager(service), policy
    telemetry.shutdown()


async def snapshot(manager):
    conversation = manager.start(PRINCIPAL, NOW)
    result = await manager.execute_tool(
        PRINCIPAL, NOW, conversation, "fetch", "get_household_snapshot", ARGS,
    )
    assert result["status"] == "COMPLETE"
    return conversation, result


@pytest.mark.anyio
async def test_missing_snapshot_scope_cannot_become_an_empty_grant_after_revocation(runtime):
    service, manager, policy = runtime
    conversation, result = await snapshot(manager)
    with write_transaction(service.database) as connection:
        connection.execute("DELETE FROM result_scopes WHERE result_id=?", (result["result_id"],))
    policy.allowed.clear()
    for view in (manager.prepare_turn, manager.render_history):
        current = await view(PRINCIPAL, NOW, conversation)
        assert current["status"] == "ERROR"
        assert current["error"]["code"] == "INTERNAL"
        assert current["data"] is None and "3000.00" not in canonical(current)


@pytest.mark.anyio
async def test_lost_scope_from_book_result_does_not_authorize_the_broader_payload(runtime):
    service, manager, policy = runtime
    conversation = manager.start(PRINCIPAL, NOW)
    operation = Operation(PRINCIPAL, NOW, "trace_book", identifier("r"),
                          "get_concentrated_positions", conversation,
                          scopes=[HH, OTHER], origin_action="concentrated_positions")
    result = Envelope[dict](
        trace_id=operation.trace_id, result_id=operation.result_id, status="COMPLETE",
        as_of=None, valuation_date=None, known_as_of=None, calculated_at=NOW,
        entitlement_revision=1, policy_revision=REVISION,
        data={"hidden_household_value": "SECRET_SECOND_HOUSEHOLD"},
    ).model_dump(mode="json")
    service.record(operation, result, None)
    with write_transaction(service.database) as connection:
        manager._append(connection, conversation, NOW, "assistant_call", ToolExchange(
            call_id="book", name="get_concentrated_positions", arguments={},
            result_id=result["result_id"],
        ).model_dump(), [result["result_id"]])
        manager._append(connection, conversation, NOW, "tool_result", ToolResultMessage(
            call_id="book", result_id=result["result_id"],
        ).model_dump(), [result["result_id"]])
        connection.execute("DELETE FROM result_scopes WHERE result_id=? AND household_id=?",
                           (result["result_id"], OTHER))
    policy.allowed.remove(OTHER)
    current = await manager.prepare_turn(PRINCIPAL, NOW, conversation)
    assert current["status"] == "ERROR" and current["error"]["code"] == "INTERNAL"
    assert "SECRET_SECOND_HOUSEHOLD" not in canonical(current)


@pytest.mark.anyio
@pytest.mark.parametrize("tool,arguments", [
    ("search_households", {"name_query": "PRIVATE_OLD_CLIENT", "limit": 5}),
    ("get_household_snapshot", {**ARGS, "known_as_of": "2026-10-01T12:00:00",
                                "private_name": "PRIVATE_OLD_CLIENT"}),
])
async def test_failed_arguments_are_not_redisclosed_under_empty_scope(runtime, tool, arguments):
    _service, manager, policy = runtime
    conversation = manager.start(PRINCIPAL, NOW)
    failed = await manager.execute_tool(PRINCIPAL, NOW, conversation, "bad", tool, arguments)
    assert failed["status"] == "ERROR"
    policy.allowed.clear()
    current = await manager.prepare_turn(PRINCIPAL, NOW, conversation)
    assert current["status"] == "COMPLETE", current
    assert "PRIVATE_OLD_CLIENT" not in canonical(current)
    assert current["data"]["messages"][0]["tool_calls"][0]["arguments"] == {}
    error = json.loads(current["data"]["messages"][1]["content"])["error"]
    assert error["code"] == "INVALID_ARGUMENT"


@pytest.mark.anyio
async def test_errors_before_secondary_handler_have_reusable_action_and_audit(runtime):
    service, manager, _policy = runtime
    conversation = manager.start(PRINCIPAL, NOW)
    for tool, args in (("get_data_quality_report", {"as_of": "2026-08-01"}),
                       ("conflicts", {}), ("invalid_request", None)):
        error = await service.execute(PRINCIPAL, NOW, tool, args, conversation)
        assert error["status"] == "ERROR"
        with read_snapshot(service.database) as connection:
            assert connection.execute("SELECT action FROM result_access WHERE result_id=?",
                                      (error["result_id"],)).fetchone()[0] == "error_result"
            assert connection.execute("SELECT 1 FROM decisions WHERE trace_id=?",
                                      (error["trace_id"],)).fetchone()


@pytest.mark.anyio
async def test_conversation_context_authorizes_the_actual_disclosed_scope(runtime):
    service, manager, _policy = runtime
    conversation, _ = await snapshot(manager)
    current = await manager.render_history(PRINCIPAL, NOW, conversation)
    assert current["status"] == "COMPLETE"
    with read_snapshot(service.database) as connection:
        decisions = connection.execute(
            "SELECT resources FROM decisions WHERE trace_id=? AND action='conversation_context'",
            (current["trace_id"],),
        ).fetchall()
    assert [json.loads(row[0]) for row in decisions] == [[HH]]


@pytest.mark.anyio
@pytest.mark.parametrize("damage", ["missing_proof", "hash", "action", "projection", "result_hash"])
async def test_integrity_proof_binds_scope_action_projection_and_payload(runtime, damage):
    service, manager, _policy = runtime
    conversation, result = await snapshot(manager)
    with write_transaction(service.database) as connection:
        if damage == "missing_proof":
            connection.execute("DELETE FROM result_scope_integrity WHERE result_id=?",
                               (result["result_id"],))
        elif damage == "hash":
            connection.execute("UPDATE result_scope_integrity SET payload='{}' WHERE result_id=?",
                               (result["result_id"],))
        elif damage in {"action", "projection"}:
            field, value = (("action", "metric_definition") if damage == "action"
                            else ("projection", "no_pii"))
            connection.execute(f"UPDATE result_access SET {field}=? WHERE result_id=?",
                               (value, result["result_id"]))
        else:
            connection.execute("UPDATE results SET payload_hash=? WHERE id=?",
                               ("f" * 64, result["result_id"]))
    migrate_business(service.database)  # Migration must not bless damaged/missing evidence.
    current = await manager.prepare_turn(PRINCIPAL, NOW, conversation)
    assert current["status"] == "ERROR" and current["error"]["code"] == "INTERNAL"
    assert current["data"] is None


@pytest.mark.anyio
async def test_legitimate_empty_scope_is_explicit_and_unscoped_arguments_are_not_replayed(runtime):
    service, manager, policy = runtime
    conversation = manager.start(PRINCIPAL, NOW)
    result = await manager.execute_tool(PRINCIPAL, NOW, conversation, "metric",
                                        "get_metric_definition", {"metric_id": "catalog"})
    assert result["status"] == "COMPLETE"
    policy.allowed.clear()
    current = await manager.prepare_turn(PRINCIPAL, NOW, conversation)
    assert current["status"] == "COMPLETE"
    assert current["data"]["messages"][0]["tool_calls"][0]["arguments"] == {}
    assert json.loads(current["data"]["messages"][1]["content"])["data"]["item_count"] > 0
    with read_snapshot(service.database) as connection:
        assert service.result_scopes(connection, result["result_id"]) == []
        evidence = json.loads(connection.execute(
            "SELECT payload FROM result_scope_integrity WHERE result_id=?", (result["result_id"],),
        ).fetchone()[0])
        assert evidence["household_ids"] == []
        assert evidence["action"] == "metric_definition" and evidence["projection"] == "full"
    with write_transaction(service.database) as connection:
        connection.execute("DELETE FROM result_scope_integrity WHERE result_id=?",
                           (result["result_id"],))
    assert (await manager.render_history(PRINCIPAL, NOW, conversation))["status"] == "ERROR"


@pytest.mark.anyio
async def test_provenance_rejects_missing_scope_before_vacuous_authorization(runtime):
    service, manager, policy = runtime
    _conversation, result = await snapshot(manager)
    with write_transaction(service.database) as connection:
        connection.execute("DELETE FROM result_scopes WHERE result_id=?", (result["result_id"],))
    policy.allowed.clear()
    reply = await service.execute(PRINCIPAL, NOW, "get_data_provenance",
                                   {"reference": result["source_refs"][0]})
    assert reply["status"] == "ERROR" and reply["error"]["code"] == "INTERNAL"
    assert reply["data"] is None


@pytest.mark.anyio
async def test_conversation_rule_denial_blocks_view_even_when_results_allow(runtime, monkeypatch):
    _service, manager, policy = runtime
    conversation, _ = await snapshot(manager)
    original = policy.decide

    async def decide(principal, action, resources, facts, revision, request_business_date):
        decision = await original(
            principal, action, resources, facts, revision, request_business_date,
        )
        if action == "conversation_context":
            assert resources == [HH]
            return decision.model_copy(update={"allow": False, "allowed_ids": []})
        return decision

    monkeypatch.setattr(policy, "decide", decide)
    current = await manager.prepare_turn(PRINCIPAL, NOW, conversation)
    assert current["status"] == "ERROR" and current["error"]["code"] == "NOT_AUTHORIZED"
    assert current["data"] is None and "3000.00" not in canonical(current)


@pytest.mark.anyio
async def test_scope_evidence_failure_rolls_back_entire_result_publication(runtime):
    service, _manager, _policy = runtime
    with write_transaction(service.database) as connection:
        connection.execute("CREATE TRIGGER fail_scope BEFORE INSERT ON result_scope_integrity "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
    reply = await service.execute(PRINCIPAL, NOW, "get_household_snapshot", ARGS)
    assert reply["status"] == "ERROR" and reply["error"]["code"] == "INTERNAL"
    with read_snapshot(service.database) as connection:
        assert not connection.execute(
            "SELECT 1 FROM results WHERE id=?", (reply["result_id"],),
        ).fetchone()
        assert not connection.execute("SELECT 1 FROM decisions WHERE trace_id=?",
                                      (reply["trace_id"],)).fetchone()


def test_authentication_rejection_has_explicit_empty_scope_for_compliance_trace(runtime):
    service, _manager, _policy = runtime
    reply = service.authentication_error(NOW)
    with read_snapshot(service.database) as connection:
        assert service.result_scopes(connection, reply["result_id"]) == []
