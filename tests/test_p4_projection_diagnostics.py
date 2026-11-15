"""P4 R2 complete identifiers and bounded relationship diagnostics."""

from datetime import UTC, datetime

import pytest
from test_secondary import DAY, NOW, OPS, SurfacePolicy, prepare, row

from wealthcontext.clock import FrozenClock
from wealthcontext.context_service import ContextService
from wealthcontext.feeds import FeedName, SourceSystem, build_delivery
from wealthcontext.ingestion import ingest_delivery
from wealthcontext.projections import REDACTED, project_no_pii
from wealthcontext.source_projection import SourceScope, project_source_record
from wealthcontext.storage import canonical, digest
from wealthcontext.telemetry import create_provider


@pytest.mark.parametrize("foreign", [
    "WC-HH-own:branch", "WC-AC-own:branch", "WC-HH-own:WC-HH-other",
    "WC-AC-own:WC-AC-other",
])
def test_colon_extension_of_authorized_id_is_not_authorized_metadata(foreign):
    payload = canonical({"kind": "household", "id": "WC-HH-own", "name": "Synthetic"})
    record = {"id": "SRC-record", "source_record_id": f"receipt:{foreign}",
              "payload": payload, "payload_hash": digest(payload)}
    scope = SourceScope(frozenset({"WC-HH-own", "WC-HH-other"}),
                        frozenset({"WC-AC-own", "WC-AC-other"}), frozenset())
    projected = project_source_record(record, scope)
    assert foreign not in canonical(projected)
    assert projected["source_record_id"] == "receipt:[REDACTED]"
    assert projected["payload"]["id"] == "WC-HH-own"


def test_exact_authorized_colon_identifier_and_structured_components_are_preserved():
    owned = "WC-HH-own:branch"
    payload = canonical({"kind": "household", "id": owned, "name": "Synthetic"})
    record = {"id": "SRC-record", "source_record_id": f"receipt:{owned}",
              "payload": payload, "payload_hash": digest(payload)}
    projected = project_source_record(record, SourceScope(frozenset({owned}), frozenset(),
                                                         frozenset()))
    assert projected["source_record_id"] == f"receipt:{owned}"
    assert projected["payload"]["id"] == owned
    components = {"components": ["WC-AC-own:WC-SEC-1", "WC-AC-own:USD"]}
    assert project_no_pii(components) == components


@pytest.mark.parametrize("detail", [
    {"reason": "LEGAL_OWNER_NOT_CURRENT_MEMBER", "membership_source": "CRM",
     "ownership_source": "CUSTODY"},
    {"reason": "CRM_CUSTODY_DISAGREEMENT", "authoritative_source": "CUSTODY",
     "fields": ["title", "registration", "status", "owners"]},
    {"reason": "AMBIGUOUS_CRM_OWNERSHIP", "authoritative_source": "CUSTODY"},
    {"reason": "AMBIGUOUS_CUSTODY_OWNERSHIP"},
    {"reason": "MISSING_CUSTODY_OWNERSHIP", "source": "CUSTODY", "entity_type": "ownership",
     "affects_valuation": False},
    {"reason": "AMBIGUOUS_TRANSACTIONS"},
    {"entity_type": "reporting_household", "authoritative_source": "CRM"},
])
def test_household_machine_diagnostics_survive_no_pii(detail):
    assert project_no_pii(detail) == detail


def test_machine_field_names_do_not_accept_unstructured_private_strings():
    assert project_no_pii({"fields": ["title", "Private person"],
                           "ownership_source": "Private household"}) == {
        "fields": ["title", REDACTED], "ownership_source": REDACTED,
    }


