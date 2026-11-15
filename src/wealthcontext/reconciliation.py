"""Reconcile complete PAS snapshots against manifest-confirmed custody snapshots.

This module does not decide snapshot availability or select transaction versions.
Callers must provide the latest canonical transactions at the requested cutoffs and
only pass a snapshot mapping when its account manifest proved completeness. An
empty mapping then means zero holdings; ``None`` means unavailable custody data.
"""

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Literal, cast

from pydantic import AwareDatetime, JsonValue, TypeAdapter

from wealthcontext.clock import utc_timestamp
from wealthcontext.contracts import Contract, DecimalString, Identifier, QualityFlag
from wealthcontext.decimal_format import money

Currency = Literal["USD", "EUR", "CAD", "GBP"]
ReconciliationStatus = Literal[
    "MATCHED", "PENDING_SETTLEMENT", "BREAK", "RECONCILIATION_UNAVAILABLE",
]
ComponentKind = Literal["quantity", "cash"]


class ReconciliationRow(Contract):
    subject: Identifier
    kind: ComponentKind
    security_id: Identifier | None
    currency: Currency | None
    authoritative: DecimalString
    settled: DecimalString | None
    pending_delta: DecimalString
    difference: DecimalString | None
    pending_transaction_ids: list[Identifier]
    reconciliation_status: ReconciliationStatus


class AccountReconciliation(Contract):
    account_id: Identifier
    valuation_date: date
    known_as_of: AwareDatetime
    rows: list[ReconciliationRow]
    quality_flags: list[QualityFlag]


class _Transaction(Contract):
    id: Identifier
    account_id: Identifier
    security_id: Identifier | None
    trade_date: date
    settle_date: date
    quantity_delta: DecimalString
    cash_delta_local: DecimalString
    currency: Currency
    status: Literal["ACTIVE", "CANCELLED"]
    recorded_at: AwareDatetime


_POSITION_VALUES = TypeAdapter(dict[Identifier, DecimalString])
_CASH_VALUES = TypeAdapter(dict[Currency, DecimalString])


def _values(values: dict[str, str], *, cash: bool) -> dict[str, Decimal]:
    validated = (_CASH_VALUES if cash else _POSITION_VALUES).validate_python(values)
    result = {key: Decimal(value) for key, value in validated.items()}
    if any(value < 0 for value in result.values()):
        raise ValueError("negative snapshot balances are outside the MVP")
    return result


def _number(value: Decimal, kind: ComponentKind) -> str:
    # Comparisons happen before display rounding; cash is displayed in cents.
    if kind == "cash":
        return money(value)
    return "0" if value == 0 else format(value, "f")


def _row(
    account_id: str, component: str, kind: ComponentKind, authoritative: Decimal,
    settled: Decimal | None, pending: list[_Transaction],
) -> ReconciliationRow:
    pending_delta = sum((Decimal(tx.quantity_delta if kind == "quantity" else tx.cash_delta_local)
                         for tx in pending), Decimal("0"))
    difference = None if settled is None else authoritative - settled
    status: ReconciliationStatus
    if difference is None:
        status = "RECONCILIATION_UNAVAILABLE"
    elif difference == pending_delta and pending:
        status = "PENDING_SETTLEMENT"
    elif difference == 0 and not pending:
        status = "MATCHED"
    else:
        status = "BREAK"
    return ReconciliationRow(
        subject=f"{account_id}:{component}", kind=kind,
        security_id=component if kind == "quantity" else None,
        currency=cast(Currency, component) if kind == "cash" else None,
        authoritative=_number(authoritative, kind),
        settled=None if settled is None else _number(settled, kind),
        pending_delta=_number(pending_delta, kind),
        difference=None if difference is None else _number(difference, kind),
        pending_transaction_ids=sorted(tx.id for tx in pending), reconciliation_status=status,
    )


def reconcile_account(
    account_id: str, valuation_date: date, known_as_of: datetime,
    pas_positions: dict[str, str], pas_cash: dict[str, str],
    custody_positions: dict[str, str] | None, custody_cash: dict[str, str] | None,
    transactions: list[dict],
) -> AccountReconciliation:
    """Apply trade-date pending rules without treating unavailable custody as zero.

    Canonical transaction dictionaries may carry additional lineage fields; this
    function reads only its typed reconciliation projection. Duplicate eligible
    transaction IDs are rejected rather than silently counted twice.
    """
    utc_timestamp(known_as_of)  # Reject naive instants before timezone conversion.
    known = known_as_of.astimezone(UTC)
    projected = [_Transaction.model_validate({key: value for key, value in row.items()
                                             if key in _Transaction.model_fields})
                 for row in transactions]
    pending = [tx for tx in projected if tx.account_id == account_id and tx.status == "ACTIVE"
               and tx.trade_date <= valuation_date < tx.settle_date and tx.recorded_at <= known]
    if len({tx.id for tx in pending}) != len(pending):
        raise ValueError("duplicate pending transaction IDs")
    pending.sort(key=lambda tx: tx.id)
    with localcontext() as arithmetic:
        arithmetic.prec = 28
        arithmetic.rounding = ROUND_HALF_EVEN
        positions = _values(pas_positions, cash=False)
        cash = _values(pas_cash, cash=True)
        settled_positions = (None if custody_positions is None
                             else _values(custody_positions, cash=False))
        settled_cash = None if custody_cash is None else _values(custody_cash, cash=True)
        securities = (positions.keys() | (settled_positions or {}).keys()
                      | {tx.security_id for tx in pending if tx.security_id is not None})
        currencies = cash.keys() | (settled_cash or {}).keys() | {tx.currency for tx in pending}
        rows = [_row(account_id, security, "quantity", positions.get(security, Decimal("0")),
                     None if settled_positions is None
                     else settled_positions.get(security, Decimal("0")),
                     [tx for tx in pending if tx.security_id == security])
                for security in sorted(securities)]
        rows.extend(_row(account_id, currency, "cash", cash.get(currency, Decimal("0")),
                         None if settled_cash is None else settled_cash.get(currency, Decimal("0")),
                         [tx for tx in pending if tx.currency == currency])
                    for currency in sorted(currencies))
    flags: list[QualityFlag] = []
    unavailable = (["quantity"] if custody_positions is None else [])
    unavailable += ["cash"] if custody_cash is None else []
    if unavailable:
        flags.append(QualityFlag(code="RECONCILIATION_UNAVAILABLE", severity="warning",
                                 subject=account_id,
                                 detail={"source": "CUSTODY",
                                         "components": list[JsonValue](unavailable)}))
    for row in rows:
        if row.reconciliation_status == "PENDING_SETTLEMENT":
            flags.append(QualityFlag(
                code="PENDING_SETTLEMENT", severity="info", subject=row.subject,
                detail={"pending_transaction_ids": list[JsonValue](row.pending_transaction_ids)},
            ))
        elif row.reconciliation_status == "BREAK":
            flags.append(QualityFlag(
                code="QUANTITY_BREAK" if row.kind == "quantity" else "CASH_BREAK",
                severity="warning", subject=row.subject,
                detail={"authoritative": row.authoritative, "settled": row.settled,
                        "pending_delta": row.pending_delta, "difference": row.difference},
            ))
    return AccountReconciliation(account_id=account_id, valuation_date=valuation_date,
                                 known_as_of=known, rows=rows, quality_flags=flags)
