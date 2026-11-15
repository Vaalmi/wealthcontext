"""Provenance is authorized-result evidence, never an unrestricted raw feed dump."""

import copy
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import wealthcontext.context_service as service_module
from wealthcontext.clock import utc_timestamp
from wealthcontext.context_service import ContextService, ResultScopeEvidence
from wealthcontext.contracts import Envelope
from wealthcontext.database import migrate, write_transaction
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyDecision
from wealthcontext.source_projection import REDACTED, SourceScope, project_source_record
from wealthcontext.storage import canonical, digest, migrate_business, read_snapshot
from wealthcontext.telemetry import create_provider

NOW = datetime(2035, 1, 1, tzinfo=UTC)
OWN = {"id": "WC-AC-0001", "title": "Synthetic owned", "custodian": "A",
       "account_number": "00420017"}
HELD = {"id": "WC-AC-0031", "title": "Synthetic held away", "custodian": "A",
        "account_number": "00420031"}
OTHER = {"id": "WC-AC-0002", "title": "Synthetic other", "custodian": "A",
         "account_number": "00420018"}


def origin(accounts: list[dict] | None = None, excluded: list[dict] | None = None) -> dict:
    return Envelope[dict].model_validate({
        "trace_id": "trace-origin", "result_id": "r_origin", "status": "COMPLETE",
        "as_of": "2026-09-30", "valuation_date": "2026-09-30", "known_as_of": NOW,
        "calculated_at": NOW, "entitlement_revision": 0, "policy_revision": "sha256:" + "a" * 64,
        "data": {"household_id": "WC-HH-0001",
                 "accounts": accounts if accounts is not None else [OWN],
                 "excluded_accounts": excluded if excluded is not None else [HELD]},
    }).model_dump(mode="json")


def stored(payload: Any, record_id: str = "received-record") -> dict:
    text = canonical(payload)
    return {
        "id": "SRC-opaque", "source_system": "PAS-A", "source_record_id": record_id,
        "business_date": "2026-09-30", "recorded_at": utc_timestamp(NOW),
        "imported_at": utc_timestamp(NOW), "payload": text, "payload_hash": digest(text),
    }


def scope_evidence(connection, payload: str, household_ids: list[str]) -> None:
    evidence = ResultScopeEvidence(result_id="r_origin", result_payload_hash=digest(payload),
                                   action="snapshot", projection="full",
                                   household_ids=sorted(household_ids))
    text = canonical(evidence.model_dump(mode="json"))
    connection.execute("INSERT OR REPLACE INTO result_scope_integrity VALUES (?,1,?,?)",
                       ("r_origin", text, digest(text)))
    # This hand-issued fixture supplies both immutable proofs explicitly. Production
    # migration must never synthesize issuance proof from these mutable indexes.
    handle = connection.execute(
        "SELECT id,principal_id,source_ids FROM provenance_handles WHERE result_id='r_origin'",
    ).fetchone()
    issuance = canonical({
        "result_id": "r_origin", "result_payload_hash": digest(payload),
        "principal_id": handle["principal_id"], "conversation_id": None,
        "provenance_handle_id": handle["id"], "source_ids": json.loads(handle["source_ids"]),
    })
    connection.execute("INSERT OR REPLACE INTO result_issuance_integrity VALUES (?,1,?,?)",
                       ("r_origin", issuance, digest(issuance)))


def receipt() -> dict:
    return {"file_received": True, "upstream_status": "SUCCESS", "record_count": 107, "manifest": {
        "source_system": "PAS-A", "feed": "pas_snapshots", "business_date": "2026-09-30",
        "checksum": "b" * 64, "record_count": 107, "account_count": 3, "rejected_records": 99,
        "expected_account_ids": [OWN["id"], HELD["id"], OTHER["id"]],
        "accounts": [{"account_id": item["id"], "custodian": item["custodian"],
                      "account_number": item["account_number"], "record_count": count,
                      "digest": str(count) * 64}
                     for item, count in [(OWN, 2), (HELD, 0), (OTHER, 105)]],
    }}


