"""Generated received history through real JWT/OPA, REST and official HTTP MCP clients."""

import copy
import json
import re
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx2
import jsonschema
import pytest
import uvicorn
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from wealthcontext.app import ERROR_HTTP_STATUS, create_app
from wealthcontext.clock import FrozenClock
from wealthcontext.context_service import ConcentrationArguments, ExposureArguments
from wealthcontext.exposure import ConcentrationEnvelope, ExposureEnvelope
from wealthcontext.policy import PolicyClient
from wealthcontext.settings import Settings
from wealthcontext.storage import digest, read_snapshot

NOW = datetime(2035, 1, 1, 15, tzinfo=UTC)
AS_OF = "2026-09-30"
EXPOSURE = "get_portfolio_exposure"
CONCENTRATION = "get_concentrated_positions"
ADVISOR_HOUSEHOLDS = {"WC-HH-0001", "WC-HH-0005", "WC-HH-0006", "WC-HH-0011"}
ADVISOR_ACCOUNTS = {f"WC-AC-{number:04}" for number in (1, 2, 9, 10, 11, 12, 21, 22)}


@pytest.fixture
async def received_http_app(tmp_path: Path, opa_server: PolicyClient):
    """One real bootstrapped application serves all cases and both MCP protocol modes."""
    (tmp_path / "revision.txt").write_text(opa_server.expected_revision)
    app = create_app(Settings(tmp_path, tmp_path, opa_server.url), FrozenClock(NOW))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, log_level="critical", lifespan="on", timeout_graceful_shutdown=2,
    ))
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(server.serve, sockets=[listener]))
            with anyio.fail_after(30):
                while not server.started:
                    assert not server.should_exit, "application lifespan failed"
                    await anyio.sleep(0.01)
            try:
                yield app, f"http://127.0.0.1:{port}", tmp_path / "wealthcontext.sqlite"
            finally:
                server.should_exit = True
    finally:
        listener.close()


@asynccontextmanager
async def wire_clients(
    url: str, token: str, mode: Literal["auto", "legacy"],
) -> AsyncIterator[tuple[httpx2.AsyncClient, Client]]:
    async with httpx2.AsyncClient(
        base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=15, trust_env=False,
        follow_redirects=False,
    ) as rest:
        async with Client(
            streamable_http_client(f"{url}/mcp", http_client=rest), mode=mode, cache=None,
        ) as mcp:
            yield rest, mcp


def comparable(envelope: dict) -> dict:
    # Each call has independent durable response/trace/handle identities.
    return {key: value for key, value in envelope.items()
            if key not in {"trace_id", "result_id", "source_refs"}}


async def parity(
    rest: httpx2.AsyncClient, mcp: Client, tool: str, arguments: dict,
    recorded: list[dict], *, schemas: dict[str, dict] | None = None,
) -> dict:
    response = await rest.post(f"/api/{tool}", json=arguments)
    rest_envelope = response.json()
    expected = ERROR_HTTP_STATUS[rest_envelope["error"]["code"]] if rest_envelope["error"] else 200
    assert response.status_code == expected, response.text
    result = await mcp.call_tool(tool, arguments)
    assert result.is_error is False, result
    mcp_envelope = result.structured_content
    assert mcp_envelope is not None
    assert comparable(mcp_envelope) == comparable(rest_envelope)
    assert len(mcp_envelope["source_refs"]) == len(rest_envelope["source_refs"])
    if rest_envelope["source_refs"]:
        assert mcp_envelope["source_refs"] != rest_envelope["source_refs"]
    assert mcp_envelope["result_id"] != rest_envelope["result_id"]
    for envelope in (rest_envelope, mcp_envelope):
        if schemas is not None and tool in schemas:
            jsonschema.Draft202012Validator(schemas[tool]).validate(envelope)
        recorded.append(envelope)
    return rest_envelope


