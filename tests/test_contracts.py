import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import JsonValue, TypeAdapter, ValidationError

from wealthcontext.cli import export_schemas
from wealthcontext.clock import FrozenClock, SystemClock
from wealthcontext.contracts import Answer, Envelope, Predicate
from wealthcontext.mcp_server import ARGUMENT_MODELS
from wealthcontext.snapshot import SnapshotEnvelope


def envelope() -> dict:
    return {
        "contract_version": "1.0", "quality_flags": [], "source_refs": [], "metric_refs": [],
        "error": None,
        "trace_id": "trace-1", "result_id": "result-1", "status": "COMPLETE",
        "as_of": "2026-09-30", "valuation_date": "2026-09-30",
        "known_as_of": "2026-10-31T16:00:00Z", "calculated_at": "2026-10-31T16:00:02Z",
        "entitlement_revision": 1, "policy_revision": "sha256:" + "a" * 64,
        "data": {"assets_usd": "3000.00"},
    }


def test_envelope_keeps_decimal_strings_and_requires_aware_clocks() -> None:
    result = Envelope[dict[str, JsonValue]].model_validate(envelope())
    assert result.data == {"assets_usd": "3000.00"}
    assert json.loads(result.model_dump_json())["data"]["assets_usd"] == "3000.00"
    with pytest.raises(ValidationError):
        Envelope[dict[str, JsonValue]].model_validate(
            {**envelope(), "known_as_of": "2026-10-31T16:00:00"}
        )


@pytest.mark.parametrize("change", [
    {"status": "ERROR"},
    {"status": "COMPLETE", "data": None},
    {"status": "INCOMPLETE", "error": {
        "code": "INTERNAL", "message": "bad", "retryable": False,
    }},
    {"status": "invented"},
    {"unknown_field": "must reject"},
    {"policy_revision": None}, {"entitlement_revision": None},
    {"policy_revision": "sha256:unverified"},
])
def test_envelope_rejects_inconsistent_or_unknown_contract(change: dict) -> None:
    with pytest.raises(ValidationError):
        Envelope[dict[str, JsonValue]].model_validate({**envelope(), **change})


def test_component_failure_is_incomplete_without_a_request_error() -> None:
    result = Envelope[dict[str, JsonValue]].model_validate({
        **envelope(), "status": "INCOMPLETE", "data": {"fresh_subtotal_usd": "0.00"},
        "quality_flags": [{"code": "PRICE_STALE", "severity": "warning", "subject": "WC-POS-1"}],
    })
    assert result.error is None
    assert result.data is not None and "total" not in result.data


def test_error_envelope_has_no_data() -> None:
    result = Envelope[dict[str, JsonValue]].model_validate({
        **envelope(), "status": "ERROR", "data": None,
        "known_as_of": None, "entitlement_revision": None, "policy_revision": None,
        "error": {"code": "NOT_AUTHORIZED", "message": "Access denied", "retryable": False},
    })
    assert result.error and result.error.code == "NOT_AUTHORIZED"


def claim() -> dict:
    return {
        "id": "c1", "result_id": "result-1", "entity": {"type": "household", "id": "HH-01"},
        "field": "assets_usd", "household_id": "HH-01", "valuation_date": "2026-09-30",
        "unit": "USD", "format": "value_only",
    }


def test_answer_rejects_ambiguous_ids_and_claims_with_refusal() -> None:
    payload = {"answer_markdown": "{{c1}}", "claims": [claim()],
               "predicates": [], "caveats": [], "refusal": None}
    assert Answer.model_validate(payload).claims[0].unit == "USD"
    with pytest.raises(ValidationError, match="unique"):
        Answer.model_validate({**payload, "claims": [claim(), claim()]})
    with pytest.raises(ValidationError, match="refusal"):
        Answer.model_validate({**payload, "refusal": {
            "code": "NOT_AUTHORIZED", "result_id": "result-2",
        }})


@pytest.mark.parametrize("kind,field,expect", [
    ("rank", "rank", 1), ("threshold", "applied_threshold_pct", "AT_OR_ABOVE"),
    ("completeness", "status", "INCOMPLETE"),
    ("reconciliation", "reconciliation_status", "RECONCILIATION_UNAVAILABLE"),
])
def test_typed_predicates_accept_only_defined_expectations(kind, field, expect) -> None:
    payload = {"id": "p1", "result_id": "result-1", "entity": {"type": "issuer", "id": "ISS-1"},
               "kind": kind, "field": field, "expect": expect}
    adapter = TypeAdapter(Predicate)
    assert adapter.validate_python(payload).expect == expect
    validator = Draft202012Validator(adapter.json_schema(mode="serialization"))
    validator.validate(payload)
    for bad in [True, "invented", -1]:
        with pytest.raises(ValidationError):
            adapter.validate_python({**payload, "expect": bad})
        assert not validator.is_valid({**payload, "expect": bad})
    assert not validator.is_valid({**payload, "field": "unknown"})


