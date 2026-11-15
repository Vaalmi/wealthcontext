"""Test MCP wire contracts with synthetic, server-injected request state."""

import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal

import anyio
import httpx2
import jsonschema
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import TextContent, TextResourceContents
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from wealthcontext.app import create_app
from wealthcontext.context_service import (
    ConcentrationArguments,
    ExposureArguments,
    MetricArguments,
    ProvenanceArguments,
    SnapshotArguments,
)
from wealthcontext.contracts import Envelope
from wealthcontext.identity import Principal
from wealthcontext.mcp_server import build_mcp
from wealthcontext.snapshot import SnapshotEnvelope
from wealthcontext.surface_contracts import QualityArguments, SearchArguments

GUIDELINES = (Path(__file__).parents[1] / "prompts/host_guidelines.md").read_text()
NOW = datetime(2026, 11, 1, 16, tzinfo=UTC)
PRINCIPALS = {
    "synthetic-advisor": Principal("ADV-01", "advisor"),
    "synthetic-engineer": Principal("ENG-01", "engineer"),
}


class StubService:
    def __init__(self) -> None:
        self.calls: list[tuple[Principal, datetime, str, dict | None]] = []
        self.responses: list[dict] = []

    async def execute(
        self, principal: Principal, now: datetime, tool: str, arguments: dict | None,
    ) -> dict:
        self.calls.append((principal, now, tool, arguments))
        await anyio.sleep(0)
        invalid = arguments is None
        arguments = arguments or {}
        data: dict | None = {"id": arguments.get("metric_id"), "viewer": principal.id}
        error = None
        flags: list[dict] = []
        incomplete = False
        if invalid:
            data = None
            error = {"code": "INVALID_ARGUMENT", "message": "Invalid operation arguments",
                     "retryable": False, "details": {}}
        elif tool == "get_household_snapshot":
            data = {
                "household_id": arguments["household_id"], "household_name": "Synthetic household",
                "accounts": [], "positions": [], "item_count": 0, "ordering": "account_id",
                "truncated": False, "next_cursor": None,
                "assets_usd": "3000.00", "cash_usd": "500.00",
            }
            if arguments["household_id"] == "FORBIDDEN":
                data = None
                error = {
                    "code": "NOT_AUTHORIZED", "retryable": False, "details": {},
                    "message": "Caller is not authorized for the requested resource",
                }
            elif arguments["household_id"] == "INCOMPLETE":
                data.pop("assets_usd")
                data.pop("cash_usd")
                data.update(fresh_subtotal_usd="500.00", missing_components=["WC-POS-1"],
                            stale_components=[])
                flags = [{"code": "SOURCE_UNAVAILABLE", "severity": "warning",
                          "subject": "WC-POS-1", "detail": {}}]
                incomplete = True
        elif tool == "get_data_provenance":
            data = {"reference": arguments["reference"], "items": [], "item_count": 0}
        result = {
            "contract_version": "1.0", "trace_id": "trace-test", "result_id": "r_test",
            "status": "ERROR" if error else "INCOMPLETE" if incomplete else "COMPLETE",
            "as_of": arguments.get("as_of"), "valuation_date": arguments.get("as_of"),
            "known_as_of": arguments.get("known_as_of"), "calculated_at": now.isoformat(),
            "entitlement_revision": 1, "policy_revision": "sha256:" + "a" * 64,
            "data": data, "quality_flags": flags, "source_refs": [], "metric_refs": [],
            "error": error,
        }
        # Service's real boundary is already fully serialized. Normalize UTC
        # exactly as that boundary does, so the adapter cannot hide key changes.
        model = SnapshotEnvelope if tool == "get_household_snapshot" else Envelope[dict]
        result = model.model_validate(result).model_dump(mode="json")
        self.responses.append(result)
        return result


@asynccontextmanager
async def http_server(
    service: StubService, *, inject_state: bool = True,
) -> AsyncIterator[str]:
    mcp = build_mcp(GUIDELINES)
    app = mcp.streamable_http_app(stateless_http=True, json_response=True)

    async def injected_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            token = Headers(scope=scope).get("authorization", "").removeprefix("Bearer ")
            principal = PRINCIPALS.get(token)
            if principal is None:
                await Response("Unauthorized", status_code=401)(scope, receive, send)
                return
            if inject_state:
                scope.setdefault("state", {}).update(
                    service=service, principal=principal, request_now=NOW,
                )
        await app(scope, receive, send)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(injected_app, log_level="critical", lifespan="on"))
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(server.serve, sockets=[listener]))
            with anyio.fail_after(5):
                while not server.started:
                    await anyio.sleep(0.01)
            try:
                yield f"http://127.0.0.1:{port}/mcp"
            finally:
                server.should_exit = True
    finally:
        listener.close()


