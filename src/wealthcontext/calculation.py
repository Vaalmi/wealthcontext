"""Authoritative Decimal valuation, exposure and concentration.

The adapter supplies authoritative PAS components and already-resolved price/FX
inputs at the requested cutoffs. It owns calendars, authorization and provenance
lookup. Internal result amounts remain Decimal until a transport serializer calls
the output formatter; no calculation consumes display-rounded amounts.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Literal

SecurityType = Literal["COMMON_STOCK", "ETF", "MUTUAL_FUND", "MMF"]
GroupBy = Literal["issuer", "security"]
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


def _finite(value: Decimal, *, positive: bool = False, nonnegative: bool = True) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("financial inputs must be finite Decimals")
    if (nonnegative and value < ZERO) or (positive and value == ZERO):
        raise ValueError("negative balances and nonpositive FX rates are outside the MVP")


def mv_position(quantity: Decimal, price_local: Decimal, fx_usd: Decimal) -> Decimal:
    """MV_POSITION@1: multiply unrounded inputs in the declared order."""
    _finite(quantity)
    _finite(price_local)
    _finite(fx_usd, positive=True)
    with localcontext() as arithmetic:
        arithmetic.prec, arithmetic.rounding = 28, ROUND_HALF_EVEN
        return quantity * price_local * fx_usd


def cash_usd(amount_local: Decimal, fx_usd: Decimal) -> Decimal:
    """CASH_USD@1: PAS cash only; custody cash is not an input."""
    _finite(amount_local)
    _finite(fx_usd, positive=True)
    with localcontext() as arithmetic:
        arithmetic.prec, arithmetic.rounding = 28, ROUND_HALF_EVEN
        return amount_local * fx_usd


def household_assets(position_values: Iterable[Decimal], cash_values: Iterable[Decimal]) -> Decimal:
    """HOUSEHOLD_ASSETS@1 for complete inputs; callers classify completeness."""
    with localcontext() as arithmetic:
        arithmetic.prec, arithmetic.rounding = 28, ROUND_HALF_EVEN
        positions, cash = list(position_values), list(cash_values)
        for amount in (*positions, *cash):
            _finite(amount)
        return sum(positions, ZERO) + sum(cash, ZERO)


@dataclass(frozen=True, slots=True)
class BlockingFlag:
    code: str
    subject: str


@dataclass(frozen=True, slots=True)
class PositionInput:
    id: str
    account_id: str
    security_id: str
    security_name: str
    issuer_id: str
    issuer_name: str
    security_type: SecurityType
    quantity: Decimal
    price_local: Decimal | None
    fx_usd: Decimal | None
    price_date: date | None = None
    stale: bool = False
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CashInput:
    id: str
    account_id: str
    currency: str
    amount_local: Decimal
    fx_usd: Decimal | None
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HouseholdInput:
    household_id: str
    positions: tuple[PositionInput, ...] = ()
    cash: tuple[CashInput, ...] = ()
    missing_account_ids: tuple[str, ...] = ()
    blocking_flags: tuple[BlockingFlag, ...] = ()
    source_ids: tuple[str, ...] = ()
    missing_components: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PositionValue:
    position: PositionInput
    value_usd: Decimal


@dataclass(frozen=True, slots=True)
class StaleComponent:
    subject: str
    last_known_value_usd: Decimal | None
    price_date: date


@dataclass(frozen=True, slots=True)
class HouseholdValuation:
    household_id: str
    positions: tuple[PositionValue, ...]
    cash_usd: Decimal
    fresh_subtotal_usd: Decimal
    assets_usd: Decimal | None
    stale_components: tuple[StaleComponent, ...]
    missing_components: tuple[str, ...]
    missing_account_ids: tuple[str, ...]
    blocking_flags: tuple[BlockingFlag, ...]
    source_ids: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.assets_usd is not None


def value_household(inputs: HouseholdInput) -> HouseholdValuation:
    """Value available fresh components, withholding assets for any blocking input."""
    ids = [item.id for item in (*inputs.positions, *inputs.cash)]
    if len(ids) != len(set(ids)):
        raise ValueError("component IDs must be unique")
    missing = set(inputs.missing_account_ids) | set(inputs.missing_components)
    flags = list(inputs.blocking_flags)
    explained_accounts = {flag.subject for flag in flags
                          if flag.code in {"AMBIGUOUS_ENTITY", "SOURCE_UNAVAILABLE"}}
    flags.extend(BlockingFlag("SOURCE_UNAVAILABLE", account)
                 for account in sorted(set(inputs.missing_account_ids) - explained_accounts))
    sources = set(inputs.source_ids)
    values: list[PositionValue] = []
    cash_values: list[Decimal] = []
    stale: list[StaleComponent] = []
    for position in sorted(inputs.positions, key=lambda item: item.id):
        sources.update(position.source_ids)
        _finite(position.quantity)
        if position.price_local is None or position.fx_usd is None:
            missing.add(position.id)
            flags.append(BlockingFlag("SOURCE_UNAVAILABLE" if position.price_local is None
                                      else "FX_UNAVAILABLE", position.id))
            value = None
        else:
            value = mv_position(position.quantity, position.price_local, position.fx_usd)
        if position.stale:
            if position.price_date is None:
                raise ValueError("a stale component requires its price date")
            stale.append(StaleComponent(position.id, value, position.price_date))
            flags.append(BlockingFlag("PRICE_STALE", position.id))
        elif value is not None:
            values.append(PositionValue(position, value))
    for cash in sorted(inputs.cash, key=lambda item: item.id):
        sources.update(cash.source_ids)
        _finite(cash.amount_local)
        if cash.fx_usd is None:
            missing.add(cash.id)
            flags.append(BlockingFlag("FX_UNAVAILABLE", cash.id))
        else:
            cash_values.append(cash_usd(cash.amount_local, cash.fx_usd))
    fresh = household_assets((value.value_usd for value in values), cash_values)
    return HouseholdValuation(
        inputs.household_id, tuple(values), household_assets((), cash_values), fresh,
        None if missing or stale or flags else fresh, tuple(stale), tuple(sorted(missing)),
        tuple(sorted(set(inputs.missing_account_ids))),
        tuple(sorted(set(flags), key=lambda flag: (flag.code, flag.subject))),
        tuple(sorted(sources)),
    )


@dataclass(frozen=True, slots=True)
class Concentration:
    ratio: Decimal
    percentage: Decimal
    meets_threshold: bool


def _threshold(threshold_pct: Decimal) -> None:
    _finite(threshold_pct)
    if not ONE <= threshold_pct <= Decimal("50"):
        raise ValueError("threshold must be between 1.00 and 50.00 percentage points")
    with localcontext() as arithmetic:
        arithmetic.prec, arithmetic.rounding = 28, ROUND_HALF_EVEN
        if threshold_pct % Decimal("0.01") != ZERO:
            raise ValueError("threshold supports at most two decimal places")


def concentration(
    exposure_usd: Decimal, assets_usd: Decimal, threshold_pct: Decimal = Decimal("10.00"),
) -> Concentration | None:
    """CONCENTRATION@1: compare the unrounded ratio; zero assets has no percentage."""
    _finite(exposure_usd)
    _finite(assets_usd, nonnegative=False)
    _threshold(threshold_pct)
    if assets_usd <= ZERO:
        return None
    with localcontext() as arithmetic:
        arithmetic.prec, arithmetic.rounding = 28, ROUND_HALF_EVEN
        ratio = exposure_usd / assets_usd
        return Concentration(ratio, ratio * HUNDRED, ratio >= threshold_pct / HUNDRED)


@dataclass(frozen=True, slots=True)
class IssuerExposureRow:
    entity_id: str
    label: str
    exposure_usd: Decimal
    rank: int
    concentration_pct: Decimal | None
    entity_type: Literal["issuer"] = "issuer"
    exposure_kind: Literal["operating_company"] = "operating_company"


@dataclass(frozen=True, slots=True)
class SecurityExposureRow:
    entity_id: str
    label: str
    exposure_kind: Literal["operating_company", "fund_vehicle"]
    exposure_usd: Decimal
    rank: int
    allocation_pct: Decimal | None
    entity_type: Literal["security"] = "security"


def allocation_pct(exposure_usd: Decimal, assets_usd: Decimal | None) -> Decimal | None:
    """Descriptive allocation, never a fund/security concentration predicate."""
    _finite(exposure_usd)
    if assets_usd is None:
        return None
    _finite(assets_usd, nonnegative=False)
    if assets_usd <= ZERO:
        return None
    with localcontext() as arithmetic:
        arithmetic.prec, arithmetic.rounding = 28, ROUND_HALF_EVEN
        return exposure_usd / assets_usd * HUNDRED


def _groups(
    valuation: HouseholdValuation, group_by: GroupBy, *, funds_only: bool = False,
) -> dict[str, list[PositionValue]]:
    if not valuation.complete:
        return {}  # Incomplete results have diagnostic subtotals, not partial percentages.
    groups: dict[str, list[PositionValue]] = {}
    for value in valuation.positions:
        position = value.position
        if position.security_type not in {"COMMON_STOCK", "ETF", "MUTUAL_FUND", "MMF"}:
            raise ValueError("unsupported security type")
        common_stock = position.security_type == "COMMON_STOCK"
        if (group_by == "issuer" and not common_stock) or (funds_only and common_stock):
            continue
        key = position.issuer_id if group_by == "issuer" else position.security_id
        groups.setdefault(key, []).append(value)
    return groups


def issuer_exposure(valuation: HouseholdValuation) -> tuple[IssuerExposureRow, ...]:
    """ISSUER_EXPOSURE@1: rank operating-company issuers independently of vehicles."""
    aggregated = []
    for issuer, positions in _groups(valuation, "issuer").items():
        amount = household_assets((position.value_usd for position in positions), ())
        aggregated.append(IssuerExposureRow(
            issuer, positions[0].position.issuer_name, amount, 0,
            allocation_pct(amount, valuation.assets_usd),
        ))
    aggregated.sort(key=lambda item: (item.exposure_usd.copy_negate(), item.entity_id))
    return tuple(replace(item, rank=rank) for rank, item in enumerate(aggregated, start=1))


def security_exposure(
    valuation: HouseholdValuation, *, funds_only: bool = False,
) -> tuple[SecurityExposureRow, ...]:
    """Rank securities or fund vehicles by security ID, never by their sponsor."""
    aggregated = []
    for security, positions in _groups(valuation, "security", funds_only=funds_only).items():
        first = positions[0].position
        amount = household_assets((position.value_usd for position in positions), ())
        kind: Literal["operating_company", "fund_vehicle"] = (
            "operating_company" if first.security_type == "COMMON_STOCK" else "fund_vehicle"
        )
        aggregated.append(SecurityExposureRow(
            security, first.security_name, kind, amount, 0,
            allocation_pct(amount, valuation.assets_usd),
        ))
    aggregated.sort(key=lambda item: (item.exposure_usd.copy_negate(), item.entity_id))
    return tuple(replace(item, rank=rank) for rank, item in enumerate(aggregated, start=1))


@dataclass(frozen=True, slots=True)
class ListMetadata:
    item_count: int
    total_item_count: int
    ordering: str
    truncated: bool
    next_cursor: None = None


@dataclass(frozen=True, slots=True)
class BreachMetadata(ListMetadata):
    rank_scope: Literal["book_concentration_breaches"] = "book_concentration_breaches"


def _metadata(total: int, shown: int, ordering: str) -> ListMetadata:
    return ListMetadata(shown, total, ordering, shown < total)


@dataclass(frozen=True, slots=True)
class ExposureList[Row]:
    items: tuple[Row, ...]
    metadata: ListMetadata
    rank_scope: Literal["household_issuers", "household_fund_vehicles", "household_securities"]


@dataclass(frozen=True, slots=True)
class IssuerPortfolioExposure:
    valuation: HouseholdValuation
    issuer_exposures: ExposureList[IssuerExposureRow]
    fund_vehicle_exposures: ExposureList[SecurityExposureRow]
    group_by: Literal["issuer"] = "issuer"


@dataclass(frozen=True, slots=True)
class SecurityPortfolioExposure:
    valuation: HouseholdValuation
    security_exposures: ExposureList[SecurityExposureRow]
    group_by: Literal["security"] = "security"


PortfolioExposure = IssuerPortfolioExposure | SecurityPortfolioExposure


def _top_twenty[Row](
    rows: tuple[Row, ...],
    scope: Literal["household_issuers", "household_fund_vehicles", "household_securities"],
) -> ExposureList[Row]:
    return ExposureList(rows[:20], _metadata(len(rows), min(len(rows), 20),
                                            "exposure_usd_desc,entity_id_asc"), scope)


def portfolio_exposure(
    valuation: HouseholdValuation, group_by: GroupBy = "issuer",
) -> PortfolioExposure:
    if group_by == "issuer":
        return IssuerPortfolioExposure(
            valuation, _top_twenty(issuer_exposure(valuation), "household_issuers"),
            _top_twenty(security_exposure(valuation, funds_only=True), "household_fund_vehicles"),
        )
    if group_by == "security":
        return SecurityPortfolioExposure(
            valuation, _top_twenty(security_exposure(valuation), "household_securities"),
        )
    raise ValueError("group_by must be issuer or security")


@dataclass(frozen=True, slots=True)
class Breach:
    household_id: str
    issuer_id: str
    label: str
    exposure_usd: Decimal
    assets_usd: Decimal
    concentration_pct: Decimal
    book_rank: int
    issuer_rank: int
    meets_threshold: bool = True


@dataclass(frozen=True, slots=True)
class Indeterminate:
    household_id: str
    reason: Literal["INCOMPLETE_ASSETS", "NON_POSITIVE_ASSETS"]
    fresh_subtotal_usd: Decimal
    stale_component_count: int
    missing_account_ids: tuple[str, ...]
    blocking_flags: tuple[BlockingFlag, ...]


@dataclass(frozen=True, slots=True)
class ConcentratedPositions:
    applied_threshold_pct: Decimal
    breaches: tuple[Breach, ...]
    indeterminate: tuple[Indeterminate, ...]
    breach_metadata: BreachMetadata
    indeterminate_metadata: ListMetadata
    status: Literal["COMPLETE", "INCOMPLETE"]
    evaluated_household_count: int
    determinate_household_count: int
    indeterminate_household_count: int
    breaching_household_count: int
    comparator: Literal[">="] = ">="


def concentrated_positions(
    valuations: Iterable[HouseholdValuation], threshold_pct: Decimal = Decimal("10.00"),
) -> ConcentratedPositions:
    """All supplied households must already be authorized; retain every indeterminate."""
    _threshold(threshold_pct)
    households = sorted(valuations, key=lambda value: value.household_id)
    if len({value.household_id for value in households}) != len(households):
        raise ValueError("households must be unique")
    candidates: list[tuple[str, IssuerExposureRow, Decimal, Decimal]] = []
    indeterminate = []
    for valuation in households:
        assets = valuation.assets_usd
        if assets is None or assets <= ZERO:
            indeterminate.append(Indeterminate(
                valuation.household_id, "INCOMPLETE_ASSETS" if assets is None
                else "NON_POSITIVE_ASSETS", valuation.fresh_subtotal_usd,
                len(valuation.stale_components), valuation.missing_account_ids,
                valuation.blocking_flags,
            ))
            continue
        for exposure in issuer_exposure(valuation):
            measured = concentration(exposure.exposure_usd, assets, threshold_pct)
            if measured is not None and measured.meets_threshold:
                candidates.append((valuation.household_id, exposure, assets, measured.percentage))
    candidates.sort(key=lambda item: (item[3].copy_negate(), item[0], item[1].entity_id))
    breaches = tuple(Breach(household, exposure.entity_id, exposure.label, exposure.exposure_usd,
                            assets, percentage, rank, exposure.rank)
                     for rank, (household, exposure, assets, percentage)
                     in enumerate(candidates[:20], start=1))
    return ConcentratedPositions(
        threshold_pct, breaches, tuple(indeterminate),
        BreachMetadata(len(breaches), len(candidates),
                       "concentration_pct_desc,household_id_asc,issuer_id_asc",
                       len(breaches) < len(candidates)),
        _metadata(len(indeterminate), len(indeterminate), "household_id_asc"),
        "INCOMPLETE" if indeterminate else "COMPLETE",
        len(households), len(households) - len(indeterminate), len(indeterminate),
        len({household for household, *_ in candidates}),
    )