def test_nested_notes_are_omitted_and_original_hash_is_clearly_distinguished() -> None:
    record = stored({"kind": "person", "id": "WC-PER-0001", "name": "Synthetic person",
                     "note": "SECRET-NOTE", "details": {"free_text_notes": "SECRET-NOTE",
                     "nested": [{"Notes": "SECRET-NOTE", "source": "CRM"}]}})
    before = copy.deepcopy(record)
    projected = project_source_record(record, SourceScope.from_result(origin(), ["WC-HH-0001"]))
    assert "SECRET-NOTE" not in canonical(projected)
    assert projected["payload"]["details"]["nested"] == [{"source": "CRM"}]
    assert projected["id"] == record["id"]
    assert projected["payload_hash"] == record["payload_hash"]
    assert projected["payload_hash_scope"] == "stored_received_record"
    assert projected["projection"]["payload_form"] == "authorized_projection"
    projected_hash = digest(canonical(projected["payload"]))
    assert projected["projection"]["projected_payload_hash"] == projected_hash
    assert projected["projection"]["projected_payload_hash"] != record["payload_hash"]
    assert record == before


def test_snapshot_manifest_filters_other_accounts_and_labels_retained_expectation_counts() -> None:
    record = stored(receipt())
    projected = project_source_record(record, SourceScope.from_result(origin(), ["WC-HH-0001"]))
    manifest = projected["payload"]["manifest"]
    assert manifest["expected_account_ids"] == [OWN["id"], HELD["id"]]
    assert [account["account_id"] for account in manifest["accounts"]] == [OWN["id"], HELD["id"]]
    assert manifest["record_count"] == 2
    assert "record_count" not in projected["payload"]
    assert "account_count" not in manifest and "rejected_records" not in manifest
    assert manifest["checksum"] == "b" * 64
    assert manifest["accounts"][0]["digest"] == "2" * 64
    assert manifest["projection"]["record_count_scope"] == "retained_account_expectations"
    assert manifest["projection"]["checksum_scope"] == "original_received_file"
    assert OTHER["id"] not in canonical(projected)
    assert OTHER["account_number"] not in canonical(projected)
    assert json.loads(record["payload"])["manifest"]["record_count"] == 107


def test_retrospective_crosswalk_projects_external_manifest_with_explicit_mapping_label() -> None:
    raw = receipt()
    raw["manifest"]["accounts"][0]["account_id"] = OTHER["id"]
    raw["manifest"]["expected_account_ids"][0] = OTHER["id"]
    record = stored(raw)
    before = copy.deepcopy(record)
    scope = SourceScope.from_result(origin(accounts=[OWN], excluded=[]), ["WC-HH-0001"])
    projected = project_source_record(record, scope)
    manifest = projected["payload"]["manifest"]
    assert manifest["expected_account_ids"] == [OWN["id"]]
    assert manifest["record_count"] == 2
    assert manifest["accounts"][0]["account_number"] == OWN["account_number"]
    assert manifest["accounts"][0]["account_id"] == OWN["id"]
    assert manifest["accounts"][0]["received_account_id"] == REDACTED
    assert manifest["accounts"][0]["account_id_scope"] == "origin_result_mapping"
    assert OTHER["id"] not in canonical(projected)
    assert OTHER["account_number"] not in canonical(projected)
    assert record == before


@pytest.mark.parametrize("changes", [
    {"feed": "crm"}, {"accounts": None},
    {"accounts": [{"account_id": OWN["id"], "custodian": "A",
                   "account_number": OWN["account_number"]}]},
])
def test_global_or_ambiguous_manifest_totals_are_omitted(changes: dict) -> None:
    payload = receipt()
    payload["manifest"].update(changes)
    scope = SourceScope.from_result(origin(), ["WC-HH-0001"])
    projected = project_source_record(stored(payload), scope)
    assert "record_count" not in projected["payload"]["manifest"]
    assert projected["payload"]["manifest"]["projection"]["record_count_scope"] == "omitted"


def test_owned_grouping_keeps_account_but_redacts_counterpart_everywhere() -> None:
    record = stored({
        "kind": "pas_grouping", "custodian": "A", "account_number": OWN["account_number"],
        "household_id": "WC-HH-0002", "detail": {"counterpart_household_id": "WC-HH-0002"},
    }, "grouping-WC-AC-0001-WC-HH-0002")
    projected = project_source_record(record, SourceScope.from_result(origin(), ["WC-HH-0001"]))
    assert projected["payload"]["kind"] == "pas_grouping"
    assert projected["payload"]["account_number"] == OWN["account_number"]
    assert projected["payload"]["household_id"] == REDACTED
    assert "WC-HH-0002" not in canonical(projected)