@pytest.mark.anyio
@pytest.mark.parametrize("case,reason", [
    ("legal_owner", "LEGAL_OWNER_NOT_CURRENT_MEMBER"),
    ("crm_disagreement", "CRM_CUSTODY_DISAGREEMENT"),
    ("ambiguous_crm", "AMBIGUOUS_CRM_OWNERSHIP"),
    ("ambiguous_custody", "AMBIGUOUS_CUSTODY_OWNERSHIP"),
    ("missing_custody", "MISSING_CUSTODY_OWNERSHIP"),
    ("ambiguous_transactions", "AMBIGUOUS_TRANSACTIONS"),
])
async def test_received_household_diagnostics_reach_ops_quality_conflicts_and_provenance(
    tmp_path, case, reason,
):
    database = tmp_path / "diagnostics.sqlite"
    prepare(database, count=2)
    received = FrozenClock(datetime(2026, 10, 1, 12, tzinfo=UTC))

    def receive(system: SourceSystem, feed: FeedName, records: list[dict]):
        return ingest_delivery(database, build_delivery(source_system=system, feed=feed,
                                                        business_date=DAY, records=records),
                               received)

    owner = row("ownership", "changed-owner", custodian="A", account_number="00420001",
                title="Private title 1", registration="INDIVIDUAL", status="OPEN",
                owners=[{"entity_id": "WC-PER-0001", "entity_type": "person", "role": "owner"}])
    if case == "legal_owner":
        receive("CRM", "crm", [row("membership", "ended-member", person_id="WC-PER-0001",
                                   household_id="WC-HH-0001", valid_to=str(DAY))])
    elif case == "crm_disagreement":
        receive("CRM", "crm", [{**owner, "status": "CLOSED"}])
    elif case == "ambiguous_crm":
        receive("CRM", "crm", [owner, {**owner, "status": "CLOSED"}])
    elif case == "ambiguous_custody":
        receive("CUSTODY-A", "custody_accounts", [owner, {**owner, "status": "CLOSED"}])
    elif case == "missing_custody":
        receive("CUSTODY-A", "custody_accounts", [{**owner, "valid_to": str(DAY)}])
    else:
        transaction = row("transaction", "ambiguous-tx", custodian="A",
                          account_number="00420001", id="WC-TXN-AMBIGUOUS",
                          txn_type="DEPOSIT", trade_date=str(DAY), settle_date=str(DAY),
                          quantity_delta="0", cash_delta_local="1", currency="USD",
                          status="ACTIVE")
        receive("PAS-A", "transactions", [transaction, {**transaction, "cash_delta_local": "2"}])

    telemetry = create_provider(tmp_path / "traces.jsonl")
    try:
        service = ContextService(database, SurfacePolicy(), telemetry.get_tracer("diagnostics"))
        quality = await service.execute(OPS, NOW, "get_data_quality_report", {"as_of": str(DAY)})
        assert quality["status"] != "ERROR", quality
        details = [flag["detail"] for flag in quality["quality_flags"]
                   if flag["subject"] == "WC-AC-0001" and flag["detail"].get("reason") == reason]
        assert len(details) == 1
        if case == "legal_owner":
            assert details[0]["membership_source"] == "CRM"
            assert details[0]["ownership_source"] == "CUSTODY"
        if case in {"crm_disagreement", "ambiguous_crm"}:
            assert details[0]["authoritative_source"] == "CUSTODY"
        if case == "crm_disagreement":
            assert details[0]["fields"] == ["status"]
        conflicts = await service.execute(OPS, NOW, "conflicts",
                                          {"household_id": "WC-HH-0001", "as_of": str(DAY)})
        assert conflicts["status"] != "ERROR", conflicts
        matches = [item for item in conflicts["data"]["items"]
                   if item["detail"].get("reason") == reason]
        assert len(matches) == (0 if case == "missing_custody" else 1)
        lineage = await service.execute(OPS, NOW, "get_data_provenance",
                                        {"reference": quality["source_refs"][0]})
        assert lineage["status"] == "COMPLETE", lineage
        assert lineage["data"]["items"]
        assert "Private" not in canonical([quality, conflicts, lineage])
        assert all(item["source_record_id"] == REDACTED for item in lineage["data"]["items"])
    finally:
        telemetry.shutdown()
