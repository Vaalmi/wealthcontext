"""Counterexamples for version 1 findings carried into review Unit A (protocol version 2).

IDs: SOL-P5-R1-SERVICE-001 and ARGON-P5-SERVICE-001. Both use real OPA.
"""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wealthcontext.bootstrap import bootstrap_history
from wealthcontext.clock import FrozenClock
from wealthcontext.context_service import ContextService
from wealthcontext.database import migrate
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient
from wealthcontext.storage import digest, migrate_business, read_snapshot
from wealthcontext.telemetry import create_provider
from wealthcontext.verifier import TEMPLATE

NOW = datetime(2026, 11, 2, 15, tzinfo=UTC)  # 10:00 in New York on 2026-11-02
DAY = "2026-11-02"


@pytest.fixture
def context(tmp_path: Path, opa_server: PolicyClient):
    database = tmp_path / "db.sqlite"
    migrate(database)
    migrate_business(database)
    bootstrap_history(database, tmp_path / "received", FrozenClock(NOW))
    provider = create_provider(tmp_path / "traces.jsonl")
    yield ContextService(database, opa_server, provider.get_tracer("wealthcontext"))
    provider.shutdown()


def _count_claim(result: dict, day: str) -> dict:
    return {
        "answer_markdown": TEMPLATE, "claims": [{
            "id": "c1", "result_id": result["result_id"],
            "entity": {"type": "result_summary", "id": result["result_id"]},
            "field": "item_count", "household_id": None, "valuation_date": day,
            "unit": "count", "format": "value_only",
        }], "predicates": [], "caveats": [], "refusal": None,
    }


@pytest.mark.anyio
@pytest.mark.parametrize("principal,tool,arguments,count", [
    (Principal("ENG-01", "engineer"), "get_metric_definition", {"metric_id": "catalog"}, 5),
    (Principal("ADV-01", "advisor"), "search_households", {"name_query": "household"}, None),
])
async def test_nonhistorical_summary_counts_can_be_cited_through_submit_answer(
    context, principal, tool, arguments, count,
):
    """SOL-P5-R1-SERVICE-001: summary counts carry the request's business date and instant."""
    conversation = context.start_conversation(principal, NOW)
    result = await context.execute(principal, NOW, tool, arguments, conversation)
    assert result["status"] == "COMPLETE", result["error"]
    assert result["valuation_date"] == DAY
    assert datetime.fromisoformat(result["known_as_of"]) == NOW
    if count is not None:
        assert result["data"]["item_count"] == count
    answer = await context.execute(principal, NOW, "submit_answer",
                                   _count_claim(result, DAY), conversation)
    assert answer["status"] == "COMPLETE", answer
    assert answer["data"]["claims"][0]["value"] == result["data"]["item_count"]
    # Exact date matching is unchanged: another date is still rejected.
    wrong = await context.execute(principal, NOW, "submit_answer",
                                  _count_claim(result, "2026-11-01"), conversation)
    assert wrong["status"] == "ERROR"
    assert wrong["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.anyio
async def test_denied_search_keeps_no_temporal_scope(context):
    """Denial still precedes temporal scope, as for historical tools."""
    denied = await context.execute(Principal("ENG-01", "engineer"), NOW, "search_households",
                                   {"name_query": "household"})
    assert denied["status"] == "ERROR"
    assert denied["valuation_date"] is None and denied["known_as_of"] is None


@pytest.mark.anyio
async def test_empty_conversation_id_keeps_durable_neutral_denial(context):
    """ARGON-P5-SERVICE-001: an empty conversation ID is denied with durable evidence."""
    principal = Principal("ADV-01", "advisor")
    result = await context.execute(principal, NOW, "get_household_snapshot",
                                   {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}, "")
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "NOT_AUTHORIZED"
    with read_snapshot(context.database) as connection:
        row = connection.execute("SELECT conversation_id,payload,payload_hash FROM results "
                                 "WHERE id=?", (result["result_id"],)).fetchone()
        assert row is not None and row["conversation_id"] is None
        assert json.loads(row["payload"]) == result and digest(row["payload"]) == row[
            "payload_hash"]
        reasons = [item[0] for item in connection.execute(
            "SELECT reason FROM decisions WHERE trace_id=?", (result["trace_id"],))]
        assert "CONVERSATION_OWNER_MISMATCH" in reasons