@pytest.mark.parametrize("payload", [
    {"account_id": OTHER["id"], "amount": "9000000.00"},
    {"kind": "account", "id": OTHER["id"], "name": "Private account"},
    {"kind": "position", "custodian": "A", "account_number": OTHER["account_number"],
     "quantity": "999999"},
    {"kind": "household", "id": "WC-HH-0002", "name": "Private household"},
    {"kind": "compound", "detail": {"account_id": OTHER["id"]}, "title": "Private account"},
])
def test_citation_alone_never_releases_a_private_counterpart_record(payload: dict) -> None:
    record = stored(payload, "record-WC-AC-0002")
    projected = project_source_record(record, SourceScope.from_result(origin(), ["WC-HH-0001"]))
    assert projected["payload"] == REDACTED
    assert projected["source_record_id"] == REDACTED
    assert OTHER["id"] not in canonical(projected)
    assert "Private" not in canonical(projected)


def test_legacy_p1_explicit_account_ids_work_but_unknown_external_keys_fail_closed() -> None:
    legacy = SourceScope.from_result(origin(accounts=[{"id": OWN["id"], "title": OWN["title"]}],
                                           excluded=[]), ["WC-HH-0001"])
    explicit = stored({"account_id": OWN["id"], "quantity": "10"})
    assert project_source_record(explicit, legacy)["payload"]["quantity"] == "10"
    external = stored({"custodian": "A", "account_number": OWN["account_number"], "quantity": "10"})
    assert project_source_record(external, legacy)["payload"] == REDACTED


def test_multiple_authorized_scopes_preserve_only_their_household_and_account_ids() -> None:
    scope = SourceScope.from_result(origin(accounts=[OWN, OTHER]), ["WC-HH-0001", "WC-HH-0002"])
    record = stored({"account_id": OTHER["id"], "household_id": "WC-HH-0002",
                     "counterpart_household_id": "WC-HH-0003"})
    payload = project_source_record(record, scope)["payload"]
    assert payload["account_id"] == OTHER["id"] and payload["household_id"] == "WC-HH-0002"
    assert payload["counterpart_household_id"] == REDACTED


def test_tampered_stored_source_is_not_projected_under_a_stale_hash() -> None:
    record = stored({"account_id": OWN["id"], "quantity": "10"})
    record["payload"] = canonical({"account_id": OWN["id"], "quantity": "20"})
    with pytest.raises(ValueError, match="hash"):
        project_source_record(record, SourceScope.from_result(origin(), ["WC-HH-0001"]))


class LocalPolicy(PolicyClient):
    def __init__(self, allowed_households: set[str]):
        super().__init__("http://unused", "sha256:" + "a" * 64)
        self.allowed_households = allowed_households
        self.calls: list[tuple[str, list[str]]] = []

    async def decide(self, principal, action, resources, facts, revision,
                     request_business_date) -> PolicyDecision:
        self.calls.append((action, resources))
        allow = (principal.role in {"advisor", "compliance"}
                 and set(resources) <= self.allowed_households)
        return PolicyDecision(allow=allow, allowed_ids=resources if allow else [],
                              projection="full",
                              reason="CURRENT_GRANT" if allow else "NO_CURRENT_GRANT",
                              policy_revision=self.expected_revision, entitlement_revision=revision)


@pytest.fixture
def evidence(tmp_path: Path):
    database = tmp_path / "evidence.sqlite"
    migrate(database)
    migrate_business(database)
    records = {
        "SRC-receipt": stored(receipt(), "receipt"),
        "SRC-note": stored({"kind": "person", "id": "WC-PER-1", "name": "Synthetic person",
                            "note": "PRIVATE-NOTE"}, "person"),
        "SRC-group": stored({"kind": "pas_grouping", "custodian": "A",
                             "account_number": OWN["account_number"], "household_id": "WC-HH-0002"},
                            "group"),
        "SRC-foreign": stored({"account_id": OTHER["id"], "amount": "9000000"}, "foreign"),
    }
    initial = origin()
    with write_transaction(database) as connection:
        connection.execute("INSERT INTO entitlement_revisions VALUES(0,?)", (utc_timestamp(NOW),))
        for source_id, record in records.items():
            connection.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?)", (
                source_id, record["source_system"], record["source_record_id"],
                record["business_date"], record["recorded_at"], record["imported_at"],
                record["payload"], record["payload_hash"],
            ))
        text = canonical(initial)
        connection.execute("INSERT INTO results VALUES(?,?,?,?,?,?,?)", (
            "r_origin", "trace-origin", "ADV-01", None, text, digest(text), utc_timestamp(NOW),
        ))
        connection.execute("INSERT INTO result_access VALUES (?, 'snapshot', 'full', 1)",
                           ("r_origin",))
        connection.execute("INSERT INTO result_scopes VALUES(?,?)", ("r_origin", "WC-HH-0001"))
        connection.execute("INSERT INTO provenance_handles VALUES(?,?,?,?)", (
            "h_origin", "r_origin", "ADV-01", canonical(sorted(records)),
        ))
        scope_evidence(connection, text, ["WC-HH-0001"])
    policy = LocalPolicy({"WC-HH-0001"})
    provider = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, policy, provider.get_tracer("source-projection-test"))
    yield service, policy, records
    provider.shutdown()