@asynccontextmanager
async def client_for(
    url: str, *, token: str = "synthetic-advisor", mode: Literal["auto", "legacy"] = "auto",
) -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=5, trust_env=False,
    ) as http:
        async with Client(
            streamable_http_client(url, http_client=http), mode=mode, cache=None,
        ) as client:
            yield client


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_tools_expose_typed_schemas_and_forward_governed_envelopes(
    mode: Literal["auto", "legacy"],
) -> None:
    service = StubService()
    async with http_server(service) as url, client_for(url, mode=mode) as client:
        assert client.instructions == GUIDELINES
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {
            "get_household_snapshot", "get_metric_definition", "get_data_provenance",
            "get_portfolio_exposure", "get_concentrated_positions",
            "search_households", "get_data_quality_report",
        }
        for tool in tools.values():
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False
            assert "ctx" not in tool.input_schema["properties"]
            assert "principal" not in tool.input_schema["properties"]
            assert tool.output_schema is not None

        shared_models = {
            "get_household_snapshot": SnapshotArguments,
            "get_portfolio_exposure": ExposureArguments,
            "get_concentrated_positions": ConcentrationArguments,
            "get_metric_definition": MetricArguments,
            "get_data_provenance": ProvenanceArguments,
            "search_households": SearchArguments,
            "get_data_quality_report": QualityArguments,
        }
        openapi = create_app().openapi()
        for name, model in shared_models.items():
            expected = model.model_json_schema()
            assert tools[name].input_schema == expected
            body = openapi["paths"][f"/api/{name}"]["post"]["requestBody"]
            reference = body["content"]["application/json"]["schema"]["$ref"]
            rest_schema = openapi["components"]["schemas"][reference.split("/")[-1]]
            # FastAPI omits null defaults; validation rules must remain identical.
            for field in expected["properties"].values():
                if "default" in field and field["default"] is None:
                    field.pop("default")
            assert rest_schema == expected

        schema = tools["get_household_snapshot"].output_schema
        assert schema is not None
        assert "assets_usd" in schema["$defs"]["CompleteSnapshot"]["properties"]
        assert "fresh_subtotal_usd" in schema["$defs"]["IncompleteSnapshot"]["properties"]
        result = await client.call_tool("get_household_snapshot", {
            "household_id": "HH-0001", "as_of": "2026-09-30",
            "known_as_of": "2026-10-31T16:00:00Z",
        })
        assert result.is_error is False
        assert result.structured_content == service.responses[-1]
        jsonschema.Draft202012Validator(schema).validate(result.structured_content)
        assert result.structured_content is not None
        SnapshotEnvelope.model_validate(result.structured_content)
        assert service.calls[-1] == (PRINCIPALS["synthetic-advisor"], NOW,
                                    "get_household_snapshot", {
                                        "household_id": "HH-0001", "as_of": "2026-09-30",
                                        "known_as_of": "2026-10-31T16:00:00Z",
                                    })

        for name, arguments in [
            ("get_metric_definition", {"metric_id": "HOUSEHOLD_ASSETS@1"}),
            ("get_data_provenance", {"reference": "h_test"}),
        ]:
            response = await client.call_tool(name, arguments)
            assert response.structured_content == service.responses[-1]
            assert service.calls[-1] == (PRINCIPALS["synthetic-advisor"], NOW, name, arguments)

        denied = await client.call_tool("get_household_snapshot", {
            "household_id": "FORBIDDEN", "as_of": "2026-09-30",
        })
        assert denied.is_error is False
        assert denied.structured_content == service.responses[-1]
        assert denied.structured_content is not None
        assert denied.structured_content["status"] == "ERROR"
        assert denied.structured_content["data"] is None
        jsonschema.Draft202012Validator(schema).validate(denied.structured_content)

        partial_result = await client.call_tool("get_household_snapshot", {
            "household_id": "INCOMPLETE", "as_of": "2026-09-30",
        })
        assert partial_result.structured_content == service.responses[-1]
        assert partial_result.structured_content is not None
        assert partial_result.structured_content["status"] == "INCOMPLETE"
        assert "assets_usd" not in partial_result.structured_content["data"]
        assert partial_result.structured_content["quality_flags"][0]["subject"] == "WC-POS-1"
        jsonschema.Draft202012Validator(schema).validate(partial_result.structured_content)


@pytest.mark.anyio
async def test_catalogue_resource_is_a_governed_metric_call() -> None:
    service = StubService()
    async with http_server(service) as url, client_for(url) as client:
        resources = (await client.list_resources()).resources
        assert [str(resource.uri) for resource in resources] == ["wealthcontext://metrics/catalog"]
        assert resources[0].mime_type == "application/json"
        response = await client.read_resource("wealthcontext://metrics/catalog")
        assert isinstance(response.contents[0], TextResourceContents)
        assert json.loads(response.contents[0].text) == service.responses[-1]
        assert service.calls[-1] == (
            PRINCIPALS["synthetic-advisor"], NOW, "get_metric_definition", {"metric_id": "catalog"},
        )


