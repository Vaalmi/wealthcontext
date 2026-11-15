"""Origin action/projection survives stored result, handle and batch redisclosure."""

import json
from datetime import UTC, date, datetime

import pytest

from wealthcontext.clock import utc_timestamp
from wealthcontext.context_service import ContextService, Operation, RequestFailure
from wealthcontext.contracts import Envelope
from wealthcontext.database import migrate, write_transaction
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyDecision
from wealthcontext.storage import (
    canonical,
    digest,
    migrate_business,
    read_snapshot,
    seed_vertical_slice,
)
from wealthcontext.telemetry import create_provider

NOW = datetime(2035, 1, 1, tzinfo=UTC)
REVISION = "sha256:" + "a" * 64
OPS = Principal("OPS-01", "ops")


class AccessPolicy(PolicyClient):
    def __init__(self):
        super().__init__("http://unused.invalid", REVISION)

    async def decide(self, principal, action, resources, facts, revision, request_business_date):
        allow = principal.role == "compliance" or (
            principal.role == "ops" and action in {
                "metric_definition", "view_quality", "result_preflight", "conversation_preflight",
                "provenance_preflight", "provenance",
            }
        )
        return PolicyDecision(
            allow=allow, allowed_ids=resources if allow else [], reason="CURRENT_GRANT",
            policy_revision=REVISION, entitlement_revision=revision,
            projection="no_pii" if principal.role == "ops" and action != "metric_definition"
            else "full",
        )


@pytest.fixture
def access_service(tmp_path):
    database = tmp_path / "access.sqlite"
    migrate(database)
    migrate_business(database)
    seed_vertical_slice(database, NOW)
    provider = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, AccessPolicy(), provider.get_tracer("access"))
    conversation = service.start_conversation(OPS, NOW)
    yield service, conversation
    provider.shutdown()


def store_result(service, conversation, result_id, action, scopes, projection, *, source=None):
    operation = Operation(OPS, NOW, f"trace-{result_id}", result_id, action, conversation,
                          revision=1, policy_revision=REVISION, scopes=scopes,
                          projection=projection, origin_action=action, account_scope=[])
    operation.decisions = [{"action": action, "resources": scopes, "allow": True,
                            "allowed_ids": scopes, "reason": "CURRENT_GRANT",
                            "policy_revision": REVISION, "projection": projection}]
    operation.sources = ([source] if isinstance(source, str) else source) if source else []
    handle = f"handle-{result_id}" if source else None
    result = Envelope[dict](
        trace_id=operation.trace_id, result_id=result_id, status="COMPLETE",
        as_of=date(2026, 9, 30),
        valuation_date=date(2026, 9, 30), known_as_of=NOW, calculated_at=NOW,
        entitlement_revision=1, policy_revision=REVISION, data={"summary": "Received quality"},
        source_refs=[handle] if handle else [],
    ).model_dump(mode="json")
    service.record(operation, result, handle)
    return result


@pytest.mark.anyio
async def test_ops_metadata_and_quality_batch_reauthorize_each_origin_and_union_only_allowed_scopes(
    access_service,
):
    service, conversation = access_service
    metric = await service.execute(OPS, NOW, "get_metric_definition", {"metric_id": "catalog"},
                                   conversation)
    assert metric["status"] == "COMPLETE"
    first = store_result(service, conversation, "r_quality_1", "view_quality", ["WC-HH-0001"],
                         "no_pii")
    second = store_result(service, conversation, "r_quality_2", "view_quality", ["WC-HH-0002"],
                          "no_pii")
    financial = store_result(service, conversation, "r_full_financial", "snapshot", ["WC-HH-0003"],
                             "full")
    operation = Operation(OPS, NOW, "trace-view", "r_view", "conversation_context", conversation)
    with read_snapshot(service.database) as connection:
        await service.authorize(operation, connection, "conversation_preflight", [])
        assert operation.projection == "no_pii"
        # A prior no_pii decision cannot prohibit public full-projection metadata.
        assert await service.resolve_result(operation, connection, metric["result_id"]) == metric
        assert await service.resolve_result(operation, connection, first["result_id"]) == first
        assert await service.resolve_result(operation, connection, second["result_id"]) == second
        payload_reads = []

        def authorize_sql(action, table, column, database, trigger):
            if table == "results" and column == "payload":
                payload_reads.append(column)
            return 0

        connection.set_authorizer(authorize_sql)
        with pytest.raises(RequestFailure) as failure:
            await service.resolve_result(operation, connection, financial["result_id"])
        assert failure.value.error.code == "NOT_AUTHORIZED"
        assert payload_reads == []
    assert operation.scopes == ["WC-HH-0001", "WC-HH-0002"]
    assert operation.projection == "no_pii"


