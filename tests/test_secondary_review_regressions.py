"""Received cutoff inventories and post-projection diagnostic contract regressions."""

import copy
import json
from datetime import UTC, date, datetime

import pytest
from test_secondary import (
    COMPLIANCE,
    DAY,
    NOW,
    OPS,
    RECEIVED,
    SurfacePolicy,
    prepare,
    row,
)

from wealthcontext.clock import FrozenClock, utc_timestamp
from wealthcontext.context_service import ContextService
from wealthcontext.contracts import QualityFlag
from wealthcontext.database import write_transaction
from wealthcontext.feeds import build_delivery
from wealthcontext.ingestion import ingest_delivery
from wealthcontext.projections import REDACTED, project_envelope, project_no_pii
from wealthcontext.secondary import _flags, _ids, _listed
from wealthcontext.source_projection import SourceScope, project_source_record
from wealthcontext.storage import canonical, digest, read_snapshot
from wealthcontext.telemetry import create_provider

CORRECTION = datetime(2026, 10, 1, 12, tzinfo=UTC)


def shorten_households(database):
    records = [row("household", f"hh-{number}", id=f"WC-HH-{number:04}",
                   name=f"Private household {number:04}", valid_to=end)
               for number, end in [(2, "2026-09-29"), (3, "2026-09-30")]]
    ingest_delivery(database, build_delivery(source_system="CRM", feed="crm", business_date=DAY,
                                             records=records), FrozenClock(CORRECTION))


def test_identifier_inventory_obeys_received_interval_corrections_and_ambiguous_activity(tmp_path):
    database = tmp_path / "inventory.sqlite"
    prepare(database)
    shorten_households(database)
    tied = [row("household", "ambiguous", id="WC-HH-TIED", name="Private tied", valid_to=end)
            for end in ("2026-09-29", "9999-12-31")]
    ingest_delivery(database, build_delivery(source_system="CRM", feed="crm", business_date=DAY,
                                             records=tied), FrozenClock(CORRECTION))
    seed = canonical({"id": "WC-HH-seed", "name": "Private seed"})
    with write_transaction(database) as connection:
        connection.execute(
            "INSERT INTO source_records "
            "(id,source_system,source_record_id,business_date,recorded_at,imported_at,payload,"
            "payload_hash) VALUES (?,?,?,?,?,?,?,?)",
            ("seed-only", "CRM", "seed-only", str(DAY), utc_timestamp(RECEIVED),
             utc_timestamp(RECEIVED), seed, digest(seed)),
        )
        connection.execute("INSERT INTO facts VALUES (?,?,?,?,?,?,?)",
                           ("household", "WC-HH-seed", "2026-09-14", "9999-12-31",
                            utc_timestamp(RECEIVED), "seed-only", seed))
    with read_snapshot(database) as connection:
        assert _ids(connection, DAY, RECEIVED) == ["WC-HH-0001", "WC-HH-0002", "WC-HH-0003"]
        statements = []
        connection.set_trace_callback(statements.append)
        assert _ids(connection, DAY, CORRECTION) == ["WC-HH-0001", "WC-HH-TIED"]
        assert all("payload" not in query.casefold() for query in statements)
        assert _ids(connection, date(2026, 9, 28), CORRECTION) == [
            "WC-HH-0001", "WC-HH-0002", "WC-HH-0003", "WC-HH-TIED",
        ]


@pytest.mark.anyio
async def test_ended_households_do_not_degrade_quality_and_feed_decisions_record_actual_scope(
    tmp_path,
):
    database = tmp_path / "quality.sqlite"
    prepare(database)
    shorten_households(database)
    tracer = create_provider(tmp_path / "trace.jsonl")
    try:
        service = ContextService(database, SurfacePolicy(), tracer.get_tracer("test"))
        result = await service.execute(OPS, NOW, "get_data_quality_report", {"as_of": str(DAY)})
        assert result["status"] == "COMPLETE", result
        assert result["data"]["evaluated_household_count"] == 1
        assert result["data"]["incomplete_household_count"] == 0
        for principal in (OPS, COMPLIANCE):
            feeds = await service.execute(principal, NOW, "feed_runs", {"as_of": str(DAY)})
            assert feeds["status"] == "COMPLETE", feeds
            with read_snapshot(database) as connection:
                decisions = connection.execute(
                    "SELECT id,resources FROM decisions WHERE trace_id=? AND action='feed_runs'",
                    (feeds["trace_id"],),
                ).fetchall()
                assert [json.loads(item["resources"]) for item in decisions] == [
                    [], ["WC-HH-0001"],
                ]
                decision_id = decisions[-1]["id"]
                allowed = connection.execute(
                    "SELECT household_id FROM decision_authorized_resources WHERE decision_id=?",
                    (decision_id,),
                ).fetchall()
                assert [item[0] for item in allowed] == ["WC-HH-0001"]
    finally:
        tracer.shutdown()