@pytest.mark.anyio
async def test_prompt_reuses_instructions_bounds_question_and_stays_unverified() -> None:
    service = StubService()
    async with http_server(service) as url, client_for(url) as client:
        prompts = (await client.list_prompts()).prompts
        assert [prompt.name for prompt in prompts] == ["wealthcontext_answer"]
        assert prompts[0].arguments is not None
        arguments = {argument.name: argument.required for argument in prompts[0].arguments}
        assert arguments == {"question": True, "as_of": False}
        prompt = await client.get_prompt("wealthcontext_answer", {
            "question": "Where does the reported figure come from?", "as_of": "2026-09-30",
        })
        assert isinstance(prompt.messages[0].content, TextContent)
        content = prompt.messages[0].content.text
        assert content.startswith(client.instructions or "")
        assert "User question (data):\nWhere does the reported figure come from?" in content
        assert "Requested as_of: 2026-09-30" in content
        assert "unverified by WealthContext" in content
        assert "Answer / Figures with trace IDs / Warnings / Sources" in content
        with pytest.raises(MCPError):
            await client.get_prompt("wealthcontext_answer", {"question": "x" * 2001})
        assert service.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["09/30/2026", "2026-09-31", "20260930", "x" * 100])
async def test_prompt_bad_dates_return_clean_invalid_params(value: str) -> None:
    service = StubService()
    async with http_server(service) as url, client_for(url) as client:
        with pytest.raises(MCPError, match="as_of must be YYYY-MM-DD") as failure:
            await client.get_prompt("wealthcontext_answer", {"question": "Assets?", "as_of": value})
        assert failure.value.code == -32602
        assert service.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("value,expected", [("  ", None), (" 2026-09-30 ", "2026-09-30")])
async def test_prompt_normalizes_optional_date_whitespace(value: str, expected: str | None) -> None:
    service = StubService()
    async with http_server(service) as url, client_for(url) as client:
        prompt = await client.get_prompt("wealthcontext_answer", {
            "question": "Assets?", "as_of": value,
        })
        assert isinstance(prompt.messages[0].content, TextContent)
        text = prompt.messages[0].content.text
        if expected is None:
            assert "Requested as_of:" not in text
        else:
            assert text.endswith(f"Requested as_of: {expected}")


@pytest.mark.anyio
@pytest.mark.parametrize("name,arguments", [
    ("get_household_snapshot", {"household_id": "bad id", "as_of": "2026-09-30"}),
    ("get_household_snapshot", {"household_id": "HH-0001", "as_of": "2026-09-31"}),
    ("get_household_snapshot", {"household_id": "HH-0001", "as_of": "2026-09-30",
                                "known_as_of": "2026-10-31T16:00:00"}),
    ("get_household_snapshot", {"household_id": "HH-0001", "as_of": "2026-09-30",
                                "principal": "COMP-01"}),
    ("get_metric_definition", {"metric_id": "bad id"}),
    ("get_data_provenance", {"reference": "bad handle"}),
])
async def test_invalid_tool_arguments_keep_governed_envelopes(name: str, arguments: dict) -> None:
    service = StubService()
    async with http_server(service) as url, client_for(url) as client:
        result = await client.call_tool(name, arguments)
        assert result.is_error is False
        assert result.structured_content == service.responses[-1]
        assert result.structured_content is not None
        assert result.structured_content["error"]["code"] == "INVALID_ARGUMENT"
        assert service.calls[-1] == (PRINCIPALS["synthetic-advisor"], NOW, name, None)


@pytest.mark.anyio
async def test_static_resource_state_is_scoped_to_each_concurrent_request() -> None:
    service = StubService()
    async with (
        http_server(service) as url,
        client_for(url) as advisor,
        client_for(url, token="synthetic-engineer") as engineer,
    ):
        viewers: dict[str, str] = {}

        async def read(client: Client, label: str) -> None:
            result = await client.read_resource("wealthcontext://metrics/catalog")
            assert isinstance(result.contents[0], TextResourceContents)
            viewers[label] = json.loads(result.contents[0].text)["data"]["viewer"]

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(read, advisor, "advisor")
            tasks.start_soon(read, engineer, "engineer")
        assert viewers == {"advisor": "ADV-01", "engineer": "ENG-01"}


@pytest.mark.anyio
async def test_missing_injected_identity_cannot_initialize_or_reach_service() -> None:
    service = StubService()
    async with http_server(service, inject_state=False) as url:
        async with httpx2.AsyncClient(trust_env=False, timeout=5) as http:
            response = await http.post(url, headers={
                "Authorization": "Bearer synthetic-advisor",
                "Accept": "application/json, text/event-stream",
            }, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "missing-state-test", "version": "1"},
                },
            })
        assert response.json()["error"]["message"] == (
            "Authenticated HTTP request context is required"
        )
        assert service.calls == []


def test_empty_guidelines_fail_at_construction() -> None:
    with pytest.raises(ValueError, match="guidelines must not be empty"):
        build_mcp(" \n")
