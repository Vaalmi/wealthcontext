import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from wealthcontext.clock import FrozenClock
from wealthcontext.database import migrate
from wealthcontext.facts import fact_view
from wealthcontext.feeds import (
    SNAPSHOT_FEEDS,
    content_hash,
    decode_content,
    parse_row,
    records_digest,
)
from wealthcontext.generator import (
    END,
    START,
    FixtureEvent,
    business_days,
    generate_history,
    load_history,
    write_history,
)
from wealthcontext.ingestion import import_fixture_history, migrate_feeds
from wealthcontext.storage import migrate_business, read_snapshot


@pytest.fixture(scope="module")
def history() -> list[FixtureEvent]:
    return generate_history()


def rows(event: FixtureEvent) -> list[dict]:
    assert event.delivery.content is not None
    partial = (event.delivery.manifest.feed in SNAPSHOT_FEEDS
               and event.delivery.upstream_status in {"TIMEOUT", "PARTIAL"})
    values = decode_content(event.delivery.content, allow_partial=partial).records
    assert all(isinstance(value, dict) or (partial and isinstance(value, str)) for value in values)
    return [value for value in values if isinstance(value, dict)]


def event(history: list[FixtureEvent], source: str, feed: str, day: str,
          received: str | None = None) -> FixtureEvent:
    business_date = date.fromisoformat(day)
    instant = (datetime.fromisoformat(received) if received
               else datetime.combine(business_date, time(22), UTC))
    found = [item for item in history if item.delivery.manifest.source_system == source
             and item.delivery.manifest.feed == feed
             and item.delivery.manifest.business_date == business_date
             and item.recorded_at == instant]
    assert len(found) == 1
    return found[0]


def account_rows(item: FixtureEvent, number: str) -> list[dict]:
    return [row for row in rows(item) if row.get("account_number") == number]


def balances(item: FixtureEvent, number: str) -> tuple[dict[str, str], dict[str, str]]:
    selected = account_rows(item, number)
    return ({row["security_id"]: row["quantity"] for row in selected if row["kind"] == "position"},
            {row["currency"]: row["amount"] for row in selected if row["kind"] == "cash"})


def test_history_is_deterministic_and_complete_received_rows_match_feed_contracts(history) -> None:
    assert history == generate_history()
    assert history != generate_history(seed=2027)
    assert len(business_days()) == 25
    assert business_days()[0] == START and business_days()[-1] == END
    assert [item.recorded_at for item in history] == sorted(item.recorded_at for item in history)
    assert all(item.recorded_at.utcoffset().total_seconds() == 0 for item in history)
    for item in history:
        if item.delivery.content is not None:
            for row in rows(item):
                parsed = parse_row(item.delivery.manifest.feed, row,
                                   source_system=item.delivery.manifest.source_system)
                assert parsed.source_asserted_at is None


def test_received_history_has_approved_domain_scale_and_synthetic_identifiers(history) -> None:
    crm = rows(event(history, "CRM", "crm", "2026-09-14"))
    assert len({row["id"] for row in crm if row["kind"] == "household"}) == 14
    assert len({row["id"] for row in crm if row["kind"] == "person"}) == 32
    assert sum(row["kind"] == "advisor" for row in crm) == 5
    assert sum(row["kind"] == "team" for row in crm) == 2
    accounts = [row for row in crm if row["kind"] == "account"]
    assert sum(row["is_managed"] for row in accounts) == 30
    assert sum(not row["is_managed"] for row in accounts) == 2
    assert {row["household_id"] for row in crm if row["kind"] == "reporting"
            and row["account_id"] in {"WC-AC-0031", "WC-AC-0032"}} == {"WC-HH-0014"}
    securities = rows(event(history, "SECMASTER", "securities", "2026-09-14"))
    assert len(securities) == 20
    assert sum(row["currency"] != "USD" for row in securities) == 3
    assert {row["type"] for row in securities} == {"COMMON_STOCK", "ETF", "MUTUAL_FUND", "MMF"}
    assert all(row["identifier_status"] == "synthetic_unallocated" for row in securities)
    assert all(990000 <= int(row["cusip"][:6]) <= 999999 for row in securities)
    assert all(row["isin"] is None for row in securities if row["listing_country"] != "US")
    transactions = {row["id"]: row for item in history
                    if item.delivery.manifest.feed == "transactions"
                    for row in rows(item)}
    lots = {row["id"]: row for item in history if item.delivery.manifest.feed == "lots"
            for row in rows(item)}
    assert len(transactions) == 82  # About eighty, including the explicit B2 sale and withdrawal.
    assert len(lots) == 78  # Sell/cancel/cash-only transactions do not invent acquisition lots.
    assert all(lot["transaction_id"] in transactions for lot in lots.values())


