from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from wealthcontext.reconciliation import AccountReconciliation, reconcile_account

ACCOUNT = "WC-AC-0001"
SECURITY = "WC-SEC-0001"
DAY = date(2026, 9, 30)
KNOWN = datetime(2026, 10, 5, tzinfo=UTC)


def transaction(**changes) -> dict:
    return {
        "id": "WC-TX-0001", "account_id": ACCOUNT, "security_id": SECURITY,
        "trade_date": "2026-09-30", "settle_date": "2026-10-01", "quantity_delta": "10",
        "cash_delta_local": "-251", "currency": "USD", "status": "ACTIVE",
        "recorded_at": "2026-09-30T14:00:00Z", **changes,
    }


def rows_by_kind(result: AccountReconciliation):
    return {row.kind: row for row in result.rows}


def test_whole_position_buy_reconciles_absent_custody_security_as_confirmed_zero():
    result = reconcile_account(ACCOUNT, DAY, KNOWN, {SECURITY: "10"}, {"USD": "9749"},
                               {}, {"USD": "10000"}, [transaction()])
    rows = rows_by_kind(result)
    assert {row.reconciliation_status for row in result.rows} == {"PENDING_SETTLEMENT"}
    assert rows["quantity"].settled == "0"
    assert rows["quantity"].pending_delta == "10"
    assert rows["cash"].difference == "-251.00"
    assert [flag.code for flag in result.quality_flags] == ["PENDING_SETTLEMENT"] * 2
    assert {flag.severity for flag in result.quality_flags} == {"info"}
    assert rows["cash"].pending_transaction_ids == ["WC-TX-0001"]


def test_whole_position_sell_joins_custody_only_security():
    result = reconcile_account(
        ACCOUNT, DAY, KNOWN, {}, {"USD": "1249"}, {SECURITY: "10"}, {"USD": "1000"},
        [transaction(quantity_delta="-10", cash_delta_local="249")],
    )
    rows = rows_by_kind(result)
    assert rows["quantity"].security_id == SECURITY
    assert rows["quantity"].authoritative == "0"
    assert rows["quantity"].difference == "-10"
    assert rows["cash"].pending_delta == "249.00"
    assert {row.reconciliation_status for row in result.rows} == {"PENDING_SETTLEMENT"}


def test_cancelled_future_trade_other_account_and_future_knowledge_are_excluded():
    transactions = [
        transaction(id="WC-TX-CANCEL", status="CANCELLED", quantity_delta="99999"),
        transaction(id="WC-TX-FUTURE", trade_date="2026-10-01"),
        transaction(id="WC-TX-OTHER", account_id="WC-AC-0002"),
        transaction(id="WC-TX-LATE", recorded_at="2026-10-06T14:00:00Z"),
    ]
    result = reconcile_account(ACCOUNT, DAY, KNOWN, {SECURITY: "10"}, {"USD": "1000"},
                               {SECURITY: "10"}, {"USD": "1000"}, transactions)
    assert {row.reconciliation_status for row in result.rows} == {"MATCHED"}
    assert all(row.pending_transaction_ids == [] for row in result.rows)
    assert result.quality_flags == []


def test_pending_only_components_and_net_zero_transactions_remain_pending():
    result = reconcile_account(ACCOUNT, DAY, KNOWN, {}, {}, {}, {}, [
        transaction(id="WC-TX-B", quantity_delta="-10", cash_delta_local="251"),
        transaction(id="WC-TX-A"),
    ])
    assert [(row.kind, row.security_id, row.currency) for row in result.rows] == [
        ("quantity", SECURITY, None), ("cash", None, "USD"),
    ]
    assert {row.reconciliation_status for row in result.rows} == {"PENDING_SETTLEMENT"}
    assert all(Decimal(row.pending_delta) == 0 for row in result.rows)
    assert all(row.pending_transaction_ids == ["WC-TX-A", "WC-TX-B"] for row in result.rows)


def test_cash_only_transaction_is_reconciled_in_its_own_currency():
    result = reconcile_account(ACCOUNT, DAY, KNOWN, {}, {"CAD": "120", "USD": "30"},
                               {}, {"CAD": "100", "USD": "30"}, [
        transaction(security_id=None, quantity_delta="0", cash_delta_local="20", currency="CAD"),
    ])
    assert [row.currency for row in result.rows] == ["CAD", "USD"]
    assert [row.reconciliation_status for row in result.rows] == ["PENDING_SETTLEMENT", "MATCHED"]
    assert result.rows[0].pending_delta == "20.00"
    assert result.rows[1].pending_transaction_ids == []


@pytest.mark.parametrize("known,expected", [
    ("2026-09-30T09:59:59.999999-04:00", "BREAK"),
    ("2026-09-30T10:00:00-04:00", "PENDING_SETTLEMENT"),
    ("2026-09-30T16:00:00+02:00", "PENDING_SETTLEMENT"),
])
def test_knowledge_cutoff_compares_instants_and_includes_equal_boundary(known, expected):
    result = reconcile_account(ACCOUNT, DAY, datetime.fromisoformat(known), {SECURITY: "10"},
                               {"USD": "9749"}, {}, {"USD": "10000"}, [transaction()])
    assert {row.reconciliation_status for row in result.rows} == {expected}
    assert result.known_as_of.tzinfo == UTC


