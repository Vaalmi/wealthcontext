"""Hand-received P3 freshness and FX boundaries; no generator truth is imported."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from wealthcontext.clock import FrozenClock
from wealthcontext.database import migrate
from wealthcontext.feeds import AccountReference, build_delivery
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.storage import migrate_business, read_snapshot
from wealthcontext.valuation import resolve_received_valuation

DAY = date(2026, 9, 30)
KNOWN = datetime(2026, 9, 30, 22, tzinfo=UTC)
HH = "WC-HH-0001"
ACCOUNT = AccountReference(account_id="WC-AC-0001", custodian="A", account_number="123")


def row(kind, key, **fields):
    return {"kind": kind, "source_record_id": key, "valid_from": "2026-09-14",
            "valid_to": "2026-10-17", **fields}


def received(tmp_path, *, country="GB", currency="GBP", price_date="2026-09-30",
             rate: str | None = "1.25", rate_at="2026-09-30T15:00:00Z", ambiguous=False):
    database = tmp_path / "received.sqlite"
    migrate(database)
    migrate_business(database)
    migrate_feeds(database)
    raw = [
        ("CRM", "crm", [row("household", "h", id=HH, name="Synthetic household"),
                         row("account", "a", id=ACCOUNT.account_id, custodian="A",
                             account_number="123", is_managed=True),
                         row("reporting", "r", account_id=ACCOUNT.account_id, household_id=HH)]),
        ("SECMASTER", "securities", [row(
            "security", "s", id="WC-SEC-0001", issuer_id="WC-ISS-0001", issuer_name="Issuer",
            name="Synthetic stock", ticker="SYN", type="COMMON_STOCK", currency=currency,
            cusip="990000101", identifier_status="synthetic_unallocated", listing_country=country,
        )]),
        ("PAS-A", "pas_snapshots", [
            row("position", "p", custodian="A", account_number="123",
                security_id="WC-SEC-0001", quantity="10"),
            row("cash", "c", custodian="A", account_number="123", currency="USD", amount="100"),
        ]),
        ("CUSTODY-A", "prices", [row("price", "q", security_id="WC-SEC-0001", currency=currency,
                                      price="50", price_date=price_date)]),
    ]
    if rate is not None:
        fixes = [row("fx", "f1", currency=currency, usd_rate=rate, rate_at=rate_at,
                     valid_from="2026-09-30", valid_to="2026-10-01")]
        if ambiguous:
            fixes.append({**fixes[0], "source_record_id": "f2", "usd_rate": "1.50"})
        raw.append(("FX", "fx", fixes))
    for source, feed, records in raw:
        run = ingest_delivery(database, build_delivery(
            source_system=source, feed=feed, business_date=DAY, records=records,
            expected_accounts=[ACCOUNT] if feed == "pas_snapshots" else [],
        ), FrozenClock(KNOWN))
        with read_snapshot(database) as connection:
            status = connection.execute(
                "SELECT status FROM feed_runs WHERE id=?", (run,),
            ).fetchone()
            assert status[0] == "SUCCESS"
    return database


def value(database, known=KNOWN):
    with read_snapshot(database) as connection:
        return resolve_received_valuation(connection, HH, DAY, known)


@pytest.mark.parametrize("country,currency,close,complete,age", [
    ("US", "USD", "2026-09-30", True, 0),
    ("US", "USD", "2026-09-29", False, 1),
    ("GB", "GBP", "2026-09-29", True, 1),
    ("GB", "GBP", "2026-09-28", False, 2),
])
def test_listing_freshness_boundary(tmp_path, country, currency, close, complete, age):
    result = value(received(tmp_path, country=country, currency=currency, price_date=close,
                            rate="1" if currency == "USD" else "1.25"))
    assert result.valuation.complete is complete
    assert result.observations["WC-AC-0001:WC-SEC-0001"].price_age_business_days == age
    expected = (Decimal("600") if complete and currency == "USD"
                else Decimal("725") if complete else Decimal("100"))
    assert result.valuation.fresh_subtotal_usd == expected
    assert any(flag.code == "PRICE_STALE" for flag in result.quality_flags) is not complete


@pytest.mark.parametrize("options,reason", [
    ({"rate": None}, "FIX_UNAVAILABLE"),
    ({"rate_at": "2026-10-01T15:00:00Z"}, "FIX_OUTSIDE_CUTOFFS"),
    ({"rate_at": "2026-09-30T23:00:00Z"}, "FIX_OUTSIDE_CUTOFFS"),
    ({"rate_at": "2026-09-29T15:00:00Z"}, "FIX_OUTSIDE_CUTOFFS"),
    ({"ambiguous": True}, "AMBIGUOUS_FIX"),
])
def test_missing_or_ineligible_foreign_fix_blocks_total(tmp_path, options, reason):
    result = value(received(tmp_path, **options))
    assert result.valuation.assets_usd is None
    assert result.valuation.fresh_subtotal_usd == Decimal("100")
    flags = [flag for flag in result.quality_flags if flag.code == "FX_UNAVAILABLE"]
    assert len(flags) == 1 and flags[0].detail["reason"] == reason


def test_rate_and_timestamp_are_received_evidence(tmp_path):
    result = value(received(tmp_path))
    assert result.valuation.assets_usd == Decimal("725")  # 10 × GBP50 × 1.25 + USD100.
    quote = result.observations["WC-AC-0001:WC-SEC-0001"]
    assert quote.fx_rate == Decimal("1.25")
    assert quote.rate_at == datetime(2026, 9, 30, 15, tzinfo=UTC)
    assert result.valuation.source_ids


def test_usd_identity_does_not_require_fx_file(tmp_path):
    result = value(received(tmp_path, country="US", currency="USD", rate=None))
    assert result.valuation.assets_usd == Decimal("600")
    assert not [flag for flag in result.quality_flags if flag.code == "FX_UNAVAILABLE"]


def test_absent_household_is_explicit_indeterminate_only_for_book(tmp_path):
    database = received(tmp_path)
    with read_snapshot(database) as connection:
        with pytest.raises(LookupError):
            resolve_received_valuation(connection, "WC-HH-9999", DAY, KNOWN)
        result = resolve_received_valuation(connection, "WC-HH-9999", DAY, KNOWN,
                                             absent_as_incomplete=True)
    assert result.valuation.assets_usd is None
    assert result.valuation.missing_components == ("WC-HH-9999",)
    assert any(flag.detail.get("reason") == "HOUSEHOLD_UNAVAILABLE_AT_CUTOFFS"
               for flag in result.quality_flags)


def restate_pas(database, observation, at, *, peer=False):
    records = [
        row("position", "p", custodian="A", account_number="123",
            security_id="WC-SEC-0001", quantity="10", pas_price=observation),
        row("cash", "c", custodian="A", account_number="123", currency="USD", amount="100"),
    ]
    expected = [ACCOUNT]
    if peer:
        other = AccountReference(account_id="WC-AC-0002", custodian="A", account_number="456")
        crm = [row("account", "peer", id=other.account_id, custodian="A",
                   account_number="456", is_managed=True),
               row("reporting", "peer-r", account_id=other.account_id, household_id=HH)]
        ingest_delivery(database, build_delivery(source_system="CRM", feed="crm",
                                                  business_date=DAY, records=crm), FrozenClock(at))
        records.append(row("cash", "peer-c", custodian="A", account_number="456",
                           currency="USD", amount="7"))
        expected.append(other)
    return ingest_delivery(database, build_delivery(
        source_system="PAS-A", feed="pas_snapshots", business_date=DAY,
        records=records, expected_accounts=expected,
    ), FrozenClock(at))


@pytest.mark.parametrize("observation,reasons", [
    (None, []),
    ({"price": "50.00", "price_date": "2026-09-30", "currency": "USD"}, []),
    ({"price": "50.0001", "price_date": "2026-09-30", "currency": "USD"}, ["PRICE"]),
    ({"price": "99", "price_date": "2026-09-30", "currency": "EUR"}, ["CURRENCY"]),
    ({"price": "99", "price_date": "2026-09-29", "currency": "USD"}, ["CLOSE_DATE"]),
    ({"price": "99", "price_date": "2026-09-29", "currency": "EUR"},
     ["CURRENCY", "CLOSE_DATE"]),
])
def test_secondary_price_disagreement_never_changes_authoritative_value(tmp_path, observation,
                                                                       reasons):
    database = received(tmp_path, country="US", currency="USD", rate=None)
    corrected = datetime(2026, 10, 1, 12, tzinfo=UTC)
    restate_pas(database, observation, corrected)
    before, after = value(database), value(database, corrected)
    assert before.valuation.assets_usd == after.valuation.assets_usd == Decimal("600")
    assert not [flag for flag in before.quality_flags if flag.code == "PRICE_SOURCE_MISMATCH"]
    mismatch = [flag for flag in after.quality_flags if flag.code == "PRICE_SOURCE_MISMATCH"]
    assert len(mismatch) == bool(reasons)
    if reasons:
        assert mismatch[0].severity == "info"
        assert mismatch[0].subject == "WC-AC-0001:WC-SEC-0001"
        assert mismatch[0].detail["authority"] == "CUSTODY"
        assert mismatch[0].detail["reasons"] == reasons
        assert mismatch[0].detail["pas_price"] == observation
        assert mismatch[0].detail["custody_price"] == "50"


@pytest.mark.parametrize("condition", ["stale", "missing", "ambiguous"])
def test_current_pas_quote_cannot_repair_unusable_authoritative_price(tmp_path, condition):
    database = received(tmp_path, country="US", currency="USD", rate=None,
                        price_date="2026-09-29" if condition == "stale" else "2026-09-30")
    corrected = datetime(2026, 10, 1, 12, tzinfo=UTC)
    restate_pas(database, {"price": "50", "price_date": "2026-09-30", "currency": "USD"},
                corrected)
    if condition != "stale":
        records = [row("price", "q", security_id="WC-SEC-0001", currency="USD",
                       price="50", price_date="2026-09-30", **(
                           {"valid_to": "2026-09-30"} if condition == "missing" else {}
                       ))]
        if condition == "ambiguous":
            records.append({**records[0], "source_record_id": "q2", "price": "51"})
        ingest_delivery(database, build_delivery(source_system="CUSTODY-A", feed="prices",
                                                  business_date=DAY, records=records),
                        FrozenClock(corrected))
    result = value(database, corrected)
    assert result.valuation.assets_usd is None
    assert result.valuation.fresh_subtotal_usd == Decimal("100")
    flags = {flag.code for flag in result.quality_flags}
    expected = {"stale": "PRICE_STALE", "missing": "SOURCE_UNAVAILABLE",
                "ambiguous": "AMBIGUOUS"}[condition]
    assert expected in flags
    assert ("PRICE_SOURCE_MISMATCH" in flags) is (condition == "stale")


@pytest.mark.parametrize("bad", [
    {"price": "50", "price_date": "2026-09-30"},
    {"price": 50.0, "price_date": "2026-09-30", "currency": "USD"},
    {"price": "50", "price_date": "2026-09-30", "currency": "JPY"},
    {"price": "50", "price_date": "2026-09-30", "currency": "USD", "model_value": "999"},
])
def test_invalid_secondary_observation_quarantines_account_and_keeps_peer(tmp_path, bad):
    database = received(tmp_path, country="US", currency="USD", rate=None)
    corrected = datetime(2026, 10, 1, 12, tzinfo=UTC)
    run = restate_pas(database, bad, corrected, peer=True)
    result = value(database, corrected)
    assert result.valuation.assets_usd is None
    assert result.valuation.fresh_subtotal_usd == Decimal("7")
    assert result.valuation.missing_account_ids == (ACCOUNT.account_id,)
    with read_snapshot(database) as connection:
        status = connection.execute("SELECT status FROM feed_runs WHERE id=?", (run,)).fetchone()
        assert status[0] == "PARTIAL"
        rejects = connection.execute("SELECT reason FROM quarantine WHERE run_id=?",
                                     (run,)).fetchall()
        assert rejects and {item[0] for item in rejects} == {"INVALID_ACCOUNT_ROW"}


def test_secondary_observation_is_not_accepted_in_custody_positions():
    from wealthcontext.feeds import parse_row
    record = row("position", "p", custodian="A", account_number="123",
                 security_id="WC-SEC-0001", quantity="10", pas_price={
                     "price": "50", "price_date": "2026-09-30", "currency": "USD",
                 })
    with pytest.raises(ValueError, match="only to PAS"):
        parse_row("custody_snapshots", record, source_system="CUSTODY-A")
    assert parse_row("custody_snapshots", {**record, "pas_price": None},
                     source_system="CUSTODY-A").kind == "position"