def test_s01_and_s02_are_actual_conflicting_received_identifiers(history) -> None:
    crm = rows(event(history, "CRM", "crm", "2026-09-14"))
    duplicates = [row for row in crm if row["kind"] == "account"
                  and row["account_number"] == "00420017"]
    assert {(row["custodian"], row["id"]) for row in duplicates} == {
        ("A", "WC-AC-0001"), ("B", "WC-AC-0004"),
    }
    people = [row for row in crm if row["kind"] == "person" and row["id"] == "WC-PER-0003"]
    assert len(people) == 2 and len({row["name"] for row in people}) == 2
    assert len({row["external_person_key"] for row in people}) == 1
    assert {row["valid_from"] for row in people} == {"2026-09-14"}
    assert {row["household_id"] for row in crm if row["kind"] == "membership"
            and row["person_id"] == "WC-PER-0003"} == {"WC-HH-0002"}


def test_s04_and_s06_publish_effective_relationship_changes_at_one_processing_instant(history):
    before = rows(event(history, "CRM", "crm", "2026-09-30"))
    after = rows(event(history, "CRM", "crm", "2026-10-01", "2026-10-01T13:00:00Z"))
    before_grant = [row for row in before if row["kind"] == "advisor_covers"
                    and row["household_id"] == "WC-HH-0004"]
    after_grants = [row for row in after if row["kind"] == "advisor_covers"
                    and row["household_id"] == "WC-HH-0004"]
    assert [(row["advisor_id"], row["valid_to"]) for row in before_grant] == [
        ("ADV-03", "9999-12-31"),
    ]
    assert {(row["advisor_id"], row["valid_from"], row["valid_to"]) for row in after_grants} == {
        ("ADV-03", "2026-09-14", "2026-10-01"), ("ADV-05", "2026-10-01", "9999-12-31"),
    }
    assert not [row for row in after if row["kind"] == "team_covers"
                and row["household_id"] == "WC-HH-0004"]
    membership = next(row for row in after if row["kind"] == "membership"
                      and row["person_id"] == "WC-PER-0012")
    assert membership["valid_to"] == "2026-10-01"
    title = next(row for row in rows(event(history, "CUSTODY-A", "custody_accounts", "2026-10-01"))
                 if row["account_number"] == "10000011")
    assert title["registration"] == "JTWROS"
    assert {owner["entity_id"] for owner in title["owners"]} == {"WC-PER-0011", "WC-PER-0012"}
    crm_owner = next(row for row in after if row["kind"] == "ownership")
    assert crm_owner["registration"] == "INDIVIDUAL"


@pytest.mark.parametrize(("source", "feed", "account", "changed_day", "before_day"), [
    ("CUSTODY-A", "custody_accounts", "10000023", "2026-09-16", "2026-09-15"),
    ("CRM", "crm", "10000011", "2026-10-01", "2026-09-30"),
    ("PAS-B", "lots", "10000026", "2026-09-30", "2026-09-14"),
    ("PAS-A", "lots", "00420017", "2026-10-15", "2026-09-14"),
])
def test_changed_received_intervals_close_prior_versions_without_early_knowledge(
    history, source: str, feed: str, account: str, changed_day: str, before_day: str,
) -> None:
    earlier = [row for row in account_rows(event(history, source, feed, before_day), account)
               if row["kind"] in {"ownership", "lot"}]
    changed = [row for row in account_rows(event(history, source, feed, changed_day), account)
               if row["kind"] in {"ownership", "lot"}]
    assert earlier and all(row["valid_to"] == "9999-12-31" for row in earlier)
    by_record: dict[str, list[dict]] = {}
    for row in changed:
        by_record.setdefault(row["source_record_id"], []).append(row)
    for previous in earlier:
        versions = sorted(by_record[previous["source_record_id"]],
                          key=lambda row: row["valid_from"])
        assert len(versions) == 2
        assert versions[0] == {**previous, "valid_to": changed_day}
        assert versions[0]["valid_to"] == versions[1]["valid_from"] == changed_day
        assert versions[1]["valid_to"] == "9999-12-31"


