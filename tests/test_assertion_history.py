"""Raw-record deduplication must preserve later assertions of an earlier value."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from wealthcontext.clock import FrozenClock, utc_timestamp
from wealthcontext.context_service import ContextService
from wealthcontext.database import migrate
from wealthcontext.facts import fact_view
from wealthcontext.feeds import build_delivery
from wealthcontext.identity import Principal
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.policy import PolicyClient
from wealthcontext.storage import entitlements, migrate_business, read_snapshot
from wealthcontext.telemetry import create_provider

FILE_DAY = date(2026, 10, 1)
HISTORICAL_DAY = date(2026, 9, 30)
REQUEST_DAY = date(2026, 10, 2)
TIMES = [datetime(2026, 10, 2, hour, tzinfo=UTC) for hour in (12, 13, 14)]
HOUSEHOLD = "WC-HH-0001"
PERSON = "WC-PER-0001"
Kind = Literal["person", "membership"]


def row(kind: str, record_id: str, **fields: Any) -> dict:
    return {"kind": kind, "source_record_id": record_id, "valid_from": "2026-09-14",
            "valid_to": "9999-12-31", **fields}


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "assertions.sqlite"
    migrate(path)
    migrate_business(path)
    migrate_feeds(path)
    initial = build_delivery(source_system="CRM", feed="crm", business_date=HISTORICAL_DAY,
                             records=[
        row("household", "household-one", id=HOUSEHOLD, name="Synthetic first household"),
        row("household", "household-two", id="WC-HH-0002", name="Synthetic control household"),
        row("advisor", "advisor", id="ADV-03", name="Synthetic advisor"),
        row("person", "context-person", id=PERSON, name="Synthetic context person",
            external_person_key="PERSON-1"),
        row("membership", "context-membership", person_id=PERSON, household_id=HOUSEHOLD),
    ])
    ingest_delivery(path, initial, FrozenClock(TIMES[0] - timedelta(days=1)))
    return path


def receive(database: Path, assertions: list[dict], stage: int) -> str:
    # Keep the assertion's source_record_id AND file business date unchanged.
    # This unrelated changed row ensures V1's third file is not a whole-run replay.
    control = row("household", "control-name", id="WC-HH-0002",
                  name=f"Synthetic control revision {stage}")
    packet = build_delivery(source_system="CRM", feed="crm", business_date=FILE_DAY,
                            records=[*assertions, control])
    return ingest_delivery(database, packet, FrozenClock(TIMES[stage - 1]))


def grant(*, active: bool) -> dict:
    return row("advisor_covers", "grant-stable", advisor_id="ADV-03", household_id=HOUSEHOLD,
               valid_to="9999-12-31" if active else "2026-10-01")


def assert_raw_dedup_with_three_assertions(database: Path, record_id: str, runs: list[str]) -> None:
    with read_snapshot(database) as connection:
        raw = connection.execute(
            "SELECT id,recorded_at,imported_at,payload FROM source_records "
            "WHERE source_system='CRM' AND source_record_id=? AND business_date=? "
            "ORDER BY recorded_at",
            (record_id, FILE_DAY.isoformat()),
        ).fetchall()
        assertions = connection.execute(
            "SELECT f.recorded_at,f.source_id FROM facts f "
            "JOIN source_records s ON s.id=f.source_id "
            "WHERE s.source_record_id=? AND s.business_date=? ORDER BY f.recorded_at",
            (record_id, FILE_DAY.isoformat()),
        ).fetchall()
        assert len(raw) == 2
        assert [item["recorded_at"] for item in raw] == [utc_timestamp(time) for time in TIMES[:2]]
        assert raw[0]["imported_at"] == utc_timestamp(TIMES[0])
        assertion_times = [item["recorded_at"] for item in assertions]
        assert assertion_times == [utc_timestamp(time) for time in TIMES]
        assert assertions[0]["source_id"] == assertions[2]["source_id"] == raw[0]["id"]
        assert assertions[1]["source_id"] != assertions[0]["source_id"]
        assert len(set(runs)) == 3
        for run in runs:
            assert connection.execute("SELECT status FROM feed_runs WHERE id=?", (run,)
                                      ).fetchone()[0] == "SUCCESS"


def test_restated_revocation_deduplicates_raw_value_but_advances_current_entitlements(
    database: Path,
) -> None:
    runs = []
    for stage, active in enumerate([False, True, False], start=1):
        runs.append(receive(database, [grant(active=active)], stage))
        with read_snapshot(database) as connection:
            revision, facts = entitlements(connection)
            assert revision == stage
            assert len(facts) == 1
            assert facts[0]["subject"] == "ADV-03" and facts[0]["object"] == HOUSEHOLD
            allowed = facts[0]["valid_from"] <= REQUEST_DAY.isoformat() < facts[0]["valid_to"]
            assert allowed is active
            assert facts[0]["valid_to"] == ("9999-12-31" if active else "2026-10-01")
            at_receipt = fact_view(connection, "advisor_covers", REQUEST_DAY, TIMES[stage - 1])
            assert bool(at_receipt.values) is active
    assert_raw_dedup_with_three_assertions(database, "grant-stable", runs)
    with read_snapshot(database) as connection:
        revisions = connection.execute(
            "SELECT revision,recorded_at FROM entitlement_revisions "
            "WHERE revision>0 ORDER BY revision",
        ).fetchall()
        assert [tuple(item) for item in revisions] == [
            (stage, utc_timestamp(time)) for stage, time in enumerate(TIMES, start=1)
        ]
        assert fact_view(connection, "advisor_covers", REQUEST_DAY, TIMES[1]).values
        assert not fact_view(connection, "advisor_covers", REQUEST_DAY, TIMES[2]).values


def assertion(kind: Kind, *, second: bool) -> dict:
    if kind == "membership":
        return row("membership", "membership-stable", person_id=PERSON,
                   household_id="WC-HH-0002" if second else HOUSEHOLD)
    return row("person", "person-stable", id=PERSON,
               name="Synthetic person second" if second else "Synthetic person first",
               external_person_key="PERSON-1")


@pytest.mark.parametrize("kind", ["person", "membership"])
def test_restated_person_or_membership_restores_v1_only_at_its_new_receipt(
    database: Path, kind: Kind,
) -> None:
    runs = [receive(database, [assertion(kind, second=second)], stage)
            for stage, second in enumerate([False, True, False], start=1)]
    assert_raw_dedup_with_three_assertions(database, f"{kind}-stable", runs)
    field = "name" if kind == "person" else "household_id"
    first, second = assertion(kind, second=False)[field], assertion(kind, second=True)[field]
    with read_snapshot(database) as connection:
        for known, expected in zip(TIMES, [first, second, first], strict=True):
            current = fact_view(connection, kind, HISTORICAL_DAY, known)
            assert current.values[PERSON][field] == expected
            assert current.values[PERSON]["recorded_at"] == utc_timestamp(known)
            assert not current.ambiguous
        just_before = fact_view(connection, kind, HISTORICAL_DAY,
                                TIMES[2] - timedelta(microseconds=1))
        assert just_before.values[PERSON][field] == second


@pytest.mark.parametrize("kind", ["person", "membership"])
def test_one_file_reasserts_reused_v1_and_new_v2_at_the_same_instant_without_a_winner(
    database: Path, kind: Kind,
) -> None:
    first = assertion(kind, second=False)
    second = assertion(kind, second=True)
    receive(database, [first], 1)
    receive(database, [first], 2)
    receive(database, [first, second], 3)
    with read_snapshot(database) as connection:
        before = fact_view(connection, kind, HISTORICAL_DAY, TIMES[2] - timedelta(microseconds=1))
        assert PERSON in before.values and PERSON not in before.ambiguous
        current = fact_view(connection, kind, HISTORICAL_DAY, TIMES[2])
        assert PERSON not in current.values
        assert len(current.ambiguous[PERSON]) == 2
        current_times = {item["recorded_at"] for item in current.ambiguous[PERSON]}
        assert current_times == {utc_timestamp(TIMES[2])}
        raw = connection.execute(
            "SELECT recorded_at FROM source_records WHERE source_record_id=? AND business_date=? "
            "ORDER BY recorded_at", (f"{kind}-stable", FILE_DAY.isoformat()),
        ).fetchall()
        assert [item[0] for item in raw] == [utc_timestamp(TIMES[0]), utc_timestamp(TIMES[2])]
        field = "name" if kind == "person" else "household_id"
        assert {item[field] for item in current.ambiguous[PERSON]} == {first[field], second[field]}


@pytest.mark.anyio
async def test_restated_revocation_denies_real_opa_and_previous_provenance_handle(
    database: Path, tmp_path: Path, opa_server: PolicyClient,
) -> None:
    principal = Principal("ADV-03", "advisor")
    args = {"household_id": HOUSEHOLD, "as_of": HISTORICAL_DAY.isoformat()}
    provider = create_provider(tmp_path / "assertion-traces.jsonl")
    service = ContextService(database, opa_server, provider.get_tracer("assertion-history"))
    try:
        receive(database, [grant(active=False)], 1)
        first = await service.execute(principal, TIMES[0], "get_household_snapshot", args)
        assert first["error"]["code"] == "NOT_AUTHORIZED", first
        receive(database, [grant(active=True)], 2)
        allowed = await service.execute(principal, TIMES[1], "get_household_snapshot", args)
        assert allowed["status"] == "COMPLETE", allowed
        assert allowed["source_refs"]
        receive(database, [grant(active=False)], 3)
        denied = await service.execute(principal, TIMES[2], "get_household_snapshot", args)
        reused = await service.execute(principal, TIMES[2], "get_data_provenance",
                                        {"reference": allowed["source_refs"][0]})
        assert denied["error"]["code"] == reused["error"]["code"] == "NOT_AUTHORIZED"
        assert denied["entitlement_revision"] == reused["entitlement_revision"] == 3
        with read_snapshot(database) as connection:
            original = connection.execute("SELECT payload FROM results WHERE id=?",
                                          (allowed["result_id"],)).fetchone()[0]
            assert json.loads(original) == allowed
    finally:
        provider.shutdown()
