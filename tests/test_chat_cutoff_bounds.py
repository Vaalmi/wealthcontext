"""Structured chat cutoff rejection must precede provider construction/invocation."""

import json
from pathlib import Path

import httpx2
import pytest
from test_secondary_cutoff_bounds import (
    INVALID_CUTOFFS,
    NOW,
    VALID_CUTOFFS,
    assert_recorded,
    boundary_settings,
)

from wealthcontext.app import create_app
from wealthcontext.chat_contracts import ChatArguments
from wealthcontext.clock import FrozenClock
from wealthcontext.provider_contracts import ProviderName, ProviderRequest, ProviderTurn, ToolCall
from wealthcontext.storage import read_snapshot


class EmptyAnswerProvider:
    provider = "openai"
    model = "synthetic-cutoff-control"
    settings = {}

    def __init__(self):
        self.calls = 0

    async def complete(self, request: ProviderRequest) -> ProviderTurn:
        self.calls += 1
        return ProviderTurn(model=self.model, calls=[ToolCall(
            id="finish", name="submit_answer", arguments={
                "answer_markdown": "Verified data is unavailable.", "claims": [],
                "predicates": [], "caveats": [], "refusal": None,
            },
        )])


@pytest.mark.anyio
async def test_chat_invalid_cutoff_is_durable_before_provider_and_normal_input_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = EmptyAnswerProvider()
    constructed: list[ProviderName] = []

    def factory(provider: ProviderName) -> EmptyAnswerProvider:
        constructed.append(provider)
        return adapter

    app = create_app(boundary_settings(tmp_path, monkeypatch), FrozenClock(NOW),
                     provider_factory=factory)
    database = tmp_path / "wealthcontext.sqlite"
    async with app.router.lifespan_context(app), httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://localhost:8000",
    ) as client:
        headers = {"Authorization": "Bearer " + app.state.identity.mint("COMP-01", NOW)}
        for cutoff in INVALID_CUTOFFS:
            result = await client.post("/api/harness/answer", headers=headers, json={
                "question": "Show available data.", "known_as_of": cutoff,
            })
            assert result.status_code == 422, result.text
            assert_recorded(database, result.json(), invalid=True)
            assert constructed == [] and adapter.calls == 0
        with read_snapshot(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        values = {"question": "Show available data.", "known_as_of": VALID_CUTOFFS[-1]}
        parsed = ChatArguments.model_validate(values)
        assert json.loads(parsed.request_text())["known_as_of"] == VALID_CUTOFFS[-1]
        result = await client.post("/api/harness/answer", headers=headers, json=values)
        assert result.status_code == 200, result.text
        assert result.json()["final_valid"] is True
        assert constructed == ["openai"] and adapter.calls == 1