def test_late_settlement_on_settle_date_is_break_then_matched_never_pending():
    day = date(2026, 10, 1)
    before = reconcile_account(
        ACCOUNT, day, datetime(2026, 10, 2, 13, tzinfo=UTC),
        {SECURITY: "10"}, {"USD": "9749"}, {}, {"USD": "10000"}, [transaction()],
    )
    after = reconcile_account(
        ACCOUNT, day, datetime(2026, 10, 2, 14, tzinfo=UTC),
        {SECURITY: "10"}, {"USD": "9749"}, {SECURITY: "10"}, {"USD": "9749"}, [transaction()],
    )
    # The caller selects each received custody version; settlement date alone
    # never manufactures that correction or turns an overdue difference pending.
    assert {row.reconciliation_status for row in before.rows} == {"BREAK"}
    assert [flag.code for flag in before.quality_flags] == ["QUANTITY_BREAK", "CASH_BREAK"]
    assert all(row.pending_transaction_ids == [] for row in before.rows)
    assert {row.reconciliation_status for row in after.rows} == {"MATCHED"}
    assert after.quality_flags == []


@pytest.mark.parametrize("positions,cash", [({}, {}), ({SECURITY: "10"}, {"USD": "100"})])
def test_missing_custody_never_turns_into_a_matched_or_zero_snapshot(positions, cash):
    result = reconcile_account(ACCOUNT, DAY, KNOWN, positions, cash, None, None, [])
    assert len(result.quality_flags) == 1
    flag = result.quality_flags[0]
    assert flag.code == "RECONCILIATION_UNAVAILABLE" and flag.subject == ACCOUNT
    assert flag.detail == {"source": "CUSTODY", "components": ["quantity", "cash"]}
    for row in result.rows:
        assert row.reconciliation_status == "RECONCILIATION_UNAVAILABLE"
        assert row.settled is None and row.difference is None


def test_quantity_and_cash_secondary_availability_remain_separate():
    result = reconcile_account(ACCOUNT, DAY, KNOWN, {SECURITY: "10"}, {"USD": "100"},
                               None, {"USD": "100"}, [])
    assert [row.reconciliation_status for row in result.rows] == [
        "RECONCILIATION_UNAVAILABLE", "MATCHED",
    ]
    assert result.quality_flags[0].detail["components"] == ["quantity"]


def test_comparison_uses_unrounded_decimal_values_and_restores_decimal_context():
    from decimal import getcontext

    old_precision = getcontext().prec
    result = reconcile_account(ACCOUNT, DAY, KNOWN, {}, {"USD": "1.004"}, {}, {"USD": "1"}, [])
    assert result.rows[0].reconciliation_status == "BREAK"
    assert result.rows[0].difference == "0.00"
    assert getcontext().prec == old_precision


@pytest.mark.parametrize("balance", [
    "1000000000000000000000000000", "9999999999999999999999999999",
])
def test_accepted_28_digit_cash_balances_reconcile_and_render_cents(balance):
    result = reconcile_account(ACCOUNT, DAY, KNOWN, {}, {"USD": balance},
                               {}, {"USD": balance}, [])
    row = result.rows[0]
    assert row.reconciliation_status == "MATCHED"
    assert row.authoritative == row.settled == balance + ".00"
    assert row.difference == row.pending_delta == "0.00"
    assert result.quality_flags == []


@pytest.mark.parametrize("sign", ["", "-"])
def test_sum_of_large_pending_cash_deltas_renders_beyond_28_integer_digits(sign):
    amount = "9000000000000000000000000000"
    transactions = [transaction(id=f"WC-TX-{index}", security_id=None,
                                quantity_delta="0", cash_delta_local=sign + amount)
                    for index in range(2)]
    result = reconcile_account(ACCOUNT, DAY, KNOWN, {}, {}, {}, {}, transactions)
    row = result.rows[0]
    assert row.reconciliation_status == "BREAK"
    assert row.pending_delta == sign + "18000000000000000000000000000.00"
    assert row.difference == "0.00"
    assert result.quality_flags[0].detail["pending_delta"] == row.pending_delta


def test_duplicate_pending_transaction_ids_rejected_instead_of_double_counted():
    with pytest.raises(ValueError, match="duplicate pending"):
        reconcile_account(ACCOUNT, DAY, KNOWN, {}, {}, {}, {}, [transaction(), transaction()])


@pytest.mark.parametrize("values", [{SECURITY: "NaN"}, {SECURITY: "1e2"}, {SECURITY: 1.1},
                                    {SECURITY: "-1"}])
def test_invalid_snapshot_values_fail_instead_of_entering_decimal_arithmetic(values):
    with pytest.raises((ValidationError, ValueError)):
        reconcile_account(ACCOUNT, DAY, KNOWN, values, {}, {}, {}, [])


def test_naive_cutoff_or_transaction_clock_rejected():
    with pytest.raises(ValueError, match="timezone"):
        reconcile_account(ACCOUNT, DAY, datetime(2026, 10, 1), {}, {}, {}, {}, [])
    with pytest.raises(ValidationError):
        reconcile_account(ACCOUNT, DAY, KNOWN, {}, {}, {}, {}, [
            transaction(recorded_at="2026-09-30T14:00:00"),
        ])


@given(st.integers(min_value=0, max_value=100000), st.integers(min_value=-10000, max_value=10000))
def test_signed_pending_delta_matches_exactly_and_extra_difference_breaks(settled, delta):
    if settled + delta < 0:
        return
    tx = transaction(quantity_delta=str(delta), cash_delta_local="0")
    exact = reconcile_account(ACCOUNT, DAY, KNOWN, {SECURITY: str(settled + delta)}, {},
                              {SECURITY: str(settled)}, {}, [tx])
    broken = reconcile_account(ACCOUNT, DAY, KNOWN, {SECURITY: str(settled + delta + 1)}, {},
                               {SECURITY: str(settled)}, {}, [tx])
    assert exact.rows[0].reconciliation_status == "PENDING_SETTLEMENT"
    assert broken.rows[0].reconciliation_status == "BREAK"
