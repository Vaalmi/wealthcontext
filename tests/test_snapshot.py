from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from wealthcontext.clock import FrozenClock
from wealthcontext.database import migrate, write_transaction
from wealthcontext.feeds import AccountReference, FeedName, SourceSystem, build_delivery
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.snapshot import (
    AccountSummary,
    CompleteSnapshot,
    calculate_snapshot,
    money,
    valuation_date,
)
from wealthcontext.storage import migrate_business, read_snapshot, seed_vertical_slice


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "db.sqlite"
    migrate(path)
    migrate_business(path)
    seed_vertical_slice(path, datetime(2035, 1, 1, tzinfo=UTC))
    return path


def test_snapshot_calculates_received_usd_fixture(database: Path) -> None:
    with read_snapshot(database) as connection:
        data, flags, sources = calculate_snapshot(
            connection, "WC-HH-0001", date(2026, 9, 30), datetime(2035, 1, 1, tzinfo=UTC),
        )
    assert isinstance(data, CompleteSnapshot)
    assert data.assets_usd == "3000.00"
    assert data.cash_usd == "500.00"
    assert data.positions[0].market_value_usd == "2500.00"
    assert not flags and len(sources) == 6


def test_missing_pas_row_never_becomes_a_partial_total(database: Path) -> None:
    with write_transaction(database) as connection:
        connection.execute("DELETE FROM facts WHERE kind='position'")
    with read_snapshot(database) as connection:
        data, flags, _ = calculate_snapshot(
            connection, "WC-HH-0001", date(2026, 9, 30), datetime(2035, 1, 1, tzinfo=UTC),
        )
    assert not isinstance(data, CompleteSnapshot)
    assert "assets_usd" not in data.model_dump()
    assert data.fresh_subtotal_usd == "0.00"
    assert flags[0].code == "SOURCE_UNAVAILABLE"


def test_no_carry_forward_when_daily_authority_is_missing(database: Path) -> None:
    with read_snapshot(database) as connection:
        data, flags, _ = calculate_snapshot(
            connection, "WC-HH-0001", date(2026, 10, 1), datetime(2035, 1, 1, tzinfo=UTC),
        )
    assert not isinstance(data, CompleteSnapshot)
    assert flags[0].code == "SOURCE_UNAVAILABLE"


def test_valuation_resolves_weekend_then_range_checks() -> None:
    assert valuation_date(date(2026, 10, 18)) == date(2026, 10, 16)
    assert valuation_date(date(2026, 10, 12)) == date(2026, 10, 12)
    with pytest.raises(ValueError, match="AS_OF_OUT_OF_RANGE"):
        valuation_date(date(2026, 9, 13))


@pytest.mark.parametrize("value,expected", [
    ("1000000000000000000000000000", "1000000000000000000000000000.00"),
    ("9999999999999999999999999999", "9999999999999999999999999999.00"),
    ("9999999999999999999999999.999", "10000000000000000000000000.00"),
    ("-9999999999999999999999999.999", "-10000000000000000000000000.00"),
    ("1E+54", "1" + "0" * 54 + ".00"),
    ("0.005", "0.01"),
    ("-0.005", "-0.01"),
])
def test_money_output_has_room_for_integer_digits_cents_and_rounding_carry(value, expected):
    with localcontext() as calculation:
        calculation.prec = 28
        calculation.rounding = ROUND_HALF_EVEN
        assert money(Decimal(value)) == expected
        assert calculation.prec == 28 and calculation.rounding == ROUND_HALF_EVEN
        assert not any(calculation.flags.values())


def received_portfolio(
    tmp_path: Path, *, quantity: str, price: str, cash: str, ownership: bool = True,
) -> Path:
    """Publish two complete accounts through real feed validation and conflict observation."""
    path = tmp_path / "received.sqlite"
    migrate(path)
    migrate_business(path)
    migrate_feeds(path)
    day = date(2026, 9, 30)
    clock = FrozenClock(datetime(2026, 9, 30, 22, tzinfo=UTC))
    accounts = [AccountReference(account_id=f"WC-AC-{index}", custodian="A",
                                 account_number=f"0042001{index}") for index in (1, 2)]

    def row(kind: str, key: str, **values) -> dict:
        return {"kind": kind, "source_record_id": key, "valid_from": day.isoformat(),
                "valid_to": "2026-10-01", **values}

    def receive(source: SourceSystem, feed: FeedName, records: list[dict]) -> None:
        ingest_delivery(path, build_delivery(
            source_system=source, feed=feed, business_date=day, records=records,
            expected_accounts=accounts if feed in {"pas_snapshots", "custody_snapshots"} else [],
        ), clock)

    crm = [row("household", "household", id="WC-HH-1", name="Synthetic household"),
           row("person", "person", id="WC-PER-1", name="Synthetic person",
               external_person_key="EXT-PER-1"),
           row("membership", "membership", person_id="WC-PER-1", household_id="WC-HH-1")]
    for account in accounts:
        crm.extend([
            row("account", account.account_id, id=account.account_id, custodian="A",
                account_number=account.account_number, is_managed=True),
            row("reporting", f"reporting-{account.account_id}", account_id=account.account_id,
                household_id="WC-HH-1"),
        ])
    receive("CRM", "crm", crm)
    if ownership:
        receive("CUSTODY-A", "custody_accounts", [
            row("ownership", account.account_id, custodian="A",
                account_number=account.account_number, title="Synthetic account",
                registration="INDIVIDUAL", status="OPEN", owners=[{
                    "entity_id": "WC-PER-1", "entity_type": "person", "role": "owner",
                }]) for account in accounts
        ])
    receive("SECMASTER", "securities", [row(
        "security", "security", id="WC-SEC-1", issuer_id="WC-ISS-1",
        issuer_name="Synthetic issuer", name="Synthetic security", ticker="SYN",
        type="COMMON_STOCK", currency="USD", cusip="990000101",
        identifier_status="synthetic_unallocated", listing_country="US",
    )])
    receive("CUSTODY-A", "prices", [row("price", "price", security_id="WC-SEC-1",
                                        currency="USD", price=price, price_date=day.isoformat())])
    snapshots = []
    for account in accounts:
        snapshots.extend([
            row("position", f"position-{account.account_id}", custodian="A",
                account_number=account.account_number, security_id="WC-SEC-1", quantity=quantity),
            row("cash", f"cash-{account.account_id}", custodian="A",
                account_number=account.account_number, currency="USD", amount=cash),
        ])
    # PAS publication persists conflicts and therefore formats reconciliation
    # before custody arrives; neither this path nor the later valuation may fail.
    receive("PAS-A", "pas_snapshots", snapshots)
    receive("CUSTODY-A", "custody_snapshots", snapshots)
    return path