@pytest.mark.anyio
async def test_authorized_service_provenance_keeps_raw_database_unchanged(evidence):
    service, policy, records = evidence
    result = await service.execute(Principal("ADV-01", "advisor"), NOW, "get_data_provenance",
                                   {"reference": "h_origin"})
    assert result["status"] == "COMPLETE", result
    assert policy.calls == [("provenance_preflight", []), ("snapshot", ["WC-HH-0001"]),
                            ("provenance", ["WC-HH-0001"])]
    serialized = canonical(result)
    assert "PRIVATE-NOTE" not in serialized
    assert "WC-HH-0002" not in serialized and OTHER["id"] not in serialized
    items = {item["id"]: item for item in result["data"]["items"]}
    assert items["SRC-foreign"]["payload"] == REDACTED
    assert items["SRC-group"]["payload"]["kind"] == "pas_grouping"
    assert items["SRC-receipt"]["payload"]["manifest"]["record_count"] == 2
    with read_snapshot(service.database) as connection:
        for row in connection.execute("SELECT * FROM source_records"):
            assert row["payload"] == records[row["id"]]["payload"]
            assert row["payload_hash"] == records[row["id"]]["payload_hash"]


@pytest.mark.anyio
async def test_compliance_resolves_another_principals_handle_with_recorded_scopes(evidence):
    service, policy, _ = evidence
    policy.allowed_households.add("WC-HH-0002")
    with write_transaction(service.database) as connection:
        expanded = canonical(origin(accounts=[OWN, OTHER]))
        connection.execute("UPDATE results SET payload=?,payload_hash=? WHERE id='r_origin'",
                           (expanded, digest(expanded)))
        connection.execute("INSERT INTO result_scopes VALUES(?,?)", ("r_origin", "WC-HH-0002"))
        scope_evidence(connection, expanded, ["WC-HH-0001", "WC-HH-0002"])
    result = await service.execute(Principal("COMP-01", "compliance"), NOW, "get_data_provenance",
                                   {"reference": "h_origin"})
    assert result["status"] == "COMPLETE", result
    items = {item["id"]: item for item in result["data"]["items"]}
    assert items["SRC-group"]["payload"]["household_id"] == "WC-HH-0002"
    assert items["SRC-foreign"]["payload"]["account_id"] == OTHER["id"]
    assert "PRIVATE-NOTE" not in canonical(result)


@pytest.mark.anyio
@pytest.mark.parametrize("corrupted_table", ["results", "source_records"])
async def test_service_refuses_tampered_origin_or_source_hashes(
    evidence, corrupted_table: str,
) -> None:
    service, _, _ = evidence
    with write_transaction(service.database) as connection:
        connection.execute(f"UPDATE {corrupted_table} SET payload='{{}}'")
    result = await service.execute(Principal("ADV-01", "advisor"), NOW, "get_data_provenance",
                                   {"reference": "h_origin"})
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "INTERNAL" and result["data"] is None


@pytest.mark.anyio
async def test_current_scope_denial_precedes_origin_and_source_lookup(evidence, monkeypatch):
    service, policy, _ = evidence
    policy.allowed_households.clear()
    queries: list[str] = []

    @contextmanager
    def observed_snapshot(path: Path):
        with read_snapshot(path) as connection:
            connection.set_trace_callback(queries.append)
            yield connection

    monkeypatch.setattr(service_module, "read_snapshot", observed_snapshot)
    result = await service.execute(Principal("ADV-01", "advisor"), NOW, "get_data_provenance",
                                   {"reference": "h_origin"})
    assert result["error"]["code"] == "NOT_AUTHORIZED"
    assert not any("SELECT payload,payload_hash FROM results" in query for query in queries)
    assert not any("SELECT * FROM source_records" in query for query in queries)