def test_schemas_are_valid_and_match_committed_contracts(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    names = {path.name for path in tmp_path.glob("*.json")}
    assert names == {path.name for path in Path("schemas/v1").glob("*.json")}
    for name in sorted(names):
        schema = json.loads((tmp_path / name).read_text())
        Draft202012Validator.check_schema(schema)
        assert schema == json.loads((Path("schemas/v1") / name).read_text())
    schema = json.loads((tmp_path / "envelope.json").read_text())
    validator = Draft202012Validator(schema)
    validator.validate(envelope())
    assert set(schema["required"]) == set(envelope())
    for field in envelope():
        assert not validator.is_valid({k: v for k, v in envelope().items() if k != field})
    for change in [{"policy_revision": None}, {"entitlement_revision": None},
                   {"policy_revision": "sha256:invalid"}, {"data": None}, {"status": "ERROR"}]:
        assert not validator.is_valid({**envelope(), **change})
    error = Envelope[dict[str, JsonValue]].model_validate({
        **envelope(), "status": "ERROR", "data": None, "known_as_of": None,
        "entitlement_revision": None, "policy_revision": None,
        "error": {"code": "INVALID_ARGUMENT", "message": "Invalid date", "retryable": False},
    })
    validator.validate(error.model_dump(mode="json"))


def test_empty_answer_is_invalid_in_python_and_published_schema() -> None:
    payload = {"answer_markdown": "", "claims": [], "predicates": [],
               "caveats": [], "refusal": None}
    with pytest.raises(ValidationError):
        Answer.model_validate(payload)
    schema = json.loads(Path("schemas/v1/answer.json").read_text())
    assert not Draft202012Validator(schema).is_valid(payload)


@pytest.mark.parametrize("schema_name,tool,arguments", [
    ("exposure-arguments", "get_portfolio_exposure",
     {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}),
    ("concentration-arguments", "get_concentrated_positions", {"as_of": "2026-09-30"}),
])
def test_published_input_contracts_accept_defaults_and_match_mcp(schema_name, tool, arguments):
    schema = json.loads(Path(f"schemas/v1/{schema_name}.json").read_text())
    Draft202012Validator(schema).validate(arguments)
    model = ARGUMENT_MODELS[tool]
    model.model_validate(arguments)
    assert {key: value for key, value in schema.items() if key not in {"$id", "$schema"}} == (
        model.model_json_schema(mode="validation")
    )


def test_clock_is_explicit_and_utc() -> None:
    assert SystemClock().now().tzinfo == UTC
    instant = datetime(2026, 9, 30, 15, tzinfo=UTC)
    assert FrozenClock(instant).now() == instant
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 9, 30))


@pytest.mark.parametrize("status,payload", [
    ("COMPLETE", {"assets_usd": "3000.00", "cash_usd": "500.00"}),
    ("INCOMPLETE", {"fresh_subtotal_usd": "500.00", "missing_components": [],
                    "stale_components": [{"subject": "WC-POS-1",
                                          "last_known_value_usd": "2500.00",
                                          "price_date": "2026-09-29"}]}),
])
def test_snapshot_status_and_payload_must_match_in_python_and_schema(status, payload) -> None:
    data = {"household_id": "WC-HH-1", "household_name": "Synthetic household",
            "accounts": [], "positions": [], "item_count": 0, **payload}
    result = SnapshotEnvelope.model_validate({**envelope(), "status": status, "data": data})
    wire = result.model_dump(mode="json")
    validator = Draft202012Validator(json.loads(Path("schemas/v1/snapshot.json").read_text()))
    validator.validate(wire)
    mismatch = {**wire, "status": "INCOMPLETE" if status == "COMPLETE" else "COMPLETE"}
    assert not validator.is_valid(mismatch)
    with pytest.raises(ValidationError, match="requires a"):
        SnapshotEnvelope.model_validate(mismatch)
    if status == "INCOMPLETE":
        invalid = {**wire, "data": {**wire["data"], "stale_components": ["WC-POS-1"]}}
        assert not validator.is_valid(invalid)
        with pytest.raises(ValidationError):
            SnapshotEnvelope.model_validate(invalid)
