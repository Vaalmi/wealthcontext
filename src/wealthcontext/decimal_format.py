"""Output rounding without narrowing the magnitude accepted by Decimal calculations."""

from decimal import ROUND_HALF_UP, Decimal, localcontext


def money(value: Decimal) -> str:
    """Render cents after calculation, allowing integer digits and a rounding carry.

    Only the output context is widened. The caller's calculation precision and
    rounding remain unchanged, including for products wider than the input fields.
    """
    if not value.is_finite():
        raise ValueError("money output requires a finite Decimal")
    with localcontext() as output:
        output.prec = max(output.prec, value.adjusted() + 4)
        return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
