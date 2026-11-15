"""Schema repair hints must not redisclose provider inputs or arbitrary error paths."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from wealthcontext.context_service import ContextService
from wealthcontext.contracts import Answer
from wealthcontext.database import migrate
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient
from wealthcontext.storage import digest, migrate_business, read_snapshot
from wealthcontext.telemetry import create_provider
from wealthcontext.verifier import answer_schema_diagnostics

NOW = datetime(2035, 1, 1, tzinfo=UTC)
SECRET = "Customer_Private_997766"


def valid_shape():
    return {"answer_markdown": "Verified data is unavailable.", "claims": [], "predicates": [],
            "caveats": [], "refusal": None}


def diagnostics(arguments):
    with pytest.raises(ValidationError) as caught:
        Answer.model_validate(arguments)
    return answer_schema_diagnostics(caught.value)


def test_embedded_tool_arguments_produce_missing_sibling_field_hints_without_echoing_markdown():
    hints = diagnostics({"answer_markdown": f"</answer_markdown><claims>{SECRET}</claims>"})
    assert {item["path"] for item in hints} == {
        "answer.claims", "answer.predicates", "answer.caveats", "answer.refusal",
    }
    assert {item["code"] for item in hints} == {"SCHEMA_REQUIRED_FIELD"}
    assert SECRET not in json.dumps(hints)
    assert all(set(item) == {"code", "path", "message"} for item in hints)


def test_schema_diagnostics_exclude_input_values_unrecognized_keys_and_union_tags():
    candidate = valid_shape()
    candidate[SECRET] = "another private value"
    candidate["claims"] = [{
        "id": "c1", "result_id": "r_one", "entity": {"type": "household", "id": "WC-HH-0001",
                                                        SECRET: "secret owner"},
        "field": "assets_usd", "household_id": "WC-HH-0001", "valuation_date": SECRET,
        "unit": SECRET, "format": "value_only",
    }]
    candidate["predicates"] = [{"kind": SECRET}]
    hints = diagnostics(candidate)
    encoded = json.dumps(hints)
    assert SECRET not in encoded and "secret owner" not in encoded
    assert "another private" not in encoded
    assert "input" not in encoded and "context" not in encoded and "https://" not in encoded
    assert any(item["path"] == "answer.claims[0].unit" for item in hints)
    assert any(item["path"] == "answer.claims[0].valuation_date" for item in hints)
    assert any(item["path"] == "answer.predicates[0]" for item in hints)
    assert any("[unrecognized_field]" in item["path"] for item in hints)


def test_schema_diagnostics_are_bounded_even_with_many_arbitrary_extra_keys():
    candidate = {**valid_shape(), **{f"{SECRET}_{index}": index for index in range(100)}}
    hints = diagnostics(candidate)
    assert len(hints) == 20
    assert hints[-1]["code"] == "SCHEMA_DIAGNOSTICS_TRUNCATED"
    assert SECRET not in json.dumps(hints)
    assert all(len(item["path"]) <= 200 and len(item["message"]) <= 120 for item in hints)


@pytest.mark.parametrize("candidate", [None, [], "secret input", {**valid_shape(), "claims": "[]"}])
def test_wrong_json_shapes_receive_static_type_guidance(candidate):
    hints = diagnostics(candidate)
    assert hints[0]["code"] == "SCHEMA_TYPE_MISMATCH"
    assert "secret input" not in json.dumps(hints)


@pytest.fixture
def schema_service(tmp_path, monkeypatch):
    database = tmp_path / "diagnostics.sqlite"
    migrate(database)
    migrate_business(database)

    async def unexpected(*_args, **_kwargs):
        pytest.fail("schema rejection must not retrieve business data or invoke OPA")

    monkeypatch.setattr(PolicyClient, "decide", unexpected)
    policy = PolicyClient("http://unused.invalid", "sha256:" + "a" * 64)
    telemetry = create_provider(tmp_path / "traces.jsonl")
    yield ContextService(database, policy, telemetry.get_tracer("schema_diagnostics"))
    telemetry.shutdown()


@pytest.mark.anyio
async def test_submit_schema_failure_returns_exact_audited_sanitized_diagnostics(schema_service):
    principal = Principal("ADV-01", "advisor")
    conversation = schema_service.start_conversation(principal, NOW)
    result = await schema_service.execute(principal, NOW, "submit_answer", {
        "answer_markdown": f"<claims>{SECRET}</claims>", SECRET: "extra secret field",
    }, conversation)
    assert result["status"] == "ERROR" and result["error"]["code"] == "INVALID_ARGUMENT"
    hints = result["error"]["details"]["diagnostics"]
    assert any(item["path"] == "answer.claims" for item in hints)
    assert SECRET not in json.dumps(result)
    with read_snapshot(schema_service.database) as connection:
        row = connection.execute("SELECT payload,payload_hash FROM results WHERE id=?",
                                 (result["result_id"],)).fetchone()
        assert json.loads(row[0]) == result and digest(row[0]) == row[1]
        assert connection.execute("SELECT reason FROM decisions WHERE trace_id=?",
                                  (result["trace_id"],)).fetchone()[0] == "INVALID_ARGUMENT"


@pytest.mark.anyio
async def test_other_operation_schema_rejections_keep_existing_public_shape(schema_service):
    result = await schema_service.execute(Principal("ADV-01", "advisor"), NOW,
                                          "get_household_snapshot", {SECRET: "private"})
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert result["error"]["details"] == {}
    assert SECRET not in json.dumps(result)
