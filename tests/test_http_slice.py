import json
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import httpx2
import pytest
from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from wealthcontext.app import ERROR_HTTP_STATUS, _response, create_app
from wealthcontext.clock import FrozenClock
from wealthcontext.contracts import ErrorCode
from wealthcontext.http_auth import Authenticate
from wealthcontext.policy import PolicyClient
from wealthcontext.settings import Settings
from wealthcontext.storage import read_snapshot


@pytest.fixture
async def validation_app(tmp_path: Path, monkeypatch):
    revision = "sha256:" + "a" * 64
    (tmp_path / "revision.txt").write_text(revision)

    async def verify(_self: PolicyClient) -> str:
        return revision

    async def unexpected_decision(*args, **kwargs):
        pytest.fail("argument/transport rejection reached a business authorization lookup")

    monkeypatch.setattr(PolicyClient, "verify_revision", verify)
    monkeypatch.setattr(PolicyClient, "decide", unexpected_decision)
    now = datetime(2035, 1, 1, 15, tzinfo=UTC)
    app = create_app(Settings(tmp_path, tmp_path, "http://unused"), FrozenClock(now))
    async with app.router.lifespan_context(app):
        yield app, now, tmp_path / "wealthcontext.sqlite"


@pytest.mark.anyio
async def test_http_identity_to_real_policy_to_snapshot_and_harness(
    tmp_path: Path, opa_server: PolicyClient,
) -> None:
    (tmp_path / "revision.txt").write_text(opa_server.expected_revision)
    now = datetime(2035, 1, 1, 15, tzinfo=UTC)
    app = create_app(Settings(tmp_path, tmp_path, opa_server.url), FrozenClock(now))
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8000",
            follow_redirects=False,
        ) as client:
            args = {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}
            no_token = await client.post("/api/get_household_snapshot", json=args)
            assert no_token.status_code == 401
            assert no_token.json()["error"]["code"] == "UNAUTHENTICATED"
            token = app.state.identity.mint("ADV-01", now)
            headers = {"Authorization": f"Bearer {token}"}
            snapshot = await client.post("/api/get_household_snapshot", json=args, headers=headers)
            assert snapshot.status_code == 200, snapshot.text
            assert snapshot.json()["data"]["assets_usd"] == "3000.00"
            answer = await client.post("/api/harness/one-claim", headers=headers)
            assert answer.status_code == 200, answer.text
            assert answer.json()["data"]["claims"][0]["value"] == "3000.00"
            invalid = await client.post("/api/get_household_snapshot", headers=headers,
                                        json={**args, "principal": "COMP-01"})
            assert invalid.status_code == 422
            expired = app.state.identity.mint("ADV-01", now, expired=True)
            denied = await client.post("/mcp", headers={"Authorization": f"Bearer {expired}"},
                                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert denied.status_code == 401
            # The exact path must reach the MCP handler without a slash redirect.
            mcp = await client.post("/mcp", headers={**headers, "Accept": "application/json"},
                                    json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
            assert mcp.status_code not in {301, 302, 307, 308, 404}
            assert "location" not in mcp.headers
    with read_snapshot(tmp_path / "wealthcontext.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE reason='UNAUTHENTICATED'",
        ).fetchone()[0] == 2


@pytest.mark.anyio
async def test_invalid_rest_bodies_are_governed_and_recorded(validation_app) -> None:
    app, now, database = validation_app
    token = app.state.identity.mint("ADV-01", now)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    bodies = [b"", b"{bad", b"null", b"[]", b'"not an object"']
    responses = []
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8000",
    ) as client:
        for tool in ["get_household_snapshot", "get_metric_definition", "get_data_provenance"]:
            for body in bodies:
                result = await client.post(f"/api/{tool}", content=body, headers=headers)
                assert result.status_code == 422, result.text
                envelope = result.json()
                assert envelope["error"]["code"] == "INVALID_ARGUMENT"
                assert envelope["data"] is None
                assert "detail" not in envelope
                responses.append(envelope)
        invalid_fields = [
            {"household_id": "bad id", "as_of": "2026-09-30"},
            {"household_id": "WC-HH-0001", "as_of": "2026-09-31"},
            {"household_id": "WC-HH-0001", "as_of": "2026-09-30",
             "known_as_of": "2026-10-31T16:00:00"},
            {"household_id": "WC-HH-0001", "as_of": "2026-09-30", "principal": "COMP-01"},
        ]
        for arguments in invalid_fields:
            result = await client.post("/api/get_household_snapshot", json=arguments,
                                       headers=headers)
            assert result.status_code == 422
            assert result.json()["error"]["code"] == "INVALID_ARGUMENT"
            responses.append(result.json())
    with read_snapshot(database) as connection:
        recorded = {row["id"]: json.loads(row["payload"])
                    for row in connection.execute("SELECT id,payload FROM results")}
        for response in responses:
            assert recorded[response["result_id"]] == response
        assert connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE reason='INVALID_ARGUMENT'",
        ).fetchone()[0] == len(responses)


@pytest.mark.anyio
@pytest.mark.parametrize("host,expected", [
    ("localhost:8000", 200), ("app:8000", 200), ("evil.example", 421),
])
async def test_mcp_host_validation(validation_app, host: str, expected: int) -> None:
    app, now, _ = validation_app
    token = app.state.identity.mint("ADV-01", now)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8000",
        follow_redirects=False,
    ) as client:
        result = await client.post("/mcp", headers={
            "Authorization": f"Bearer {token}", "Host": host,
            "Accept": "application/json, text/event-stream",
        }, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "host-test", "version": "1"},
            },
        })
    assert result.status_code == expected, result.text
    assert "location" not in result.headers


@pytest.mark.anyio
async def test_websocket_is_rejected_before_application_or_auth_state() -> None:
    sent: list[Message] = []

    async def downstream(_scope: Scope, _receive: Receive, _send: Send) -> None:
        pytest.fail("unsupported WebSocket reached the application")

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = Authenticate(downstream, application=FastAPI(),
                              clock=FrozenClock(datetime(2035, 1, 1, tzinfo=UTC)))
    await middleware({"type": "websocket", "path": "/mcp"}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_all_error_codes_have_explicit_http_status_and_unknown_errors_fail() -> None:
    assert set(ERROR_HTTP_STATUS) == set(get_args(ErrorCode))
    for code, status in ERROR_HTTP_STATUS.items():
        assert _response({"error": {"code": code}}).status_code == status
        assert status >= 400
    assert _response({"error": {"code": "UNRECOGNIZED"}}).status_code == 500
    assert _response({"status": "INCOMPLETE", "error": None}).status_code == 200
