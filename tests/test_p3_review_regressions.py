"""Review counterexamples through the same hand-written received files in both engines."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_p3_oracle_agreement import compare_book, compare_household, import_export
from test_reference_oracle import A1, A2, DAY, HH, KNOWN, Export, cash, fix, position, quote, row
from test_reference_oracle import security as security_row

from reference_oracle import evaluate, load_received_history
from wealthcontext.calculation import concentrated_positions
from wealthcontext.exposure import serialize_concentration, serialize_exposure
from wealthcontext.storage import read_snapshot
from wealthcontext.valuation import resolve_received_valuation, serialize_received_snapshot


def compare(directory: Path):
    database = directory / "received.sqlite"
    import_export(directory, database, datetime(2055, 1, 1, tzinfo=UTC))
    expected = evaluate(load_received_history(directory), household_ids=[HH], as_of=DAY,
                        known_as_of=KNOWN)
    with read_snapshot(database) as connection:
        actual = resolve_received_valuation(connection, HH, DAY, KNOWN)
    compare_household(actual, expected["households"][0])
    compare_book([actual.valuation], expected)
    return actual, expected["households"][0]


def foreign_export(directory: Path, *, close=DAY) -> Export:
    export = Export(directory)
    export.crm()
    export.feed("SECMASTER", "securities", [security_row(currency="GBP", country="GB")])
    export.snapshots([position("10"), cash("100")])
    export.feed("CUSTODY-A", "prices", [quote("50", currency="GBP", day=close)])
    return export


@pytest.mark.parametrize("rate_at,complete", [
    ("2026-09-30T00:30:00+02:00", False),
    ("2026-10-01T00:30:00+02:00", True),
    ("2026-09-30T21:00:00-04:00", False),
    ("2026-09-29T21:00:00-04:00", True),
])
def test_offset_fix_uses_utc_date_in_runtime_and_oracle(tmp_path, rate_at, complete):
    export = foreign_export(tmp_path)
    export.feed("FX", "fx", [{**fix("GBP", "1.25"), "rate_at": rate_at}])
    actual, expected = compare(tmp_path)
    assert actual.valuation.complete is complete
    assert expected.get("assets_usd") == ("725.00" if complete else None)
    if complete:
        instant = actual.observations[f"{A1['account_id']}:WC-SEC-0001"].rate_at
        assert instant is not None and instant.tzinfo == UTC


@pytest.mark.parametrize("earlier_correct_fix", [False, True])
def test_wrong_file_date_fix_never_values_the_household(tmp_path, earlier_correct_fix):
    export = foreign_export(tmp_path)
    if earlier_correct_fix:
        export.feed("FX", "fx", [fix("GBP", "1.25")])
    export.feed("FX", "fx", [fix("GBP", "1.50")], day=date(2026, 9, 29),
                at="2026-10-01T12:00:00Z")
    actual, expected = compare(tmp_path)
    assert not actual.valuation.complete
    assert expected["fresh_subtotal_usd"] == "100.00"
    assert "FX_UNAVAILABLE" in {flag.code for flag in actual.quality_flags}


def test_stale_foreign_position_without_fx_remains_explicit_with_unknown_usd_value(tmp_path):
    foreign_export(tmp_path, close=date(2026, 9, 28))
    actual, expected = compare(tmp_path)
    subject = f"{A1['account_id']}:WC-SEC-0001"
    assert {flag.code for flag in actual.valuation.blocking_flags} == {
        "PRICE_STALE", "FX_UNAVAILABLE",
    }
    snapshot = serialize_received_snapshot(actual).model_dump(mode="json")
    assert snapshot["missing_components"] == [subject]
    assert snapshot["stale_components"] == [{
        "subject": subject, "last_known_value_usd": None, "price_date": "2026-09-28",
        "price_age_business_days": 2,
    }]
    for group in ("issuer", "security"):
        exposure = serialize_exposure(actual.valuation, "Synthetic household", group)
        assert exposure.model_dump(mode="json")["stale_components"][0][
            "last_known_value_usd"] is None
    book = serialize_concentration(concentrated_positions([actual.valuation]))
    assert book.indeterminate[0].stale_component_count == 1
    assert expected["stale_components"][0]["last_known_value_usd"] is None


def test_pas_and_custody_failure_do_not_promote_secondary_warnings_to_blockers(tmp_path):
    export = Export(tmp_path)
    export.crm()
    for source, feed in (("PAS-A", "pas_snapshots"), ("CUSTODY-A", "custody_snapshots")):
        export.feed(source, feed, [], accounts=(A1,), absent=True, status="FAILED")
    actual, _expected = compare(tmp_path)
    assert {flag.code for flag in actual.quality_flags} >= {
        "SOURCE_UNAVAILABLE", "RECONCILIATION_UNAVAILABLE", "OWNERSHIP_UNAVAILABLE",
    }
    assert {flag.code for flag in actual.valuation.blocking_flags} == {"SOURCE_UNAVAILABLE"}


def test_pending_settlement_remains_info_when_price_is_missing_for_same_position(tmp_path):
    export = Export(tmp_path)
    export.crm()
    export.feed("SECMASTER", "securities", [security_row()])
    export.snapshots([position("10"), cash("100")], custody=[cash("200")])
    export.feed("PAS-A", "transactions", [row(
        "transaction", "buy", id="WC-TXN-0001", custodian=A1["custodian"],
        account_number=A1["account_number"], security_id="WC-SEC-0001", txn_type="BUY",
        trade_date=DAY.isoformat(), settle_date="2026-10-01", quantity_delta="10",
        cash_delta_local="-100", currency="USD", status="ACTIVE",
    )])
    actual, _expected = compare(tmp_path)
    subject = f"{A1['account_id']}:WC-SEC-0001"
    assert any(flag.code == "PENDING_SETTLEMENT" and flag.subject == subject
               and flag.severity == "info" for flag in actual.quality_flags)
    assert {flag.code for flag in actual.valuation.blocking_flags} == {"SOURCE_UNAVAILABLE"}


def test_precision_boundary_sums_cash_before_combining_position_subtotal(tmp_path):
    export = Export(tmp_path)
    export.crm(accounts=(A1, A2))
    export.feed("SECMASTER", "securities", [security_row()])
    export.snapshots([position("1"), cash("0.4"), cash("0.4", account=A2)], accounts=(A1, A2))
    export.feed("CUSTODY-A", "prices", [quote("1000000000000000000000000000")])
    actual, expected = compare(tmp_path)
    assert actual.valuation.assets_usd == Decimal("1000000000000000000000000001")
    assert expected["assets_usd"] == "1000000000000000000000000001.00"
    assert expected["cash_usd"] == "0.80"
