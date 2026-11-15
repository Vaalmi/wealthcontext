"""Actual secondary service/REST/MCP cutoff validation; policy is a synthetic stub."""

import hashlib
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
import pytest
import uvicorn
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_reference_oracle import Export, cash

from wealthcontext.app import create_app
from wealthcontext.clock import FrozenClock
from wealthcontext.contracts import Contract
from wealthcontext.generator import load_history
from wealthcontext.identity import Principal
from wealthcontext.ingestion import import_fixture_history, migrate_feeds
from wealthcontext.policy import PolicyClient, PolicyDecision
from wealthcontext.settings import Settings
from wealthcontext.storage import read_snapshot
from wealthcontext.surface_contracts import ConflictArguments, FeedRunsArguments, QualityArguments

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
REVISION = "sha256:" + "a" * 64
INVALID_CUTOFFS = ["0001-01-01T00:00:00+00:01", "9999-12-31T23:59:59-00:01"]
VALID_CUTOFFS = [
    "0001-01-01T00:01:00+00:01", "9999-12-31T23:58:59.999999-00:01",
    "2026-10-02T15:00:00.123456+01:00",
]


def boundary_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Use hand-received cash and actual ingestion, without an external policy service."""
    (tmp_path / "revision.txt").write_text(REVISION)

    async def revision(_self):
        return REVISION

    async def decide(_self, _principal, _action, resources, _facts, revision, _day):
        return PolicyDecision(allow=True, allowed_ids=resources, projection="full",
                              reason="SYNTHETIC_CUTOFF_TEST", policy_revision=REVISION,
                              entitlement_revision=revision)

    def bootstrap(database, directory, clock):
        directory.mkdir(parents=True)
        received = Export(directory)
        received.crm()
        received.snapshots([cash("100")])
        migrate_feeds(database)
        import_fixture_history(database, load_history(directory), clock)

    monkeypatch.setattr(PolicyClient, "verify_revision", revision)
    monkeypatch.setattr(PolicyClient, "decide", decide)
    monkeypatch.setattr(PolicyClient, "filter_resources", decide)
    monkeypatch.setattr("wealthcontext.app.bootstrap_history", bootstrap)
    return Settings(tmp_path, tmp_path, "http://unused.invalid")


def assert_recorded(database: Path, envelope: dict, *, invalid: bool = False) -> None:
    with read_snapshot(database) as connection:
        row = connection.execute("SELECT payload,payload_hash FROM results WHERE id=?",
                                 (envelope["result_id"],)).fetchone()
        assert row is not None
        assert json.loads(row["payload"]) == envelope
        assert hashlib.sha256(row["payload"].encode()).hexdigest() == row["payload_hash"]
        if invalid:
            assert envelope["status"] == "ERROR"
            assert envelope["error"]["code"] == "INVALID_ARGUMENT"
            assert envelope["data"] is None
            assert envelope["source_refs"] == envelope["metric_refs"] == []
            decision = connection.execute(
                "SELECT principal_id,reason,allowed FROM decisions WHERE trace_id=?",
                (envelope["trace_id"],),
            ).fetchone()
            assert decision is not None
            assert tuple(decision) == ("COMP-01", "INVALID_ARGUMENT", 0)


@pytest.fixture
async def governed_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = create_app(boundary_settings(tmp_path, monkeypatch), FrozenClock(NOW))
    async with app.router.lifespan_context(app):
        yield app, tmp_path / "wealthcontext.sqlite"


@pytest.mark.parametrize("model", [QualityArguments, FeedRunsArguments, ConflictArguments])
def test_secondary_cutoff_models_preserve_valid_offsets(model: type[Contract]) -> None:
    for cutoff in VALID_CUTOFFS:
        values = {"as_of": "2026-09-30", "known_as_of": cutoff,
                  **({"household_id": "WC-HH-0001"} if model is ConflictArguments else {})}
        parsed = model.model_validate(values).model_dump()["known_as_of"]
        assert parsed == datetime.fromisoformat(cutoff)
        assert parsed.isoformat() == datetime.fromisoformat(cutoff).isoformat()


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["get_data_quality_report", "feed_runs", "conflicts"])
async def test_secondary_cutoffs_are_governed_in_direct_service_and_rest(
    governed_boundary, tool: str,
) -> None:
    app, database = governed_boundary
    token = app.state.identity.mint("COMP-01", NOW)
    arguments = {"as_of": "2026-09-30",
                 **({"household_id": "WC-HH-0001"} if tool == "conflicts" else {})}
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://localhost:8000",
    ) as client:
        for cutoff in INVALID_CUTOFFS:
            values = {**arguments, "known_as_of": cutoff}
            direct = await app.state.service.execute(
                Principal("COMP-01", "compliance"), NOW, tool, values,
            )
            assert_recorded(database, direct, invalid=True)
            result = await client.post(f"/api/{tool}", json=values,
                                       headers={"Authorization": "Bearer " + token})
            assert result.status_code == 422, result.text
            assert_recorded(database, result.json(), invalid=True)
        normal = await client.post(f"/api/{tool}", json={
            **arguments, "known_as_of": VALID_CUTOFFS[-1],
        }, headers={"Authorization": "Bearer " + token})
        assert normal.status_code == 200, normal.text
        assert normal.json()["status"] != "ERROR"
        assert normal.json()["data"] is not None
        assert_recorded(database, normal.json())


@asynccontextmanager
async def local_http(app: FastAPI) -> AsyncIterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    # The fixture already runs the real app/MCP lifespan; no second initialization.
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
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


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_quality_shared_validation_preserves_governed_error_without_handler_changes(
    governed_boundary, mode: Literal["auto", "legacy"],
) -> None:
    app, database = governed_boundary
    token = app.state.identity.mint("COMP-01", NOW)
    async with local_http(app) as url, httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + token}, timeout=5, trust_env=False,
    ) as http, Client(
        streamable_http_client(url, http_client=http), mode=mode, cache=None,
    ) as client:
        for cutoff in INVALID_CUTOFFS:
            result = await client.call_tool("get_data_quality_report", {
                "as_of": "2026-09-30", "known_as_of": cutoff,
            })
            assert result.is_error is False
            assert result.structured_content is not None
            assert_recorded(database, result.structured_content, invalid=True)
        normal = await client.call_tool("get_data_quality_report", {
            "as_of": "2026-09-30", "known_as_of": VALID_CUTOFFS[-1],
        })
        assert normal.is_error is False
        assert normal.structured_content is not None
        assert normal.structured_content["status"] != "ERROR"
        assert_recorded(database, normal.structured_content)
