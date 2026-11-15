import pytest

from wealthcontext.contracts import Answer
from wealthcontext.verifier import TEMPLATE, InvalidAnswer, verify_one_claim


def candidate(**change) -> Answer:
    return Answer.model_validate({
        "answer_markdown": TEMPLATE, "claims": [{
            "id": "c1", "result_id": "r_one", "entity": {"type": "household", "id": "WC-HH-0001"},
            "field": "assets_usd", "household_id": "WC-HH-0001", "valuation_date": "2026-09-30",
            "unit": "USD", "format": "value_only", **change,
        }], "predicates": [], "caveats": [], "refusal": None,
    })


RESULT = {"result_id": "r_one", "trace_id": "trace_one", "status": "COMPLETE",
          "quality_flags": [], "source_refs": ["h_one"], "valuation_date": "2026-09-30",
          "data": {"household_id": "WC-HH-0001", "assets_usd": "3000.00"}}


@pytest.mark.parametrize("change", [
    {"entity": {"type": "account", "id": "WC-HH-0001"}}, {"field": "cash_usd"},
    {"household_id": "WC-HH-0002"}, {"valuation_date": "2026-10-01"}, {"unit": "pct"},
    {"result_id": "r_other"},
])
def test_wrong_claim_scope_is_rejected(change: dict) -> None:
    with pytest.raises(InvalidAnswer):
        verify_one_claim(candidate(**change), RESULT)


def test_renderer_uses_exact_recorded_value_and_rejects_unverified_prose() -> None:
    assert verify_one_claim(candidate(), RESULT)["markdown"].endswith("USD 3000.00.")
    wrong = candidate()
    wrong.answer_markdown = "Assets total 9999 dollars."
    with pytest.raises(InvalidAnswer):
        verify_one_claim(wrong, RESULT)
