"""OPA requires exactly HTTP 200, even when another status has a valid allow body."""

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx2
import pytest
from opentelemetry.trace import NoOpTracerProvider

from wealthcontext.app import create_app
from wealthcontext.clock import FrozenClock
from wealthcontext.context_service import ContextService
from wealthcontext.database import migrate
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyUnavailable
from wealthcontext.settings import Settings
from wealthcontext.storage import canonical, migrate_business, read_snapshot, seed_vertical_slice

REVISION = "sha256:" + "a" * 64
PRINCIPAL = Principal("ADV-01", "advisor")
NOW = datetime(2035, 1, 1, tzinfo=UTC)
ARGS = {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}
NON_200 = [201, 202, 204, 206, 302, 400, 503]


def mock_opa(monkeypatch: pytest.MonkeyPatch, status: int) -> list[str]:
    """No network: every response is otherwise a valid current policy response."""
    original = httpx2.AsyncClient
    json_reads: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "policy.invalid"
        if request.url.path == "/v1/data/system/bundles":
            payload = {"result": {"bundle": {"manifest": {"revision": REVISION}}}}
        else:
            assert request.url.path == "/v1/data/wealthcontext/authz/decision"
            submitted = json.loads(request.content)["input"]
            payload = {"result": {
                "allow": True, "allowed_ids": submitted["resources"], "projection": "full",
                "reason": "CURRENT_GRANT", "policy_revision": REVISION,
                "entitlement_revision": submitted["revision"],
            }}
        response = httpx2.Response(status, json=payload)
        original_json = response.json

        def tracked_json():
            json_reads.append(request.url.path)
            return original_json()

        monkeypatch.setattr(response, "json", tracked_json)
        return response

    transport = httpx2.MockTransport(respond)
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    return json_reads


@pytest.mark.anyio
@pytest.mark.parametrize("status", NON_200)
@pytest.mark.parametrize("path", ["exact", "filtered", "revision"])
async def test_non_200_valid_policy_body_fails_before_json(monkeypatch, status, path):
    reads = mock_opa(monkeypatch, status)
    policy = PolicyClient("http://policy.invalid", REVISION)
    with pytest.raises(PolicyUnavailable):
        if path == "revision":
            await policy.verify_revision()
        elif path == "filtered":
            await policy.filter_candidates(PRINCIPAL, ["WC-HH-0001"], [], 1, date(2026, 11, 1))
        else:
            await policy.decide(PRINCIPAL, "snapshot", ["WC-HH-0001"], [], 1,
                                date(2026, 11, 1))
    assert reads == [], "Rejected HTTP status must precede parsing any policy body"


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["exact", "filtered", "revision"])
async def test_200_valid_policy_body_remains_usable(monkeypatch, path):
    reads = mock_opa(monkeypatch, 200)
    policy = PolicyClient("http://policy.invalid", REVISION)
    if path == "revision":
        assert await policy.verify_revision() == REVISION
    elif path == "filtered":
        decision = await policy.filter_candidates(PRINCIPAL, ["WC-HH-0001"], [], 1,
                                                  date(2026, 11, 1))
        assert decision.allow and decision.allowed_ids == ["WC-HH-0001"]
    else:
        decision = await policy.decide(PRINCIPAL, "snapshot", ["WC-HH-0001"], [], 1,
                                       date(2026, 11, 1))
        assert decision.allow and decision.allowed_ids == ["WC-HH-0001"]
    assert len(reads) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status", [200, 201, 202, 206, 503])
async def test_revision_status_controls_actual_health_response(tmp_path, monkeypatch, status):
    original = httpx2.AsyncClient
    reads = mock_opa(monkeypatch, status)
    (tmp_path / "revision.txt").write_text(REVISION)
    app = create_app(Settings(tmp_path, tmp_path, "http://policy.invalid"), FrozenClock(NOW))
    app.state.policy = PolicyClient("http://policy.invalid", REVISION)
    async with original(transport=httpx2.ASGITransport(app=app), base_url="http://localhost") as c:
        response = await c.get("/health")
    assert response.status_code == (200 if status == 200 else 503)
    if status == 200:
        assert response.json()["policy_revision"] == REVISION and len(reads) == 1
    else:
        assert response.json() == {"status": "unavailable", "component": "policy"}
        assert reads == []


