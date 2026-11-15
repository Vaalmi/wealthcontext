"""Hand-calculated boundaries for authoritative Decimal arithmetic."""

from dataclasses import asdict, replace
from datetime import date
from decimal import ROUND_DOWN, Decimal, localcontext

import pytest

from wealthcontext.calculation import (
    BlockingFlag,
    CashInput,
    HouseholdInput,
    IssuerPortfolioExposure,
    PositionInput,
    SecurityPortfolioExposure,
    SecurityType,
    cash_usd,
    concentrated_positions,
    concentration,
    household_assets,
    issuer_exposure,
    mv_position,
    portfolio_exposure,
    security_exposure,
    value_household,
)
from wealthcontext.decimal_format import money

D = Decimal


def position(
    security: str, value: str, *, issuer: str | None = None, kind: SecurityType = "COMMON_STOCK",
) -> PositionInput:
    issuer = issuer or security
    return PositionInput(
        id=f"A:{security}", account_id="A", security_id=security,
        security_name=f"Security {security}", issuer_id=issuer,
        issuer_name=f"Issuer {issuer}", security_type=kind,
        quantity=D("1"), price_local=D(value), fx_usd=D("1"),
        source_ids=(f"source-{security}",),
    )


def cash(amount: str) -> CashInput:
    return CashInput("A:USD", "A", "USD", D(amount), D("1"))


def household(name: str, *positions: PositionInput, amount: str = "0"):
    return value_household(HouseholdInput(name, tuple(positions), (cash(amount),)))


def test_usd_and_fx_metrics_use_pas_components_once():
    assert mv_position(D("3"), D("12.50"), D("1")) == D("37.50")
    assert mv_position(D("3"), D("12.50"), D("1.08")) == D("40.5000")
    assert cash_usd(D("50"), D("0.75")) == D("37.50")
    assert household_assets((D("40.5"), D("37.5")), (D("37.5"),)) == D("115.5")
    foreign = replace(position("EUR", "12.50"), quantity=D("3"), fx_usd=D("1.08"))
    value = value_household(HouseholdInput(
        "H", (foreign,), (CashInput("A:CAD", "A", "CAD", D("50"), D("0.75")),),
    ))
    assert value.assets_usd == D("78")
    assert money(value.positions[0].value_usd) == "40.50"
    assert value.source_ids == ("source-EUR",)


def test_two_half_cent_positions_sum_before_display_rounding():
    value = household("H", position("A", "0.005", issuer="ISS"),
                      position("B", "0.005", issuer="ISS"))
    assert [money(item.value_usd) for item in value.positions] == ["0.01", "0.01"]
    assert value.assets_usd is not None
    assert value.assets_usd == D("0.010") and money(value.assets_usd) == "0.01"
    exposure = issuer_exposure(value)
    assert len(exposure) == 1 and exposure[0].exposure_usd == D("0.010")
    assert money(exposure[0].exposure_usd) == "0.01"


def test_percentage_rounding_to_threshold_does_not_manufacture_a_breach():
    measured = concentration(D("999.96"), D("10000"))
    assert measured is not None
    assert measured.percentage == D("9.999600")
    assert money(measured.percentage) == "10.00"
    assert measured.meets_threshold is False
    value = household("H", position("A", "999.96"), amount="9000.04")
    result = concentrated_positions((value,))
    assert result.breaches == () and result.indeterminate == ()
    assert result.applied_threshold_pct == D("10.00") and result.comparator == ">="


def test_exact_threshold_uses_greater_than_or_equal():
    result = concentrated_positions((household("H", position("A", "1000"), amount="9000"),))
    assert len(result.breaches) == 1
    assert result.breaches[0].concentration_pct == D("10.0")
    assert result.breaches[0].meets_threshold is True


