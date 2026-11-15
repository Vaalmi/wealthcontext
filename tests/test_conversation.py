"""Revocation must protect follow-ups even when no new data tool is requested."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from wealthcontext.clock import utc_timestamp
from wealthcontext.context_service import ContextService, RequestFailure
from wealthcontext.conversation import TOMBSTONE, ConversationManager
from wealthcontext.database import migrate, write_transaction
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyDecision, PolicyUnavailable
from wealthcontext.storage import (
    canonical,
    digest,
    migrate_business,
    read_snapshot,
    seed_vertical_slice,
)
from wealthcontext.telemetry import create_provider
from wealthcontext.verifier import TEMPLATE

NOW = datetime(2035, 1, 1, tzinfo=UTC)
ADVISOR = Principal("ADV-01", "advisor")
ARGS = {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}
REVISION = "sha256:" + "a" * 64


class GrantPolicy(PolicyClient):
    """Offline policy double; the same conversation suite also runs with real OPA."""

    def __init__(self):
        super().__init__("http://unused.invalid", REVISION)

    async def decide(self, principal, action, resources, facts, revision, request_business_date):
        entitled = {item["object"] for item in facts if item["kind"] == "advisor_covers"
                    and item["subject"] == principal.id
                    and item["valid_from"] <= request_business_date.isoformat() < item["valid_to"]}
        allow = (principal.role == "compliance" or action in {
            "metric_definition", "conversation_preflight", "result_preflight",
        } or (principal.role == "advisor" and set(resources) <= entitled))
        return PolicyDecision(
            allow=allow, allowed_ids=resources if allow else [], projection="full",
            reason="CURRENT_GRANT" if allow else "NO_CURRENT_GRANT",
            policy_revision=REVISION, entitlement_revision=revision,
        )


@pytest.fixture(params=["offline", "opa"])
def conversation_service(request, tmp_path: Path):
    policy = GrantPolicy() if request.param == "offline" else request.getfixturevalue("opa_server")
    database = tmp_path / "db.sqlite"
    migrate(database)
    migrate_business(database)
    seed_vertical_slice(database, NOW)
    provider = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, policy, provider.get_tracer("conversation-tests"))
    yield service
    provider.shutdown()


def revoke(service):
    with write_transaction(service.database) as connection:
        connection.execute("INSERT INTO entitlement_revisions VALUES (2,?)", (utc_timestamp(NOW),))


def candidate(result_id):
    return {
        "answer_markdown": TEMPLATE,
        "claims": [{"id": "c1", "result_id": result_id,
                    "entity": {"type": "household", "id": "WC-HH-0001"},
                    "field": "assets_usd", "household_id": "WC-HH-0001",
                    "valuation_date": "2026-09-30", "unit": "USD", "format": "value_only"}],
        "predicates": [], "caveats": [], "refusal": None,
    }


async def answered(manager):
    conversation = manager.start(ADVISOR, NOW)
    manager.add_user(ADVISOR, NOW, conversation, "Show the included household assets.")
    before = await manager.prepare_turn(ADVISOR, NOW, conversation)
    assert before["status"] == "COMPLETE", before
    result = await manager.execute_tool(
        ADVISOR, NOW, conversation, "call_1", "get_household_snapshot", ARGS,
    )
    assert result["data"]["assets_usd"] == "3000.00", result
    answer = await manager.service.execute(
        ADVISOR, NOW, "submit_answer", candidate(result["result_id"]), conversation,
    )
    assert answer["status"] == "COMPLETE", answer
    manager.append_answer(ADVISOR, NOW, conversation, answer["result_id"])
    return conversation, result, answer


@pytest.mark.anyio
async def test_followup_without_retrieval_has_no_revoked_results_or_prior_answer(
    conversation_service,
):
    manager = ConversationManager(conversation_service)
    conversation, result, answer = await answered(manager)
    initial = await manager.render_history(ADVISOR, NOW, conversation)
    assert "USD 3000.00." in initial["data"]["answers"][0]["content"]
    assert initial["data"]["answers"][0]["data"]["claims"][0]["value"] == "3000.00"
    revoke(conversation_service)
    manager.add_user(ADVISOR, NOW, conversation, "Repeat the amount from your previous answer.")
    followup = await manager.prepare_turn(ADVISOR, NOW, conversation)
    assert followup["status"] == "COMPLETE", followup
    assert followup["entitlement_revision"] == 2
    # This is the exact provider input for a turn that requests no further tools.
    provider_messages = followup["data"]["messages"]
    assert "3000.00" not in canonical(provider_messages)
    assert "Okafor" not in canonical(provider_messages)
    assert "25.00" not in canonical(provider_messages)
    calls = [item for item in provider_messages if "tool_calls" in item]
    results = [item for item in provider_messages if item["role"] == "tool"]
    assert calls[0]["tool_calls"] == [{
        "id": "call_1", "name": "get_household_snapshot", "arguments": {},
    }]
    assert results == [{"role": "tool", "tool_call_id": "call_1", "content": TOMBSTONE}]
    assert followup["data"]["answers"] == [{
        "sequence": 4, "content": TOMBSTONE, "redacted": True, "data": None,
        "result_id": answer["result_id"],
    }]
    assert set(followup["data"]["redacted_result_ids"]) == {
        result["result_id"], answer["result_id"],
    }
    # Rendering is another current-authorization boundary, not a cached answer.
    rendered = await manager.render_history(ADVISOR, NOW, conversation)
    assert rendered["data"]["answers"][0]["content"] == TOMBSTONE
    claim = await manager.service.execute(
        ADVISOR, NOW, "submit_answer", candidate(result["result_id"]), conversation,
    )
    assert claim["error"]["code"] == "NOT_AUTHORIZED"
    with read_snapshot(conversation_service.database) as connection:
        # Audit facts are immutable even though redisclosure is now prohibited.
        original = connection.execute(
            "SELECT payload FROM results WHERE id=?", (result["result_id"],),
        ).fetchone()
        assert json.loads(original[0])["data"]["assets_usd"] == "3000.00"
        stored = connection.execute(
            "SELECT payload,payload_hash FROM results WHERE id=?", (followup["result_id"],),
        ).fetchone()
        assert json.loads(stored[0]) == followup and digest(stored[0]) == stored[1]


@pytest.mark.anyio
async def test_persona_switch_starts_empty_conversation_and_cannot_read_prior(conversation_service):
    manager = ConversationManager(conversation_service)
    old, _, _ = await answered(manager)
    other = Principal("ADV-02", "advisor")
    fresh = manager.start(other, NOW)
    assert old != fresh
    assert (await manager.prepare_turn(other, NOW, fresh))["data"]["messages"] == []
    for method in (manager.prepare_turn, manager.render_history):
        forbidden = await method(other, NOW, old)
        unknown = await method(other, NOW, "conv_unknown")
        assert forbidden["error"] == unknown["error"]
        assert forbidden["error"]["code"] == "NOT_AUTHORIZED"
    with pytest.raises(RequestFailure):
        manager.add_user(other, NOW, old, "Cannot take over this conversation")


@pytest.mark.anyio
async def test_cross_conversation_result_in_message_never_reaches_provider(conversation_service):
    manager = ConversationManager(conversation_service)
    old, result, _ = await answered(manager)
    new = manager.start(ADVISOR, NOW)
    # Simulate malformed persisted context attempting to transplant an owned result.
    with write_transaction(conversation_service.database) as connection:
        manager._append(connection, new, NOW, "assistant_call", {
            "call_id": "call_cross", "name": "get_household_snapshot", "arguments": ARGS,
            "result_id": result["result_id"],
        }, [result["result_id"]])
        manager._append(connection, new, NOW, "tool_result", {
            "call_id": "call_cross", "result_id": result["result_id"],
        }, [result["result_id"]])
    checked = await manager.prepare_turn(ADVISOR, NOW, new)
    # A foreign-conversation association is corrupt evidence, not a revoked grant.
    # Reject the entire view; only genuine entitlement revocation yields tombstones.
    assert checked["data"] is None and checked["error"]["code"] == "INTERNAL"
    assert "3000.00" not in canonical(checked)
    with pytest.raises(RequestFailure):
        manager.append_answer(ADVISOR, NOW, new, result["result_id"])
    assert old != new


@pytest.mark.anyio
@pytest.mark.parametrize("tamper", ["hash", "dependencies", "pairing"])
async def test_corrupt_message_evidence_stops_context_disclosure(conversation_service, tamper):
    manager = ConversationManager(conversation_service)
    conversation, _, _ = await answered(manager)
    with write_transaction(conversation_service.database) as connection:
        if tamper == "hash":
            connection.execute(
                "UPDATE conversation_messages SET payload_hash='bad' WHERE sequence=2",
            )
        elif tamper == "dependencies":
            connection.execute("DELETE FROM conversation_message_results WHERE sequence=2")
        else:
            connection.execute("DELETE FROM conversation_message_results WHERE sequence=3")
            connection.execute("DELETE FROM conversation_messages WHERE sequence=3")
    checked = await manager.prepare_turn(ADVISOR, NOW, conversation)
    assert checked["status"] == "ERROR" and checked["data"] is None
    assert checked["error"]["code"] == "INTERNAL"


@pytest.mark.anyio
async def test_policy_outage_blocks_entire_provider_context(conversation_service, monkeypatch):
    manager = ConversationManager(conversation_service)
    conversation, _, _ = await answered(manager)
    async def unavailable(*_args, **_kwargs):
        raise PolicyUnavailable("injected outage")

    monkeypatch.setattr(conversation_service.policy, "decide", unavailable)
    checked = await manager.prepare_turn(ADVISOR, NOW, conversation)
    assert checked["status"] == "ERROR" and checked["data"] is None
    assert checked["error"]["code"] == "POLICY_UNAVAILABLE"


@pytest.mark.anyio
async def test_one_read_snapshot_and_revision_for_all_results_in_turn(
    conversation_service, monkeypatch,
):
    manager = ConversationManager(conversation_service)
    conversation, _, _ = await answered(manager)
    decide = conversation_service.policy.decide
    committed = False

    async def with_concurrent_commit(*args, **kwargs):
        nonlocal committed
        if not committed:
            revoke(conversation_service)
            committed = True
        return await decide(*args, **kwargs)

    monkeypatch.setattr(conversation_service.policy, "decide", with_concurrent_commit)
    first = await manager.prepare_turn(ADVISOR, NOW, conversation)
    assert first["entitlement_revision"] == 1
    assert first["data"]["redacted_result_ids"] == []
    second = await manager.prepare_turn(ADVISOR, NOW, conversation)
    assert second["entitlement_revision"] == 2
    assert second["data"]["answers"][0]["content"] == TOMBSTONE


@pytest.mark.anyio
async def test_append_exchange_is_paired_and_duplicate_call_id_rejected(conversation_service):
    manager = ConversationManager(conversation_service)
    conversation = manager.start(ADVISOR, NOW)
    await manager.execute_tool(ADVISOR, NOW, conversation, "unique", "get_household_snapshot", ARGS)
    with pytest.raises(ValueError, match="already belongs"):
        await manager.execute_tool(
            ADVISOR, NOW, conversation, "unique", "get_household_snapshot", ARGS,
        )
    with read_snapshot(conversation_service.database) as connection:
        kinds = [row[0] for row in connection.execute(
            "SELECT kind FROM conversation_messages ORDER BY sequence",
        )]
        assert kinds == ["assistant_call", "tool_result"]


def call(call_id, name="get_household_snapshot", arguments=None):
    return {"call_id": call_id, "name": name, "arguments": ARGS if arguments is None else arguments}


@pytest.mark.anyio
async def test_tool_group_keeps_native_pairing_and_atomic_dependency_links(conversation_service):
    manager = ConversationManager(conversation_service)
    conversation = manager.start(ADVISOR, NOW)
    results = await manager.execute_tool_group(ADVISOR, NOW, conversation, [
        call("snapshot"), call("metrics", "get_metric_definition", {"metric_id": "catalog"}),
    ])
    assert len(results) == 2
    view = await manager.prepare_turn(ADVISOR, NOW, conversation)
    assert view["status"] == "COMPLETE", view
    messages = view["data"]["messages"]
    assert [message["role"] for message in messages] == ["assistant", "tool", "tool"]
    assert [item["id"] for item in messages[0]["tool_calls"]] == ["snapshot", "metrics"]
    assert [item["tool_call_id"] for item in messages[1:]] == ["snapshot", "metrics"]
    result_ids = {item["result_id"] for item in results}
    assert set(view["data"]["authorized_result_ids"]) == result_ids
    with read_snapshot(conversation_service.database) as connection:
        for sequence in (1, 2, 3):
            bound = {row[0] for row in connection.execute(
                "SELECT result_id FROM conversation_message_results "
                "WHERE conversation_id=? AND sequence=?", (conversation, sequence),
            )}
            assert bound == result_ids
    with pytest.raises(ValueError, match="already belongs"):
        await manager.execute_tool_group(ADVISOR, NOW, conversation, [call("metrics")])


@pytest.mark.anyio
@pytest.mark.parametrize("calls", [
    [], [call(str(index)) for index in range(9)], [call("same"), call("same")],
    [call("internal", "feed_runs", {"as_of": "2026-09-30"})],
    [call("terminal", "submit_answer", {}), call("data")],
])
async def test_invalid_group_never_executes_a_service_operation(
    conversation_service, monkeypatch, calls,
):
    manager = ConversationManager(conversation_service)
    conversation = manager.start(ADVISOR, NOW)

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid group reached service execution")

    monkeypatch.setattr(conversation_service, "execute", forbidden)
    with pytest.raises(ValueError):
        await manager.execute_tool_group(ADVISOR, NOW, conversation, calls)


@pytest.mark.anyio
async def test_provider_opaque_state_is_rejected_before_persistence(conversation_service):
    manager = ConversationManager(conversation_service)
    conversation = manager.start(ADVISOR, NOW)
    with pytest.raises(ValidationError):
        await manager.execute_tool_group(ADVISOR, NOW, conversation, [
            {**call("opaque"), "provider_state": "hidden previous household text"},
        ])
    assert (await manager.prepare_turn(ADVISOR, NOW, conversation))["data"]["messages"] == []


@pytest.mark.anyio
async def test_foreign_context_is_rejected_before_any_call_executes(
    conversation_service, monkeypatch,
):
    manager = ConversationManager(conversation_service)
    old = manager.start(ADVISOR, NOW)
    result = await manager.execute_tool(ADVISOR, NOW, old, "old", "get_household_snapshot", ARGS)
    new = manager.start(ADVISOR, NOW)

    async def forbidden(*args, **kwargs):
        pytest.fail("foreign context reached service execution")

    monkeypatch.setattr(conversation_service, "execute", forbidden)
    with pytest.raises(RequestFailure) as caught:
        await manager.execute_tool_group(ADVISOR, NOW, new, [call("new")],
                                         context_result_ids=[result["result_id"]])
    assert caught.value.error.code == "NOT_AUTHORIZED"


@pytest.mark.anyio
async def test_revoked_context_is_rejected_before_any_new_call(conversation_service, monkeypatch):
    manager = ConversationManager(conversation_service)
    conversation = manager.start(ADVISOR, NOW)
    result = await manager.execute_tool(
        ADVISOR, NOW, conversation, "old", "get_household_snapshot", ARGS,
    )
    revoke(conversation_service)

    async def forbidden(*args, **kwargs):
        pytest.fail("revoked context reached service execution")

    monkeypatch.setattr(conversation_service, "execute", forbidden)
    with pytest.raises(RequestFailure) as caught:
        await manager.execute_tool_group(ADVISOR, NOW, conversation, [call("new")],
                                         context_result_ids=[result["result_id"]])
    assert caught.value.error.code == "NOT_AUTHORIZED"


@pytest.mark.anyio
async def test_second_service_failure_cannot_persist_half_a_group(
    conversation_service, monkeypatch,
):
    manager = ConversationManager(conversation_service)
    conversation = manager.start(ADVISOR, NOW)
    original = conversation_service.execute
    count = 0

    async def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("injected second operation failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(conversation_service, "execute", fail_second)
    with pytest.raises(RuntimeError, match="second"):
        await manager.execute_tool_group(
            ADVISOR, NOW, conversation, [call("first"), call("second")],
        )
    with read_snapshot(conversation_service.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1


@pytest.mark.anyio
async def test_pair_persistence_failure_rolls_back_entire_group(conversation_service, monkeypatch):
    manager = ConversationManager(conversation_service)
    conversation = manager.start(ADVISOR, NOW)
    original = manager._append
    count = 0

    def fail_last(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 3:
            raise RuntimeError("injected pair persistence failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_append", fail_last)
    with pytest.raises(RuntimeError, match="persistence"):
        await manager.execute_tool_group(
            ADVISOR, NOW, conversation, [call("first"), call("second")],
        )
    with read_snapshot(conversation_service.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
        links = connection.execute(
            "SELECT COUNT(*) FROM conversation_message_results",
        ).fetchone()[0]
        assert links == 0


@pytest.mark.anyio
async def test_invalid_terminal_diagnostic_and_uncited_old_amount_are_tainted(conversation_service):
    manager = ConversationManager(conversation_service)
    conversation = manager.start(ADVISOR, NOW)
    source = await manager.execute_tool(
        ADVISOR, NOW, conversation, "data", "get_household_snapshot", ARGS,
    )
    prepared = await manager.prepare_turn(ADVISOR, NOW, conversation)
    invalid = {"answer_markdown": "The prior amount was 3000.00.", "claims": [],
               "predicates": [], "caveats": [], "refusal": None}
    result = (await manager.execute_tool_group(ADVISOR, NOW, conversation, [
        call("terminal", "submit_answer", invalid),
    ], context_result_ids=prepared["data"]["authorized_result_ids"]))[0]
    assert result["error"]["code"] == "INVALID_ARGUMENT", result
    assert "3000.00" in canonical(result["error"]["details"])
    with read_snapshot(conversation_service.database) as connection:
        assert connection.execute("SELECT action FROM result_access WHERE result_id=?",
                                  (result["result_id"],)).fetchone()[0] == "error_result"
        assert connection.execute("SELECT COUNT(*) FROM result_scopes WHERE result_id=?",
                                  (result["result_id"],)).fetchone()[0] == 0
    before = await manager.prepare_turn(ADVISOR, NOW, conversation)
    assert result["result_id"] in before["data"]["authorized_result_ids"]
    revoke(conversation_service)
    after = await manager.prepare_turn(ADVISOR, NOW, conversation)
    assert after["status"] == "COMPLETE", after
    assert "3000.00" not in canonical(after["data"])
    assert set(after["data"]["redacted_result_ids"]) == {source["result_id"], result["result_id"]}
    assert after["data"]["authorized_result_ids"] == []
    terminal = after["data"]["messages"][2:]
    assert terminal[0]["tool_calls"][0]["arguments"] == {}
    assert terminal[1]["content"] == TOMBSTONE and terminal[1]["tool_call_id"] == "terminal"


def add_second_household(service):
    """A hand-received second P1 household supplies an uncited context dependency."""
    with write_transaction(service.database) as connection:
        rows = connection.execute("SELECT * FROM facts").fetchall()
        for row in rows:
            payload = json.loads(row["payload"].replace("-0001", "-0002"))
            if row["kind"] == "household":
                payload["name"] = "Uncited synthetic household"
            if row["kind"] == "cash":
                payload["amount"] = "876543.21"
            source = connection.execute("SELECT * FROM source_records WHERE id=?",
                                        (row["source_id"],)).fetchone()
            source_id = source["id"].replace("P1", "P2")
            text = canonical(payload)
            connection.execute("INSERT INTO source_records VALUES (?,?,?,?,?,?,?,?)", (
                source_id, source["source_system"], source_id, source["business_date"],
                source["recorded_at"], source["imported_at"], text, digest(text),
            ))
            connection.execute("INSERT INTO facts VALUES (?,?,?,?,?,?,?)", (
                row["kind"], row["entity_id"].replace("-0001", "-0002"), row["valid_from"],
                row["valid_to"], row["recorded_at"], source_id, text,
            ))
        connection.execute("INSERT INTO entitlement_facts VALUES (1,?,?,?,?,?)", (
            "advisor_covers", "ADV-01", "WC-HH-0002", "2026-09-14", "9999-12-31",
        ))


@pytest.mark.anyio
async def test_answer_citing_household_one_is_tainted_by_uncited_household_two_context(
    conversation_service,
):
    add_second_household(conversation_service)
    manager = ConversationManager(conversation_service)
    conversation = manager.start(ADVISOR, NOW)
    first = await manager.execute_tool(
        ADVISOR, NOW, conversation, "first", "get_household_snapshot", ARGS,
    )
    second = await manager.execute_tool(
        ADVISOR, NOW, conversation, "second", "get_household_snapshot",
        {**ARGS, "household_id": "WC-HH-0002"},
    )
    prepared = await manager.prepare_turn(ADVISOR, NOW, conversation)
    context_ids = prepared["data"]["authorized_result_ids"]
    assert set(context_ids) == {first["result_id"], second["result_id"]}
    verified = (await manager.execute_tool_group(ADVISOR, NOW, conversation, [
        call("terminal", "submit_answer", candidate(first["result_id"])),
    ], context_result_ids=context_ids))[0]
    assert verified["status"] == "COMPLETE", verified
    manager.append_answer(ADVISOR, NOW, conversation, verified["result_id"],
                          context_result_ids=context_ids)
    with write_transaction(conversation_service.database) as connection:
        connection.execute("INSERT INTO entitlement_revisions VALUES (2,?)", (utc_timestamp(NOW),))
        connection.execute(
            "INSERT INTO entitlement_facts SELECT 2,kind,subject,object,valid_from,"
            "valid_to FROM entitlement_facts WHERE revision=1 AND object='WC-HH-0001'",
        )
    rendered = await manager.render_history(ADVISOR, NOW, conversation)
    assert rendered["status"] == "COMPLETE", rendered
    assert first["result_id"] in rendered["data"]["authorized_result_ids"]
    assert "876543.21" not in canonical(rendered["data"])
    assert "Uncited synthetic household" not in canonical(rendered["data"])
    assert rendered["data"]["answers"][-1]["redacted"] is True
    assert rendered["data"]["answers"][-1]["data"] is None
    assert rendered["data"]["answers"][-1]["content"] == TOMBSTONE
