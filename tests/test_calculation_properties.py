"""Financial properties against exact integer/Fraction expectations.

A quantity in hundredths of a share times a price in cents is an integer number
of 1/10,000 USD units. At most 12 positions and the bounds below keep sums under
14 significant digits, so Decimal precision 28 represents these arithmetic
steps exactly. Threshold cases use a 10,000 USD denominator, making their ratios
terminate within eight decimal places. No expected amount calls a metric helper.
"""

from decimal import Decimal
from fractions import Fraction

from hypothesis import example, given
from hypothesis import strategies as st

from wealthcontext.calculation import (
    CashInput,
    HouseholdInput,
    PositionInput,
    concentrated_positions,
    concentration,
    issuer_exposure,
    security_exposure,
    value_household,
)
from wealthcontext.decimal_format import money

USD_UNITS = 10_000


def decimal_units(units: int, places: int) -> Decimal:
    """Construct input decimals from integer units without float or ambient rounding."""
    scale = 10 ** places
    return Decimal(f"{units // scale}.{units % scale:0{places}d}")


def formatted_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def stock(
    account: str, security: str, issuer: str, quantity_hundredths: int, price_cents: int,
) -> PositionInput:
    return PositionInput(
        id=f"{account}:{security}", account_id=account, security_id=security,
        security_name=f"Security {security}", issuer_id=issuer, issuer_name=f"Issuer {issuer}",
        security_type="COMMON_STOCK", quantity=decimal_units(quantity_hundredths, 2),
        price_local=decimal_units(price_cents, 2), fx_usd=Decimal("1"),
    )


@given(
    specs=st.lists(st.tuples(st.integers(0, 100_000), st.integers(0, 1_000_000)), max_size=10),
    cash_cents=st.integers(0, 100_000_000),
)
@example(specs=[], cash_cents=0)
def test_subcent_values_sum_exactly_before_money_rounding(
    specs: list[tuple[int, int]], cash_cents: int,
):
    # Two half-cent holdings guarantee a witness against per-position cent rounding.
    specs = [(1, 50), (1, 50), *specs]
    positions = tuple(stock(f"A-{index % 3}", f"SEC-{index}", "ISS",
                            quantity, price)
                      for index, (quantity, price) in enumerate(specs))
    value = value_household(HouseholdInput(
        "H", positions,
        (CashInput("A:CASH", "A", "USD", decimal_units(cash_cents, 2), Decimal("1")),),
    ))
    expected_position_units = {item.id: quantity * price
                               for item, (quantity, price) in zip(positions, specs, strict=True)}
    expected_total_units = sum(expected_position_units.values()) + cash_cents * 100
    assert value.assets_usd is not None
    assert Fraction(value.assets_usd) == Fraction(expected_total_units, USD_UNITS)
    assert {item.position.id: Fraction(item.value_usd) for item in value.positions} == {
        key: Fraction(units, USD_UNITS) for key, units in expected_position_units.items()
    }
    # Nonnegative HALF_UP to cents: integer division after adding half a cent.
    rounded_cents = (expected_total_units + 50) // 100
    assert money(value.assets_usd) == formatted_cents(rounded_cents)
    issuers = issuer_exposure(value)
    assert len(issuers) == 1
    assert Fraction(issuers[0].exposure_usd) == Fraction(
        sum(expected_position_units.values()), USD_UNITS,
    )


@given(
    quantities=st.lists(st.integers(0, 10_000), min_size=12, max_size=12),
    prices=st.lists(st.integers(0, 100_000), min_size=4, max_size=4),
    order=st.permutations(tuple(range(12))),
)
def test_issuer_and_security_aggregation_ignore_account_and_input_permutations(
    quantities: list[int], prices: list[int], order: tuple[int, ...],
):
    # Three accounts each hold four securities, with two share classes per issuer.
    # A security has the same quote in each account, as a resolved price feed would.
    positions = []
    expected_issuers = {"ISS-0": 0, "ISS-1": 0}
    expected_securities = {f"SEC-{index}": 0 for index in range(4)}
    for index, quantity in enumerate(quantities):
        security_index = index % 4
        issuer = f"ISS-{security_index // 2}"
        security = f"SEC-{security_index}"
        positions.append(stock(f"A-{index // 4}", security, issuer, quantity,
                               prices[security_index]))
        units = quantity * prices[security_index]
        expected_issuers[issuer] += units
        expected_securities[security] += units
    baseline = value_household(HouseholdInput("H", tuple(positions)))
    shuffled = value_household(HouseholdInput("H", tuple(positions[index] for index in order)))
    assert baseline.assets_usd == shuffled.assets_usd
    assert issuer_exposure(baseline) == issuer_exposure(shuffled)
    assert security_exposure(baseline) == security_exposure(shuffled)
    for rows, expected in ((issuer_exposure(shuffled), expected_issuers),
                           (security_exposure(shuffled), expected_securities)):
        assert {item.entity_id: Fraction(item.exposure_usd) for item in rows} == {
            key: Fraction(units, USD_UNITS) for key, units in expected.items()
        }
        expected_order = sorted(expected, key=lambda key: (-expected[key], key))
        assert [(item.entity_id, item.rank) for item in rows] == [
            (key, index) for index, key in enumerate(expected_order, start=1)
        ]


@given(threshold_cents=st.integers(100, 5_000), offset_units=st.integers(-49, 49))
@example(threshold_cents=1_000, offset_units=-1)
@example(threshold_cents=1_000, offset_units=0)
@example(threshold_cents=1_000, offset_units=1)
def test_unrounded_threshold_predicate_matches_fraction_on_both_sides_of_boundary(
    threshold_cents: int, offset_units: int,
):
    # Holding 0.01 share at a cent-denominated quote gives the desired subcent
    # exposure. The MMF remainder fixes total assets at exactly USD 10,000.
    total_units = 10_000 * USD_UNITS
    exposure_units = threshold_cents * USD_UNITS + offset_units
    target = stock("A", "TARGET", "TARGET-ISSUER", 1, exposure_units)
    fund = PositionInput(
        id="A:MMF", account_id="A", security_id="MMF", security_name="Sweep vehicle",
        issuer_id="FUND-SPONSOR", issuer_name="Fund sponsor", security_type="MMF",
        quantity=Decimal("0.01"), price_local=decimal_units(total_units - exposure_units, 2),
        fx_usd=Decimal("1"),
    )
    value = value_household(HouseholdInput("H", (fund, target)))
    threshold = decimal_units(threshold_cents, 2)
    expected_ratio = Fraction(exposure_units, total_units)
    expected_threshold_ratio = Fraction(threshold_cents, 10_000)
    expected_breach = expected_ratio >= expected_threshold_ratio
    issuer = issuer_exposure(value)[0]
    assert value.assets_usd is not None and Fraction(value.assets_usd) == 10_000
    measured = concentration(issuer.exposure_usd, value.assets_usd, threshold)
    assert measured is not None
    assert Fraction(measured.ratio) == expected_ratio
    assert Fraction(measured.percentage) == expected_ratio * 100
    assert measured.meets_threshold == expected_breach
    # Below/equal/above all display the threshold; the display cannot decide >=.
    assert money(measured.percentage) == formatted_cents(threshold_cents)
    book = concentrated_positions((value,), threshold)
    assert [item.issuer_id for item in book.breaches] == (
        ["TARGET-ISSUER"] if expected_breach else []
    )
    assert book.indeterminate == ()