def test_issuer_aggregation_ties_and_security_grouping():
    value = household(
        "H", position("SHARE-B", "60", issuer="ISS-1"),
        position("SHARE-A", "40", issuer="ISS-1"), position("OTHER", "100", issuer="ISS-2"),
    )
    issuers = issuer_exposure(value)
    assert [(item.entity_id, item.exposure_usd, item.rank) for item in issuers] == [
        ("ISS-1", D("100"), 1), ("ISS-2", D("100"), 2),
    ]
    securities = security_exposure(value)
    assert [(item.entity_id, item.rank) for item in securities] == [
        ("OTHER", 1), ("SHARE-B", 2), ("SHARE-A", 3),
    ]
    assert [item.allocation_pct for item in securities] == [D("50"), D("30"), D("20")]
    assert all("concentration_pct" not in asdict(item) for item in securities)


def test_ranking_uses_unrounded_values_before_lexical_tie_break():
    value = household("H", position("A", "1.001"), position("Z", "1.004"))
    assert [money(item.value_usd) for item in value.positions] == ["1.00", "1.00"]
    assert [item.entity_id for item in issuer_exposure(value)] == ["Z", "A"]


def test_fund_vehicles_never_merge_into_sponsor_or_concentration_numerators():
    value = household(
        "H", position("STOCK", "100", issuer="SPONSOR"),
        position("FUND-A", "200", issuer="SPONSOR", kind="ETF"),
        position("FUND-B", "100", issuer="SPONSOR", kind="MUTUAL_FUND"),
        position("SWEEP", "150", issuer="SPONSOR", kind="MMF"), amount="450",
    )
    assert value.assets_usd == D("1000")
    exposure = portfolio_exposure(value)
    assert isinstance(exposure, IssuerPortfolioExposure)
    issuers = exposure.issuer_exposures
    funds = exposure.fund_vehicle_exposures
    assert issuers.rank_scope == "household_issuers"
    assert funds.rank_scope == "household_fund_vehicles"
    assert issuers.metadata.item_count == 1 and funds.metadata.item_count == 3
    assert [(row.entity_id, row.rank) for row in funds.items] == [
        ("FUND-A", 1), ("SWEEP", 2), ("FUND-B", 3),
    ]
    assert [row.allocation_pct for row in funds.items] == [D("20"), D("15"), D("10")]
    assert all(row.exposure_kind == "fund_vehicle" for row in funds.items)
    assert all("concentration_pct" not in asdict(row) for row in funds.items)
    issuer = issuers.items[0]
    assert issuer.entity_id == "SPONSOR" and issuer.rank == 1
    assert issuer.exposure_usd == D("100") and issuer.concentration_pct == D("10.0")
    assert "allocation_pct" not in asdict(issuer)
    result = concentrated_positions((value,))
    assert [row.issuer_id for row in result.breaches] == ["SPONSOR"]
    assert result.breaches[0].assets_usd == D("1000")


def test_zero_denominator_is_indeterminate_and_has_no_money():
    value = household("ZERO", position("EMPTY", "0"), position("SWEEP", "0", kind="MMF"))
    assert value.complete and value.assets_usd == D("0")
    assert issuer_exposure(value)[0].concentration_pct is None
    assert all(row.allocation_pct is None for row in security_exposure(value))
    assert concentration(D("0"), D("0")) is None
    assert concentration(D("1"), D("-1")) is None
    result = concentrated_positions((value,))
    assert result.breaches == () and result.status == "INCOMPLETE"
    assert result.indeterminate[0].reason == "NON_POSITIVE_ASSETS"
    assert result.indeterminate[0].fresh_subtotal_usd == D("0")


