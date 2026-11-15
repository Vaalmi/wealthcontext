"""Pydantic/JSON Schema parity and exact omission at the P3 serialization boundary."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Literal

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from wealthcontext.calculation import (
    CashInput,
    GroupBy,
    HouseholdInput,
    PositionInput,
    concentrated_positions,
    value_household,
)
from wealthcontext.exposure import (
    ConcentrationEnvelope,
    ExposureEnvelope,
    serialize_concentration,
    serialize_exposure,
)
from wealthcontext.exposure_contracts import (
    CompleteConcentration,
    CompleteIssuerExposure,
    CompleteSecurityExposure,
    IncompleteConcentration,
    IncompleteIssuerExposure,
    IncompleteSecurityExposure,
)

D = Decimal


def position(security: str, value: str, *, fund: bool = False) -> PositionInput:
    return PositionInput(
        id=f"A:{security}", account_id="A", security_id=security,
        security_name=f"Security {security}", issuer_id="ISSUER",
        issuer_name="Synthetic issuer", security_type="MMF" if fund else "COMMON_STOCK",
        quantity=D("1"), price_local=D(value), fx_usd=D("1"),
    )


def valuation(state: Literal["positive", "zero", "incomplete"] = "positive", *, name="H"):
    stock, fund, amount = ("100", "150", "750") if state == "positive" else ("0", "0", "0")
    positions = (position("STOCK", stock), position("SWEEP", fund, fund=True))
    if state == "incomplete":
        positions = (position("STOCK", "10"),
                     replace(position("SWEEP", "40", fund=True),
                             stale=True, price_date=date(2026, 9, 24)))
    return value_household(HouseholdInput(
        name, positions, (CashInput("A:USD", "A", "USD", D(amount), D("1")),),
        missing_components=("MISSING-MEMBERSHIP",) if state == "incomplete" else (),
    ))


def envelope(data: dict | None, status: str) -> dict:
    return {
        "trace_id": "trace-1", "result_id": "result-1", "status": status,
        "as_of": "2026-09-30", "valuation_date": "2026-09-30",
        "known_as_of": "2026-10-31T16:00:00Z", "calculated_at": "2026-10-31T16:00:02Z",
        "entitlement_revision": 1, "policy_revision": "sha256:" + "a" * 64,
        "data": data, "quality_flags": [], "source_refs": [], "metric_refs": [], "error": None,
    }


def checked(model: type[ExposureEnvelope] | type[ConcentrationEnvelope], payload: dict) -> dict:
    result = model.model_validate(payload)
    wire = result.model_dump(mode="json")
    schema = model.model_json_schema(mode="serialization")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(wire)
    return wire


def invalid_both(
    model: type[ExposureEnvelope] | type[ConcentrationEnvelope], payload: dict,
) -> None:
    assert not Draft202012Validator(model.model_json_schema(mode="serialization")).is_valid(payload)
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("group_by", ["issuer", "security"])
@pytest.mark.parametrize("state", ["positive", "zero", "incomplete"])
def test_exposure_status_matches_payload_in_python_and_json_schema(
    group_by: GroupBy, state: Literal["positive", "zero", "incomplete"],
):
    computed = valuation(state)
    payload = serialize_exposure(computed, "Synthetic household", group_by)
    status = "COMPLETE" if computed.complete else "INCOMPLETE"
    wire = checked(ExposureEnvelope, envelope(payload.model_dump(mode="json"), status))
    classes = {
        ("issuer", True): CompleteIssuerExposure,
        ("security", True): CompleteSecurityExposure,
        ("issuer", False): IncompleteIssuerExposure,
        ("security", False): IncompleteSecurityExposure,
    }
    assert isinstance(payload, classes[(group_by, computed.complete)])
    invalid_both(ExposureEnvelope, {
        **wire, "status": "INCOMPLETE" if status == "COMPLETE" else "COMPLETE",
    })


def test_issuer_and_fund_lists_have_flat_metadata_and_distinct_percentage_fields():
    payload = serialize_exposure(valuation(), "Synthetic household").model_dump(mode="json")
    issuers, funds = payload["issuer_exposures"], payload["fund_vehicle_exposures"]
    for listing in (issuers, funds):
        assert set(listing) == {
            "items", "item_count", "total_item_count", "ordering", "truncated",
            "next_cursor", "rank_scope",
        }
        assert listing["item_count"] == listing["total_item_count"] == 1
        assert listing["next_cursor"] is None and listing["truncated"] is False
    assert issuers["rank_scope"] == "household_issuers"
    assert funds["rank_scope"] == "household_fund_vehicles"
    issuer, fund = issuers["items"][0], funds["items"][0]
    assert issuer["rank"] == fund["rank"] == 1
    assert issuer["concentration_pct"] == "10.00" and "allocation_pct" not in issuer
    assert fund["allocation_pct"] == "15.00" and "concentration_pct" not in fund
    assert payload["assets_usd"] == "1000.00" and "accounts" not in payload
    wire = checked(ExposureEnvelope, envelope(payload, "COMPLETE"))
    for key, field in (("issuer_exposures", "allocation_pct"),
                       ("fund_vehicle_exposures", "concentration_pct")):
        invalid = deepcopy(wire)
        invalid["data"][key]["items"][0][field] = "10.00"
        invalid_both(ExposureEnvelope, invalid)


@pytest.mark.parametrize("group_by", ["issuer", "security"])
def test_zero_complete_exposure_has_amount_rows_without_any_percentage_keys(group_by: GroupBy):
    payload = serialize_exposure(valuation("zero"), "Synthetic zero", group_by)
    wire = checked(ExposureEnvelope, envelope(payload.model_dump(mode="json"), "COMPLETE"))
    data = wire["data"]
    assert data["assets_usd"] == "0.00"
    lists = [data[key] for key in (
        "issuer_exposures", "fund_vehicle_exposures", "security_exposures",
    ) if key in data]
    for listing in lists:
        for row in listing["items"]:
            assert "concentration_pct" not in row and "allocation_pct" not in row
    # A null percentage is also forbidden: absence must survive normal model_dump.
    invalid = deepcopy(wire)
    key = "issuer_exposures" if group_by == "issuer" else "security_exposures"
    field = "concentration_pct" if group_by == "issuer" else "allocation_pct"
    invalid["data"][key]["items"][0][field] = None
    invalid_both(ExposureEnvelope, invalid)


def test_tiny_positive_unrounded_assets_can_display_zero_without_losing_valid_percentage():
    computed = value_household(HouseholdInput("TINY", (position("TINY-STOCK", "0.004"),)))
    payload = serialize_exposure(computed, "Synthetic tiny").model_dump(mode="json")
    assert payload["assets_usd"] == "0.00"  # Display is not the denominator predicate.
    assert payload["issuer_exposures"]["items"][0]["concentration_pct"] == "100.00"
    checked(ExposureEnvelope, envelope(payload, "COMPLETE"))


@pytest.mark.parametrize("group_by", ["issuer", "security"])
def test_incomplete_exposure_has_diagnostics_and_no_total_or_partial_rows(group_by: GroupBy):
    payload = serialize_exposure(valuation("incomplete"), "Synthetic incomplete", group_by)
    wire = checked(ExposureEnvelope, envelope(payload.model_dump(mode="json"), "INCOMPLETE"))
    data = wire["data"]
    assert "assets_usd" not in data and "cash_usd" not in data
    assert data["fresh_subtotal_usd"] == "10.00"
    assert data["missing_components"] == ["MISSING-MEMBERSHIP"]
    assert data["stale_components"] == [{
        "subject": "A:SWEEP", "last_known_value_usd": "40.00", "price_date": "2026-09-24",
    }]
    invalid_both(ExposureEnvelope, {**wire, "data": {**data, "assets_usd": "50.00"}})
    key = "issuer_exposures" if group_by == "issuer" else "security_exposures"
    positive = serialize_exposure(valuation(), "Synthetic positive", group_by).model_dump(
        mode="json",
    )
    invalid_both(ExposureEnvelope, {**wire, "data": {**data, key: positive[key]}})


@pytest.mark.parametrize("state", ["positive", "zero", "incomplete"])
def test_concentration_status_matches_indeterminate_presence(
    state: Literal["positive", "zero", "incomplete"],
):
    computed = concentrated_positions((valuation(state),))
    payload = serialize_concentration(computed)
    wire = checked(ConcentrationEnvelope,
                   envelope(payload.model_dump(mode="json"), computed.status))
    assert isinstance(payload, CompleteConcentration if state == "positive"
                      else IncompleteConcentration)
    invalid_both(ConcentrationEnvelope, {
        **wire, "status": "INCOMPLETE" if computed.status == "COMPLETE" else "COMPLETE",
    })
    if state != "positive":
        item = wire["data"]["indeterminate"][0]
        assert "assets_usd" not in item and "concentration_pct" not in item
        assert item["reason"] == (
            "NON_POSITIVE_ASSETS" if state == "zero" else "INCOMPLETE_ASSETS"
        )
        invalid = deepcopy(wire)
        invalid["data"]["indeterminate"][0]["concentration_pct"] = "0.00"
        invalid_both(ConcentrationEnvelope, invalid)


def test_concentration_preserves_full_household_counts_and_all_indeterminate_after_cap():
    complete = [valuation(name=f"COMPLETE-{index:02}") for index in range(25)]
    missing = [valuation("incomplete", name=f"MISSING-{index:02}") for index in range(25)]
    clear = value_household(HouseholdInput(
        "CLEAR", (), (CashInput("A:USD", "A", "USD", D("1000"), D("1")),),
    ))
    computed = concentrated_positions([*reversed(complete), *missing, clear])
    payload = serialize_concentration(computed).model_dump(mode="json")
    checked(ConcentrationEnvelope, envelope(payload, "INCOMPLETE"))
    assert payload["evaluated_household_count"] == 51
    assert payload["determinate_household_count"] == 26
    assert payload["indeterminate_household_count"] == 25
    assert payload["breaching_household_count"] == 25
    assert len(payload["breaches"]) == 20
    assert payload["breach_metadata"]["total_item_count"] == 25
    assert payload["breach_metadata"]["truncated"] is True
    assert payload["breach_metadata"]["next_cursor"] is None
    assert payload["breach_metadata"]["rank_scope"] == "book_concentration_breaches"
    assert [row["book_rank"] for row in payload["breaches"]] == list(range(1, 21))
    assert all(row["issuer_rank"] == 1 and "rank" not in row for row in payload["breaches"])
    assert len(payload["indeterminate"]) == 25
    assert payload["indeterminate_metadata"]["item_count"] == 25
    assert payload["indeterminate_metadata"]["total_item_count"] == 25
    assert payload["indeterminate_metadata"]["truncated"] is False
    assert "accounts" not in payload and "household_name" not in payload


def test_serialization_does_not_turn_display_rounded_percentage_into_a_breach():
    value = value_household(HouseholdInput(
        "H", (position("STOCK", "999.96"),),
        (CashInput("A:USD", "A", "USD", D("9000.04"), D("1")),),
    ))
    exposure = serialize_exposure(value, "Synthetic household").model_dump(mode="json")
    assert exposure["issuer_exposures"]["items"][0]["concentration_pct"] == "10.00"
    data = serialize_concentration(concentrated_positions((value,))).model_dump(mode="json")
    assert data["applied_threshold_pct"] == "10.00"
    assert data["breaches"] == [] and data["breaching_household_count"] == 0
    checked(ConcentrationEnvelope, envelope(data, "COMPLETE"))


@pytest.mark.parametrize("model", [ExposureEnvelope, ConcentrationEnvelope])
def test_error_envelope_has_no_success_payload_or_required_revisions(model):
    payload = {
        **envelope(None, "ERROR"), "entitlement_revision": None, "policy_revision": None,
        "error": {"code": "NOT_AUTHORIZED", "message": "Access denied", "retryable": False},
    }
    wire = checked(model, payload)
    invalid_both(model, {**wire, "data": {"household_id": "H"}})


def test_schema_generation_supports_openapi_reference_prefixes():
    for model in (ExposureEnvelope, ConcentrationEnvelope):
        schema = model.model_json_schema(mode="serialization",
                                         ref_template="#/components/schemas/{model}")
        rules = schema["allOf"]
        assert len(rules) == 3
        for rule in rules[1:]:
            assert all(item["$ref"].startswith("#/components/schemas/")
                       for item in rule["then"]["properties"]["data"]["anyOf"])
