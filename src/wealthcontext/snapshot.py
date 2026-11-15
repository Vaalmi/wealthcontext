"""Typed household snapshots; received history uses the shared unrounded valuation engine."""

import sqlite3
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Literal, Self

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, model_validator

from wealthcontext.contracts import (
    ENVELOPE_STATUS_RULES,
    Contract,
    DecimalString,
    Envelope,
    Identifier,
    QualityCode,
    QualityFlag,
)
from wealthcontext.decimal_format import money as money
from wealthcontext.reconciliation import AccountReconciliation
from wealthcontext.storage import facts_at


class LegalOwner(Contract):
    entity_id: Identifier
    entity_type: Literal["person", "trust"]
    role: Literal["owner", "joint_owner", "trustee"]


class AccountSummary(Contract):
    id: Identifier
    title: str
    custodian: Literal["A", "B"] | None = None
    account_number: str | None = None
    status: Literal["OPEN", "CLOSED"] | None = None
    registration: Literal[
        "INDIVIDUAL", "JTWROS", "TRADITIONAL_IRA", "ROTH_IRA", "REVOCABLE_TRUST",
    ] | None = None
    legal_owners: list[LegalOwner] = Field(default_factory=list)


class MemberSummary(Contract):
    id: Identifier
    name: str


class ExcludedAccount(Contract):
    id: Identifier
    custodian: Literal["A", "B"]
    account_number: str
    reason: Literal["HELD_AWAY_EXCLUDED"]


class ConflictSummary(Contract):
    code: QualityCode
    subject: Identifier
    detail: dict[str, JsonValue]


class PositionValue(Contract):
    id: Identifier
    account_id: Identifier
    security_id: Identifier
    quantity: DecimalString
    price_usd: DecimalString
    price_date: date
    market_value_usd: DecimalString
    listing_currency: Literal["USD", "EUR", "CAD", "GBP"] = "USD"
    price_local: DecimalString | None = None
    fx_rate: DecimalString = "1"
    rate_at: AwareDatetime | None = None
    price_age_business_days: int = Field(default=0, ge=0)


class CashValue(Contract):
    id: Identifier
    account_id: Identifier
    currency: Literal["USD", "EUR", "CAD", "GBP"]
    amount_local: DecimalString
    fx_rate: DecimalString
    rate_at: AwareDatetime | None
    market_value_usd: DecimalString


class SnapshotBase(Contract):
    household_id: Identifier
    household_name: str
    accounts: list[AccountSummary]
    positions: list[PositionValue]
    cash_balances: list[CashValue] = Field(default_factory=list)
    members: list[MemberSummary] = Field(default_factory=list)
    excluded_accounts: list[ExcludedAccount] = Field(default_factory=list)
    reconciliation: list[AccountReconciliation] = Field(default_factory=list)
    conflicts: list[ConflictSummary] = Field(default_factory=list)
    item_count: int
    ordering: Literal["account_id"] = "account_id"
    truncated: Literal[False] = False
    next_cursor: None = None


class CompleteSnapshot(SnapshotBase):
    assets_usd: DecimalString
    cash_usd: DecimalString


class StaleComponent(Contract):
    subject: Identifier
    last_known_value_usd: DecimalString | None
    price_date: date
    price_age_business_days: int = Field(default=0, ge=0)


class IncompleteSnapshot(SnapshotBase):
    fresh_subtotal_usd: DecimalString
    missing_components: list[Identifier]
    stale_components: list[StaleComponent]


Snapshot = CompleteSnapshot | IncompleteSnapshot


def snapshot_status_schema(schema: dict) -> None:
    # Reuse generated references so OpenAPI's components and standalone $defs both work.
    # The variants follow Snapshot's explicit CompleteSnapshot | IncompleteSnapshot order.
    variants = schema["properties"]["data"]["anyOf"]
    schema["allOf"] = [*ENVELOPE_STATUS_RULES, *[
        {"if": {"properties": {"status": {"const": status}}},
         "then": {"properties": {"data": variant}}}
        for status, variant in zip(("COMPLETE", "INCOMPLETE"), variants, strict=False)
    ]]


class SnapshotEnvelope(Envelope[Snapshot]):
    model_config = ConfigDict(json_schema_extra=snapshot_status_schema)

    @model_validator(mode="after")
    def matching_snapshot_status(self) -> Self:
        if self.status == "COMPLETE" and not isinstance(self.data, CompleteSnapshot):
            raise ValueError("COMPLETE requires a complete snapshot")
        if self.status == "INCOMPLETE" and not isinstance(self.data, IncompleteSnapshot):
            raise ValueError("INCOMPLETE requires an incomplete snapshot")
        return self