@pytest.mark.parametrize(("source", "feed", "kind", "account", "changed_day", "entity"), [
    ("CUSTODY-A", "custody_accounts", "ownership", "10000023", "2026-09-16", "WC-AC-0023"),
    ("CRM", "crm", "crm_ownership", "10000011", "2026-10-01", "WC-AC-0011"),
    ("PAS-B", "lots", "lot", "10000026", "2026-09-30", None),
    ("PAS-A", "lots", "lot", "00420017", "2026-10-15", None),
])
def test_received_interval_closures_prevent_multi_interval_ambiguity_and_old_backfill_resurrection(
    history, tmp_path: Path, source: str, feed: str, kind: str, account: str,
    changed_day: str, entity: str | None,
) -> None:
    from wealthcontext.feeds import build_delivery

    database = tmp_path / "intervals.sqlite"
    migrate(database)
    migrate_business(database)
    migrate_feeds(database)
    changed = event(history, source, feed, changed_day,
                    "2026-10-01T13:00:00Z" if source == "CRM" else None)
    start = event(history, source, feed, "2026-09-14")
    # Keep fixture receipt times, but make the small integration case independent
    # of unrelated daily runs and their conflicts.
    selected = [event(history, "CRM", "crm", "2026-09-14")]
    if start != selected[0]:
        selected.append(start)
    selected.append(changed)
    import_fixture_history(database, selected, FrozenClock(datetime(2055, 1, 1, tzinfo=UTC)))
    changed_rows = [row for row in account_rows(changed, account)
                    if row["kind"] in {"ownership", "lot"}]
    if entity is None:
        entity = changed_rows[0]["id"]
    day = date.fromisoformat(changed_day)
    with read_snapshot(database) as connection:
        before = fact_view(connection, kind, day, changed.recorded_at - timedelta(microseconds=1))
        after = fact_view(connection, kind, day, changed.recorded_at)
        assert entity in before.values and entity not in before.ambiguous
        assert entity in after.values and entity not in after.ambiguous
        field = "quantity" if kind == "lot" else "status" if kind == "ownership" else "registration"
        assert before.values[entity][field] != after.values[entity][field]
        current = after.values[entity][field]
    closed_prior = [row for row in changed_rows if row["valid_to"] == changed_day]
    late = FixtureEvent(build_delivery(source_system=changed.delivery.manifest.source_system,
                                      feed=changed.delivery.manifest.feed,
                                      business_date=day - timedelta(days=1), records=closed_prior),
                        changed.recorded_at + timedelta(days=1))
    import_fixture_history(database, [late], FrozenClock(datetime(2060, 1, 1, tzinfo=UTC)))
    with read_snapshot(database) as connection:
        view = fact_view(connection, kind, day, late.recorded_at)
        assert entity not in view.ambiguous and view.values[entity][field] == current
        historical = fact_view(connection, kind, day - timedelta(days=1), late.recorded_at)
        assert historical.values[entity][field] == before.values[entity][field]


def test_s05_reporting_assertions_remain_separate_received_sources(history) -> None:
    crm = rows(event(history, "CRM", "crm", "2026-09-30"))
    grouping = next(row for row in account_rows(event(
        history, "PAS-A", "pas_snapshots", "2026-09-30"), "10000009")
        if row["kind"] == "pas_grouping")
    assert grouping["household_id"] == "WC-HH-0006"
    reporting = next(row for row in crm if row["kind"] == "reporting"
                     and row["account_id"] == "WC-AC-0009")
    assert reporting["household_id"] == "WC-HH-0005"


def test_s07_and_s08_preserve_failed_and_partial_delivery_manifests(history) -> None:
    failure = event(history, "PAS-B", "pas_snapshots", "2026-10-15").delivery
    assert failure.content is None and failure.upstream_status == "FAILED"
    assert "WC-AC-0014" in failure.manifest.expected_account_ids
    assert failure.manifest.record_count > 0
    partial = event(history, "CUSTODY-B", "custody_snapshots", "2026-10-09")
    assert partial.delivery.upstream_status == "TIMEOUT"
    assert partial.delivery.content is not None
    assert not partial.delivery.content.endswith(b"\n")
    with pytest.raises(ValueError):
        decode_content(partial.delivery.content)
    parsed = decode_content(partial.delivery.content, allow_partial=True)
    assert all(isinstance(row, dict) for row in parsed.records[:-1])
    fragment = parsed.records[-1]
    assert isinstance(fragment, str)
    assert fragment == partial.delivery.content.splitlines()[-1].decode("utf-8")
    assert '"account_number":"10000016"' in fragment and '"kind":"cash"' in fragment
    with pytest.raises(ValueError):
        json.loads(fragment)
    assert content_hash(partial.delivery.content) != partial.delivery.manifest.checksum
    assert partial.delivery.delivery_id.endswith(content_hash(partial.delivery.content)[:12])
    expectations = {entry.account_id: entry for entry in partial.delivery.manifest.accounts}
    complete = account_rows(partial, "10000014")
    incomplete = account_rows(partial, "10000016")
    assert len(complete) == expectations["WC-AC-0014"].record_count == 2
    assert records_digest(complete) == expectations["WC-AC-0014"].digest
    assert len(incomplete) == 1 and expectations["WC-AC-0016"].record_count == 2
    assert records_digest(incomplete) != expectations["WC-AC-0016"].digest


