"""Independent received-evidence and transport counterexamples from P4 round three."""

import json
import sqlite3
from typing import Literal

import pytest
from test_conversation import ADVISOR, ARGS, NOW, answered
from test_conversation import conversation_service as conversation_fixture
from test_mcp_server import client_for, http_server
from test_p4_transports import audit, bearer, comparable, transported
from test_p4_transports import failure_app as failure_app_fixture
from test_secondary import DAY, OPS, SurfacePolicy, prepare
from test_source_projection import evidence as evidence_fixture
from test_source_projection import stored

from wealthcontext.context_service import ContextService, RequestFailure
from wealthcontext.conversation import ConversationManager
from wealthcontext.database import write_transaction
from wealthcontext.feeds import FXRow
from wealthcontext.identity import Principal
from wealthcontext.ingestion import migrate_feeds
from wealthcontext.policy import PolicyClient, PolicyUnavailable
from wealthcontext.projections import REDACTED, project_no_pii
from wealthcontext.storage import canonical, digest, migrate_business, read_snapshot
from wealthcontext.telemetry import create_provider

conversation_service = conversation_fixture
evidence = evidence_fixture
failure_app = failure_app_fixture

@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["owner", "message_conversation", "sequence", "kind"])
async def test_user_only_transcript_metadata_cannot_be_reassigned(conversation_service, mutation):
    service = conversation_service
    manager = ConversationManager(service)
    other = Principal("ADV-02", "advisor")
    original = manager.start(ADVISOR, NOW)
    target = manager.start(other, NOW)
    manager.add_user(ADVISOR, NOW, original, "Synthetic private name: MeadowClient")
    with write_transaction(service.database) as connection:
        if mutation == "owner":
            connection.execute("UPDATE conversations SET principal_id=? WHERE id=?",
                               (other.id, original))
            target = original
        elif mutation == "message_conversation":
            connection.execute("UPDATE conversation_messages SET conversation_id=? "
                               "WHERE conversation_id=?", (target, original))
        elif mutation == "sequence":
            connection.execute("UPDATE conversation_messages SET sequence=9 "
                               "WHERE conversation_id=?", (original,))
            target, other = original, ADVISOR
        else:
            connection.execute("UPDATE conversation_messages SET kind='assistant_answer' "
                               "WHERE conversation_id=?", (original,))
            target, other = original, ADVISOR
    for view in (manager.prepare_turn, manager.render_history):
        result = await view(other, NOW, target)
        assert result["data"] is None and result["error"]["code"] == "INTERNAL", result
        assert "MeadowClient" not in canonical(result)
    with pytest.raises((sqlite3.DatabaseError, ValueError)):
        manager.add_user(other, NOW, target, "Do not append to damaged history")


@pytest.mark.anyio
async def test_append_answer_rejects_index_corruption_before_persisting(conversation_service):
    service = conversation_service
    manager = ConversationManager(service)
    source, _snapshot, answer = await answered(manager)
    target = manager.start(ADVISOR, NOW)
    with write_transaction(service.database) as connection:
        connection.execute("UPDATE results SET conversation_id=? WHERE id=?",
                           (target, answer["result_id"]))
    with pytest.raises(RequestFailure):
        manager.append_answer(ADVISOR, NOW, target, answer["result_id"])
    with read_snapshot(service.database) as connection:
        assert connection.execute("SELECT count(*) FROM conversation_messages "
                                  "WHERE conversation_id=?", (target,)).fetchone()[0] == 0
    assert source != target


@pytest.mark.anyio
@pytest.mark.parametrize("raw_key", ["raw_payload", "raw_content", "unstructured_payload"])
@pytest.mark.parametrize("projection", ["full", "no_pii"])
async def test_wire_provenance_hash_binds_final_redacted_payload(evidence, raw_key, projection):
    service, policy, _records = evidence
    original = stored({"kind": "household", "id": "WC-HH-0001", raw_key: "private fragment"})
    with write_transaction(service.database) as connection:
        connection.execute("UPDATE source_records SET payload=?,payload_hash=? WHERE id='SRC-note'",
                           (original["payload"], original["payload_hash"]))
    principal = Principal("ADV-01", "advisor")
    if projection == "no_pii":
        # The original handle and its immutable proofs are hand-issued no-PII evidence.
        from test_source_projection import scope_evidence
        with write_transaction(service.database) as connection:
            connection.execute("UPDATE result_access SET projection='no_pii' "
                               "WHERE result_id='r_origin'")
            row = connection.execute("SELECT payload FROM results WHERE id='r_origin'").fetchone()
            scope_evidence(connection, row[0], ["WC-HH-0001"])
            proof = connection.execute("SELECT payload FROM result_scope_integrity "
                                       "WHERE result_id='r_origin'").fetchone()[0]
            body = json.loads(proof)
            body["projection"] = "no_pii"
            text = canonical(body)
            connection.execute("UPDATE result_scope_integrity SET payload=?,payload_hash=? "
                               "WHERE result_id='r_origin'", (text, digest(text)))
    result = await service.execute(principal, NOW, "get_data_provenance", {"reference": "h_origin"})
    assert result["status"] == "COMPLETE", result
    item = next(x for x in result["data"]["items"] if x["id"] == "SRC-note")
    assert item["projection"]["projected_payload_hash"] == digest(canonical(item["payload"]))
    assert item["projection"]["redacted"] is True
    assert item["payload"].get(raw_key, REDACTED) == REDACTED
    assert item["payload_hash"] == original["payload_hash"]