def test_missing_and_stale_components_withhold_total_and_all_partial_percentages():
    stale = replace(position("STALE", "40"), stale=True, price_date=date(2026, 9, 24))
    missing_price = replace(position("NO-PRICE", "1"), price_local=None)
    missing_fx = replace(position("NO-FX", "1"), fx_usd=None)
    missing_cash_fx = replace(cash("20"), fx_usd=None)
    value = value_household(HouseholdInput(
        "PARTIAL", (position("FRESH", "10"), stale, missing_price, missing_fx),
        (missing_cash_fx,), ("MISSING-ACCOUNT",),
        (BlockingFlag("AMBIGUOUS_ENTITY", "MEMBER"),), ("source-household",),
    ))
    assert value.assets_usd is None and value.fresh_subtotal_usd == D("10")
    assert value.stale_components[0].last_known_value_usd == D("40")
    assert value.missing_components == ("A:NO-FX", "A:NO-PRICE", "A:USD", "MISSING-ACCOUNT")
    assert issuer_exposure(value) == ()
    assert security_exposure(value) == ()
    issuer_view = portfolio_exposure(value)
    security_view = portfolio_exposure(value, "security")
    assert isinstance(issuer_view, IssuerPortfolioExposure)
    assert isinstance(security_view, SecurityPortfolioExposure)
    assert issuer_view.issuer_exposures.items == issuer_view.fund_vehicle_exposures.items == ()
    assert security_view.security_exposures.items == ()
    result = concentrated_positions((value,))
    assert result.breaches == () and result.status == "INCOMPLETE"
    item = result.indeterminate[0]
    assert item.reason == "INCOMPLETE_ASSETS" and item.fresh_subtotal_usd == D("10")
    assert item.stale_component_count == 1 and item.missing_account_ids == ("MISSING-ACCOUNT",)
    assert {flag.code for flag in item.blocking_flags} == {
        "SOURCE_UNAVAILABLE", "FX_UNAVAILABLE", "PRICE_STALE", "AMBIGUOUS_ENTITY",
    }


def test_concentration_checks_issuer_beyond_exposure_top_twenty():
    funds = [position(f"FUND-{index:02}", "100", kind="ETF") for index in range(20)]
    value = household("H", *funds, position("ISSUER", "50"))
    exposure = portfolio_exposure(value, "security")
    assert isinstance(exposure, SecurityPortfolioExposure)
    securities = exposure.security_exposures
    assert securities.rank_scope == "household_securities"
    assert securities.metadata.item_count == 20 and securities.metadata.total_item_count == 21
    assert securities.metadata.truncated and securities.metadata.next_cursor is None
    assert all(row.exposure_kind == "fund_vehicle" for row in securities.items)
    result = concentrated_positions((value,), D("1"))
    assert [row.issuer_id for row in result.breaches] == ["ISSUER"]


def test_book_limits_breaches_after_comparison_and_retains_every_indeterminate():
    complete = [household(f"H-{index:02}", position("ISS", "100")) for index in range(25)]
    unknown = [value_household(HouseholdInput(
        f"MISSING-{index:02}", missing_account_ids=(f"ACCOUNT-{index:02}",),
    )) for index in range(25)]
    result = concentrated_positions(reversed(complete + unknown))
    assert len(result.breaches) == 20 and result.breach_metadata.total_item_count == 25
    assert result.breach_metadata.truncated and result.breach_metadata.item_count == 20
    assert [row.household_id for row in result.breaches] == [f"H-{index:02}" for index in range(20)]
    assert [row.book_rank for row in result.breaches] == list(range(1, 21))
    assert [row.issuer_rank for row in result.breaches] == [1] * 20
    assert len(result.indeterminate) == result.indeterminate_metadata.item_count == 25
    assert result.indeterminate_metadata.truncated is False
    assert [row.household_id for row in result.indeterminate] == [
        f"MISSING-{index:02}" for index in range(25)
    ]


def test_issuer_fund_and_security_lists_have_independent_ranks_and_limits():
    funds = [position(f"FUND-{index:02}", "1", issuer="SPONSOR", kind="ETF")
             for index in range(25)]
    stocks = [position(f"STOCK-{index:02}", "1") for index in range(25)]
    value = household("H", *reversed(funds + stocks))
    issuer_view = portfolio_exposure(value)
    security_view = portfolio_exposure(value, "security")
    assert isinstance(issuer_view, IssuerPortfolioExposure)
    assert isinstance(security_view, SecurityPortfolioExposure)
    for listing in (issuer_view.issuer_exposures, issuer_view.fund_vehicle_exposures):
        assert listing.metadata.item_count == 20 and listing.metadata.total_item_count == 25
        assert listing.metadata.truncated
        assert [row.rank for row in listing.items] == list(range(1, 21))
    assert issuer_view.issuer_exposures.items[0].entity_id == "STOCK-00"
    assert issuer_view.fund_vehicle_exposures.items[0].entity_id == "FUND-00"
    assert security_view.security_exposures.metadata.total_item_count == 50
    assert [row.entity_id for row in security_view.security_exposures.items] == [
        f"FUND-{index:02}" for index in range(20)
    ]
    result = concentrated_positions((value,), D("1"))
    assert result.breach_metadata.total_item_count == 25
    assert len(result.breaches) == 20  # All 25 issuers qualified before the cap.