@pytest.mark.anyio
async def test_revision_201_aborts_startup_before_bootstrap(tmp_path, monkeypatch):
    reads = mock_opa(monkeypatch, 201)
    (tmp_path / "revision.txt").write_text(REVISION)
    app = create_app(Settings(tmp_path, tmp_path, "http://policy.invalid"), FrozenClock(NOW))

    def forbidden_bootstrap(*args):
        pytest.fail("non-200 revision verification reached business bootstrap")

    monkeypatch.setattr("wealthcontext.app.bootstrap_history", forbidden_bootstrap)
    with pytest.raises(PolicyUnavailable):
        async with app.router.lifespan_context(app):
            pytest.fail("non-200 revision verification opened the application")
    assert not (tmp_path / "wealthcontext.sqlite").exists()
    assert reads == []


@pytest.mark.anyio
@pytest.mark.parametrize("status", [201, 202, 206, 503])
async def test_non_200_allow_is_governed_audited_denial_before_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int,
):
    reads = mock_opa(monkeypatch, status)
    database = tmp_path / "db.sqlite"
    migrate(database)
    migrate_business(database)
    seed_vertical_slice(database, NOW)
    service = ContextService(database, PolicyClient("http://policy.invalid", REVISION),
                             NoOpTracerProvider().get_tracer("policy-status-test"))

    def forbidden_lookup(*args):
        pytest.fail("non-200 authorization reached financial retrieval")

    monkeypatch.setattr("wealthcontext.context_service.calculate_snapshot", forbidden_lookup)
    result = await service.execute(PRINCIPAL, NOW, "get_household_snapshot", ARGS)
    assert result["status"] == "ERROR" and result["error"]["code"] == "POLICY_UNAVAILABLE"
    assert result["error"]["retryable"] is True
    assert result["data"] is None and result["source_refs"] == []
    assert result["policy_revision"] is None and reads == []
    with read_snapshot(database) as connection:
        stored = connection.execute("SELECT * FROM results WHERE id=?", (result["result_id"],))
        row = stored.fetchone()
        assert row and row["principal_id"] == PRINCIPAL.id and row["trace_id"] == result["trace_id"]
        assert row["payload"] == canonical(result)
        assert row["payload_hash"] == hashlib.sha256(row["payload"].encode()).hexdigest()
        decisions = connection.execute(
            "SELECT principal_id,action,allowed,reason,policy_revision FROM decisions "
            "WHERE trace_id=?", (result["trace_id"],),
        ).fetchall()
        assert [tuple(item) for item in decisions] == [
            (PRINCIPAL.id, "snapshot", 0, "POLICY_UNAVAILABLE", None),
        ]
        assert not connection.execute("SELECT 1 FROM result_scopes WHERE result_id=?",
                                      (result["result_id"],)).fetchall()


@pytest.mark.anyio
async def test_200_allow_preserves_governed_financial_retrieval(tmp_path, monkeypatch):
    reads = mock_opa(monkeypatch, 200)
    database = tmp_path / "db.sqlite"
    migrate(database)
    migrate_business(database)
    seed_vertical_slice(database, NOW)
    service = ContextService(database, PolicyClient("http://policy.invalid", REVISION),
                             NoOpTracerProvider().get_tracer("policy-status-test"))
    result = await service.execute(PRINCIPAL, NOW, "get_household_snapshot", ARGS)
    assert result["status"] == "COMPLETE" and result["data"]["assets_usd"] == "3000.00"
    assert result["source_refs"] and result["policy_revision"] == REVISION
    assert reads == ["/v1/data/wealthcontext/authz/decision"]


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["decision", "revision"])
async def test_200_malformed_json_still_fails_closed(monkeypatch, path):
    original = httpx2.AsyncClient
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200, content=b"{broken"))
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    policy = PolicyClient("http://policy.invalid", REVISION)
    with pytest.raises(PolicyUnavailable):
        if path == "revision":
            await policy.verify_revision()
        else:
            await policy.decide(PRINCIPAL, "snapshot", ["WC-HH-0001"], [], 1,
                                date(2026, 11, 1))


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["decision", "revision"])
async def test_timeout_still_fails_closed(monkeypatch, path):
    original = httpx2.AsyncClient

    def timeout(request):
        raise httpx2.ReadTimeout("Synthetic policy timeout", request=request)

    transport = httpx2.MockTransport(timeout)
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    policy = PolicyClient("http://policy.invalid", REVISION)
    with pytest.raises(PolicyUnavailable):
        if path == "revision":
            await policy.verify_revision()
        else:
            await policy.decide(PRINCIPAL, "snapshot", ["WC-HH-0001"], [], 1,
                                date(2026, 11, 1))
