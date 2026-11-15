"""Resolve received inputs once; retain unrounded values for every metric serializer."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

from wealthcontext.calculation import (
    BlockingFlag,
    CashInput,
    HouseholdInput,
    HouseholdValuation,
    PositionInput,
    SecurityType,
    cash_usd,
    mv_position,
    value_household,
)
from wealthcontext.contracts import QualityFlag
from wealthcontext.decimal_format import money
from wealthcontext.facts import FactView, fact_view
from wealthcontext.households import HouseholdContext, household_context
from wealthcontext.prices import price_view
from wealthcontext.snapshot import (
    AccountSummary,
    CashValue,
    CompleteSnapshot,
    ConflictSummary,
    ExcludedAccount,
    IncompleteSnapshot,
    MemberSummary,
    PositionValue,
    Snapshot,
    StaleComponent,
)


@dataclass(frozen=True)
class Observation:
    currency: str
    fx_rate: Decimal | None
    rate_at: datetime | None
    price_age_business_days: int | None = None


@dataclass(frozen=True)
class ReceivedValuation:
    context: HouseholdContext
    valuation: HouseholdValuation
    quality_flags: tuple[QualityFlag, ...]
    observations: dict[str, Observation]
    cash_inputs: tuple[CashInput, ...]


def business_age(close: date, day: date) -> int:
    """Weekday age within the declared synthetic window; foreign holidays are deferred."""
    if close > day:
        raise ValueError("a future close cannot value an earlier business date")
    age = 0
    while close < day:
        close += timedelta(days=1)
        age += close.weekday() < 5
    return age


def resolve_received_valuation(
    connection: sqlite3.Connection, household_id: str, day: date, known: datetime,
    *, absent_as_incomplete: bool = False,
) -> ReceivedValuation:
    context = household_context(connection, household_id, day, known)
    if context.household is None and not context.missing_components:
        if not absent_as_incomplete:
            raise LookupError("Household not available at the requested cutoffs")
        context.missing_components.append(household_id)
        context.quality_flags.append(QualityFlag(
            code="SOURCE_UNAVAILABLE", severity="warning", subject=household_id,
            detail={"source": "CRM", "reason": "HOUSEHOLD_UNAVAILABLE_AT_CUTOFFS"},
        ))
    prices = price_view(connection, day, known)
    securities = fact_view(connection, "security", day, known)
    fixes = fact_view(connection, "fx", day, known)
    sources = set(context.source_ids)
    flags = list(context.quality_flags)
    missing = set(context.missing_components)
    missing_accounts = {item.account["id"] for item in context.accounts if not item.pas.available}
    missing_accounts.update(
        flag.subject for flag in context.quality_flags
        if flag.code in {"AMBIGUOUS_ENTITY", "SOURCE_UNAVAILABLE"}
        and flag.detail.get("entity_type") in {"account", "reporting_household"}
    )
    positions: list[PositionInput] = []
    cash: list[CashInput] = []
    observations: dict[str, Observation] = {}

    def select(view: FactView, key: str, subject: str, source: str) -> dict | None:
        if key in view.ambiguous:
            sources.update(row["source_id"] for row in view.ambiguous[key])
            flags.append(QualityFlag(code="AMBIGUOUS", severity="warning", subject=subject,
                                     detail={"source": source}))
            missing.add(subject)
            return None
        selected = view.values.get(key)
        if selected is None:
            flags.append(QualityFlag(code="SOURCE_UNAVAILABLE", severity="warning",
                                     subject=subject, detail={"source": source}))
            missing.add(subject)
            return None
        sources.add(selected["source_id"])
        return selected

    def fx(currency: str, subject: str) -> Observation:
        if currency == "USD":
            return Observation(currency, Decimal("1"), None)
        row = fixes.values.get(currency)
        if currency in fixes.ambiguous:
            sources.update(item["source_id"] for item in fixes.ambiguous[currency])
            reason = "AMBIGUOUS_FIX"
        elif row:
            sources.add(row["source_id"])
            # Old admitted facts may predate timestamp-range validation. Reject
            # the selected fix without rewriting it or falling back to an older one.
            try:
                timestamp = row.get("rate_at")
                if not isinstance(timestamp, str):
                    raise ValueError("FX fix timestamp must be text")
                reported = datetime.fromisoformat(timestamp)
                if reported.tzinfo is None or reported.utcoffset() is None:
                    raise ValueError("FX fix timestamp must have a timezone")
                at = reported.astimezone(UTC)
            except (ValueError, OverflowError):
                reason = "INVALID_FIX_TIMESTAMP"
            else:
                receipt = connection.execute(
                    "SELECT business_date FROM source_records WHERE id=?", (row["source_id"],),
                ).fetchone()
                if receipt and receipt["business_date"] == day.isoformat() and at.date() == day \
                        and at <= known:
                    return Observation(currency, Decimal(row["usd_rate"]), at)
                reason = "FIX_OUTSIDE_CUTOFFS"
        else:
            reason = "FIX_UNAVAILABLE"
        flags.append(QualityFlag(code="FX_UNAVAILABLE", severity="warning", subject=subject,
                                 detail={"currency": currency, "reason": reason}))
        return Observation(currency, None, None)

    for account in context.accounts:
        account_id = account.account["id"]
        if not account.pas.available:
            continue
        for security_id, position in sorted(account.pas.positions.items()):
            subject = position["id"]
            instrument = select(securities, security_id, subject, "security")
            price = select(prices, security_id, subject, "price")
            if instrument is None or price is None:
                continue
            close = date.fromisoformat(price["price_date"])
            if close > day or price["currency"] != instrument["currency"]:
                missing.add(subject)
                flags.append(QualityFlag(code="SOURCE_UNAVAILABLE", severity="warning",
                                         subject=subject, detail={"source": "price"}))
                continue
            age = business_age(close, day)
            tolerance = 0 if instrument["listing_country"] == "US" else 1
            stale = age > tolerance
            quote = fx(instrument["currency"], subject)
            observations[subject] = Observation(quote.currency, quote.fx_rate, quote.rate_at, age)
            if stale:
                flags.append(QualityFlag(code="PRICE_STALE", severity="warning", subject=subject,
                                         detail={"price_date": close.isoformat(),
                                                 "age_business_days": age,
                                                 "tolerance_business_days": tolerance}))
            pas_price = position.get("pas_price")
            mismatch = []
            if pas_price is not None:
                if pas_price["currency"] != price["currency"]:
                    mismatch.append("CURRENCY")
                if pas_price["price_date"] != price["price_date"]:
                    mismatch.append("CLOSE_DATE")
                if not mismatch and Decimal(pas_price["price"]) != Decimal(price["price"]):
                    mismatch.append("PRICE")
            if mismatch:
                flags.append(QualityFlag(
                    code="PRICE_SOURCE_MISMATCH", severity="info", subject=subject,
                    detail={"authority": "CUSTODY", "reasons": mismatch,
                            "custody_price": price["price"],
                            "custody_currency": price["currency"],
                            "custody_price_date": price["price_date"], "pas_price": pas_price},
                ))
            positions.append(PositionInput(
                id=subject, account_id=account_id, security_id=security_id,
                security_name=instrument["name"], issuer_id=instrument["issuer_id"],
                issuer_name=instrument["issuer_name"],
                security_type=cast(SecurityType, instrument["type"]),
                quantity=Decimal(position["quantity"]), price_local=Decimal(price["price"]),
                fx_usd=quote.fx_rate, price_date=close, stale=stale,
            ))
        for currency, row in sorted(account.pas.cash.items()):
            quote = fx(currency, row["id"])
            observations[row["id"]] = quote
            cash.append(CashInput(row["id"], account_id, currency, Decimal(row["amount"]),
                                  quote.fx_rate))
    blocking = tuple(BlockingFlag(flag.code, flag.subject) for flag in flags
                     if flag.code in {"AMBIGUOUS", "AMBIGUOUS_ENTITY", "SOURCE_UNAVAILABLE",
                                      "FX_UNAVAILABLE", "PRICE_STALE"})
    inputs = HouseholdInput(
        household_id=household_id, positions=tuple(positions), cash=tuple(cash),
        missing_account_ids=tuple(sorted(missing_accounts)),
        missing_components=tuple(sorted(missing)), blocking_flags=blocking,
        source_ids=tuple(sorted(sources)),
    )
    calculated = value_household(inputs)
    # Keep the detailed resolver flag when the pure engine identifies the same blocker.
    seen = {(flag.code, flag.subject) for flag in flags}
    for flag in calculated.blocking_flags:
        if (flag.code, flag.subject) not in seen:
            flags.append(QualityFlag.model_validate({"code": flag.code, "severity": "warning",
                                                     "subject": flag.subject}))
            seen.add((flag.code, flag.subject))
    return ReceivedValuation(context, calculated, tuple(flags), observations, tuple(cash))


def serialize_received_snapshot(received: ReceivedValuation) -> Snapshot:
    """Round only the final displayed values; exposure never consumes this payload."""
    context, value = received.context, received.valuation
    accounts = []
    for item in context.accounts:
        account, owner = item.account, item.ownership or {}
        accounts.append(AccountSummary(
            id=account["id"], title=owner.get("title", "Account title unavailable"),
            custodian=account["custodian"], account_number=account["account_number"],
            status=owner.get("status"), registration=owner.get("registration"),
            legal_owners=owner.get("owners", []),
        ))
    positions = []
    for valued in value.positions:
        position = valued.position
        observation = received.observations[position.id]
        assert position.price_local is not None and position.fx_usd is not None
        assert position.price_date is not None
        positions.append(PositionValue.model_validate({
            "id": position.id, "account_id": position.account_id,
            "security_id": position.security_id, "quantity": format(position.quantity, "f"),
            "price_usd": format(mv_position(Decimal("1"), position.price_local,
                                             position.fx_usd), "f"),
            "price_local": format(position.price_local, "f"),
            "listing_currency": observation.currency, "fx_rate": format(position.fx_usd, "f"),
            "rate_at": observation.rate_at, "price_date": position.price_date,
            "price_age_business_days": observation.price_age_business_days,
            "market_value_usd": money(valued.value_usd),
        }))
    cash_balances = [CashValue.model_validate({
        "id": item.id, "account_id": item.account_id, "currency": item.currency,
        "amount_local": format(item.amount_local, "f"), "fx_rate": format(item.fx_usd, "f"),
        "rate_at": received.observations[item.id].rate_at,
        "market_value_usd": money(cash_usd(item.amount_local, item.fx_usd)),
    }) for item in received.cash_inputs if item.fx_usd is not None]
    common = {
        "household_id": value.household_id,
        "household_name": (context.household["name"] if context.household
                           else "Household identity unresolved"),
        "members": [MemberSummary(id=row["id"], name=row["name"]) for row in context.members],
        "accounts": accounts, "positions": positions, "cash_balances": cash_balances,
        "item_count": len(accounts),
        "excluded_accounts": [ExcludedAccount.model_validate({
            key: row[key] for key in ExcludedAccount.model_fields
        }) for row in context.excluded_accounts],
        "reconciliation": [account.reconciliation for account in context.accounts
                           if account.reconciliation is not None],
        "conflicts": [ConflictSummary.model_validate({
            key: row[key] for key in ConflictSummary.model_fields
        }) for row in context.conflicts],
    }
    if value.assets_usd is None:
        return IncompleteSnapshot.model_validate({
            **common, "fresh_subtotal_usd": money(value.fresh_subtotal_usd),
            "missing_components": list(value.missing_components),
            "stale_components": [StaleComponent(
                subject=item.subject, last_known_value_usd=(money(item.last_known_value_usd)
                    if item.last_known_value_usd is not None else None),
                price_date=item.price_date,
                price_age_business_days=(
                    received.observations[item.subject].price_age_business_days or 0
                ),
            ) for item in value.stale_components],
        })
    return CompleteSnapshot.model_validate({
        **common, "assets_usd": money(value.assets_usd), "cash_usd": money(value.cash_usd),
    })


def calculate_received_snapshot(
    connection: sqlite3.Connection, household_id: str, day: date, known: datetime,
) -> tuple[Snapshot, list[QualityFlag], list[str]]:
    result = resolve_received_valuation(connection, household_id, day, known)
    return (serialize_received_snapshot(result), list(result.quality_flags),
            list(result.valuation.source_ids))