@pytest.mark.anyio
@pytest.mark.parametrize("principal", [OPS, Principal("COMP-01", "compliance")])
async def test_quality_handle_keeps_origin_no_pii_even_for_full_lineage_reviewer(
    access_service, principal,
):
    service, conversation = access_service
    raw = canonical({"kind": "person", "id": "WC-PER-0001", "name": "PRIVATE_PERSON",
                     "note": "PRIVATE_NOTE", "details": {"account_title": "PRIVATE_TITLE"}})
    with write_transaction(service.database) as connection:
        connection.execute("INSERT INTO source_records VALUES (?,?,?,?,?,?,?,?)", (
            "SRC-private", "CRM", "private-record", "2026-09-30", utc_timestamp(NOW),
            utc_timestamp(NOW), raw, digest(raw),
        ))
    origin = store_result(service, conversation, "r_quality", "view_quality", ["WC-HH-0001"],
                          "no_pii", source="SRC-private")
    reply = await service.execute(principal, NOW, "get_data_provenance",
                                  {"reference": origin["source_refs"][0]})
    assert reply["status"] == "COMPLETE", reply
    serialized = json.dumps(reply)
    assert all(value not in serialized for value in (
        "PRIVATE_PERSON", "PRIVATE_NOTE", "PRIVATE_TITLE",
    ))
    with read_snapshot(service.database) as connection:
        original = connection.execute(
            "SELECT payload FROM source_records WHERE id='SRC-private'",
        ).fetchone()
        assert original[0] == raw
        assert connection.execute("SELECT projection FROM result_access WHERE result_id=?",
                                  (reply["result_id"],)).fetchone()[0] == "no_pii"


@pytest.mark.anyio
async def test_ops_cannot_resolve_own_full_financial_handle_via_quality_lineage_grant(
    access_service,
):
    service, conversation = access_service
    origin = store_result(service, conversation, "r_legacy_full", "snapshot", ["WC-HH-0001"],
                          "full", source="not-read-before-denial")
    reply = await service.execute(OPS, NOW, "get_data_provenance",
                                  {"reference": origin["source_refs"][0]})
    assert reply["error"]["code"] == "NOT_AUTHORIZED"
    assert reply["data"] is None


@pytest.mark.anyio
async def test_provenance_caps_display_without_shrinking_handle_or_scope(access_service):
    service, conversation = access_service
    source_ids = [f"SRC-{number:02}" for number in range(21)]
    raw = canonical({"kind": "person", "id": "WC-PER-0001", "name": "PRIVATE_PERSON"})
    with write_transaction(service.database) as connection:
        for source_id in source_ids:
            connection.execute("INSERT INTO source_records VALUES (?,?,?,?,?,?,?,?)", (
                source_id, "CRM", source_id, "2026-09-30", utc_timestamp(NOW),
                utc_timestamp(NOW), raw, digest(raw),
            ))
    origin = store_result(service, conversation, "r_bounded", "view_quality", ["WC-HH-0001"],
                          "no_pii", source=source_ids)
    handle = origin["source_refs"][0]
    reply = await service.execute(OPS, NOW, "get_data_provenance", {"reference": handle})
    assert reply["status"] == "COMPLETE", reply
    assert reply["data"]["item_count"] == 20
    assert reply["data"]["total_item_count"] == 21
    assert reply["data"]["truncated"] is True and reply["data"]["next_cursor"] is None
    assert [item["id"] for item in reply["data"]["items"]] == source_ids[:20]
    with read_snapshot(service.database) as connection:
        retained = connection.execute("SELECT source_ids FROM provenance_handles WHERE id=?",
                                      (handle,)).fetchone()[0]
        assert json.loads(retained) == source_ids
        assert [row[0] for row in connection.execute(
            "SELECT household_id FROM result_scopes WHERE result_id=?", (reply["result_id"],),
        )] == ["WC-HH-0001"]