@pytest.mark.parametrize("quantity,price,cash,position_value,total,cash_total", [
    ("0", "0", "1000000000000000000000000000", "0.00",
     "2000000000000000000000000000.00", "2000000000000000000000000000.00"),
    ("0", "0", "9000000000000000000000000000", "0.00",
     "18000000000000000000000000000.00", "18000000000000000000000000000.00"),
    ("0", "0", "9" * 28, "0.00", "2" + "0" * 28 + ".00", "2" + "0" * 28 + ".00"),
    ("1000000000000000000000000000", "1000000000000000000000000000", "0",
     "1" + "0" * 54 + ".00", "2" + "0" * 54 + ".00", "0.00"),
    ("9" * 28, "9" * 28, "0",
     "9" * 27 + "8" + "0" * 28 + ".00", "2" + "0" * 56 + ".00", "0.00"),
])
def test_received_boundary_values_publish_reconcile_and_sum_without_output_overflow(
    tmp_path, quantity, price, cash, position_value, total, cash_total,
):
    path = received_portfolio(tmp_path, quantity=quantity, price=price, cash=cash)
    with read_snapshot(path) as connection:
        runs = connection.execute("SELECT id,status FROM feed_runs").fetchall()
        assert len(runs) == 6 and {run["status"] for run in runs} == {"SUCCESS"}
        assert connection.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM conflict_snapshots c JOIN feed_runs r ON r.id=c.run_id "
            "WHERE r.feed='pas_snapshots' AND c.household_id='WC-HH-1'",
        ).fetchone()[0] == 1
        result, flags, _ = calculate_snapshot(
            connection, "WC-HH-1", date(2026, 9, 30), datetime(2026, 10, 1, tzinfo=UTC),
        )
    assert isinstance(result, CompleteSnapshot)
    assert result.assets_usd == total and result.cash_usd == cash_total
    assert [position.market_value_usd for position in result.positions] == [position_value] * 2
    assert all(row.reconciliation_status == "MATCHED"
               for account in result.reconciliation for row in account.rows)
    assert flags == []


def test_missing_ownership_metadata_is_disclosed_but_received_valuation_remains_complete(tmp_path):
    path = received_portfolio(tmp_path, quantity="10", price="25", cash="500", ownership=False)
    with read_snapshot(path) as connection:
        result, flags, _ = calculate_snapshot(
            connection, "WC-HH-1", date(2026, 9, 30), datetime(2026, 10, 1, tzinfo=UTC),
        )
    assert isinstance(result, CompleteSnapshot)
    assert result.assets_usd == "1500.00" and result.cash_usd == "1000.00"
    assert {flag.code for flag in flags} == {"OWNERSHIP_UNAVAILABLE"}
    assert {flag.subject for flag in flags} == {"WC-AC-1", "WC-AC-2"}
    assert all(flag.detail["affects_valuation"] is False for flag in flags)
    for account in result.accounts:
        assert account.title == "Account title unavailable"
        assert account.status is None and account.registration is None
        assert account.legal_owners == []


@pytest.mark.parametrize("changes", [
    {"custodian": "UNRECOGNIZED"},
    {"registration": "UNRECOGNIZED"},
    {"legal_owners": [{"entity_id": "WC-PER-1", "entity_type": "person", "role": "owner",
                       "ownership_percentage": "50.00"}]},
    {"legal_owners": [{"entity_id": "WC-PER-1", "entity_type": "company", "role": "owner"}]},
    {"legal_owners": [{"entity_id": "WC-PER-1", "entity_type": "person", "role": "adviser"}]},
])
def test_account_summary_rejects_unknown_ownership_fields_in_python_and_schema(changes):
    valid = {"id": "WC-AC-1", "title": "Synthetic account", "custodian": "A",
             "registration": "INDIVIDUAL", "legal_owners": [
                 {"entity_id": "WC-PER-1", "entity_type": "person", "role": "owner"},
             ]}
    account = AccountSummary.model_validate(valid)
    assert account.legal_owners[0].entity_id == "WC-PER-1"
    validator = Draft202012Validator(AccountSummary.model_json_schema())
    validator.validate(valid)
    invalid = {**valid, **changes}
    assert not validator.is_valid(invalid)
    with pytest.raises(ValidationError):
        AccountSummary.model_validate(invalid)
