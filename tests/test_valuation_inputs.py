"""Valuation guards exercised through hand-received files and real ingestion."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from wealthcontext.clock import FrozenClock, utc_timestamp
from wealthcontext.database import migrate
from wealthcontext.facts import account_snapshot, fact_view
from wealthcontext.feeds import AccountReference, FeedName, SourceSystem, build_delivery
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.prices import price_view
from wealthcontext.snapshot import CompleteSnapshot, IncompleteSnapshot, calculate_snapshot
from wealthcontext.storage import migrate_business, read_snapshot

DAY = date(2026, 9, 30)
RECEIVED = datetime(2026, 9, 30, 22, tzinfo=UTC)
CORRECTED = datetime(2026, 10, 2, 14, tzinfo=UTC)
BEFORE_CORRECTION = CORRECTED - timedelta(microseconds=1)
ACCOUNT = AccountReference(account_id="WC-AC-0001", custodian="A", account_number="00420017")
HOUSEHOLD = "WC-HH-0001"
CLEAN_SECURITY = "WC-SEC-0001"
TARGET_SECURITY = "WC-SEC-0002"
TARGET_COMPONENT = f"{ACCOUNT.account_id}:{TARGET_SECURITY}"
GuardCase = Literal[
    "missing_security", "ambiguous_security", "future_price", "wrong_price_currency",
]


def row(kind: str, record_id: str, **fields: Any) -> dict:
    return {"kind": kind, "source_record_id": record_id, "valid_from": DAY.isoformat(),
            "valid_to": "2026-10-01", **fields}


def instrument(*, target: bool, record_id: str | None = None, name: str | None = None) -> dict:
    number = 2 if target else 1
    return row("security", record_id or f"security-{number}",
               id=TARGET_SECURITY if target else CLEAN_SECURITY,
               issuer_id=f"WC-ISS-{number:04}", issuer_name=f"Synthetic issuer {number}",
               name=name or f"Synthetic security {number}", ticker=f"SYN{number}",
               type="COMMON_STOCK", currency="USD", cusip="990000119" if target else "990000101",
               identifier_status="synthetic_unallocated",
               isin="US9900001192" if target else "US9900001010", listing_country="US")


def quote(*, target: bool, **changes: Any) -> dict:
    number = 2 if target else 1
    return row("price", f"price-{number}", **{
        "security_id": TARGET_SECURITY if target else CLEAN_SECURITY, "currency": "USD",
        "price": "25.00" if target else "100.00", "price_date": DAY.isoformat(), **changes,
    })


def receive(
    database: Path, source: SourceSystem, feed: FeedName, records: list[dict],
    *, at: datetime = RECEIVED, day: date = DAY,
) -> str:
    delivery = build_delivery(
        source_system=source, feed=feed, business_date=day, records=records,
        expected_accounts=[ACCOUNT] if feed in {"pas_snapshots", "custody_snapshots"} else [],
    )
    return ingest_delivery(database, delivery, FrozenClock(at))


@pytest.fixture
def received_account(tmp_path: Path) -> Path:
    database = tmp_path / "valuation.sqlite"
    migrate(database)
    migrate_business(database)
    migrate_feeds(database)
    receive(database, "CRM", "crm", [
        row("household", "household", id=HOUSEHOLD, name="Synthetic household"),
        row("person", "person", id="WC-PER-0001", external_person_key="P-1",
            name="Synthetic person"),
        row("membership", "membership", person_id="WC-PER-0001", household_id=HOUSEHOLD),
        row("account", "account", id=ACCOUNT.account_id, custodian=ACCOUNT.custodian,
            account_number=ACCOUNT.account_number, is_managed=True),
        row("reporting", "reporting", account_id=ACCOUNT.account_id, household_id=HOUSEHOLD),
    ], at=RECEIVED - timedelta(hours=1))
    receive(database, "CUSTODY-A", "custody_accounts", [
        row("ownership", "ownership", custodian=ACCOUNT.custodian,
            account_number=ACCOUNT.account_number, title="Synthetic individual",
            registration="INDIVIDUAL", status="OPEN", owners=[{
                "entity_id": "WC-PER-0001", "entity_type": "person", "role": "owner",
            }]),
    ])
    snapshots = [
        row("position", "clean-position", custodian=ACCOUNT.custodian,
            account_number=ACCOUNT.account_number, security_id=CLEAN_SECURITY, quantity="2"),
        row("position", "target-position", custodian=ACCOUNT.custodian,
            account_number=ACCOUNT.account_number, security_id=TARGET_SECURITY, quantity="10"),
        row("cash", "cash", custodian=ACCOUNT.custodian, account_number=ACCOUNT.account_number,
            currency="USD", amount="500.00"),
    ]
    receive(database, "PAS-A", "pas_snapshots", snapshots)
    receive(database, "CUSTODY-A", "custody_snapshots", snapshots)
    return database


@pytest.mark.parametrize("case", [
    "missing_security", "ambiguous_security", "future_price", "wrong_price_currency",
])
def test_invalid_received_valuation_input_blocks_only_affected_component_until_correction(
    received_account: Path, case: GuardCase,
) -> None:
    database = received_account
    securities = [instrument(target=False)]
    if case == "ambiguous_security":
        # Both received assertions are individually valid. A stable lexical
        # source-record order cannot manufacture a winning security definition.
        securities.extend([
            instrument(target=True, record_id="AAA-target", name="Synthetic target assertion A"),
            instrument(target=True, record_id="ZZZ-target", name="Synthetic target assertion B"),
        ])
    elif case != "missing_security":
        securities.append(instrument(target=True))
    receive(database, "SECMASTER", "securities", securities)
    bad_quote = quote(target=True)
    if case == "future_price":
        bad_quote["price_date"] = "2026-10-01"
    elif case == "wrong_price_currency":
        bad_quote["currency"] = "EUR"
    receive(database, "CUSTODY-A", "prices", [quote(target=False), bad_quote])

    with read_snapshot(database) as connection:
        assert account_snapshot(connection, "pas_snapshots", ACCOUNT.account_id,
                                DAY, BEFORE_CORRECTION).available
        assert account_snapshot(connection, "custody_snapshots", ACCOUNT.account_id,
                                DAY, BEFORE_CORRECTION).available
        definitions = fact_view(connection, "security", DAY, BEFORE_CORRECTION)
        if case == "missing_security":
            assert TARGET_SECURITY not in definitions.values
            assert TARGET_SECURITY not in definitions.ambiguous
        elif case == "ambiguous_security":
            assert TARGET_SECURITY not in definitions.values
            assert len(definitions.ambiguous[TARGET_SECURITY]) == 2
        else:
            assert definitions.values[TARGET_SECURITY]["currency"] == "USD"
            received_price = fact_view(connection, "price", DAY, BEFORE_CORRECTION)
            assert received_price.values[TARGET_SECURITY]["price_date"] == bad_quote["price_date"]
            assert received_price.values[TARGET_SECURITY]["currency"] == bad_quote["currency"]
        # These are valid received records, not syntax/quarantine failures. The
        # authoritative calculation must enforce the cross-input constraints.
        assert connection.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 0
        statuses = {item[0] for item in connection.execute("SELECT status FROM feed_runs")}
        assert statuses == {"SUCCESS"}
        incomplete, flags, original_sources = calculate_snapshot(
            connection, HOUSEHOLD, DAY, BEFORE_CORRECTION,
        )
    assert isinstance(incomplete, IncompleteSnapshot)
    assert "assets_usd" not in incomplete.model_dump(mode="json")
    # Independent hand calculation: clean 2 x USD100 plus cash USD500 = USD700.
    assert incomplete.fresh_subtotal_usd == "700.00"
    assert incomplete.missing_components == [TARGET_COMPONENT]
    assert incomplete.stale_components == []  # A future close is not an old close.
    assert [(item.security_id, item.market_value_usd) for item in incomplete.positions] == [
        (CLEAN_SECURITY, "200.00"),
    ]
    expected_code = "AMBIGUOUS" if case == "ambiguous_security" else "SOURCE_UNAVAILABLE"
    blocking = [flag for flag in flags if flag.code == expected_code]
    assert len(blocking) == 1 and blocking[0].subject == TARGET_COMPONENT
    assert blocking[0].detail["source"] == (
        "security" if case in {"missing_security", "ambiguous_security"} else "price"
    )
    assert not {"FX_UNAVAILABLE", "PRICE_STALE", "QUANTITY_BREAK", "CASH_BREAK"}.intersection(
        flag.code for flag in flags
    )

    if case in {"missing_security", "ambiguous_security"}:
        # A newly asserted correction has a new source-record identity. Merely
        # re-ingesting one old assertion cannot resolve a simultaneous conflict.
        receive(database, "SECMASTER", "securities", [
            instrument(target=True, record_id="target-definition-correction"),
        ], at=CORRECTED)
        corrected_kind = "security"
    else:
        receive(database, "CUSTODY-A", "prices", [quote(target=True)], at=CORRECTED)
        corrected_kind = "price"
    with read_snapshot(database) as connection:
        historical, historical_flags, historical_sources = calculate_snapshot(
            connection, HOUSEHOLD, DAY, BEFORE_CORRECTION,
        )
        restored, restored_flags, restored_sources = calculate_snapshot(
            connection, HOUSEHOLD, DAY, CORRECTED,
        )
        corrected = fact_view(connection, corrected_kind, DAY, CORRECTED).values[TARGET_SECURITY]
        assert corrected["recorded_at"] == utc_timestamp(CORRECTED)
        assert corrected["source_id"] in restored_sources
    assert historical.model_dump(mode="json") == incomplete.model_dump(mode="json")
    assert historical_flags == flags and historical_sources == original_sources
    assert isinstance(restored, CompleteSnapshot)
    # Add the corrected target: 10 x USD25 = USD250; total USD700 + USD250 = USD950.
    assert restored.assets_usd == "950.00" and restored.cash_usd == "500.00"
    assert {item.security_id: item.market_value_usd for item in restored.positions} == {
        CLEAN_SECURITY: "200.00", TARGET_SECURITY: "250.00",
    }
    assert restored_flags == []


@pytest.mark.parametrize("delivery_shape", ["late_old_close", "multiple_closes", "future_close"])
def test_price_date_precedes_receipt_time_and_other_closes_do_not_create_ambiguity(
    received_account: Path, delivery_shape: str,
) -> None:
    database = received_account
    receive(database, "SECMASTER", "securities",
            [instrument(target=False), instrument(target=True)])
    previous = DAY - timedelta(days=1)
    old = quote(target=True, price="20.00", price_date=previous.isoformat(),
                valid_from=previous.isoformat(), valid_to="9999-12-31")
    fresh = quote(target=True)
    future = quote(target=True, price="999.00", price_date="2026-10-01")
    if delivery_shape == "late_old_close":
        receive(database, "CUSTODY-A", "prices", [quote(target=False), fresh])
        receive(database, "CUSTODY-A", "prices", [old], at=CORRECTED, day=previous)
    else:
        receive(database, "CUSTODY-A", "prices", [quote(target=False), fresh,
                                                  old if delivery_shape == "multiple_closes"
                                                  else future])
    with read_snapshot(database) as connection:
        selected = price_view(connection, DAY, CORRECTED)
        assert selected.ambiguous == {}
        assert selected.values[TARGET_SECURITY]["price_date"] == DAY.isoformat()
        assert selected.values[TARGET_SECURITY]["price"] == "25.00"
        snapshot, flags, _sources = calculate_snapshot(connection, HOUSEHOLD, DAY, CORRECTED)
    assert isinstance(snapshot, CompleteSnapshot)
    assert snapshot.assets_usd == "950.00"
    assert flags == []


def test_latest_close_corrections_apply_at_exact_knowledge_cutoff(received_account: Path) -> None:
    receive(received_account, "SECMASTER", "securities",
            [instrument(target=False), instrument(target=True)])
    receive(received_account, "CUSTODY-A", "prices", [quote(target=False), quote(target=True)])
    receive(received_account, "CUSTODY-A", "prices", [quote(target=True, price="26.00")],
            at=CORRECTED)
    with read_snapshot(received_account) as connection:
        before = price_view(connection, DAY, BEFORE_CORRECTION).values[TARGET_SECURITY]
        after = price_view(connection, DAY, CORRECTED).values[TARGET_SECURITY]
        assert before["price"] == "25.00" and after["price"] == "26.00"
        assert before["source_id"] != after["source_id"]
        assert after["recorded_at"] == utc_timestamp(CORRECTED)
        snapshot, flags, sources = calculate_snapshot(connection, HOUSEHOLD, DAY, CORRECTED)
    assert isinstance(snapshot, CompleteSnapshot)
    assert snapshot.assets_usd == "960.00" and after["source_id"] in sources
    assert flags == []


def test_ambiguous_latest_close_does_not_fall_back_to_unambiguous_older_close(
    received_account: Path,
) -> None:
    receive(received_account, "SECMASTER", "securities",
            [instrument(target=False), instrument(target=True)])
    previous = DAY - timedelta(days=1)
    old = quote(target=True, price="20.00", price_date=previous.isoformat(),
                valid_from=previous.isoformat(), valid_to="9999-12-31")
    conflicting = {**quote(target=True, price="26.00"), "source_record_id": "conflicting-price"}
    receive(received_account, "CUSTODY-A", "prices",
            [quote(target=False), old, quote(target=True), conflicting])
    with read_snapshot(received_account) as connection:
        prices = price_view(connection, DAY, BEFORE_CORRECTION)
        assert TARGET_SECURITY not in prices.values
        assert {item["price"] for item in prices.ambiguous[TARGET_SECURITY]} == {"25.00", "26.00"}
        snapshot, flags, _sources = calculate_snapshot(
            connection, HOUSEHOLD, DAY, BEFORE_CORRECTION,
        )
    assert isinstance(snapshot, IncompleteSnapshot)
    assert snapshot.fresh_subtotal_usd == "700.00" and snapshot.stale_components == []
    assert snapshot.missing_components == [TARGET_COMPONENT]
    assert [(flag.code, flag.subject) for flag in flags] == [
        ("AMBIGUOUS", TARGET_COMPONENT),
    ]
    receive(received_account, "CUSTODY-A", "prices", [quote(target=True, price="27.00")],
            at=CORRECTED)
    with read_snapshot(received_account) as connection:
        assert price_view(connection, DAY, CORRECTED).values[TARGET_SECURITY]["price"] == "27.00"


def test_price_end_correction_supersedes_open_interval_before_validity_filter(
    received_account: Path,
) -> None:
    previous = DAY - timedelta(days=1)
    old = quote(target=True, price="20.00", price_date=previous.isoformat(),
                valid_from=previous.isoformat(), valid_to="9999-12-31")
    receive(received_account, "CUSTODY-A", "prices", [old])
    receive(received_account, "CUSTODY-A", "prices", [{**old, "valid_to": DAY.isoformat()}],
            at=CORRECTED)
    with read_snapshot(received_account) as connection:
        assert TARGET_SECURITY in price_view(connection, DAY, BEFORE_CORRECTION).values
        assert TARGET_SECURITY not in price_view(connection, DAY, CORRECTED).values


@pytest.mark.parametrize("resolution", ["same_close_correction", "newer_close"])
def test_competing_price_interval_ends_remain_ambiguous_until_superseded(
    received_account: Path, resolution: str,
) -> None:
    receive(received_account, "SECMASTER", "securities",
            [instrument(target=False), instrument(target=True)])
    previous = DAY - timedelta(days=1)
    start = DAY - timedelta(days=2)
    older = quote(target=True, price="15.00", price_date=start.isoformat(),
                  valid_from=start.isoformat(), valid_to="9999-12-31")
    closed = quote(target=True, price_date=previous.isoformat(),
                   valid_from=start.isoformat(), valid_to=DAY.isoformat())
    opened = {**closed, "valid_to": "9999-12-31"}
    receive(received_account, "CUSTODY-A", "prices",
            [quote(target=False), older, closed, opened])
    with read_snapshot(received_account) as connection:
        # Both latest assertions are active before the exact end boundary and
        # agree on the current value; different future ends alone do not block it.
        prior_day = price_view(connection, previous, BEFORE_CORRECTION)
        assert TARGET_SECURITY not in prior_day.ambiguous
        assert prior_day.values[TARGET_SECURITY]["price"] == "25.00"
        boundary = price_view(connection, DAY, BEFORE_CORRECTION)
        assert TARGET_SECURITY not in boundary.values
        competing = boundary.ambiguous[TARGET_SECURITY]
        assert len(competing) == 2
        assert {item["price_date"] for item in competing} == {previous.isoformat()}
        # Interval-resolution metadata stays internal, including in ambiguity.
        assert all(set(item) == {"security_id", "currency", "price", "price_date", "source_id",
                                 "recorded_at", "source_system"} for item in competing)
        snapshot, flags, _sources = calculate_snapshot(
            connection, HOUSEHOLD, DAY, BEFORE_CORRECTION,
        )
    assert isinstance(snapshot, IncompleteSnapshot)
    assert snapshot.fresh_subtotal_usd == "700.00"
    assert snapshot.missing_components == [TARGET_COMPONENT]
    assert snapshot.stale_components == []  # No fallback to the unambiguous older close.
    assert [(flag.code, flag.subject) for flag in flags] == [
        ("AMBIGUOUS", TARGET_COMPONENT),
    ]
    correction = opened if resolution == "same_close_correction" else quote(target=True)
    receive(received_account, "CUSTODY-A", "prices", [correction], at=CORRECTED)
    with read_snapshot(received_account) as connection:
        historical = price_view(connection, DAY, BEFORE_CORRECTION)
        current = price_view(connection, DAY, CORRECTED)
        assert TARGET_SECURITY in historical.ambiguous
        assert TARGET_SECURITY not in current.ambiguous
        assert current.values[TARGET_SECURITY]["price"] == "25.00"
        assert current.values[TARGET_SECURITY]["price_date"] == correction["price_date"]
        assert current.values[TARGET_SECURITY]["recorded_at"] == utc_timestamp(CORRECTED)
        assert set(current.values[TARGET_SECURITY]) == set(competing[0])


def test_late_interval_ambiguity_on_older_close_does_not_block_newer_close(
    received_account: Path,
) -> None:
    receive(received_account, "SECMASTER", "securities",
            [instrument(target=False), instrument(target=True)])
    receive(received_account, "CUSTODY-A", "prices", [quote(target=False), quote(target=True)])
    previous = DAY - timedelta(days=1)
    closed = quote(target=True, price="20.00", price_date=previous.isoformat(),
                   valid_from=previous.isoformat(), valid_to=DAY.isoformat())
    receive(received_account, "CUSTODY-A", "prices",
            [closed, {**closed, "valid_to": "9999-12-31"}], at=CORRECTED)
    with read_snapshot(received_account) as connection:
        prices = price_view(connection, DAY, CORRECTED)
        assert TARGET_SECURITY not in prices.ambiguous
        assert prices.values[TARGET_SECURITY]["price_date"] == DAY.isoformat()
        assert prices.values[TARGET_SECURITY]["recorded_at"] == utc_timestamp(RECEIVED)
        snapshot, flags, _sources = calculate_snapshot(connection, HOUSEHOLD, DAY, CORRECTED)
    assert isinstance(snapshot, CompleteSnapshot)
    assert snapshot.assets_usd == "950.00" and flags == []
