"""Caller cutoff validation through models, governed service and real REST/MCP."""

import json
from datetime import datetime

import pytest
from pydantic import ValidationError
from test_p3_legacy_timestamps import NOW
from test_p3_oracle_agreement import import_export
from test_p3_timestamp_bounds import INVALID_TIMESTAMPS, mixed_currency_export
from test_p3_transports import NOW as HTTP_NOW
from test_p3_transports import received_http_app as received_http_app
from test_p3_transports import wire_clients
from test_reference_oracle import DAY, HH, fix

from wealthcontext.context_service import (
    ConcentrationArguments,
    ContextService,
    ExposureArguments,
    SnapshotArguments,
)
from wealthcontext.identity import Principal
from wealthcontext.storage import digest, read_snapshot
from wealthcontext.telemetry import create_provider

MODELS = [SnapshotArguments, ExposureArguments, ConcentrationArguments]
TOOLS = ["get_household_snapshot", "get_portfolio_exposure", "get_concentrated_positions"]
VALID = [
    ("0001-01-01T00:01:00+00:01", "NOT_FOUND"),
    ("9999-12-31T23:58:59.999999-00:01", None),
    ("2026-10-02T15:00:00.123456+01:00", None),
]


def arguments(timestamp):
    return {"household_id": HH, "as_of": DAY.isoformat(), "known_as_of": timestamp}


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
def test_historical_arguments_reject_unrepresentable_knowledge_cutoff(model, timestamp):
    with pytest.raises(ValidationError, match="UTC instant is outside years 1 through 9999"):
        model.model_validate(arguments(timestamp))


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("timestamp,_", VALID)
def test_historical_arguments_keep_representable_offset_and_precision(model, timestamp, _):
    parsed = model.model_validate(arguments(timestamp))
    assert parsed.known_as_of.isoformat() == datetime.fromisoformat(timestamp).isoformat()


def assert_response(database, envelope, code):
    assert envelope["status"] == ("ERROR" if code else "COMPLETE"), envelope
    assert (envelope["error"]["code"] if envelope["error"] else None) == code
    if code:
        assert envelope["data"] is None and not envelope["source_refs"]
    if code == "INVALID_ARGUMENT":
        assert envelope["known_as_of"] is None
        assert envelope["error"]["retryable"] is False
    with read_snapshot(database) as connection:
        row = connection.execute(
            "SELECT payload,payload_hash,trace_id FROM results WHERE id=?",
            (envelope["result_id"],),
        ).fetchone()
        assert row and json.loads(row["payload"]) == envelope
        assert row["payload_hash"] == digest(row["payload"])
        assert row["trace_id"] == envelope["trace_id"]
        decisions = connection.execute(
            "SELECT allowed,reason FROM decisions WHERE trace_id=?", (envelope["trace_id"],),
        ).fetchall()
        assert decisions
        if code == "INVALID_ARGUMENT":
            assert [(item["allowed"], item["reason"]) for item in decisions] == [
                (0, "INVALID_ARGUMENT"),
            ]
            assert not connection.execute(
                "SELECT 1 FROM result_scopes WHERE result_id=?", (envelope["result_id"],),
            ).fetchall()
        else:
            assert all(item["allowed"] for item in decisions)


@pytest.mark.anyio
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
async def test_service_audits_invalid_caller_cutoffs_and_accepts_valid_endpoints(
    tmp_path, opa_server, timestamp,
):
    export = mixed_currency_export(tmp_path)
    export.feed("FX", "fx", [fix("GBP", "1.25")])
    database = tmp_path / "received.sqlite"
    import_export(tmp_path, database, NOW)
    provider = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, opa_server, provider.get_tracer("cutoff-boundary"))
    try:
        for value, code in [(timestamp, "INVALID_ARGUMENT"), *VALID]:
            for tool in TOOLS:
                result = await service.execute(Principal("COMP-01", "compliance"), NOW,
                                               tool, arguments(value))
                assert_response(database, result, code)
                if code is None and tool != "get_concentrated_positions":
                    assert result["data"]["assets_usd"] == "48.25"
                if code != "INVALID_ARGUMENT":
                    assert datetime.fromisoformat(result["known_as_of"]) \
                        == datetime.fromisoformat(value)
    finally:
        provider.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
async def test_real_rest_and_both_mcp_modes_return_durable_cutoff_errors(
    received_http_app, timestamp,
):
    app, url, database = received_http_app
    token = app.state.identity.mint("COMP-01", HTTP_NOW)
    for mode in ("auto", "legacy"):
        async with wire_clients(url, token, mode) as (rest, mcp):
            for value, code in [(timestamp, "INVALID_ARGUMENT"), *VALID]:
                for tool in TOOLS:
                    response = await rest.post("/api/" + tool, json=arguments(value))
                    assert response.status_code == (
                        422 if code == "INVALID_ARGUMENT" else 404 if code else 200
                    ), response.text
                    assert response.headers["content-type"].startswith("application/json")
                    assert_response(database, response.json(), code)
                    result = await mcp.call_tool(tool, arguments(value))
                    assert result.is_error is False
                    assert result.structured_content is not None
                    assert_response(database, result.structured_content, code)
                    if code is None and tool != "get_concentrated_positions":
                        assert response.json()["data"]["assets_usd"] == "3000.00"
                        assert result.structured_content["data"]["assets_usd"] == "3000.00"