@pytest.mark.parametrize("detail", [
    {"source": "security", "reason": "HOUSEHOLD_UNAVAILABLE_AT_CUTOFFS"},
    {"source": "price", "reason": "FIX_OUTSIDE_CUTOFFS"},
    {"source": "CUSTODY", "reason": "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST",
     "components": ["WC-AC-lower_case:WC-SEC-1", "WC-AC-1:USD"]},
    {"price_date": "2026-09-29", "age_business_days": 1, "tolerance_business_days": 0},
    {"authority": "CUSTODY", "reasons": ["CURRENCY", "CLOSE_DATE", "PRICE"],
     "custody_price": "20.00", "custody_currency": "USD", "custody_price_date": "2026-09-30",
     "pas_price": {"price": "21.00", "currency": "CAD", "price_date": "2026-09-29"}},
])
def test_no_pii_retains_bounded_machine_diagnostics(detail):
    assert project_no_pii(detail) == detail
    assert project_no_pii({"source": "Private person", "reason": "Private household"}) == {
        "source": REDACTED, "reason": REDACTED,
    }


def test_projected_flags_are_deduplicated_and_sorted_without_mutating_evidence():
    original = {"data": {}, "quality_flags": [
        {"code": "SOURCE_UNAVAILABLE", "subject": "WC-AC-1:USD", "severity": "warning",
         "detail": {"unknown": "Private Z"}},
        {"code": "SOURCE_UNAVAILABLE", "subject": "WC-AC-1:USD", "severity": "warning",
         "detail": {"source": "price"}},
        {"code": "SOURCE_UNAVAILABLE", "subject": "WC-AC-1:USD", "severity": "warning",
         "detail": {"unknown": "Private A"}},
    ]}
    before = copy.deepcopy(original)
    projected = project_envelope(original, "no_pii")
    assert [item["detail"] for item in projected["quality_flags"]] == [{"source": "price"}, {}]
    assert original == before


def test_secondary_flags_project_before_sort_deduplication_counts_and_top_twenty():
    flags = [QualityFlag(code="SOURCE_UNAVAILABLE", subject="WC-AC-1:USD", severity="warning",
                         detail={"unknown": f"Private person {number}"}) for number in range(30)]
    flags.extend(QualityFlag(code="SOURCE_UNAVAILABLE", subject="WC-AC-1:USD", severity="warning",
                             detail={"source": source}) for source in ("security", "price"))
    before = [item.model_dump(mode="json") for item in flags]
    listed = _listed(_flags(flags, "no_pii"), "subject ASC, code ASC, detail ASC")
    assert (listed.item_count, listed.total_item_count, listed.truncated) == (3, 3, False)
    assert [item.detail for item in listed.items] == [
        {"source": "price"}, {"source": "security"}, {},
    ]
    assert [item.model_dump(mode="json") for item in flags] == before


@pytest.mark.anyio
async def test_trace_scope_index_loss_fails_closed_before_current_scope_authorization(tmp_path):
    database = tmp_path / "trace.sqlite"
    prepare(database)
    tracer = create_provider(tmp_path / "trace.jsonl")
    policy = SurfacePolicy()
    try:
        service = ContextService(database, policy, tracer.get_tracer("test"))
        origin = await service.execute(COMPLIANCE, NOW, "search_households",
                                       {"name_query": "Private"})
        with write_transaction(database) as connection:
            connection.execute("DELETE FROM result_scopes WHERE result_id=? AND household_id=?",
                               (origin["result_id"], "WC-HH-0002"))
        policy.calls.clear()
        result = await service.execute(COMPLIANCE, NOW, "trace", {"trace_id": origin["trace_id"]})
        assert result["status"] == "ERROR" and result["data"] is None
        assert result["error"]["code"] == "INTERNAL"
        assert [action for action, _ in policy.calls] == ["trace_preflight"]
    finally:
        tracer.shutdown()


@pytest.mark.parametrize("foreign", ["WC-HH-target-only", "WC-AC-other_2.x@branch"])
def test_full_source_projection_redacts_valid_lowercase_punctuated_foreign_ids(foreign):
    payload = canonical({"kind": "household", "id": "WC-HH-own", "name": "Synthetic"})
    record = {"id": "SRC-opaque", "source_record_id": f"receipt:{foreign}:WC-HH-own",
              "payload": payload, "payload_hash": digest(payload)}
    result = project_source_record(record, SourceScope(frozenset({"WC-HH-own"}),
                                                       frozenset(), frozenset()))
    assert foreign not in canonical(result)
    # An opaque colon-separated chain can itself be a valid single identifier.
    # The complete unrecognized token is redacted; an authorized prefix/suffix
    # cannot establish that this was actually a composite rather than a foreign ID.
    assert result["source_record_id"] == "receipt:[REDACTED]"
    assert result["payload_hash"] == record["payload_hash"]