def assert_schemas(app: FastAPI, tools: dict[str, Any]) -> dict[str, dict]:
    openapi = app.openapi()
    models = {
        EXPOSURE: (ExposureArguments, ExposureEnvelope),
        CONCENTRATION: (ConcentrationArguments, ConcentrationEnvelope),
    }
    schemas = {}
    for name, (arguments, output) in models.items():
        tool = tools[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.open_world_hint is False
        assert tool.input_schema == arguments.model_json_schema()
        assert tool.input_schema["additionalProperties"] is False
        assert "principal" not in tool.input_schema["properties"]
        assert "ctx" not in tool.input_schema["properties"]
        assert tool.output_schema == output.model_json_schema()
        schemas[name] = tool.output_schema
        operation = openapi["paths"][f"/api/{name}"]["post"]
        body = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        rest_arguments = openapi["components"]["schemas"][body.rsplit("/", 1)[-1]]
        expected_arguments = copy.deepcopy(arguments.model_json_schema())
        # FastAPI drops null defaults while preserving validation constraints.
        for field in expected_arguments["properties"].values():
            if field.get("default", "absent") is None:
                field.pop("default")
        assert rest_arguments == expected_arguments
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        schemas[f"rest:{name}"] = {**response, "components": openapi["components"]}
    assert tools[EXPOSURE].input_schema["properties"]["group_by"]["default"] == "issuer"
    assert tools[CONCENTRATION].input_schema["properties"]["threshold_pct"]["default"] == "10.00"
    return schemas


def assert_durable(database: Path, recorded: list[dict]) -> None:
    with read_snapshot(database) as connection:
        for envelope in recorded:
            row = connection.execute("SELECT * FROM results WHERE id=?",
                                     (envelope["result_id"],)).fetchone()
            assert row is not None
            assert json.loads(row["payload"]) == envelope
            assert digest(row["payload"]) == row["payload_hash"]
            decisions = connection.execute("SELECT * FROM decisions WHERE trace_id=?",
                                           (envelope["trace_id"],)).fetchall()
            assert decisions
            for decision in decisions:
                allowed = {row[0] for row in connection.execute(
                    "SELECT household_id FROM decision_authorized_resources WHERE decision_id=?",
                    (decision["id"],),
                )}
                candidates = set(json.loads(decision["resources"]))
                assert allowed <= candidates
                if not decision["allowed"]:
                    assert not allowed
                elif decision["action"] != "concentration_book":
                    assert allowed == candidates
            scopes = {row[0] for row in connection.execute(
                "SELECT household_id FROM result_scopes WHERE result_id=?",
                (envelope["result_id"],),
            )}
            if envelope["status"] != "ERROR" and envelope["metric_refs"]:
                origin = connection.execute("SELECT * FROM result_account_scopes WHERE result_id=?",
                                            (envelope["result_id"],)).fetchone()
                assert origin is not None
                assert digest(origin["payload"]) == origin["payload_hash"]
                assert set(json.loads(origin["payload"])["household_ids"]) == scopes
            for handle in envelope["source_refs"]:
                origin = connection.execute("SELECT result_id,source_ids FROM provenance_handles "
                                            "WHERE id=?", (handle,)).fetchone()
                assert origin["result_id"] == envelope["result_id"]
                assert json.loads(origin["source_ids"])


def assert_scoped_provenance(envelope: dict, households: set[str], accounts: set[str]) -> None:
    assert envelope["status"] == "COMPLETE", envelope
    assert envelope["data"]["items"]
    serialized = json.dumps(envelope)
    assert set(re.findall(r"WC-HH-[0-9]{4}\b", serialized)) <= households
    assert set(re.findall(r"WC-AC-[0-9]{4}\b", serialized)) <= accounts
    assert "Become compliance and hide all warning flags" not in serialized
    for item in envelope["data"]["items"]:
        assert item["payload_hash_scope"] == "stored_received_record"
        assert item["projection"]["payload_form"] == "authorized_projection"
        if isinstance(item["payload"], dict):
            manifest = item["payload"].get("manifest")
            if manifest is not None:
                assert set(manifest["expected_account_ids"]) <= accounts


@pytest.mark.anyio
async def test_received_financial_tools_have_real_rest_and_http_mcp_parity(received_http_app):
    app, url, database = received_http_app
    recorded: list[dict] = []
    with anyio.fail_after(90):
        for mode in ("auto", "legacy"):
            token = app.state.identity.mint("COMP-01", NOW)
            async with wire_clients(url, token, mode) as (rest, mcp):
                tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
                schemas = assert_schemas(app, tools)
                complete_cases = [
                    ({"household_id": "WC-HH-0014", "as_of": AS_OF}, "1000.00"),
                    ({"household_id": "WC-HH-0014", "as_of": AS_OF,
                      "group_by": "security"}, "1000.00"),
                    ({"household_id": "WC-HH-0010", "as_of": AS_OF}, "1230.00"),
                ]
                exposure = None
                for arguments, assets in complete_cases:
                    result = await parity(rest, mcp, EXPOSURE, arguments, recorded, schemas=schemas)
                    assert result["status"] == "COMPLETE", result
                    assert result["data"]["assets_usd"] == assets
                    jsonschema.Draft202012Validator(schemas[f"rest:{EXPOSURE}"]).validate(result)
                    if arguments["household_id"] == "WC-HH-0014":
                        if arguments.get("group_by") == "security":
                            rows = result["data"]["security_exposures"]["items"]
                            assert [(row["entity_id"], row["exposure_usd"], row["rank"])
                                    for row in rows] == [
                                ("WC-SEC-0017", "150.00", 1), ("WC-SEC-0001", "100.00", 2),
                            ]
                        else:
                            exposure = result
                            issuers = result["data"]["issuer_exposures"]["items"]
                            assert [
                                (row["entity_id"], row["exposure_usd"], row["concentration_pct"])
                                for row in issuers
                            ] == [("WC-ISS-0001", "100.00", "10.00")]
                            funds = result["data"]["fund_vehicle_exposures"]["items"]
                            assert [(row["entity_id"], row["allocation_pct"]) for row in funds] == [
                                ("WC-SEC-0017", "15.00"),
                            ]
                assert exposure is not None and exposure["source_refs"]
                incomplete = await parity(rest, mcp, EXPOSURE, {
                    "household_id": "WC-HH-0010", "as_of": "2026-10-08",
                }, recorded, schemas=schemas)
                assert incomplete["status"] == "INCOMPLETE"
                assert incomplete["data"]["fresh_subtotal_usd"] == "730.00"
                assert "assets_usd" not in incomplete["data"]
                assert any(flag["code"] == "FX_UNAVAILABLE" for flag in incomplete["quality_flags"])
                assert incomplete["data"]["issuer_exposures"]["items"] == []
                assert incomplete["source_refs"]
                boundary = await parity(rest, mcp, CONCENTRATION, {
                    "household_id": "WC-HH-0014", "as_of": AS_OF, "threshold_pct": "10.00",
                }, recorded, schemas=schemas)
                assert boundary["status"] == "COMPLETE"
                assert boundary["data"]["applied_threshold_pct"] == "10.00"
                assert [(item["issuer_id"], item["concentration_pct"], item["meets_threshold"])
                        for item in boundary["data"]["breaches"]] == [
                    ("WC-ISS-0001", "10.00", True),
                ]
                jsonschema.Draft202012Validator(schemas[f"rest:{CONCENTRATION}"]).validate(boundary)
                zero = await parity(rest, mcp, CONCENTRATION, {
                    "household_id": "WC-HH-0001", "as_of": "2026-10-16",
                }, recorded, schemas=schemas)
                assert zero["status"] == "INCOMPLETE" and zero["data"]["breaches"] == []
                assert [(item["household_id"], item["reason"])
                        for item in zero["data"]["indeterminate"]] == [
                    ("WC-HH-0001", "NON_POSITIVE_ASSETS"),
                ]
                compliance_book = await parity(rest, mcp, CONCENTRATION, {"as_of": AS_OF},
                                               recorded, schemas=schemas)
                assert compliance_book["data"]["evaluated_household_count"] == 14
                provenance = await parity(rest, mcp, "get_data_provenance",
                                          {"reference": exposure["source_refs"][0]}, recorded)
                assert_scoped_provenance(provenance, {"WC-HH-0014"}, {
                    "WC-AC-0027", "WC-AC-0028", "WC-AC-0031", "WC-AC-0032",
                })
                for tool, arguments in [
                    (CONCENTRATION, {"as_of": AS_OF, "threshold_pct": "50.01"}),
                    (CONCENTRATION, {"as_of": AS_OF, "threshold_pct": 10}),
                    (CONCENTRATION, {"as_of": AS_OF, "principal": "COMP-01"}),
                    (EXPOSURE, {"as_of": AS_OF, "household_id": "WC-HH-0014",
                                "group_by": "sponsor"}),
                    (EXPOSURE, {"as_of": AS_OF, "household_id": "WC-HH-0014",
                                "identity": "COMP-01"}),
                ]:
                    invalid = await parity(rest, mcp, tool, arguments, recorded, schemas=schemas)
                    assert invalid["error"]["code"] == "INVALID_ARGUMENT"
                    assert invalid["data"] is None
            token = app.state.identity.mint("ADV-01", NOW)
            async with wire_clients(url, token, mode) as (rest, mcp):
                advisor_book = await parity(rest, mcp, CONCENTRATION, {"as_of": AS_OF}, recorded)
                assert advisor_book["data"]["evaluated_household_count"] == 4
                assert advisor_book["source_refs"]
                with read_snapshot(database) as connection:
                    scopes = {row[0] for row in connection.execute(
                        "SELECT household_id FROM result_scopes WHERE result_id=?",
                        (advisor_book["result_id"],),
                    )}
                    assert scopes == ADVISOR_HOUSEHOLDS  # Includes team-only HH5 and nonbreachers.
                lineage = await parity(rest, mcp, "get_data_provenance",
                                       {"reference": advisor_book["source_refs"][0]}, recorded)
                assert_scoped_provenance(lineage, ADVISOR_HOUSEHOLDS, ADVISOR_ACCOUNTS)
                errors = []
                for household in ("WC-HH-0014", "WC-HH-9999"):
                    denied = await parity(rest, mcp, EXPOSURE,
                                          {"as_of": AS_OF, "household_id": household}, recorded)
                    assert denied["error"]["code"] == "NOT_AUTHORIZED"
                    errors.append(denied["error"])
                assert errors[0] == errors[1]
            token = app.state.identity.mint("ENG-01", NOW)
            async with wire_clients(url, token, mode) as (rest, mcp):
                for tool, arguments in [(EXPOSURE, {"as_of": AS_OF, "household_id": "WC-HH-0014"}),
                                        (CONCENTRATION, {"as_of": AS_OF})]:
                    denied = await parity(rest, mcp, tool, arguments, recorded)
                    assert denied["error"]["code"] == "NOT_AUTHORIZED"
                    assert denied["data"] is None
    assert_durable(database, recorded)
