"""Serialize already-computed Decimal results; no comparisons or aggregation here."""

from dataclasses import asdict

from wealthcontext.calculation import (
    ConcentratedPositions,
    GroupBy,
    HouseholdValuation,
    IssuerExposureRow,
    IssuerPortfolioExposure,
    SecurityExposureRow,
    portfolio_exposure,
)
from wealthcontext.decimal_format import money
from wealthcontext.exposure_contracts import (
    CompleteConcentration,
    CompleteIssuerExposure,
    CompleteSecurityExposure,
    ConcentrationEnvelope,
    ConcentrationPayload,
    ExposureEnvelope,
    ExposurePayload,
    FundAmountRow,
    FundPercentageRow,
    FundRow,
    IncompleteConcentration,
    IncompleteIssuerExposure,
    IncompleteSecurityExposure,
    IssuerAmountRow,
    IssuerPercentageRow,
    IssuerRow,
    SecurityAmountRow,
    SecurityPercentageRow,
    SecurityRow,
)

__all__ = [
    "ConcentrationEnvelope", "ExposureEnvelope", "serialize_concentration", "serialize_exposure",
]


def _issuer_row(row: IssuerExposureRow) -> IssuerRow:
    payload = {
        "entity_id": row.entity_id, "entity_type": row.entity_type,
        "label": row.label, "exposure_kind": row.exposure_kind,
        "exposure_usd": money(row.exposure_usd), "rank": row.rank,
    }
    if row.concentration_pct is None:
        return IssuerAmountRow.model_validate(payload)
    return IssuerPercentageRow.model_validate({
        **payload, "concentration_pct": money(row.concentration_pct),
    })


def _security_row(row: SecurityExposureRow, *, fund_only: bool = False) -> SecurityRow | FundRow:
    payload = {
        "entity_id": row.entity_id, "entity_type": row.entity_type,
        "label": row.label, "exposure_kind": row.exposure_kind,
        "exposure_usd": money(row.exposure_usd), "rank": row.rank,
    }
    if row.allocation_pct is None:
        model = FundAmountRow if fund_only else SecurityAmountRow
        return model.model_validate(payload)
    model = FundPercentageRow if fund_only else SecurityPercentageRow
    return model.model_validate({**payload, "allocation_pct": money(row.allocation_pct)})


def serialize_exposure(
    computed: HouseholdValuation, household_name: str, group_by: GroupBy = "issuer",
) -> ExposurePayload:
    """No public account list is needed; source/account scopes belong to service evidence."""
    view = portfolio_exposure(computed, group_by)
    identity = {"household_id": computed.household_id, "household_name": household_name,
                "group_by": group_by}
    if computed.assets_usd is None:
        diagnostics = {
            "fresh_subtotal_usd": money(computed.fresh_subtotal_usd),
            "missing_components": list(computed.missing_components),
            "stale_components": [
                {"subject": row.subject, "last_known_value_usd": (money(row.last_known_value_usd)
                    if row.last_known_value_usd is not None else None),
                 "price_date": row.price_date}
                for row in computed.stale_components
            ],
        }
    else:
        diagnostics = {"assets_usd": money(computed.assets_usd)}
    if isinstance(view, IssuerPortfolioExposure):
        issuers, funds = view.issuer_exposures, view.fund_vehicle_exposures
        lists = {
            "issuer_exposures": {
                **asdict(issuers.metadata), "rank_scope": issuers.rank_scope,
                "items": [_issuer_row(row).model_dump(mode="json") for row in issuers.items],
            },
            "fund_vehicle_exposures": {
                **asdict(funds.metadata), "rank_scope": funds.rank_scope,
                "items": [_security_row(row, fund_only=True).model_dump(mode="json")
                          for row in funds.items],
            },
        }
        model = CompleteIssuerExposure if computed.complete else IncompleteIssuerExposure
        return model.model_validate({**identity, **diagnostics, **lists})
    securities = view.security_exposures
    listing = {
        **asdict(securities.metadata), "rank_scope": securities.rank_scope,
        "items": [_security_row(row).model_dump(mode="json") for row in securities.items],
    }
    model = CompleteSecurityExposure if computed.complete else IncompleteSecurityExposure
    return model.model_validate({**identity, **diagnostics, "security_exposures": listing})


def serialize_concentration(computed: ConcentratedPositions) -> ConcentrationPayload:
    """Preserve pre-cap counts, decisions and all indeterminate rows from calculation."""
    payload = {
        "applied_threshold_pct": money(computed.applied_threshold_pct),
        "comparator": computed.comparator,
        "evaluated_household_count": computed.evaluated_household_count,
        "determinate_household_count": computed.determinate_household_count,
        "indeterminate_household_count": computed.indeterminate_household_count,
        "breaching_household_count": computed.breaching_household_count,
        "breaches": [
            {
                "household_id": row.household_id, "issuer_id": row.issuer_id, "label": row.label,
                "exposure_usd": money(row.exposure_usd), "assets_usd": money(row.assets_usd),
                "concentration_pct": money(row.concentration_pct),
                "book_rank": row.book_rank, "issuer_rank": row.issuer_rank,
                "meets_threshold": row.meets_threshold,
            } for row in computed.breaches
        ],
        "indeterminate": [
            {
                "household_id": row.household_id, "reason": row.reason,
                "fresh_subtotal_usd": money(row.fresh_subtotal_usd),
                "stale_component_count": row.stale_component_count,
                "missing_account_ids": list(row.missing_account_ids),
                "blocking_flags": [
                    {"code": flag.code, "subject": flag.subject,
                     "severity": "warning", "detail": {}}
                    for flag in row.blocking_flags
                ],
            } for row in computed.indeterminate
        ],
        "breach_metadata": asdict(computed.breach_metadata),
        "indeterminate_metadata": asdict(computed.indeterminate_metadata),
    }
    model = CompleteConcentration if computed.status == "COMPLETE" else IncompleteConcentration
    return model.model_validate(payload)
