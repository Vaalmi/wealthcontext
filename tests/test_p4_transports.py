"""Independent transport acceptance: real adapters, policy wire and durable evidence."""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from wealthcontext.app import create_app
from wealthcontext.clock import FrozenClock
from wealthcontext.identity import AUDIENCE, ISSUER, Identity
from wealthcontext.policy import PolicyClient
from wealthcontext.settings import Settings
from wealthcontext.storage import read_snapshot

NOW = datetime(2035, 1, 1, 15, tzinfo=UTC)
REVISION = "sha256:" + "a" * 64
ARGS = {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-11-25",
}


@asynccontextmanager
async def app_client(directory: Path, policy: PolicyClient):
    (directory / "revision.txt").write_text(policy.expected_revision)
    app = create_app(Settings(directory, directory, policy.url), FrozenClock(NOW))
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8000",
            follow_redirects=False,
        ) as client:
            yield app, client, directory / "wealthcontext.sqlite"


def bearer(app, persona: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + app.state.identity.mint(persona, NOW)}


async def transported(client, transport, tool, arguments, headers):
    if transport == "rest":
        response = await client.post(f"/api/{tool}", json=arguments, headers=headers)
        return response, response.json()
    response = await client.post("/mcp", headers={**MCP_HEADERS, **headers}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })
    payload = response.json()
    # Authentication is rejected by the common outer HTTP boundary, before RPC.
    if response.status_code == 401:
        return response, payload
    assert "error" not in payload, payload
    result = payload["result"]
    envelope = result["structuredContent"]
    # A governed error envelope is still a successfully executed MCP operation.
    assert result["isError"] is False
    return response, envelope


def audit(database: Path, envelope: dict, principal: str | None) -> list[dict]:
    with read_snapshot(database) as connection:
        rows = connection.execute(
            "SELECT * FROM results WHERE id=?", (envelope["result_id"],),
        ).fetchall()
        assert len(rows) == 1
        saved = rows[0]
        assert saved["principal_id"] == principal
        assert saved["trace_id"] == envelope["trace_id"]
        assert json.loads(saved["payload"]) == envelope
        assert hashlib.sha256(saved["payload"].encode()).hexdigest() == saved["payload_hash"]
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM decisions WHERE trace_id=? ORDER BY id", (envelope["trace_id"],),
        )]
        assert decisions, "Every transported business response requires decision evidence"
        assert all(row["principal_id"] == principal and row["id"] for row in decisions)
        assert all(row["action"] and row["reason"] for row in decisions)
        return decisions


def comparable(envelope: dict) -> dict:
    return {
        **{key: value for key, value in envelope.items()
           if key not in {"trace_id", "result_id", "source_refs"}},
        "source_ref_count": len(envelope["source_refs"]),
    }


@pytest.fixture
async def failure_app(tmp_path, monkeypatch):
    async def verify(_self):
        return REVISION
    monkeypatch.setattr(PolicyClient, "verify_revision", verify)
    async with app_client(tmp_path, PolicyClient("http://policy.invalid", REVISION)) as value:
        yield value


@pytest.mark.anyio
async def test_signed_token_failures_are_identical_and_audited_before_rest_or_mcp(
    failure_app, tmp_path, monkeypatch,
):
    app, client, database = failure_app

    async def forbidden_policy(*args, **kwargs):
        pytest.fail("unauthenticated input reached OPA")
    monkeypatch.setattr(PolicyClient, "decide", forbidden_policy)
    token = app.state.identity.mint("ADV-01", NOW)
    decoded = jwt.decode(token, options={"verify_signature": False})
    private_key = serialization.load_pem_private_key(
        (tmp_path / "demo-signing.pem").read_bytes(), password=None,
    )
    assert isinstance(private_key, rsa.RSAPrivateKey)
    wrong_signer = Identity.load_or_create(tmp_path / "other-signer")
    tokens = {
        "absent": None,
        "expired": app.state.identity.mint("ADV-01", NOW, expired=True),
        "wrong_signature": wrong_signer.mint("ADV-01", NOW),
        "wrong_issuer": jwt.encode({**decoded, "iss": ISSUER + "-other"},
                                   private_key, algorithm="RS256"),
        "wrong_audience": jwt.encode({**decoded, "aud": AUDIENCE + "-other"},
                                     private_key, algorithm="RS256"),
        "forged_role": jwt.encode({**decoded, "role": "compliance"},
                                  private_key, algorithm="RS256"),
        "future_nbf": jwt.encode({**decoded, "nbf": int(NOW.timestamp()) + 1},
                                  private_key, algorithm="RS256"),
    }
    for label, value in tokens.items():
        responses = []
        for transport in ("rest", "mcp"):
            headers = {} if value is None else {"Authorization": f"Bearer {value}"}
            response, envelope = await transported(
                client, transport, "get_household_snapshot", ARGS, headers,
            )
            assert response.status_code == 401, label
            assert response.headers["www-authenticate"] == "Bearer"
            assert envelope["error"]["code"] == "UNAUTHENTICATED"
            assert envelope["data"] is None and not envelope["source_refs"]
            assert {row["reason"] for row in audit(database, envelope, None)} == {
                "UNAUTHENTICATED",
            }
            responses.append(envelope)
        assert comparable(responses[0]) == comparable(responses[1]), label


