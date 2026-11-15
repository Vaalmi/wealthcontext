"""`list_households` (ADR 0009 section 1): the workbench's list of entitled households.

It is `search_households` without the name predicate: `search_preflight` on the empty
scope, then `search_households` in filter mode over the identifier-only inventory, with
names loaded for authorized identifiers only, at most 20, honest counts, and the result
recorded as search records its own. It is REST for the workbench only, never MCP.
"""

import json
from pathlib import Path

import pytest
import yaml

from tests.test_p4_transports import app_client, audit, bearer
from tests.test_secondary import (
    ADVISOR,
    COMPLIANCE,
    NOW,
    OPS,
    SurfacePolicy,
    prepare,
    secondary,  # noqa: F401  (a fixture)
)
from wealthcontext.context_service import ContextService
from wealthcontext.conversation import MODEL_TOOLS
from wealthcontext.facts import fact_view
from wealthcontext.harness import tool_specs
from wealthcontext.identity import Principal
from wealthcontext.mcp_server import ARGUMENT_MODELS
from wealthcontext.storage import canonical, read_snapshot
from wealthcontext.surface_contracts import (
    SURFACE_ARGUMENT_MODELS,
    SURFACE_OUTPUT_MODELS,
    HouseholdListData,
)
from wealthcontext.telemetry import create_provider

ROOT = Path(__file__).resolve().parents[1]
ENGINEER = Principal("ENG-01", "engineer")


def recorded_scope(database: Path, result_id: str) -> tuple[str, list[str], dict]:
    with read_snapshot(database) as connection:
        action = connection.execute("SELECT action FROM result_access WHERE result_id=?",
                                    (result_id,)).fetchone()[0]
        scopes = [row[0] for row in connection.execute(
            "SELECT household_id FROM result_scopes WHERE result_id=? ORDER BY household_id",
            (result_id,))]
        accounts = json.loads(connection.execute(
            "SELECT payload FROM result_account_scopes WHERE result_id=?",
            (result_id,)).fetchone()[0])
    return action, scopes, accounts


@pytest.mark.anyio
async def test_lists_only_authorized_households_and_records_them_as_search_does(secondary):  # noqa: F811
    service, policy = secondary
    result = await service.execute(ADVISOR, NOW, "list_households", {})
    assert result["status"] == "COMPLETE" and result["error"] is None, result
    assert result["data"] == {
        "items": [{"household_id": "WC-HH-0001", "household_name": "Private household 0001"}],
        "item_count": 1, "total_item_count": 1, "evaluated_household_count": 1,
        "ordering": "household_name.casefold ASC, household_id ASC", "truncated": False,
        "next_cursor": None,
    }
    assert "Private household 0002" not in canonical(result)
    assert [call[0] for call in policy.calls] == ["search_preflight", "search_households"]
    assert policy.calls[1][1] == ["WC-HH-0001", "WC-HH-0002", "WC-HH-0003"], (
        "the whole identifier-only inventory is filtered")
    action, scopes, accounts = recorded_scope(service.database, result["result_id"])
    assert (action, scopes, accounts["accounts"]) == ("search_households", ["WC-HH-0001"], [])
    # Its source records are the household records read, as search's are.
    provenance = await service.execute(ADVISOR, NOW, "get_data_provenance",
                                       {"reference": result["source_refs"][0]})
    assert provenance["error"] is None, provenance
    assert [item["source_record_id"] for item in provenance["data"]["items"]] == ["hh-1"]
    # Compliance is allowed every household; the counts say so.
    every = await service.execute(COMPLIANCE, NOW, "list_households", {})
    assert every["data"]["evaluated_household_count"] == 3
    assert [item["household_id"] for item in every["data"]["items"]] == [
        "WC-HH-0001", "WC-HH-0002", "WC-HH-0003"]


@pytest.mark.anyio
async def test_names_load_for_authorized_identifiers_only(secondary, monkeypatch):  # noqa: F811
    service, _ = secondary
    statements = []

    def observed(connection, kind, day, known, *, entity_ids=None):
        connection.set_trace_callback(statements.append)
        return fact_view(connection, kind, day, known, entity_ids=entity_ids)

    monkeypatch.setattr("wealthcontext.secondary.fact_view", observed)
    result = await service.execute(ADVISOR, NOW, "list_households", {})
    assert result["status"] == "COMPLETE", result
    names = [statement for statement in statements if "SELECT f.entity_id,f.payload" in statement]
    assert len(names) == 1 and "AND f.entity_id IN ('WC-HH-0001')" in names[0]
    assert "WC-HH-0002" not in names[0]


@pytest.mark.anyio
async def test_at_most_twenty_with_honest_counts_and_the_whole_scope_recorded(tmp_path: Path):
    database = tmp_path / "many.sqlite"
    prepare(database, count=23)
    policy = SurfacePolicy()
    policy.allowed = {f"WC-HH-{number:04}" for number in range(1, 24)}
    provider = create_provider(tmp_path / "trace.jsonl")
    try:
        service = ContextService(database, policy, provider.get_tracer("test"))
        result = await service.execute(ADVISOR, NOW, "list_households", {})
        data = result["data"]
        assert (data["item_count"], data["total_item_count"], data["truncated"],
                data["evaluated_household_count"], data["next_cursor"]) == (20, 23, True, 23, None)
        assert len(data["items"]) == 20
        _, scopes, _ = recorded_scope(database, result["result_id"])
        assert len(scopes) == 23, "the cap shrinks the output, never the recorded scope"
    finally:
        provider.shutdown()


