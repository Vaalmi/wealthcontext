"""Compare independent calculations of the same received bytes, never latent truth.

Only this integration boundary imports both implementations. The oracle package
and its hand-authored tests remain independent of every production module.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from test_reference_oracle import A1, DAY, HH, Export, cash, position, quote
from test_reference_oracle import security as security_row

from reference_oracle import History, evaluate, load_received_history
from wealthcontext.calculation import GroupBy, concentrated_positions
from wealthcontext.clock import FrozenClock
from wealthcontext.database import migrate
from wealthcontext.exposure import serialize_concentration, serialize_exposure
from wealthcontext.generator import load_history, write_history
from wealthcontext.ingestion import import_fixture_history, migrate_feeds
from wealthcontext.storage import migrate_business, read_snapshot
from wealthcontext.valuation import resolve_received_valuation, serialize_received_snapshot

HOUSEHOLDS = [f"WC-HH-{number:04}" for number in range(1, 15)]
KNOWN = datetime(2026, 10, 17, 12, tzinfo=UTC)
QUARTER_END = date(2026, 9, 30)
DAYS = [
    day for offset in range(33)
    if (day := date(2026, 9, 14) + timedelta(days=offset)).weekday() < 5
]
FINANCE_FLAGS = {
    "AMBIGUOUS", "AMBIGUOUS_ENTITY", "SOURCE_UNAVAILABLE", "FX_UNAVAILABLE", "PRICE_STALE",
    "PRICE_SOURCE_MISMATCH", "HELD_AWAY_EXCLUDED", "CLOSED_ACCOUNT_WITH_BALANCE",
    "RECONCILIATION_UNAVAILABLE", "REPORTING_HOUSEHOLD_CONFLICT",
}


@dataclass(frozen=True)
class Received:
    directory: Path
    database: Path
    history: History


def import_export(directory: Path, database: Path, imported: datetime) -> None:
    migrate(database)
    migrate_business(database)
    migrate_feeds(database)
    # The producer's in-memory event list is deliberately discarded. Both paths
    # re-read the exported bytes and their persisted receipt timestamps.
    import_fixture_history(database, load_history(directory), FrozenClock(imported))


@pytest.fixture(scope="module")
def received(tmp_path_factory) -> Received:
    directory = tmp_path_factory.mktemp("p3-oracle-received")
    write_history(directory)
    database = directory / "runtime.sqlite"
    import_export(directory, database, datetime(2055, 3, 1, tzinfo=UTC))
    return Received(directory, database, load_received_history(directory))


def subject(value: str) -> str:
    """Cash component identifiers differ in spelling, not financial scope."""
    return value.replace(":CASH:", ":")


def flags(values: list[dict]) -> set[tuple[str, str, str]]:
    return {
        (item["code"], subject(item["subject"]), item.get("severity", "warning"))
        for item in values if item["code"] in FINANCE_FLAGS
    }


def fx_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def compare_list(actual: dict, expected: dict, identifier: str, percentage: str) -> None:
    for key in ("item_count", "total_item_count", "truncated", "next_cursor", "rank_scope"):
        assert actual[key] == expected[key], key
    # Ordering syntax is implementation-specific; exact ordered rows prove the
    # declared descending amount / ascending identifier semantics instead.
    assert actual["ordering"] and expected["ordering"]
    assert [
        (row["entity_id"], row["exposure_usd"], row["rank"], row.get(percentage))
        for row in actual["items"]
    ] == [
        (row[identifier], row["market_value_usd"], row["rank"], row.get(percentage))
        for row in expected["items"]
    ]


def compare_household(actual, expected: dict) -> None:
    value = actual.valuation
    snapshot = serialize_received_snapshot(actual).model_dump(mode="json")
    assert value.complete == (expected["status"] == "COMPLETE")
    assert sorted(row["id"] for row in snapshot["accounts"]) == expected["included_account_ids"]
    assert sorted(row["id"] for row in snapshot["excluded_accounts"]) == (
        expected["excluded_account_ids"]
    )
    expected_cash = expected["cash_usd"] if value.complete else expected["fresh_cash_subtotal_usd"]
    assert value.cash_usd == Decimal(expected_cash)
    if value.complete:
        assert snapshot["assets_usd"] == expected["assets_usd"]
        assert snapshot["cash_usd"] == expected["cash_usd"]
    else:
        assert "assets_usd" not in snapshot and "assets_usd" not in expected
        assert snapshot["fresh_subtotal_usd"] == expected["fresh_subtotal_usd"]
        assert sorted(map(subject, snapshot["missing_components"])) == sorted(
            map(subject, expected["missing_components"])
        )
        assert list(value.missing_account_ids) == expected["missing_account_ids"]
    assert sorted(
        (row["account_id"], row["security_id"], Decimal(row["quantity"]),
         Decimal(row["price_local"]), row["market_value_usd"], row["price_date"],
         row["price_age_business_days"], row["listing_currency"],
         Decimal(row["fx_rate"]), fx_time(row["rate_at"]))
        for row in snapshot["positions"]
    ) == sorted(
        (row["account_id"], row["security_id"], Decimal(row["quantity"]),
         Decimal(row["price"]), row["market_value_usd"], row["price_date"],
         row["price_age_business_days"], row["fx"]["currency"],
         Decimal(row["fx"]["usd_rate"]), fx_time(row["fx"]["rate_at"]))
        for row in expected["positions"]
    )
    assert sorted(
        (row["account_id"], row["currency"], Decimal(row["amount_local"]),
         row["market_value_usd"], Decimal(row["fx_rate"]), fx_time(row["rate_at"]))
        for row in snapshot["cash_balances"]
    ) == sorted(
        (row["account_id"], row["currency"], Decimal(row["amount_local"]),
         row["value_usd"], Decimal(row["fx"]["usd_rate"]), fx_time(row["fx"]["rate_at"]))
        for row in expected["cash_components"]
    )
    assert [
        (row["subject"], row["last_known_value_usd"], row["price_date"],
         row["price_age_business_days"])
        for row in snapshot.get("stale_components", [])
    ] == [
        (row["subject"], row["last_known_value_usd"], row["price_date"],
         row["age_business_days"])
        for row in expected["stale_components"]
    ]
    actual_flags = [flag.model_dump(mode="json") for flag in actual.quality_flags]
    assert flags(actual_flags) == flags(expected["quality_flags"])
    for group_by, lists in (
        ("issuer", [("issuer_exposures", "issuer_id", "concentration_pct"),
                    ("fund_vehicle_exposures", "security_id", "allocation_pct")]),
        ("security", [("security_exposures", "security_id", "allocation_pct")]),
    ):
        exposure = serialize_exposure(
            value, snapshot["household_name"], cast(GroupBy, group_by),
        ).model_dump(mode="json")
        for key, identifier, percentage in lists:
            if value.complete:
                compare_list(exposure[key], expected[key], identifier, percentage)
            else:
                assert not exposure[key]["items"] and exposure[key]["total_item_count"] == 0
                assert key not in expected


def compare_book(values, expected: dict) -> None:
    actual = serialize_concentration(concentrated_positions(values)).model_dump(mode="json")
    for key in (
        "applied_threshold_pct", "comparator", "evaluated_household_count",
        "determinate_household_count", "indeterminate_household_count", "breaching_household_count",
    ):
        assert actual[key] == expected[key], key
    household_assets = {
        row["household_id"]: row.get("assets_usd") for row in expected["households"]
    }
    assert [
        (row["household_id"], row["issuer_id"], row["exposure_usd"], row["assets_usd"],
         row["concentration_pct"], row["book_rank"], row["issuer_rank"], row["meets_threshold"])
        for row in actual["breaches"]
    ] == [
        (row["household_id"], row["issuer_id"], row["market_value_usd"],
         household_assets[row["household_id"]], row["concentration_pct"],
         row["book_rank"], row["issuer_rank"], True)
        for row in expected["breaches"]
    ]
    assert [
        (row["household_id"], row["reason"].replace("NON_POSITIVE", "NONPOSITIVE"),
         row["fresh_subtotal_usd"], row["stale_component_count"], row["missing_account_ids"],
         flags(row["blocking_flags"]))
        for row in actual["indeterminate"]
    ] == [
        (row["household_id"], row["reason"], row["fresh_subtotal_usd"],
         row["stale_component_count"], row["missing_account_ids"], flags(row["blocking_flags"]))
        for row in expected["indeterminate"]
    ]
    for key in ("breach_metadata", "indeterminate_metadata"):
        for field in ("item_count", "total_item_count", "truncated", "next_cursor"):
            assert actual[key][field] == expected[key][field]
    assert actual["breach_metadata"]["rank_scope"] == expected["breach_metadata"]["rank_scope"]


@pytest.mark.parametrize("day", DAYS, ids=lambda day: day.isoformat())
def test_entire_received_book_agrees_across_all_business_dates(received, day):
    expected = evaluate(received.history, household_ids=HOUSEHOLDS, as_of=day, known_as_of=KNOWN)
    with read_snapshot(received.database) as connection:
        values = []
        for household in expected["households"]:
            actual = resolve_received_valuation(connection, household["household_id"], day, KNOWN)
            compare_household(actual, household)
            values.append(actual.valuation)
        compare_book(values, expected)


def test_manual_s03_s10_s15_b1_b2_and_optional_pas_price_results(received):
    def oracle(household, day=QUARTER_END):
        return evaluate(received.history, household_ids=[f"WC-HH-{household:04}"],
                        as_of=day, known_as_of=KNOWN)

    full_fx = oracle(10)["households"][0]
    assert (full_fx["assets_usd"], full_fx["cash_usd"]) == ("1230.00", "410.00")
    missing_fx = oracle(10, date(2026, 10, 8))["households"][0]
    assert "assets_usd" not in missing_fx
    assert (missing_fx["fresh_subtotal_usd"], missing_fx["fresh_cash_subtotal_usd"]) == (
        "730.00", "285.00",
    )
    stale = oracle(3, date(2026, 10, 15))["households"][0]
    assert stale["fresh_subtotal_usd"] == "300.00"
    assert stale["stale_components"][0]["last_known_value_usd"] == "1120.00"
    assert stale["stale_components"][0]["age_business_days"] == 4
    # S03 intentionally overlaps the S07 PAS-B outage; neither condition is erased.
    assert stale["missing_account_ids"] == ["WC-AC-0006"]
    assert {row["code"] for row in stale["quality_flags"]} >= {
        "PRICE_STALE", "SOURCE_UNAVAILABLE", "PRICE_SOURCE_MISMATCH",
    }
    boundary = oracle(14)
    household = boundary["households"][0]
    assert (household["assets_usd"], household["cash_usd"]) == ("1000.00", "750.00")
    assert household["fund_vehicle_exposures"]["items"][0]["allocation_pct"] == "15.00"
    assert len(boundary["breaches"]) == 1
    assert boundary["breaches"][0]["concentration_pct"] == "10.00"
    assert household["excluded_account_ids"] == ["WC-AC-0031", "WC-AC-0032"]
    zero = oracle(1, date(2026, 10, 16))
    assert zero["households"][0]["assets_usd"] == "0.00"
    assert not zero["breaches"] and len(zero["indeterminate"]) == 1
    mismatch = oracle(12)["households"][0]
    observed = [flag for flag in mismatch["quality_flags"]
                if flag["code"] == "PRICE_SOURCE_MISMATCH"]
    assert len(observed) == 1 and observed[0]["severity"] == "info"
    detail = observed[0]["detail"]
    assert detail["reasons"] == ["PRICE"] and detail["authoritative_source"] == "CUSTODY"
    assert Decimal(detail["pas_price"]["price"]) - Decimal(detail["custody_price"]["price"]) == 1
    position = next(row for row in mismatch["positions"] if row["account_id"] == "WC-AC-0023")
    assert position["price"] == detail["custody_price"]["price"]
    assert detail["pas_evidence"]["source_record_id"]
    assert detail["custody_evidence"]["source_record_id"]
    with read_snapshot(received.database) as connection:
        actual = resolve_received_valuation(connection, "WC-HH-0012", QUARTER_END, KNOWN)
        runtime_flags = [flag for flag in actual.quality_flags
                         if flag.code == "PRICE_SOURCE_MISMATCH"]
        assert len(runtime_flags) == 1 and runtime_flags[0].detail["reasons"] == ["PRICE"]
        compare_household(actual, mismatch)


@pytest.mark.parametrize("known,total,cash", [
    (datetime(2026, 10, 6, 13, 59, 59, 999999, tzinfo=UTC), "3500.00", "1000.00"),
    (datetime(2026, 10, 6, 14, tzinfo=UTC), "3549.00", "799.00"),
])
def test_s09_exact_receipt_cutoffs_agree_after_far_future_bootstrap(received, known, total, cash):
    expected = evaluate(received.history, household_ids=["WC-HH-0009"],
                        as_of=QUARTER_END, known_as_of=known)
    household = expected["households"][0]
    assert (household["assets_usd"], household["cash_usd"]) == (total, cash)
    with read_snapshot(received.database) as connection:
        actual = resolve_received_valuation(connection, "WC-HH-0009", QUARTER_END, known)
        compare_household(actual, household)
        compare_book([actual.valuation], expected)
        assert {row[0] for row in connection.execute("SELECT imported_at FROM feed_runs")} == {
            "2055-03-01T00:00:00.000000+00:00",
        }


def test_received_manifest_population_includes_duplicate_missing_and_truncated_files(received):
    receipts = received.history.receipts
    assert len(receipts) == 384
    present = [receipt for receipt in receipts if receipt.content is not None]
    assert len(present) == 383
    assert len({receipt.content_name for receipt in present}) == 383
    assert len({receipt.content_hash for receipt in present}) == 382
    assert sum(row is None for receipt in present for row in receipt.rows) == 1
    assert all(receipt.header_valid for receipt in present)


@pytest.mark.parametrize("security_state,price_state,expected_flags", [
    ("ambiguous", "valid", {("AMBIGUOUS", "security")}),
    ("valid", "ambiguous", {("AMBIGUOUS", "price")}),
    ("ambiguous", "missing", {("AMBIGUOUS", "security"), ("SOURCE_UNAVAILABLE", "price")}),
    ("missing", "ambiguous", {("SOURCE_UNAVAILABLE", "security"), ("AMBIGUOUS", "price")}),
    ("ambiguous", "ambiguous", {("AMBIGUOUS", "security"), ("AMBIGUOUS", "price")}),
    ("missing", "missing", {("SOURCE_UNAVAILABLE", "security"), ("SOURCE_UNAVAILABLE", "price")}),
])
def test_independent_security_and_price_flags_agree_before_and_after_received_correction(
    tmp_path, security_state, price_state, expected_flags,
):
    export = Export(tmp_path)
    export.crm()
    export.snapshots([position("1"), cash("7")])
    definitions = [] if security_state == "missing" else [security_row()]
    if security_state == "ambiguous":
        definitions.append({**security_row(), "name": "Competing synthetic security"})
    if definitions:
        export.feed("SECMASTER", "securities", definitions)
    prices = [] if price_state == "missing" else [quote("25")]
    if price_state == "ambiguous":
        prices.append(quote("26"))
    if prices:
        export.feed("CUSTODY-A", "prices", prices)
    correction = datetime(2026, 10, 2, 14, tzinfo=UTC)
    before = correction - timedelta(microseconds=1)
    historical = evaluate(load_received_history(tmp_path), household_ids=[HH], as_of=DAY,
                          known_as_of=before)
    export.feed("SECMASTER", "securities", [security_row()], at=correction.isoformat())
    export.feed("CUSTODY-A", "prices", [quote("25")], at=correction.isoformat())
    database = tmp_path / "agreement.sqlite"
    import_export(tmp_path, database, datetime(2055, 1, 1, tzinfo=UTC))
    history = load_received_history(tmp_path)
    assert evaluate(history, household_ids=[HH], as_of=DAY, known_as_of=before) == historical
    with read_snapshot(database) as connection:
        for known in (before, correction):
            expected = evaluate(history, household_ids=[HH], as_of=DAY, known_as_of=known)
            actual = resolve_received_valuation(connection, HH, DAY, known)
            compare_household(actual, expected["households"][0])
            compare_book([actual.valuation], expected)
            if known == before:
                assert expected["households"][0]["fresh_subtotal_usd"] == "7.00"
                assert {(flag.code, flag.detail.get("source")) for flag in actual.quality_flags
                        if flag.subject == A1["account_id"] + ":WC-SEC-0001"} == expected_flags
            else:
                assert expected["households"][0]["assets_usd"] == "32.00"
                assert not actual.valuation.blocking_flags