@pytest.mark.anyio
async def test_no_transport_accepts_caller_supplied_identity(failure_app, monkeypatch):
    app, client, database = failure_app

    async def forbidden_policy(*args, **kwargs):
        pytest.fail("identity injection should be rejected by the argument contract")
    monkeypatch.setattr(PolicyClient, "decide", forbidden_policy)
    for field, value in (("principal", "COMP-01"), ("role", "compliance")):
        pair = []
        for transport in ("rest", "mcp"):
            _response, result = await transported(
                client, transport, "get_household_snapshot", {**ARGS, field: value},
                bearer(app, "ADV-01"),
            )
            assert result["error"]["code"] == "INVALID_ARGUMENT"
            assert result["data"] is None and not result["source_refs"]
            audit(database, result, "ADV-01")
            pair.append(result)
        assert comparable(pair[0]) == comparable(pair[1])


@pytest.mark.anyio
async def test_policy_wire_failures_are_fail_closed_at_both_business_transports(
    failure_app, monkeypatch,
):
    app, client, database = failure_app
    original_client = httpx2.AsyncClient
    faults = ["non_200", "timeout", "no_result", "malformed", "wrong_policy_revision",
              "wrong_entitlement_revision", "unknown_projection", "foreign_allowed_id",
              "duplicate_allowed_id", "string_allow"]
    for fault in faults:
        def respond(request: httpx2.Request, fault=fault):
            assert request.url.host == "policy.invalid"
            request_input = json.loads(request.content)["input"]
            if fault == "timeout":
                raise httpx2.ReadTimeout("Synthetic policy timeout", request=request)
            if fault == "non_200":
                return httpx2.Response(503, json={"error": "Synthetic unavailable"})
            if fault == "no_result":
                return httpx2.Response(200, json={})
            if fault == "malformed":
                return httpx2.Response(200, content=b"{broken")
            decision = {
                "allow": True, "allowed_ids": request_input["resources"], "projection": "full",
                "reason": "CURRENT_GRANT", "policy_revision": REVISION,
                "entitlement_revision": request_input["revision"],
            }
            changes = {
                "wrong_policy_revision": {"policy_revision": "sha256:" + "b" * 64},
                "wrong_entitlement_revision": {
                    "entitlement_revision": request_input["revision"] + 1,
                },
                "unknown_projection": {"projection": "invented_projection"},
                "foreign_allowed_id": {"allowed_ids": ["WC-HH-9999"]},
                "duplicate_allowed_id": {"allowed_ids": [*request_input["resources"]] * 2},
                "string_allow": {"allow": "true"},
            }
            return httpx2.Response(200, json={"result": {**decision, **changes[fault]}})
        mock = httpx2.MockTransport(respond)
        with monkeypatch.context() as patch:
            patch.setattr(httpx2, "AsyncClient",
                          lambda transport=mock, **kw: original_client(transport=transport, **kw))
            results = []
            for transport in ("rest", "mcp"):
                _response, envelope = await transported(
                    client, transport, "get_household_snapshot", ARGS, bearer(app, "ADV-01"),
                )
                assert envelope["error"]["code"] == "POLICY_UNAVAILABLE", (fault, envelope)
                assert envelope["error"]["retryable"] is True
                assert envelope["data"] is None and envelope["source_refs"] == []
                assert {row["reason"] for row in audit(database, envelope, "ADV-01")} == {
                    "POLICY_UNAVAILABLE",
                }
                results.append(envelope)
            assert comparable(results[0]) == comparable(results[1]), fault


