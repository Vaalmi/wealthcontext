"""Conflict cutoff views stay current when received files backdate other dates."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wealthcontext.clock import FrozenClock
from wealthcontext.conflicts import conflicts_at, observed_conflicts_at
from wealthcontext.database import migrate
from wealthcontext.feeds import AccountReference, FeedName, SourceSystem, build_delivery
from wealthcontext.households import household_context
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.snapshot import CompleteSnapshot, calculate_snapshot
from wealthcontext.storage import migrate_business, read_snapshot

DAY = date(2026, 9, 30)
LATE_DAY = date(2026, 10, 6)
EARLY = datetime(2026, 9, 30, 22, tzinfo=UTC)
LATE = datetime(2026, 10, 6, 14, tzinfo=UTC)
FIRST_HH, SECOND_HH = "WC-HH-0001", "WC-HH-0002"
ACCOUNT = AccountReference(account_id="WC-AC-0001", custodian="A", account_number="00420017")
EXTERNAL = {"custodian": ACCOUNT.custodian, "account_number": ACCOUNT.account_number}


def row(kind: str, key: str, **data: Any) -> dict:
    return {"kind": kind, "source_record_id": key, "valid_from": "2026-09-14",
            "valid_to": "9999-12-31", **data}


def receive(
    database: Path, source: SourceSystem, feed: FeedName, records: list[dict],
    *, file_day: date = DAY, known: datetime = EARLY,
) -> str:
    packet = build_delivery(
        source_system=source, feed=feed, business_date=file_day, records=records,
        expected_accounts=[ACCOUNT] if feed in {"pas_snapshots", "custody_snapshots"} else [],
    )
    return ingest_delivery(database, packet, FrozenClock(known))


@pytest.fixture
def received_database(tmp_path: Path) -> Path:
    database = tmp_path / "conflict-corrections.sqlite"
    migrate(database)
    migrate_business(database)
    migrate_feeds(database)
    receive(database, "CRM", "crm", [
        row("household", "first-household", id=FIRST_HH, name="Synthetic first household"),
        row("household", "second-household", id=SECOND_HH, name="Synthetic second household"),
        row("person", "person", id="WC-PER-0001", name="Synthetic person",
            external_person_key="P1"),
        row("membership", "membership", person_id="WC-PER-0001", household_id=FIRST_HH),
        row("account", "account", id=ACCOUNT.account_id, **EXTERNAL, is_managed=True),
        row("reporting", "reporting", account_id=ACCOUNT.account_id, household_id=FIRST_HH),
    ])
    receive(database, "CUSTODY-A", "custody_accounts", [
        row("ownership", "ownership", **EXTERNAL, title="Synthetic individual",
            registration="INDIVIDUAL", status="OPEN", owners=[{
                "entity_id": "WC-PER-0001", "entity_type": "person", "role": "owner",
            }]),
    ])
    receive(database, "SECMASTER", "securities", [
        row("security", "security", id="WC-SEC-0001", issuer_id="WC-ISS-0001",
            issuer_name="Synthetic issuer", name="Synthetic stock", ticker="SYN1",
            type="COMMON_STOCK", currency="USD", cusip="990000101",
            identifier_status="synthetic_unallocated", isin="US9900001010", listing_country="US"),
    ])
    receive(database, "CUSTODY-A", "prices", [
        row("price", "price", security_id="WC-SEC-0001", currency="USD", price="25.00",
            price_date=DAY.isoformat()),
    ])
    daily = {"valid_from": DAY.isoformat(), "valid_to": "2026-10-01"}
    receive(database, "PAS-A", "pas_snapshots", [
        row("position", "pas-position", **EXTERNAL, security_id="WC-SEC-0001",
            quantity="10", **daily),
        row("cash", "pas-cash", **EXTERNAL, currency="USD", amount="749.00", **daily),
        row("pas_grouping", "pas-grouping", **EXTERNAL, household_id=FIRST_HH, **daily),
    ])
    receive(database, "CUSTODY-A", "custody_snapshots", [
        row("cash", "custody-cash", **EXTERNAL, currency="USD", amount="1000.00", **daily),
    ])
    return database


def test_late_trade_updates_historical_conflict_view_without_rewriting_run_observation(
    received_database: Path,
) -> None:
    database = received_database
    with read_snapshot(database) as connection:
        before = conflicts_at(connection, FIRST_HH, DAY, EARLY)
        assert {item["code"] for item in before} == {"QUANTITY_BREAK", "CASH_BREAK"}
        original_log = [tuple(item) for item in connection.execute(
            "SELECT * FROM conflict_snapshots ORDER BY run_id,household_id",
        )]
    run = receive(database, "PAS-A", "transactions", [
        row("transaction", "late-buy", id="WC-TXN-0001", **EXTERNAL,
            security_id="WC-SEC-0001", txn_type="BUY", trade_date=DAY.isoformat(),
            settle_date="2026-10-01", quantity_delta="10", cash_delta_local="-251.00",
            currency="USD", status="ACTIVE", valid_from=DAY.isoformat()),
    ], file_day=LATE_DAY, known=LATE)
    with read_snapshot(database) as connection:
        after = conflicts_at(connection, FIRST_HH, DAY, LATE)
        assert after == household_context(connection, FIRST_HH, DAY, LATE).conflicts == []
        assert conflicts_at(connection, FIRST_HH, DAY, LATE - timedelta(microseconds=1)) == before
        data, flags, _ = calculate_snapshot(connection, FIRST_HH, DAY, LATE)
        assert isinstance(data, CompleteSnapshot) and data.assets_usd == "999.00"
        assert data.conflicts == []
        assert {flag.code for flag in flags} == {"PENDING_SETTLEMENT"}
        # The audit record still describes what the Sep30 publication observed.
        assert observed_conflicts_at(connection, FIRST_HH, DAY, LATE) == before
        new_dates = {item[0] for item in connection.execute(
            "SELECT business_date FROM conflict_snapshots WHERE run_id=?", (run,),
        )}
        assert new_dates == {LATE_DAY.isoformat()}
        all_log = [tuple(item) for item in connection.execute(
            "SELECT * FROM conflict_snapshots ORDER BY run_id,household_id",
        )]
        assert set(original_log) <= set(all_log)


def test_backdated_reporting_creates_historical_conflict_even_without_new_historical_run(
    received_database: Path,
) -> None:
    database = received_database
    receive(database, "CRM", "crm", [
        row("reporting", "corrected-reporting", account_id=ACCOUNT.account_id,
            household_id=SECOND_HH),
        row("membership", "corrected-membership", person_id="WC-PER-0001", household_id=SECOND_HH),
    ], file_day=LATE_DAY, known=LATE)
    with read_snapshot(database) as connection:
        assert conflicts_at(connection, SECOND_HH, DAY, EARLY) == []
        current = conflicts_at(connection, SECOND_HH, DAY, LATE)
        assert "REPORTING_HOUSEHOLD_CONFLICT" in {item["code"] for item in current}
        assert current == household_context(connection, SECOND_HH, DAY, LATE).conflicts
        data, _, _ = calculate_snapshot(connection, SECOND_HH, DAY, LATE)
        assert {item.code for item in data.conflicts} == {item["code"] for item in current}
        assert observed_conflicts_at(connection, SECOND_HH, DAY, LATE) == []


@pytest.mark.parametrize("file_day", [DAY, LATE_DAY])
def test_ended_household_clears_derived_conflicts_and_records_explicit_publication_resolution(
    received_database: Path, file_day: date,
) -> None:
    database = received_database
    with read_snapshot(database) as connection:
        before = conflicts_at(connection, FIRST_HH, DAY, EARLY)
        assert before
    run = receive(database, "CRM", "crm", [
        row("household", "ended-household", id=FIRST_HH, name="Synthetic first household",
            valid_to=DAY.isoformat()),
    ], file_day=file_day, known=LATE)
    with read_snapshot(database) as connection:
        assert conflicts_at(connection, FIRST_HH, DAY, LATE) == []
        assert household_context(connection, FIRST_HH, DAY, LATE).household is None
        assert conflicts_at(connection, FIRST_HH, DAY, LATE - timedelta(microseconds=1)) == before
        assert observed_conflicts_at(connection, FIRST_HH, file_day, LATE) == []
        observation = connection.execute(
            "SELECT payload FROM conflict_snapshots WHERE run_id=? AND household_id=?",
            (run, FIRST_HH),
        ).fetchone()
        assert observation is not None and observation[0] == "[]"
        assert observed_conflicts_at(connection, FIRST_HH, DAY, EARLY) == before


def test_future_valuation_day_has_current_conflicts_before_its_first_feed_run(
    received_database: Path,
) -> None:
    database = received_database
    received = EARLY + timedelta(hours=1)
    receive(database, "CRM", "crm", [
        row("person", "ambiguous-a", id="WC-PER-0001", name="Synthetic assertion A",
            external_person_key="P1"),
        row("person", "ambiguous-z", id="WC-PER-0001", name="Synthetic assertion Z",
            external_person_key="P1"),
    ], known=received)
    future_day = date(2026, 10, 8)
    with read_snapshot(database) as connection:
        current = conflicts_at(connection, FIRST_HH, future_day, received)
        assert {item["code"] for item in current} == {"AMBIGUOUS_ENTITY"}
        assert current == household_context(connection, FIRST_HH, future_day, received).conflicts
        data, _, _ = calculate_snapshot(connection, FIRST_HH, future_day, received)
        assert {item.code for item in data.conflicts} == {"AMBIGUOUS_ENTITY"}
        assert observed_conflicts_at(connection, FIRST_HH, future_day, received) is None
        assert connection.execute(
            "SELECT COUNT(*) FROM conflict_snapshots WHERE business_date=?",
            (future_day.isoformat(),),
        ).fetchone()[0] == 0