def valuation_date(as_of: date) -> date:
    resolved = as_of
    while resolved.weekday() > 4:
        resolved -= timedelta(days=1)
    if not date(2026, 9, 14) <= resolved <= date(2026, 10, 16):
        raise ValueError("AS_OF_OUT_OF_RANGE")
    return resolved


def calculate_snapshot(
    connection: sqlite3.Connection, household_id: str, day: date, known: datetime,
) -> tuple[Snapshot, list[QualityFlag], list[str]]:
    received_pipeline = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feed_runs'",
    ).fetchone()
    if received_pipeline and connection.execute("SELECT 1 FROM feed_runs LIMIT 1").fetchone():
        from wealthcontext.valuation import calculate_received_snapshot
        return calculate_received_snapshot(connection, household_id, day, known)
    households = facts_at(connection, "household", day, known)
    if household_id not in households:
        raise LookupError("Household not available at the requested cutoffs")
    household = households[household_id]
    accounts = {key: value for key, value in facts_at(connection, "account", day, known).items()
                if value["household_id"] == household_id and value["is_managed"]}
    coverage = facts_at(connection, "pas_coverage", day, known)
    positions = facts_at(connection, "position", day, known)
    cash = facts_at(connection, "cash", day, known)
    prices = facts_at(connection, "price", day, known)
    sources = {household["source_id"]}
    missing: list[str] = []
    flags: list[QualityFlag] = []
    valued: list[PositionValue] = []
    with localcontext() as arithmetic:
        arithmetic.prec = 28
        arithmetic.rounding = ROUND_HALF_EVEN
        total, cash_total = Decimal("0"), Decimal("0")
        for account_id, account in sorted(accounts.items()):
            sources.add(account["source_id"])
            manifest = coverage.get(account_id)
            expected_positions = set(manifest["position_ids"]) if manifest else set()
            expected_cash = set(manifest["cash_ids"]) if manifest else set()
            actual_positions = {key for key, row in positions.items()
                                if row["account_id"] == account_id}
            actual_cash = {key for key, row in cash.items() if row["account_id"] == account_id}
            if (not manifest or expected_positions != actual_positions
                    or expected_cash != actual_cash):
                missing.append(account_id)
                flags.append(QualityFlag(code="SOURCE_UNAVAILABLE", severity="warning",
                                         subject=account_id, detail={"source": "PAS"}))
                continue
            sources.add(manifest["source_id"])
            for position_id in sorted(actual_positions):
                row = positions[position_id]
                sources.add(row["source_id"])
                price = prices.get(row["security_id"])
                if not price:
                    missing.append(position_id)
                    flags.append(QualityFlag(code="SOURCE_UNAVAILABLE", severity="warning",
                                             subject=position_id, detail={"source": "price"}))
                    continue
                sources.add(price["source_id"])
                if price["currency"] != "USD":
                    missing.append(position_id)
                    flags.append(QualityFlag(code="FX_UNAVAILABLE", severity="warning",
                                             subject=position_id))
                    continue
                value = Decimal(row["quantity"]) * Decimal(price["price"])
                total += value
                valued.append(PositionValue(
                    id=position_id, account_id=account_id, security_id=row["security_id"],
                    quantity=row["quantity"], price_usd=price["price"],
                    price_date=price["price_date"], market_value_usd=money(value),
                ))
            for cash_id in sorted(actual_cash):
                row = cash[cash_id]
                sources.add(row["source_id"])
                if row["currency"] != "USD":
                    missing.append(cash_id)
                    flags.append(QualityFlag(code="FX_UNAVAILABLE", severity="warning",
                                             subject=cash_id))
                    continue
                cash_total += Decimal(row["amount"])
        total += cash_total
        common = {
            "household_id": household_id, "household_name": household["name"],
            "accounts": [{"id": key, "title": row["title"]}
                         for key, row in sorted(accounts.items())],
            "positions": valued, "item_count": len(accounts),
        }
        data: Snapshot
        if missing:
            data = IncompleteSnapshot.model_validate({
                **common, "fresh_subtotal_usd": money(total),
                "missing_components": sorted(missing), "stale_components": [],
            })
        else:
            data = CompleteSnapshot.model_validate({
                **common, "assets_usd": money(total), "cash_usd": money(cash_total),
            })
    return data, flags, sorted(sources)