@pytest.mark.anyio
async def test_real_policy_rest_and_mcp_financial_metric_and_lineage_parity(tmp_path, opa_server):
    async with app_client(tmp_path, opa_server) as (app, client, database):
        spec = yaml.safe_load((Path(__file__).parents[1] / "policies/matrix.yaml").read_text())
        role_actions = {row["tool"]: spec["actions"][row["action"]]["roles"]
                        for row in spec["routes"] if row.get("tool")}
        listing = await client.post("/mcp", headers={**MCP_HEADERS, **bearer(app, "ADV-01")},
                                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = listing.json()["result"]["tools"]
        assert {row["name"] for row in tools} == {
            "get_household_snapshot", "get_portfolio_exposure", "get_concentrated_positions",
            "get_metric_definition", "get_data_provenance", "search_households",
            "get_data_quality_report",
        }
        for tool in tools:
            assert tool["annotations"] == {
                "readOnlyHint": True, "destructiveHint": False,
                "idempotentHint": True, "openWorldHint": False,
            }
            assert "principal" not in tool["inputSchema"]["properties"]
        operations = [
            ("get_household_snapshot", ARGS),
            ("get_portfolio_exposure", {**ARGS, "group_by": "issuer"}),
            ("get_portfolio_exposure", {**ARGS, "group_by": "security"}),
            ("get_concentrated_positions", {**ARGS, "threshold_pct": "10.00"}),
            ("get_concentrated_positions", {"as_of": "2026-09-30", "threshold_pct": "10.00"}),
            ("get_metric_definition", {"metric_id": "HOUSEHOLD_ASSETS@1"}),
            ("search_households", {"name_query": "Synthetic"}),
            ("get_data_quality_report", {"as_of": "2026-09-30"}),
        ]
        for persona, role in (("ADV-01", "advisor"), ("OPS-01", "ops"),
                              ("COMP-01", "compliance"), ("ENG-01", "engineer")):
            for tool, arguments in operations:
                pair = []
                for transport in ("rest", "mcp"):
                    _response, envelope = await transported(
                        client, transport, tool, arguments, bearer(app, persona),
                    )
                    allowed = role in role_actions[tool]
                    assert (envelope["error"] is None) is allowed, (persona, tool, envelope)
                    audit(database, envelope, persona)
                    pair.append(envelope)
                assert comparable(pair[0]) == comparable(pair[1]), (persona, tool)
                if tool in {"get_household_snapshot", "get_data_quality_report"} and (
                    pair[0]["source_refs"]
                ):
                    reference = {"reference": pair[0]["source_refs"][0]}
                    lineage = []
                    for transport in ("rest", "mcp"):
                        _response, result = await transported(
                            client, transport, "get_data_provenance", reference,
                            bearer(app, persona),
                        )
                        assert result["error"] is None
                        audit(database, result, persona)
                        lineage.append(result)
                    assert comparable(lineage[0]) == comparable(lineage[1])
            unknown_lineage = []
            for transport in ("rest", "mcp"):
                _response, result = await transported(
                    client, transport, "get_data_provenance", {"reference": "h_unknown"},
                    bearer(app, persona),
                )
                assert result["error"]["code"] == "NOT_AUTHORIZED"
                audit(database, result, persona)
                unknown_lineage.append(result)
            assert comparable(unknown_lineage[0]) == comparable(unknown_lineage[1])
        for household in ("WC-HH-0001", "WC-HH-9999"):
            denied = []
            for transport in ("rest", "mcp"):
                _response, result = await transported(
                    client, transport, "get_household_snapshot",
                    {**ARGS, "household_id": household},
                    bearer(app, "ENG-01"),
                )
                assert result["error"]["code"] == "NOT_AUTHORIZED"
                assert result["error"]["message"] == (
                    "Caller is not authorized for the requested resource"
                )
                audit(database, result, "ENG-01")
                denied.append(result)
            assert comparable(denied[0]) == comparable(denied[1])
