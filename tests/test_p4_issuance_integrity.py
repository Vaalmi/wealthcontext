"""P4 R2: authentic records cannot be transplanted through mutable issuance indexes."""

import json
from contextlib import contextmanager

import pytest
from test_redisclosure_integrity import (
    ARGS,
    HH,
    NOW,
    PRINCIPAL,
    CurrentPolicy,
    snapshot,
)
from test_redisclosure_integrity import runtime as runtime
from test_secondary import prepare

import wealthcontext.context_service as service_module
from wealthcontext.context_service import ContextService
from wealthcontext.conversation import ConversationManager
from wealthcontext.database import write_transaction
from wealthcontext.identity import Principal
from wealthcontext.storage import canonical, migrate_business, read_snapshot
from wealthcontext.telemetry import create_provider
from wealthcontext.verifier import TEMPLATE


@pytest.fixture
def received(tmp_path):
    database = tmp_path / "received.sqlite"
    prepare(database)
    policy = CurrentPolicy()
    policy.allowed = {HH}
    telemetry = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, policy, telemetry.get_tracer("issuance"))
    yield service, ConversationManager(service)
    telemetry.shutdown()


def candidate(result):
    return {
        "answer_markdown": TEMPLATE,
        "claims": [{"id": "c1", "result_id": result["result_id"],
                    "entity": {"type": "household", "id": HH}, "field": "assets_usd",
                    "household_id": HH, "valuation_date": "2026-09-30", "unit": "USD",
                    "format": "value_only"}],
        "predicates": [], "caveats": [], "refusal": None,
    }


@pytest.mark.anyio
@pytest.mark.parametrize("damage", ["foreign_person", "append_person", "omit_source"])
async def test_handle_source_membership_cannot_be_replaced_with_authentic_records(
    received, monkeypatch, damage,
):
    service, manager = received
    _, result = await snapshot(manager)
    handle = result["source_refs"][0]
    with write_transaction(service.database) as connection:
        foreign = connection.execute(
            "SELECT source_id FROM facts WHERE kind='person' AND entity_id='WC-PER-0002'",
        ).fetchone()[0]
        source_hash = connection.execute(
            "SELECT payload_hash FROM source_records WHERE id=?", (foreign,),
        ).fetchone()[0]
        original = json.loads(connection.execute(
            "SELECT source_ids FROM provenance_handles WHERE id=?", (handle,),
        ).fetchone()[0])
        assert foreign not in original and len(original) > 1
        changed = ([foreign] if damage == "foreign_person" else
                   sorted([*original, foreign]) if damage == "append_person" else original[1:])
        connection.execute("UPDATE provenance_handles SET source_ids=? WHERE id=?",
                           (canonical(changed), handle))
    statements = []

    @contextmanager
    def observed(path):
        with read_snapshot(path) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(service_module, "read_snapshot", observed)
    reply = await service.execute(PRINCIPAL, NOW, "get_data_provenance", {"reference": handle})
    assert reply["status"] == "ERROR" and reply["error"]["code"] == "INTERNAL"
    assert reply["data"] is None and "Private person 0002" not in canonical(reply)
    assert not any("SELECT * FROM source_records" in sql for sql in statements)
    with read_snapshot(service.database) as connection:
        assert connection.execute(
            "SELECT payload_hash FROM source_records WHERE id=?", (foreign,),
        ).fetchone()[0] == source_hash


@pytest.mark.anyio
@pytest.mark.parametrize("change_owner", [False, True])
async def test_result_original_conversation_and_owner_survive_index_reassignment(
    runtime, change_owner,
):
    service, manager, _ = runtime
    original_conversation, result = await snapshot(manager)
    legitimate = await service.execute(PRINCIPAL, NOW, "submit_answer", candidate(result),
                                       original_conversation)
    assert legitimate["status"] == "COMPLETE"
    caller = Principal("ADV-02", "advisor") if change_owner else PRINCIPAL
    other = manager.start(caller, NOW)
    with write_transaction(service.database) as connection:
        connection.execute("UPDATE results SET principal_id=?,conversation_id=? WHERE id=?",
                           (caller.id, other, result["result_id"]))
    reply = await service.execute(caller, NOW, "submit_answer", candidate(result), other)
    assert reply["status"] == "ERROR" and reply["error"]["code"] == "INTERNAL"
    assert reply["data"] is None and "3000.00" not in canonical(reply)