def test_breach_book_rank_is_distinct_from_household_issuer_rank():
    first = household("H-A", position("LARGE", "60"), position("SMALL", "40"))
    second = household("H-B", position("SINGLE", "90"), amount="10")
    result = concentrated_positions((first, second))
    assert result.breach_metadata.rank_scope == "book_concentration_breaches"
    assert [(row.household_id, row.issuer_id, row.book_rank, row.issuer_rank)
            for row in result.breaches] == [
        ("H-B", "SINGLE", 1, 1), ("H-A", "LARGE", 2, 1), ("H-A", "SMALL", 3, 2),
    ]
    assert all("rank" not in asdict(row) for row in result.breaches)


def test_generic_missing_components_block_total_without_becoming_missing_accounts():
    flag = BlockingFlag("AMBIGUOUS_ENTITY", "MEMBERSHIP")
    value = value_household(HouseholdInput(
        "H", (position("FRESH", "10"),), missing_components=("MEMBERSHIP", "UNKNOWN-SECURITY"),
        blocking_flags=(flag, flag),
    ))
    assert value.assets_usd is None and value.fresh_subtotal_usd == D("10")
    assert value.missing_components == ("MEMBERSHIP", "UNKNOWN-SECURITY")
    assert value.missing_account_ids == () and value.blocking_flags == (flag,)
    assert issuer_exposure(value) == security_exposure(value) == ()
    item = concentrated_positions((value,)).indeterminate[0]
    assert item.missing_account_ids == () and item.blocking_flags == (flag,)


def test_intermediate_products_use_precision_28_half_even_before_output_half_up():
    # Both quantities are 28-digit feed values. Multiplying by 1.5 creates a
    # 29th significant digit exactly halfway between the retained neighbors.
    odd = mv_position(D("1.000000000000000000000000001"), D("1.5"), D("1"))
    even = mv_position(D("1.000000000000000000000000003"), D("1.5"), D("1"))
    assert odd == D("1.500000000000000000000000002")
    assert even == D("1.500000000000000000000000004")
    assert money(D("0.005")) == "0.01"
    assert money(D("1.005")) == "1.01"


def test_calculation_precision_is_local_and_output_accommodates_large_magnitudes():
    with localcontext() as ambient:
        ambient.prec, ambient.rounding = 3, ROUND_DOWN
        assert mv_position(D("1"), D("1.23456789"), D("1.01")) == D("1.2469135689")
        value = household("H", position("A", "1.001"), position("Z", "1.004"))
        assert [row.entity_id for row in issuer_exposure(value)] == ["Z", "A"]
        assert value.assets_usd == D("2.005")
        large = mv_position(D("1" + "0" * 27), D("1" + "0" * 27), D("1"))
        assert money(large) == "1" + "0" * 54 + ".00"
        assert ambient.prec == 3 and ambient.rounding == ROUND_DOWN


@pytest.mark.parametrize("threshold", [D("0.99"), D("50.01"), D("10.005"), D("NaN")])
def test_invalid_thresholds_are_rejected(threshold):
    with pytest.raises(ValueError):
        concentrated_positions((), threshold)


@pytest.mark.parametrize("value", [1.1, D("NaN"), D("Infinity"), D("-1")])
def test_non_decimal_nonfinite_or_negative_financial_inputs_are_rejected(value):
    with pytest.raises(ValueError):
        mv_position(value, D("1"), D("1"))