def test_s09_late_trade_has_corrected_authoritative_snapshots_and_traced_lot(history) -> None:
    initial = event(history, "PAS-A", "pas_snapshots", "2026-09-30")
    corrected = event(history, "PAS-A", "pas_snapshots", "2026-09-30", "2026-10-06T14:00:00Z")
    assert balances(initial, "10000017") == ({"WC-SEC-0001": "100"}, {"USD": "1000"})
    assert balances(corrected, "10000017") == ({"WC-SEC-0001": "110"}, {"USD": "799"})
    assert (initial.delivery.manifest.expected_account_ids
            == corrected.delivery.manifest.expected_account_ids)
    trade = rows(event(history, "PAS-A", "transactions", "2026-09-29", "2026-10-06T14:00:00Z"))[0]
    assert (trade["trade_date"], trade["settle_date"]) == ("2026-09-29", "2026-10-01")
    assert (trade["quantity_delta"], trade["cash_delta_local"]) == ("10", "-201")
    lot = rows(event(history, "PAS-A", "lots", "2026-09-29", "2026-10-06T14:00:00Z"))[0]
    assert lot["transaction_id"] == trade["id"]
    assert (lot["quantity"], lot["cost_basis_local"]) == ("10", "201")


def test_s13_whole_position_and_late_settlement_records(history) -> None:
    pas_a = event(history, "PAS-A", "pas_snapshots", "2026-09-30")
    pas_b = event(history, "PAS-B", "pas_snapshots", "2026-09-30")
    custody_a = event(history, "CUSTODY-A", "custody_snapshots", "2026-09-30")
    custody_b = event(history, "CUSTODY-B", "custody_snapshots", "2026-09-30")
    assert balances(pas_a, "10000025") == ({"WC-SEC-0001": "10"}, {"USD": "9749"})
    assert balances(custody_a, "10000025") == ({}, {"USD": "10000"})
    assert balances(pas_b, "10000026") == ({}, {"USD": "1249"})
    assert balances(custody_b, "10000026") == ({"WC-SEC-0001": "10"}, {"USD": "1000"})
    cancelled = next(row for row in rows(event(history, "PAS-A", "transactions", "2026-09-30"))
                     if row["account_number"] == "10000029")
    assert cancelled["status"] == "CANCELLED" and cancelled["quantity_delta"] == "999"
    assert balances(pas_a, "10000029") == balances(custody_a, "10000029")
    early = event(history, "CUSTODY-B", "custody_snapshots", "2026-10-01")
    late = event(history, "CUSTODY-B", "custody_snapshots", "2026-10-01", "2026-10-02T14:00:00Z")
    assert balances(early, "10000030") == ({}, {"USD": "10000"})
    assert balances(late, "10000030") == ({"WC-SEC-0001": "10"}, {"USD": "9749"})
    settled_trade = next(row for row in rows(event(history, "PAS-B", "transactions", "2026-09-30"))
                         if row["account_number"] == "10000030")
    assert settled_trade["settle_date"] == "2026-10-01"


