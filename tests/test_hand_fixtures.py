import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("path", sorted(Path("fixtures/hand").glob("*.json")))
def test_hand_calculations(path: Path) -> None:
    case = json.loads(path.read_text())
    securities = sum(
        (Decimal(p["quantity"]) * Decimal(p["price"]) * Decimal(p["fx_to_usd"])
         for p in case["positions"]), Decimal(0),
    )
    cash = sum(
        (Decimal(c["amount"]) * Decimal(c["fx_to_usd"]) for c in case["cash"]), Decimal(0),
    )
    assert securities + cash == Decimal(case["expected_assets_usd"])
    if "threshold_pct" in case:
        percent = Decimal(case["issuer_exposure_usd"]) / (securities + cash) * 100
        assert percent == Decimal(case["expected_concentration_pct"])
        assert (percent >= Decimal(case["threshold_pct"])) == case["expected_breach"]


def test_public_acceptance_has_twenty_financial_and_ten_adversarial_questions() -> None:
    cases = yaml.safe_load(Path("acceptance/catalogue.yaml").read_text())
    assert cases["status"] == "specified"
    assert len(cases["financial"]) == 20
    assert len(cases["security_failure"]) == 10
    ids = [case["id"] for kind in ("financial", "security_failure") for case in cases[kind]]
    assert len(ids) == len(set(ids))
