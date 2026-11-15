"""P2 acceptance from received files and explicit hand-reviewed scenario assertions."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from wealthcontext.clock import FrozenClock
from wealthcontext.conflicts import conflicts_at
from wealthcontext.context_service import ContextService
from wealthcontext.database import migrate
from wealthcontext.facts import account_snapshot, fact_view, resolve_account
from wealthcontext.feeds import build_delivery
from wealthcontext.generator import FixtureEvent, generate_history
from wealthcontext.households import household_context
from wealthcontext.identity import Principal
from wealthcontext.ingestion import import_fixture_history, ingest_delivery, migrate_feeds
from wealthcontext.policy import PolicyClient
from wealthcontext.snapshot import CompleteSnapshot, IncompleteSnapshot, calculate_snapshot
from wealthcontext.storage import entitlements, migrate_business, read_snapshot
from wealthcontext.telemetry import create_provider
from wealthcontext.verifier import TEMPLATE

KNOWN = datetime(2026, 10, 17, 12, tzinfo=UTC)
LATE_BOOTSTRAP = datetime(2055, 3, 1, tzinfo=UTC)
QUARTER_END = date(2026, 9, 30)


def create_database(path: Path, events: list[FixtureEvent], imported: datetime) -> Path:
    migrate(path)
    migrate_business(path)
    migrate_feeds(path)
    import_fixture_history(path, events, FrozenClock(imported))
    return path


@pytest.fixture(scope="module")
def received_history() -> list[FixtureEvent]:
    return generate_history()


@pytest.fixture(scope="module")
def received_database(tmp_path_factory, received_history) -> Path:
    path = tmp_path_factory.mktemp("p2-received") / "history.sqlite"
    return create_database(path, received_history, LATE_BOOTSTRAP)


def snapshot(database: Path, household: int, day: date = QUARTER_END,
             known: datetime = KNOWN):
    with read_snapshot(database) as connection:
        return calculate_snapshot(connection, f"WC-HH-{household:04}", day, known)


def test_p1_clean_control_and_s01_custodian_crosswalk_remain_distinct(received_database) -> None:
    with read_snapshot(received_database) as connection:
        first = resolve_account(connection, "A", "00420017", QUARTER_END, KNOWN)
        second = resolve_account(connection, "B", "00420017", QUARTER_END, KNOWN)
        assert first == "WC-AC-0001" and second == "WC-AC-0004"
        assert account_snapshot(connection, "pas_snapshots", first, QUARTER_END, KNOWN).available
        assert account_snapshot(connection, "pas_snapshots", second, QUARTER_END, KNOWN).available
    data, flags, sources = snapshot(received_database, 1)
    assert isinstance(data, CompleteSnapshot)
    assert data.assets_usd == "3000.00" and data.cash_usd == "500.00"
    assert [account.id for account in data.accounts] == ["WC-AC-0001", "WC-AC-0002"]
    assert not flags and sources


def test_s02_ambiguous_person_blocks_completeness_without_choosing_a_name(received_database):
    data, flags, _ = snapshot(received_database, 2)
    assert isinstance(data, IncompleteSnapshot)
    assert "assets_usd" not in data.model_dump()
    assert "WC-PER-0003" in data.missing_components
    assert any(flag.code == "AMBIGUOUS_ENTITY" and flag.subject == "WC-PER-0003" for flag in flags)
    assert "WC-PER-0003" not in {member.id for member in data.members}
    with read_snapshot(received_database) as connection:
        people = fact_view(connection, "person", QUARTER_END, KNOWN)
        assert len(people.ambiguous["WC-PER-0003"]) == 2
        assert "WC-PER-0003" not in people.values


def test_s05_crm_reporting_wins_and_discloses_only_redacted_counterpart(received_database):
    data, flags, _ = snapshot(received_database, 5)
    assert isinstance(data, CompleteSnapshot)
    assert [account.id for account in data.accounts] == ["WC-AC-0009", "WC-AC-0010"]
    conflicts = [flag for flag in flags if flag.code == "REPORTING_HOUSEHOLD_CONFLICT"]
    assert len(conflicts) == 1 and conflicts[0].subject == "WC-AC-0009"
    assert conflicts[0].detail["authoritative_source"] == "CRM"
    assert conflicts[0].detail["counterpart_household_id"] == "[REDACTED]"
    assert "WC-HH-0006" not in data.model_dump_json()
    counterpart, _, _ = snapshot(received_database, 6)
    assert "WC-AC-0009" not in {account.id for account in counterpart.accounts}


def test_s06_membership_and_custody_legal_ownership_remain_separate(received_database):
    before, _, _ = snapshot(received_database, 6, date(2026, 9, 30))
    after, flags, _ = snapshot(received_database, 6, date(2026, 10, 1))
    assert "WC-PER-0012" in {member.id for member in before.members}
    assert "WC-PER-0012" not in {member.id for member in after.members}
    joint = next(account for account in after.accounts if account.id == "WC-AC-0011")
    assert joint.registration == "JTWROS"
    assert {owner.entity_id for owner in joint.legal_owners} == {
        "WC-PER-0011", "WC-PER-0012",
    }
    assert any(flag.code == "OWNERSHIP_CONFLICT" and flag.subject == joint.id for flag in flags)
    assert isinstance(after, CompleteSnapshot)


def test_s07_failed_pas_never_carries_previous_day_positions_forward(received_database):
    with read_snapshot(received_database) as connection:
        previous = account_snapshot(connection, "pas_snapshots", "WC-AC-0014",
                                    date(2026, 10, 14), KNOWN)
        failed = account_snapshot(connection, "pas_snapshots", "WC-AC-0014",
                                  date(2026, 10, 15), KNOWN)
        assert previous.available and previous.positions
        assert not failed.available and not failed.positions and not failed.cash
    data, flags, _ = snapshot(received_database, 7, date(2026, 10, 15))
    assert isinstance(data, IncompleteSnapshot) and "assets_usd" not in data.model_dump()
    assert "WC-AC-0014" in data.missing_components
    assert not [position for position in data.positions if position.account_id == "WC-AC-0014"]
    assert any(flag.code == "SOURCE_UNAVAILABLE" and flag.subject == "WC-AC-0014"
               for flag in flags)


def test_s08_partial_secondary_preserves_complete_accounts_and_valuation(received_database):
    day = date(2026, 10, 9)
    with read_snapshot(received_database) as connection:
        bad = account_snapshot(connection, "custody_snapshots", "WC-AC-0016", day, KNOWN)
        good = account_snapshot(connection, "custody_snapshots", "WC-AC-0014", day, KNOWN)
        zero = account_snapshot(connection, "custody_snapshots", "WC-AC-0018", day, KNOWN)
        assert not bad.available and not bad.positions and not bad.cash
        assert good.available and len(good.positions) == 1 and len(good.cash) == 1
        assert zero.available and not zero.positions and not zero.cash
        run = connection.execute("SELECT * FROM feed_runs WHERE id=?", (good.run_id,)).fetchone()
        assert run["status"] == "PARTIAL" and run["upstream_status"] == "TIMEOUT"
        rejects = connection.execute("SELECT * FROM quarantine WHERE run_id=? AND account_id=?",
                                     (good.run_id, "WC-AC-0016")).fetchall()
        assert rejects
        assert not connection.execute(
            "SELECT 1 FROM source_records s JOIN facts f ON f.source_id=s.id "
            "WHERE s.feed_run_id=? AND f.kind IN ('custody_position','custody_cash') "
            "AND json_extract(f.payload,'$.account_id')='WC-AC-0016'", (good.run_id,),
        ).fetchall()
    data, flags, _ = snapshot(received_database, 8, day)
    assert isinstance(data, CompleteSnapshot)
    assert any(flag.code == "RECONCILIATION_UNAVAILABLE" and flag.subject == "WC-AC-0016"
               for flag in flags)
    assert not any(flag.code == "SOURCE_UNAVAILABLE" for flag in flags)
    unavailable = next(item for item in data.reconciliation if item.account_id == "WC-AC-0016")
    assert {row.reconciliation_status for row in unavailable.rows} == {"RECONCILIATION_UNAVAILABLE"}


def test_s09_historical_answer_changes_only_after_late_received_snapshot(received_database):
    before = datetime(2026, 10, 6, 13, 59, 59, 999999, tzinfo=UTC)
    after = datetime(2026, 10, 6, 14, tzinfo=UTC)
    original, _, original_sources = snapshot(received_database, 9, known=before)
    corrected, _, corrected_sources = snapshot(received_database, 9, known=after)
    assert isinstance(original, CompleteSnapshot) and isinstance(corrected, CompleteSnapshot)
    assert original.assets_usd == "3500.00"
    assert corrected.assets_usd == "3549.00"
    assert corrected.positions[0].quantity == "110" and corrected.cash_usd == "799.00"
    assert set(corrected_sources) != set(original_sources)


def test_fixture_bootstrap_date_does_not_change_historical_answers(
    received_database, received_history, tmp_path: Path,
) -> None:
    early_database = create_database(tmp_path / "earlier.sqlite", received_history,
                                     datetime(2026, 11, 1, tzinfo=UTC))
    for known in (datetime(2026, 10, 6, 13, 59, 59, tzinfo=UTC),
                  datetime(2026, 10, 6, 14, tzinfo=UTC)):
        early = snapshot(early_database, 9, known=known)
        late = snapshot(received_database, 9, known=known)
        assert early[0].model_dump(mode="json") == late[0].model_dump(mode="json")
        assert early[1] == late[1] and early[2] == late[2]
    with read_snapshot(received_database) as connection:
        imported = {row[0] for row in connection.execute("SELECT imported_at FROM feed_runs")}
        assert imported == {"2055-03-01T00:00:00.000000+00:00"}
        assert connection.execute("SELECT COUNT(*) FROM source_records WHERE source_asserted_at "
                                  "IS NOT NULL").fetchone()[0] == 0


def test_s12_closed_account_residual_is_included_not_dropped(received_database):
    data, flags, _ = snapshot(received_database, 12)
    assert isinstance(data, CompleteSnapshot)
    account = next(account for account in data.accounts if account.id == "WC-AC-0023")
    assert account.status == "CLOSED"
    assert any(position.account_id == account.id for position in data.positions)
    assert any(flag.code == "CLOSED_ACCOUNT_WITH_BALANCE" and flag.subject == account.id
               for flag in flags)


def test_s13_whole_buy_sell_cancel_and_late_settlement_reconcile_snapshots(received_database):
    data, _, _ = snapshot(received_database, 13)
    before = {item.account_id: item for item in data.reconciliation}
    for account in ("WC-AC-0025", "WC-AC-0026", "WC-AC-0030"):
        assert {row.kind for row in before[account].rows} == {"quantity", "cash"}
        assert {row.reconciliation_status for row in before[account].rows} == {"PENDING_SETTLEMENT"}
    assert {row.reconciliation_status for row in before["WC-AC-0029"].rows} == {"MATCHED"}
    assert not any("WC-TXN-0079" in row.pending_transaction_ids
                   for item in data.reconciliation for row in item.rows)
    early, early_flags, _ = snapshot(received_database, 13, date(2026, 10, 1),
                                     datetime(2026, 10, 2, 13, 59, 59, tzinfo=UTC))
    late, late_flags, _ = snapshot(received_database, 13, date(2026, 10, 1),
                                   datetime(2026, 10, 2, 14, tzinfo=UTC))
    initial = {item.account_id: item for item in early.reconciliation}
    assert {row.reconciliation_status for row in initial["WC-AC-0030"].rows} == {"BREAK"}
    assert all(not row.pending_transaction_ids for row in initial["WC-AC-0030"].rows)
    assert {flag.code for flag in early_flags} >= {"QUANTITY_BREAK", "CASH_BREAK"}
    assert all(row.reconciliation_status == "MATCHED" for item in late.reconciliation
               for row in item.rows)
    assert not {"QUANTITY_BREAK", "CASH_BREAK", "PENDING_SETTLEMENT"}.intersection(
        flag.code for flag in late_flags)
    with read_snapshot(received_database) as connection:
        old_register = conflicts_at(connection, "WC-HH-0013", date(2026, 10, 1),
                                    datetime(2026, 10, 2, 13, 59, 59, tzinfo=UTC))
        new_register = conflicts_at(connection, "WC-HH-0013", date(2026, 10, 1),
                                    datetime(2026, 10, 2, 14, tzinfo=UTC))
        assert {item["code"] for item in old_register} >= {"QUANTITY_BREAK", "CASH_BREAK"}
        assert not {"QUANTITY_BREAK", "CASH_BREAK"}.intersection(
            item["code"] for item in new_register)


def test_s14_heldaway_exclusions_and_source_lots_remain_traceable(received_database):
    data, flags, _ = snapshot(received_database, 14)
    assert isinstance(data, CompleteSnapshot)
    assert {account.id for account in data.excluded_accounts} == {"WC-AC-0031", "WC-AC-0032"}
    assert {flag.code for flag in flags} == {"HELD_AWAY_EXCLUDED"}
    assert {account.id for account in data.accounts} == {"WC-AC-0027", "WC-AC-0028"}
    with read_snapshot(received_database) as connection:
        context = household_context(connection, "WC-HH-0009", QUARTER_END, KNOWN)
        account = next(account for account in context.accounts
                       if account.account["id"] == "WC-AC-0017")
        trade = next(transaction for transaction in account.transactions
                     if transaction["id"] == "WC-TXN-0076")
        lot = next(lot for lot in account.lots if lot["transaction_id"] == trade["id"])
        assert lot["quantity"] == "10" and lot["cost_basis_local"] == "201"
        for value in (trade, lot):
            source = connection.execute("SELECT * FROM source_records WHERE id=?",
                                        (value["source_id"],)).fetchone()
            assert source["source_asserted_at"] is None
            assert source["recorded_at"] == "2026-10-06T14:00:00.000000+00:00"
            assert json.loads(source["payload"])["id"] == value["id"]


def candidate(result: dict) -> dict:
    return {"answer_markdown": TEMPLATE, "claims": [{
        "id": "c1", "result_id": result["result_id"],
        "entity": {"type": "household", "id": "WC-HH-0004"}, "field": "assets_usd",
        "household_id": "WC-HH-0004", "valuation_date": "2026-09-30", "unit": "USD",
        "format": "value_only",
    }], "predicates": [], "caveats": [], "refusal": None}


@pytest.mark.anyio
async def test_s04_revision_commit_revokes_next_request_old_handle_and_old_claim(
    received_history, tmp_path: Path, opa_server: PolicyClient,
) -> None:
    boundary = datetime(2026, 10, 1, 13, tzinfo=UTC)
    before = datetime(2026, 10, 1, 12, 59, 59, tzinfo=UTC)
    events = [event for event in received_history if event.recorded_at < boundary]
    database = create_database(tmp_path / "reassignment.sqlite", events, LATE_BOOTSTRAP)
    provider = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, opa_server, provider.get_tracer("wealthcontext"))
    old, new = Principal("ADV-03", "advisor"), Principal("ADV-05", "advisor")
    args = {"household_id": "WC-HH-0004", "as_of": "2026-09-30",
            "known_as_of": "2026-09-30T23:00:00Z"}
    try:
        conversation = service.start_conversation(old, before)
        initial = await service.execute(old, before, "get_household_snapshot", args, conversation)
        assert initial["status"] == "COMPLETE", initial
        verified = await service.execute(old, before, "submit_answer",
                                         candidate(initial), conversation)
        assert verified["status"] == "COMPLETE", verified
        with read_snapshot(database) as connection:
            revision_before = entitlements(connection)[0]
        correction = [event for event in received_history if event.recorded_at == boundary
                      and event.delivery.manifest.feed == "crm"]
        assert len(correction) == 1
        import_fixture_history(database, correction, FrozenClock(LATE_BOOTSTRAP))
        with read_snapshot(database) as connection:
            assert entitlements(connection)[0] == revision_before + 1
        denied = await service.execute(old, boundary, "get_household_snapshot", args, conversation)
        handle = await service.execute(old, boundary, "get_data_provenance",
                                       {"reference": initial["source_refs"][0]}, conversation)
        reused = await service.execute(old, boundary, "submit_answer",
                                       candidate(initial), conversation)
        assert [result["error"]["code"] for result in (denied, handle, reused)] == [
            "NOT_AUTHORIZED", "NOT_AUTHORIZED", "NOT_AUTHORIZED",
        ]
        allowed = await service.execute(new, boundary, "get_household_snapshot", args)
        assert allowed["status"] == "COMPLETE" and allowed["valuation_date"] == "2026-09-30"
        assert allowed["data"]["assets_usd"] == initial["data"]["assets_usd"]
    finally:
        provider.shutdown()


@pytest.mark.anyio
async def test_future_effective_received_grant_waits_for_request_business_date(
    received_history, tmp_path: Path, opa_server: PolicyClient,
) -> None:
    database = create_database(tmp_path / "future.sqlite", received_history, LATE_BOOTSTRAP)
    future = build_delivery(source_system="CRM", feed="crm", business_date=date(2026, 10, 16),
                            records=[
        {"kind": "advisor_covers", "source_record_id": "future-end-ADV05-HH4",
         "valid_from": "2026-10-01", "valid_to": "2026-11-01",
         "advisor_id": "ADV-05", "household_id": "WC-HH-0004"},
        {"kind": "advisor_covers", "source_record_id": "future-start-ADV03-HH4",
         "valid_from": "2026-11-01", "valid_to": "9999-12-31",
         "advisor_id": "ADV-03", "household_id": "WC-HH-0004"},
    ])
    ingest_delivery(database, future, FrozenClock(datetime(2026, 10, 16, 23, tzinfo=UTC)))
    provider = create_provider(tmp_path / "future-traces.jsonl")
    service = ContextService(database, opa_server, provider.get_tracer("wealthcontext"))
    args = {"household_id": "WC-HH-0004", "as_of": "2026-09-30"}
    try:
        for now, allowed_id, denied_id in [
            (datetime(2026, 11, 1, 3, 59, 59, tzinfo=UTC), "ADV-05", "ADV-03"),
            (datetime(2026, 11, 1, 4, tzinfo=UTC), "ADV-03", "ADV-05"),
        ]:
            allowed = await service.execute(Principal(allowed_id, "advisor"), now,
                                            "get_household_snapshot", args)
            denied = await service.execute(Principal(denied_id, "advisor"), now,
                                           "get_household_snapshot", args)
            assert allowed["status"] == "COMPLETE", allowed
            assert denied["error"]["code"] == "NOT_AUTHORIZED", denied
    finally:
        provider.shutdown()