@pytest.mark.anyio
async def test_refused_roles_get_the_same_denial_before_any_inventory_read(secondary, monkeypatch):  # noqa: F811
    service, _ = secondary

    def forbidden(*_args):
        pytest.fail("household identifiers were read before an allow")

    monkeypatch.setattr("wealthcontext.secondary._ids", forbidden)
    refusals = []
    for principal in (OPS, ENGINEER):
        result = await service.execute(principal, NOW, "list_households", {})
        assert result["error"]["code"] == "NOT_AUTHORIZED" and result["data"] is None
        assert not result["source_refs"] and not result["quality_flags"]
        refusals.append({key: value for key, value in result.items()
                         if key not in {"trace_id", "result_id", "policy_revision"}})
    assert refusals[0] == refusals[1]
    # Arguments are checked for shape first, for every caller, and nothing is read.
    for principal in (ADVISOR, OPS):
        invalid = await service.execute(principal, NOW, "list_households", {"name_query": "x"})
        assert invalid["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.anyio
async def test_redisclosure_reauthorizes_the_whole_recorded_scope(secondary):  # noqa: F811
    service, policy = secondary
    policy.allowed = {"WC-HH-0001", "WC-HH-0002"}
    result = await service.execute(ADVISOR, NOW, "list_households", {})
    handle = {"reference": result["source_refs"][0]}
    assert (await service.execute(ADVISOR, NOW, "get_data_provenance", handle))["error"] is None
    other = Principal("ADV-02", "advisor")
    foreign = await service.execute(other, NOW, "get_data_provenance", handle)
    unknown = await service.execute(other, NOW, "get_data_provenance", {"reference": "h_unknown"})
    assert foreign["error"] == unknown["error"] and foreign["error"]["code"] == "NOT_AUTHORIZED"
    # One of the two listed households is revoked: the handle no longer opens.
    policy.allowed = {"WC-HH-0001"}
    for tool, arguments in (("get_data_provenance", handle),
                            ("trace", {"trace_id": result["trace_id"]})):
        revoked = await service.execute(ADVISOR, NOW, tool, arguments)
        assert revoked["error"]["code"] == "NOT_AUTHORIZED", (tool, revoked)
        assert revoked["data"] is None
    reopened = await service.execute(COMPLIANCE, NOW, "get_data_provenance", handle)
    assert reopened["error"] is None


def test_it_stays_off_mcp_the_harness_and_the_evaluation_map():
    assert "list_households" not in ARGUMENT_MODELS
    assert "list_households" not in MODEL_TOOLS
    assert "list_households" not in {spec.name for spec in tool_specs()}
    assert "list_households" not in SURFACE_ARGUMENT_MODELS
    assert "list_households" in SURFACE_OUTPUT_MODELS
    rows = yaml.safe_load((ROOT / "policies/matrix.yaml").read_text())["routes"]
    (row,) = [row for row in rows if row.get("tool") == "list_households"]
    assert (row["method"], row["path"], row["action"], row["business"]) == (
        "POST", "/api/list_households", "search_households", True)
    schema = json.loads((ROOT / "schemas/v1/list-households.json").read_text())
    data = schema["$defs"]["HouseholdListData"]
    assert set(data["required"]) >= {"items", "item_count", "total_item_count", "truncated",
                                     "evaluated_household_count"}
    assert data["properties"]["items"]["maxItems"] == 20
    assert HouseholdListData.model_json_schema()["additionalProperties"] is False


@pytest.mark.anyio
async def test_real_policy_rest_route_for_every_role(tmp_path, opa_server):
    async with app_client(tmp_path, opa_server) as (app, client, database):
        for persona, allowed in (("ADV-01", True), ("COMP-01", True), ("OPS-01", False),
                                 ("ENG-01", False)):
            response = await client.post("/api/list_households", json={},
                                         headers=bearer(app, persona))
            envelope = response.json()
            decisions = audit(database, envelope, persona)
            if allowed:
                assert response.status_code == 200 and envelope["error"] is None, envelope
                assert envelope["source_refs"] and envelope["data"]["item_count"] > 0
                # audit() orders decisions by their random IDs, so compare as a set.
                assert sorted(row["action"] for row in decisions) == [
                    "search_households", "search_preflight"]
            else:
                assert response.status_code == 403
                assert envelope["error"]["message"] == (
                    "Caller is not authorized for the requested resource")
                assert envelope["data"] is None and not envelope["source_refs"]
                assert [row["action"] for row in decisions] == ["search_preflight"]
        advisor = (await client.post("/api/list_households", json={},
                                     headers=bearer(app, "ADV-01"))).json()
        assert [item["household_id"] for item in advisor["data"]["items"]] == [
            "WC-HH-0001", "WC-HH-0005", "WC-HH-0006", "WC-HH-0011"]
        extra = await client.post("/api/list_households", json={"name_query": "x"},
                                  headers=bearer(app, "ADV-01"))
        assert extra.status_code == 422 and extra.json()["error"]["code"] == "INVALID_ARGUMENT"
        anonymous = await client.post("/api/list_households", json={})
        assert anonymous.status_code == 401