@pytest.mark.anyio
async def test_handle_original_owner_cannot_be_reassigned_to_another_covered_advisor(runtime):
    service, manager, _ = runtime
    _, result = await snapshot(manager)
    handle = result["source_refs"][0]
    other = Principal("ADV-02", "advisor")
    before = await service.execute(other, NOW, "get_data_provenance", {"reference": handle})
    assert before["error"]["code"] == "NOT_AUTHORIZED"
    with write_transaction(service.database) as connection:
        connection.execute("UPDATE provenance_handles SET principal_id=? WHERE id=?",
                           (other.id, handle))
    reply = await service.execute(other, NOW, "get_data_provenance", {"reference": handle})
    assert reply["status"] == "ERROR" and reply["error"]["code"] == "INTERNAL"
    assert reply["data"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("damage", ["result_association", "handle_id"])
async def test_handle_identity_and_origin_result_are_bound_at_issuance(runtime, damage):
    service, manager, _ = runtime
    _, result = await snapshot(manager)
    handle = result["source_refs"][0]
    other = await service.execute(PRINCIPAL, NOW, "get_household_snapshot", ARGS)
    assert other["status"] == "COMPLETE"
    reference = handle
    with write_transaction(service.database) as connection:
        if damage == "result_association":
            connection.execute("UPDATE provenance_handles SET result_id=? WHERE id=?",
                               (other["result_id"], handle))
        else:
            reference = "h_reassigned"
            connection.execute("UPDATE provenance_handles SET id=? WHERE id=?",
                               (reference, handle))
    reply = await service.execute(PRINCIPAL, NOW, "get_data_provenance", {"reference": reference})
    assert reply["status"] == "ERROR" and reply["error"]["code"] == "INTERNAL"
    assert reply["data"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("damage", ["missing", "payload", "transplanted_proof"])
async def test_issuance_proof_is_required_and_cannot_be_reconstructed_from_indexes(runtime, damage):
    service, manager, _ = runtime
    conversation, result = await snapshot(manager)
    other = await service.execute(PRINCIPAL, NOW, "get_household_snapshot", ARGS)
    with write_transaction(service.database) as connection:
        if damage == "missing":
            connection.execute("DELETE FROM result_issuance_integrity WHERE result_id=?",
                               (result["result_id"],))
        elif damage == "payload":
            connection.execute(
                "UPDATE result_issuance_integrity SET payload='{}' WHERE result_id=?",
                (result["result_id"],),
            )
        else:
            proof = connection.execute(
                "SELECT payload,payload_hash FROM result_issuance_integrity WHERE result_id=?",
                (other["result_id"],),
            ).fetchone()
            connection.execute(
                "UPDATE result_issuance_integrity SET payload=?,payload_hash=? WHERE result_id=?",
                (proof["payload"], proof["payload_hash"], result["result_id"]),
            )
    migrate_business(service.database)
    answer = await service.execute(PRINCIPAL, NOW, "submit_answer", candidate(result), conversation)
    lineage = await service.execute(PRINCIPAL, NOW, "get_data_provenance",
                                    {"reference": result["source_refs"][0]})
    assert all(reply["status"] == "ERROR" and reply["error"]["code"] == "INTERNAL"
               and reply["data"] is None for reply in (answer, lineage))
    fresh = await service.execute(PRINCIPAL, NOW, "get_household_snapshot", ARGS, conversation)
    fresh_answer = await service.execute(PRINCIPAL, NOW, "submit_answer", candidate(fresh),
                                        conversation)
    assert fresh_answer["status"] == "COMPLETE"


@pytest.mark.anyio
async def test_issuance_write_failure_rolls_back_all_response_evidence(runtime):
    service, _, _ = runtime
    with write_transaction(service.database) as connection:
        connection.execute(
            "CREATE TRIGGER fail_issuance BEFORE INSERT ON result_issuance_integrity "
            "BEGIN SELECT RAISE(ABORT, 'synthetic issuance failure'); END",
        )
    reply = await service.execute(PRINCIPAL, NOW, "get_household_snapshot", ARGS)
    assert reply["status"] == "ERROR" and reply["error"]["code"] == "INTERNAL"
    assert reply["data"] is None and not reply["source_refs"]
    with read_snapshot(service.database) as connection:
        for table in ("results", "result_scope_integrity", "result_issuance_integrity",
                      "result_access", "provenance_handles"):
            assert not connection.execute(f"SELECT 1 FROM {table}").fetchone()
        assert not connection.execute("SELECT 1 FROM decisions").fetchone()


def test_authentication_denial_issues_explicit_null_identity_and_no_sources(runtime):
    service, _, _ = runtime
    reply = service.authentication_error(NOW)
    with read_snapshot(service.database) as connection:
        proof = service.result_issuance(connection, reply["result_id"])
        assert proof.principal_id is None and proof.conversation_id is None
        assert proof.provenance_handle_id is None and proof.source_ids == []
        assert service.result_scopes(connection, reply["result_id"]) == []