def test_no_pii_keeps_actual_received_fx_rate():
    row = FXRow.model_validate({
        "kind": "fx", "source_record_id": "fx-test", "valid_from": "2026-09-30",
        "valid_to": "2026-10-01", "currency": "EUR", "usd_rate": "1.08",
        "rate_at": "2026-09-30T15:00:00Z",
    })
    assert project_no_pii(row.model_dump(mode="json"))["usd_rate"] == "1.08"


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["detached", "reassigned"])
async def test_trace_rejects_result_association_corruption(conversation_service, target):
    service = conversation_service
    result = await service.execute(ADVISOR, NOW, "get_household_snapshot", ARGS)
    with write_transaction(service.database) as connection:
        connection.execute("UPDATE results SET trace_id='other-trace' WHERE id=?",
                           (result["result_id"],))
    trace = result["trace_id"] if target == "detached" else "other-trace"
    envelope = await service.execute(ADVISOR, NOW, "trace", {"trace_id": trace})
    assert envelope["data"] is None and envelope["error"]["code"] == "INTERNAL", envelope


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_padded_maximum_search_query_reaches_governed_mcp_audit(
    conversation_service, mode: Literal["auto", "legacy"],
):
    service = conversation_service
    migrate_feeds(service.database)
    # The received inventory needs feed tables; the offline policy fixture must
    # implement filtered searches as well as exact conversation decisions.
    if service.policy.url == "http://unused.invalid":
        service.policy = SurfacePolicy()
    async with http_server(service) as url, client_for(url, mode=mode) as client:
        result = await client.call_tool("search_households", {"name_query": " " + "x" * 120 + " "})
        assert not result.is_error, result
        envelope = result.structured_content
        assert envelope is not None and envelope["status"] == "COMPLETE", result
        audit(service.database, envelope, ADVISOR.id)


@pytest.mark.anyio
@pytest.mark.parametrize("damage", ["creation_missing", "creation_hash", "creation_transplant",
                                  "message_missing", "message_hash", "deleted_message"])
async def test_legacy_or_damaged_conversation_evidence_is_not_rebuilt(
    conversation_service, damage,
):
    service = conversation_service
    manager = ConversationManager(service)
    conversation = manager.start(ADVISOR, NOW)
    manager.add_user(ADVISOR, NOW, conversation, "Synthetic private conversation")
    other = manager.start(ADVISOR, NOW)
    with write_transaction(service.database) as connection:
        if damage == "creation_missing":
            connection.execute("DELETE FROM conversation_creation_integrity "
                               "WHERE conversation_id=?", (conversation,))
        elif damage == "creation_hash":
            connection.execute("UPDATE conversation_creation_integrity SET payload_hash='bad' "
                               "WHERE conversation_id=?", (conversation,))
        elif damage == "creation_transplant":
            row = connection.execute("SELECT payload,payload_hash FROM "
                                     "conversation_creation_integrity WHERE conversation_id=?",
                                     (other,)).fetchone()
            connection.execute("UPDATE conversation_creation_integrity "
                               "SET payload=?,payload_hash=? WHERE conversation_id=?",
                               (*row, conversation))
        elif damage == "message_missing":
            connection.execute("DELETE FROM conversation_message_integrity WHERE conversation_id=?",
                               (conversation,))
        elif damage == "message_hash":
            connection.execute("UPDATE conversation_message_integrity SET payload_hash='bad' "
                               "WHERE conversation_id=?", (conversation,))
        else:
            connection.execute("DELETE FROM conversation_messages WHERE conversation_id=?",
                               (conversation,))
    migrate_business(service.database)
    result = await manager.prepare_turn(ADVISOR, NOW, conversation)
    assert result["data"] is None and result["error"]["code"] == "INTERNAL", result
    with pytest.raises(sqlite3.DatabaseError):
        manager.add_user(ADVISOR, NOW, conversation, "Do not invent legacy evidence")
    fresh = manager.start(ADVISOR, NOW)
    manager.add_user(ADVISOR, NOW, fresh, "Fresh supported conversation")
    assert (await manager.prepare_turn(ADVISOR, NOW, fresh))["status"] == "COMPLETE"


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["creation", "message"])
async def test_conversation_evidence_failure_rolls_back_original_write(
    conversation_service, target,
):
    service = conversation_service
    manager = ConversationManager(service)
    conversation = manager.start(ADVISOR, NOW)
    table = ("conversation_creation_integrity" if target == "creation"
             else "conversation_message_integrity")
    with write_transaction(service.database) as connection:
        before = connection.execute("SELECT count(*) FROM conversations").fetchone()[0]
        connection.execute(f"CREATE TRIGGER fail_proof BEFORE INSERT ON {table} "
                           "BEGIN SELECT RAISE(ABORT,'synthetic proof failure'); END")
    with pytest.raises(sqlite3.DatabaseError, match="synthetic proof failure"):
        if target == "creation":
            manager.start(ADVISOR, NOW)
        else:
            manager.add_user(ADVISOR, NOW, conversation, "Must roll back")
    with read_snapshot(service.database) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone()[0] == before
        assert connection.execute("SELECT count(*) FROM conversation_messages").fetchone()[0] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("persona", ["ADV-01", "OPS-01"])