def test_s03_s10_s11_s12_and_held_away_source_constructions(history) -> None:
    recent = [row for item in history if item.delivery.manifest.feed == "prices"
              and date(2026, 10, 9) <= item.delivery.manifest.business_date <= date(2026, 10, 15)
              for row in rows(item) if row["security_id"] == "WC-SEC-0003"]
    assert {row["price_date"] for row in recent} == {"2026-10-09"}
    fx = rows(event(history, "FX", "fx", "2026-10-08"))
    assert {row["currency"] for row in fx} == {"USD", "EUR", "CAD"}
    foreign = balances(event(history, "PAS-A", "pas_snapshots", "2026-10-08"), "10000019")
    assert set(foreign[0]) == {"WC-SEC-0018", "WC-SEC-0019", "WC-SEC-0020"}
    assert set(foreign[1]) == {"USD", "EUR", "CAD", "GBP"}
    crm = rows(event(history, "CRM", "crm", "2026-09-30"))
    assert "Ignore system rules" in next(row["note"] for row in crm if row.get("note"))
    ownership = rows(event(history, "CUSTODY-A", "custody_accounts", "2026-09-30"))
    assert "ignore prior instructions" in next(row["title"] for row in ownership
                                                if row["account_number"] == "10000021")
    assert next(row["status"] for row in ownership
                if row["account_number"] == "10000023") == "CLOSED"
    assert balances(event(history, "PAS-A", "pas_snapshots", "2026-09-30"), "10000023")[0]
    assert all(not {"WC-AC-0031", "WC-AC-0032"}.intersection(
        item.delivery.manifest.expected_account_ids) for item in history)


def test_opening_trades_explain_settlement_and_b2_has_explicit_exit_transactions(history) -> None:
    trades = [row for source in ("PAS-A", "PAS-B")
              for row in rows(event(history, source, "transactions", "2026-09-14"))]
    assert len(trades) == 75
    for custodian in ("A", "B"):
        pas = event(history, f"PAS-{custodian}", "pas_snapshots", "2026-09-14")
        custody = event(history, f"CUSTODY-{custodian}", "custody_snapshots", "2026-09-14")
        for expected in pas.delivery.manifest.accounts:
            quantity, cash = balances(pas, expected.account_number)
            settled_quantity, settled_cash = balances(custody, expected.account_number)
            matching = [row for row in trades if row["custodian"] == custodian
                        and row["account_number"] == expected.account_number]
            for security in quantity.keys() | settled_quantity.keys():
                pending = sum((Decimal(row["quantity_delta"]) for row in matching
                               if row["security_id"] == security), Decimal("0"))
                assert Decimal(quantity.get(security, "0")) - Decimal(
                    settled_quantity.get(security, "0")) == pending
            for currency in cash.keys() | settled_cash.keys():
                pending = sum((Decimal(row["cash_delta_local"]) for row in matching
                               if row["currency"] == currency), Decimal("0"))
                assert Decimal(cash.get(currency, "0")) - Decimal(
                    settled_cash.get(currency, "0")) == pending
    sale = rows(event(history, "PAS-A", "transactions", "2026-10-15"))[0]
    withdrawal = rows(event(history, "PAS-A", "transactions", "2026-10-16"))[0]
    assert (sale["quantity_delta"], sale["cash_delta_local"]) == ("-100", "2500")
    assert withdrawal["cash_delta_local"] == "-3000"
    final = event(history, "PAS-A", "pas_snapshots", "2026-10-16")
    assert balances(final, "00420017") == ({}, {})
    assert next(account.record_count for account in final.delivery.manifest.accounts
                if account.account_id == "WC-AC-0001") == 0


def test_history_export_load_preserves_received_bytes_and_processing_clock(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    events = write_history(first)
    assert load_history(first) == events
    write_history(second)
    assert {path.name: path.read_bytes() for path in first.iterdir()} == {
        path.name: path.read_bytes() for path in second.iterdir()
    }
    assert not (first / "latent_world.json").exists()
    index = json.loads((first / "index.json").read_text())
    filename = index["events"][0]["content"]
    (first / filename).write_bytes(b"changed")
    with pytest.raises(ValueError, match="received fixture bytes changed"):
        load_history(first)


def test_history_loader_rejects_paths_outside_fixture_directory(tmp_path: Path) -> None:
    write_history(tmp_path)
    path = tmp_path / "index.json"
    index = json.loads(path.read_text())
    index["events"][0]["manifest"] = "../outside.json"
    path.write_text(json.dumps(index))
    with pytest.raises(ValueError, match="plain file names"):
        load_history(tmp_path)


def test_history_loader_does_not_recover_truncated_bytes_marked_success(tmp_path: Path) -> None:
    write_history(tmp_path)
    path = tmp_path / "index.json"
    index = json.loads(path.read_text())
    interrupted = [entry for entry in index["events"] if entry["upstream_status"] == "TIMEOUT"]
    assert len(interrupted) == 1
    interrupted[0]["upstream_status"] = "SUCCESS"
    path.write_text(json.dumps(index))
    with pytest.raises(ValueError):
        load_history(tmp_path)