async def test_rest_mcp_padded_search_parity_includes_denials_and_durable_audit(
    failure_app, monkeypatch, persona,
):
    app, client, database = failure_app
    policy = SurfacePolicy()

    async def decide(_self, principal, action, resources, _facts, revision, _day):
        return policy.response(principal, action, resources, revision)

    async def filtered(_self, principal, action, resources, _facts, revision, _day):
        return policy.response(principal, action, resources, revision, filtered=True)

    monkeypatch.setattr(PolicyClient, "decide", decide)
    monkeypatch.setattr(PolicyClient, "filter_resources", filtered)
    for query, invalid in [(" " + "x" * 120 + " ", False), ("x" * 121, True), ("   ", True)]:
        results = []
        for transport in ("rest", "mcp"):
            _response, result = await transported(
                client, transport, "search_households", {"name_query": query}, bearer(app, persona),
            )
            if invalid:
                assert result["error"]["code"] == "INVALID_ARGUMENT", result
            elif persona == "OPS-01":
                assert result["error"]["code"] == "NOT_AUTHORIZED", result
            else:
                assert result["status"] == "COMPLETE", result
            audit(database, result, persona)
            results.append(result)
        assert comparable(results[0]) == comparable(results[1])


@pytest.mark.anyio
async def test_owner_reassignment_with_existing_tool_answers_blocks_user_prompts(
    conversation_service,
):
    manager = ConversationManager(conversation_service)
    conversation, _snapshot, _answer = await answered(manager)
    with write_transaction(conversation_service.database) as connection:
        connection.execute("UPDATE conversations SET principal_id='ADV-02' WHERE id=?",
                           (conversation,))
    for view in (manager.prepare_turn, manager.render_history):
        result = await view(Principal("ADV-02", "advisor"), NOW, conversation)
        assert result["data"] is None and result["error"]["code"] == "INTERNAL", result
        assert "included household assets" not in canonical(result)


@pytest.mark.anyio
@pytest.mark.parametrize("operation,target", [
    ("get_data_quality_report", "view_quality"), ("feed_runs", "feed_runs"),
    ("trace", "trace_scope"),
])
@pytest.mark.parametrize("fault", ["denied", "unavailable"])
async def test_secondary_failure_never_issues_unauthorized_scopes(
    tmp_path, operation, target, fault,
):
    database = tmp_path / "secondary.sqlite"
    prepare(database)
    policy = SurfacePolicy()
    tracer = create_provider(tmp_path / "traces.jsonl")
    try:
        service = ContextService(database, policy, tracer.get_tracer("regression"))
        arguments = {"as_of": str(DAY)}
        principal = ADVISOR if operation == "trace" else OPS
        if operation == "trace":
            origin = await service.execute(ADVISOR, NOW, "search_households",
                                           {"name_query": "Private"})
            assert origin["status"] == "COMPLETE", origin
            arguments = {"trace_id": origin["trace_id"]}
        original = policy.response

        def response(principal, action, resources, revision, filtered=False):
            decision = original(principal, action, resources, revision, filtered)
            if action == target and resources:
                if fault == "unavailable":
                    raise PolicyUnavailable("Synthetic target outage")
                return decision.model_copy(update={"allow": False, "allowed_ids": []})
            return decision

        policy.response = response
        result = await service.execute(principal, NOW, operation, arguments)
        expected = "NOT_AUTHORIZED" if fault == "denied" else "POLICY_UNAVAILABLE"
        assert result["data"] is None and result["error"]["code"] == expected, result
        with read_snapshot(database) as connection:
            assert service.result_scopes(connection, result["result_id"]) == []
            assert connection.execute("SELECT action FROM result_access WHERE result_id=?",
                                      (result["result_id"],)).fetchone()[0] == "error_result"
        policy.response = original
        # A current denial/error does not make its own neutral evidence undisclosable.
        trace = await service.execute(principal, NOW, "trace", {"trace_id": result["trace_id"]})
        assert trace["status"] == "COMPLETE", trace
    finally:
        tracer.shutdown()
