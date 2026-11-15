"""The operations contracts of ADR 0009 sections 3 and 4 (build step 6, O1 and O2), in secondary.py.

`feed_health` and `feed_run_impact` are REST routes for the workbench only, on the existing
`feed_runs` action. Each takes `feed_runs` on the empty scope before it resolves a date or
looks up a run; unknown and forbidden run IDs get one identical response; each result is
recorded as `feed_runs` records its own; both return identifiers, closed codes, counts and
timestamps only.
"""

import json
import sqlite3
from pathlib import Path

import anyio
import pytest
import yaml

from tests.test_p4_transports import app_client, audit, bearer
from tests.test_secondary import ADVISOR, COMPLIANCE, NOW, OPS, SurfacePolicy, prepare
from tests.test_secondary_compliance_contracts import comparable, recorded
from wealthcontext import secondary
from wealthcontext.bootstrap import bootstrap_history
from wealthcontext.clock import FrozenClock
from wealthcontext.context_service import ContextService, Operation, RequestFailure
from wealthcontext.conversation import MODEL_TOOLS
from wealthcontext.database import migrate, write_transaction
from wealthcontext.facts import FactView
from wealthcontext.feeds import FEED_SOURCES
from wealthcontext.harness import tool_specs
from wealthcontext.identity import Principal
from wealthcontext.mcp_server import ARGUMENT_MODELS, build_mcp
from wealthcontext.policy import PolicyDecision
from wealthcontext.storage import canonical, migrate_business, read_snapshot
from wealthcontext.surface_contracts import (
    EXPECTED_FEED_PAIRS,
    SURFACE_ARGUMENT_MODELS,
    SURFACE_OUTPUT_MODELS,
)
from wealthcontext.telemetry import create_provider

ROOT = Path(__file__).resolve().parents[1]
ENGINEER = Principal("ENG-01", "engineer")
TOOLS = ("feed_health", "feed_run_impact")
CUTOFF = "2026-10-31T16:00:00Z"
WINDOW = {"date_from": "2026-09-14", "date_to": "2026-10-16", "known_as_of": CUTOFF}
MCP_TOOLS = {
    "get_household_snapshot", "get_portfolio_exposure", "get_concentrated_positions",
    "get_data_provenance", "search_households", "get_metric_definition",
    "get_data_quality_report",
}


@pytest.fixture(scope="module")
def fixture_database(tmp_path_factory) -> Path:
    """The generated demo history, once: S07's fully failed PAS-B snapshot run."""
    directory = tmp_path_factory.mktemp("feeds")
    database = directory / "demo.sqlite"
    migrate(database)
    migrate_business(database)
    bootstrap_history(database, directory / "received", FrozenClock(NOW))
    return database


@pytest.fixture
def demo(fixture_database: Path, tmp_path: Path):
    database = tmp_path / "demo.sqlite"
    with sqlite3.connect(fixture_database) as source, sqlite3.connect(database) as target:
        source.backup(target)
    provider = create_provider(tmp_path / "trace.jsonl")
    policy = SurfacePolicy()
    yield ContextService(database, policy, provider.get_tracer("test")), policy
    provider.shutdown()


@pytest.fixture
def unit(tmp_path: Path):
    database = tmp_path / "surfaces.sqlite"
    prepare(database)
    provider = create_provider(tmp_path / "trace.jsonl")
    policy = SurfacePolicy()
    yield ContextService(database, policy, provider.get_tracer("test")), policy
    provider.shutdown()


def run_id(database: Path, system: str, feed: str, day: str) -> str:
    with read_snapshot(database) as connection:
        (row,) = connection.execute(
            "SELECT id FROM feed_runs WHERE source_system=? AND feed=? AND business_date=?",
            (system, feed, day)).fetchall()
    return row[0]


def private_strings(database: Path) -> set[str]:
    """Every received name and title, the S11 instruction titles included."""
    private = set()
    with read_snapshot(database) as connection:
        for row in connection.execute("SELECT payload FROM facts WHERE kind IN "
                                      "('household','person','ownership','crm_ownership',"
                                      "'advisor','team')"):
            payload = json.loads(row["payload"])
            private.update(payload[key] for key in ("name", "title")
                           if isinstance(payload.get(key), str) and payload[key])
    assert len(private) > 20 and any("ignore" in value.lower() for value in private), (
        "S11 titles are in the scan")
    return private


# --- Shared: off MCP, the closed table, schemas, matrix rows ------------------------------


def test_they_stay_off_mcp_the_harness_and_the_evaluation_map():
    rows = {row.get("tool"): row for row in
            yaml.safe_load((ROOT / "policies/matrix.yaml").read_text())["routes"]}
    for tool in TOOLS:
        assert tool not in ARGUMENT_MODELS and tool not in MODEL_TOOLS
        assert tool not in {spec.name for spec in tool_specs()}
        assert tool not in SURFACE_ARGUMENT_MODELS, "evaluation.py reads that map (Unit B)"
        assert tool in SURFACE_OUTPUT_MODELS
        row = rows[tool]
        assert (row["method"], row["path"], row["action"], row["business"]) == (
            "POST", f"/api/{tool}", "feed_runs", True), "the existing feed_runs action"
    server = build_mcp((ROOT / "prompts/host_guidelines.md").read_text())
    assert {tool.name for tool in anyio.run(server.list_tools)} == MCP_TOOLS, "still the seven"


def test_the_expected_cells_are_the_closed_table_of_the_fifteen_feed_source_pairs():
    pairs = {(system, feed) for feed, systems in FEED_SOURCES.items() for system in systems}
    assert len(EXPECTED_FEED_PAIRS) == 15 == len(set(EXPECTED_FEED_PAIRS))
    assert set(EXPECTED_FEED_PAIRS) == pairs
    assert list(EXPECTED_FEED_PAIRS) == sorted(EXPECTED_FEED_PAIRS)


def test_published_schemas_close_their_shapes():
    health = json.loads((ROOT / "schemas/v1/feed-health.json").read_text())["$defs"]
    data = health["FeedHealthData"]
    assert data["additionalProperties"] is False
    assert data["properties"]["items"]["maxItems"] == 375
    assert data["properties"]["business_dates"]["maxItems"] == 25
    cell = health["FeedHealthCell"]
    assert cell["additionalProperties"] is False
    assert set(cell["properties"]) == {
        "source_system", "feed", "business_date", "state", "run_id", "upstream_status",
        "accepted_records", "rejected_records", "recorded_at", "run_count"}
    impact = json.loads((ROOT / "schemas/v1/feed-run-impact.json").read_text())["$defs"]
    account = impact["AffectedAccount"]
    assert set(account["properties"]) == {"account_id", "manifest_account_id", "reason",
                                          "household_id", "household_attribution"}
    assert account["additionalProperties"] is False
    fields = {name for model in [*health.values(), *impact.values()]
              for name in model.get("properties", {})}
    assert not {name for name in fields if name.endswith(("name", "title", "number"))}, (
        "identifiers, codes, counts and timestamps only")
    arguments = json.loads((ROOT / "schemas/v1/feed-health-arguments.json").read_text())
    assert set(arguments["properties"]) == {"date_from", "date_to", "known_as_of"}
    arguments = json.loads((ROOT / "schemas/v1/feed-run-impact-arguments.json").read_text())
    assert set(arguments["properties"]) == {"run_id", "known_as_of"}


# --- The account_availability rows of a fully failed snapshot run (ADR 0009 section 4) ----


def test_a_fully_failed_snapshot_run_writes_one_unavailable_row_per_manifest_account(
    fixture_database,
):
    run = run_id(fixture_database, "PAS-B", "pas_snapshots", "2026-10-15")
    with read_snapshot(fixture_database) as connection:
        stored = connection.execute("SELECT status,upstream_status,content,accepted_records "
                                    "FROM feed_runs WHERE id=?", (run,)).fetchone()
        manifest = json.loads(connection.execute(
            "SELECT payload FROM feed_manifests WHERE run_id=?", (run,)).fetchone()[0])
        availability = [dict(row) for row in connection.execute(
            "SELECT account_id,available,reason,source_ids FROM account_availability "
            "WHERE run_id=? ORDER BY account_id", (run,))]
        publications = [dict(row) for row in connection.execute(
            "SELECT manifest_account_id,available,reason,source_ids FROM account_publications "
            "WHERE run_id=? ORDER BY manifest_account_id", (run,))]
        quarantine = [dict(row) for row in connection.execute(
            "SELECT account_id,reason FROM quarantine WHERE run_id=? ORDER BY account_id",
            (run,))]
    assert (stored["status"], stored["upstream_status"], stored["content"],
            stored["accepted_records"]) == ("FAILED", "FAILED", None, 0)
    labels = sorted(account["account_id"] for account in manifest["accounts"])
    assert len(labels) == 15 and sorted(manifest["expected_account_ids"]) == labels
    assert [row["account_id"] for row in availability] == labels
    assert all((row["available"], row["reason"], row["source_ids"]) == (
        0, "ACCOUNT_MANIFEST_MISMATCH", "[]") for row in availability), "nothing carried forward"
    assert [row["manifest_account_id"] for row in publications] == labels
    assert all((row["available"], row["reason"], row["source_ids"]) == (
        0, "ACCOUNT_MANIFEST_MISMATCH", "[]") for row in publications)
    assert [row["account_id"] for row in quarantine] == labels
    assert {row["reason"] for row in quarantine} == {"ACCOUNT_MANIFEST_MISMATCH"}


# --- O1: feed_health ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_health_returns_every_expected_cell_with_counts_in_the_data(demo):
    service, _ = demo
    for principal, projection in ((OPS, "no_pii"), (COMPLIANCE, "full")):
        result = await service.execute(principal, NOW, "feed_health", WINDOW)
        assert result["error"] is None and result["status"] == "COMPLETE", result
        data = result["data"]
        assert (result["as_of"], result["valuation_date"], result["known_as_of"]) == (
            "2026-10-16", "2026-10-16", CUTOFF)
        assert (data["date_from"], data["date_to"], data["business_day_count"]) == (
            "2026-09-14", "2026-10-16", 25)
        assert data["item_count"] == data["total_item_count"] == 375 and not data["truncated"]
        assert [(pair["source_system"], pair["feed"]) for pair in data["pairs"]] == list(
            EXPECTED_FEED_PAIRS)
        cells = [(item["business_date"], item["source_system"], item["feed"])
                 for item in data["items"]]
        assert cells == sorted(cells) and len(set(cells)) == 375
        assert cells == [(day, system, feed) for day in data["business_dates"]
                         for system, feed in EXPECTED_FEED_PAIRS]
        counts = {item["state"]: item["cell_count"] for item in data["state_counts"]}
        assert counts == {"SUCCESS": 373, "PARTIAL": 1, "FAILED": 1, "TIMEOUT": 0,
                          "NOT_RECEIVED": 0}
        (failed,) = [item for item in data["items"] if item["state"] == "FAILED"]
        assert (failed["business_date"], failed["source_system"], failed["feed"]) == (
            "2026-10-15", "PAS-B", "pas_snapshots")
        assert failed["run_id"] == run_id(service.database, "PAS-B", "pas_snapshots",
                                          "2026-10-15")
        assert (failed["upstream_status"], failed["accepted_records"],
                failed["rejected_records"], failed["run_count"]) == ("FAILED", 0, 15, 1)
        # Recorded as feed_runs records its own: every household of the range, no accounts,
        # no sources (run metadata only).
        action, recorded_projection, scopes, accounts, handles = recorded(
            service.database, result["result_id"])
        assert (action, recorded_projection) == ("feed_runs", projection)
        assert len(scopes) == 14 and accounts is not None and accounts["accounts"] == []
        assert handles == [] and result["source_refs"] == []


@pytest.mark.anyio
async def test_cells_not_received_at_the_cutoff_are_marked_and_counted(demo):
    service, _ = demo
    result = await service.execute(OPS, NOW, "feed_health", {
        "date_from": "2026-10-12", "date_to": "2026-10-16",
        "known_as_of": "2026-10-14T23:00:00Z"})
    data = result["data"]
    assert data["business_day_count"] == 5 and data["total_item_count"] == 75
    late = [item for item in data["items"] if item["business_date"] >= "2026-10-15"]
    assert len(late) == 30 and all(item["state"] == "NOT_RECEIVED" for item in late)
    assert all(item["run_id"] is None and item["run_count"] == 0 and item["recorded_at"] is None
               for item in late), "a never-received cell carries no run"
    counts = {item["state"]: item["cell_count"] for item in data["state_counts"]}
    assert counts["NOT_RECEIVED"] == 30 and sum(counts.values()) == 75


@pytest.mark.anyio
async def test_the_latest_run_wins_and_every_run_is_counted(demo):
    service, _ = demo
    with read_snapshot(service.database) as connection:
        repeated = connection.execute(
            "SELECT business_date,source_system,feed,COUNT(*) AS runs FROM feed_runs "
            "WHERE recorded_at<=? GROUP BY 1,2,3 HAVING COUNT(*)>1 ORDER BY 1,2,3 LIMIT 1",
            ("2026-10-31T16:00:00.000000Z",)).fetchone()
        assert repeated is not None, "the fixture holds a corrected cell"
        latest = connection.execute(
            "SELECT id,status FROM feed_runs WHERE business_date=? AND source_system=? "
            "AND feed=? ORDER BY recorded_at DESC,id ASC LIMIT 1", tuple(repeated)[:3]).fetchone()
    result = await service.execute(OPS, NOW, "feed_health", {
        "date_from": repeated["business_date"], "date_to": repeated["business_date"],
        "known_as_of": CUTOFF})
    (cell,) = [item for item in result["data"]["items"]
               if (item["source_system"], item["feed"]) == (repeated["source_system"],
                                                            repeated["feed"])]
    assert (cell["run_id"], cell["state"], cell["run_count"]) == (
        latest["id"], latest["status"], repeated["runs"])


@pytest.mark.anyio
async def test_health_dates_resolve_as_as_of_does_and_are_bounded(demo):
    service, _ = demo
    weekend = await service.execute(OPS, NOW, "feed_health", {
        "date_from": "2026-10-10", "date_to": "2026-10-11", "known_as_of": CUTOFF})
    assert weekend["error"] is None
    assert (weekend["as_of"], weekend["valuation_date"]) == ("2026-10-11", "2026-10-09")
    assert weekend["data"]["business_dates"] == ["2026-10-09"]
    for arguments in ({"date_from": "2026-09-01", "date_to": "2026-09-30"},
                      {"date_from": "2026-10-01", "date_to": "2026-10-30"}):
        out = await service.execute(OPS, NOW, "feed_health", arguments)
        assert out["error"]["code"] == "AS_OF_OUT_OF_RANGE" and out["data"] is None
    # Shape checks come first and read nothing, for any caller.
    for principal in (OPS, ADVISOR, ENGINEER):
        reversed_range = await service.execute(principal, NOW, "feed_health", {
            "date_from": "2026-10-16", "date_to": "2026-10-15"})
        assert reversed_range["error"]["code"] == "INVALID_ARGUMENT"
        missing = await service.execute(principal, NOW, "feed_health", {"date_to": "2026-10-15"})
        assert missing["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.anyio
async def test_health_is_refused_before_any_date_or_household_is_read(demo, monkeypatch):
    service, _ = demo

    def forbidden(*_args, **_kwargs):
        pytest.fail("the inventory was read before an allow")

    monkeypatch.setattr(secondary, "_ids", forbidden)
    for principal in (ADVISOR, ENGINEER):
        responses = []
        for arguments in (WINDOW, {**WINDOW, "date_from": "2025-01-01"}):
            result = await service.execute(principal, NOW, "feed_health", arguments)
            assert result["error"]["code"] == "NOT_AUTHORIZED" and result["data"] is None
            responses.append(comparable(result))
        assert responses[0]["error"] == responses[1]["error"]
        with read_snapshot(service.database) as connection:
            decisions = [(row[0], row[1]) for row in connection.execute(
                "SELECT action,resources FROM decisions WHERE principal_id=?", (principal.id,))]
        assert set(decisions) == {("feed_runs", "[]")}


# --- O2: feed_run_impact --------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_failed_snapshot_run_lists_every_manifest_account_as_affected(demo):
    service, _ = demo
    run = run_id(service.database, "PAS-B", "pas_snapshots", "2026-10-15")
    for principal, projection in ((OPS, "no_pii"), (COMPLIANCE, "full")):
        result = await service.execute(principal, NOW, "feed_run_impact",
                                       {"run_id": run, "known_as_of": CUTOFF})
        assert result["error"] is None and result["status"] == "COMPLETE", result
        data = result["data"]
        assert (result["as_of"], result["valuation_date"], result["known_as_of"]) == (
            "2026-10-15", "2026-10-15", CUTOFF), "the run's business date is the valuation date"
        assert (data["status"], data["upstream_status"], data["file_received"],
                data["account_attribution"]) == ("FAILED", "FAILED", False, "MANIFEST")
        assert (data["expected_account_count"], data["committed_account_count"],
                data["affected_account_count"], data["affected_household_count"],
                data["unattributed_account_count"]) == (15, 0, 15, 14, 0)
        assert data["affected_account_count"] != 0, "a failed run never shows zero affected"
        accounts = data["affected_accounts"]
        assert (accounts["item_count"], accounts["total_item_count"]) == (15, 15)
        assert all(item["reason"] == "ACCOUNT_MANIFEST_MISMATCH"
                   and item["household_attribution"] == "RESOLVED"
                   and item["account_id"] == item["manifest_account_id"]
                   for item in accounts["items"])
        ids = [item["account_id"] for item in accounts["items"]]
        assert ids == sorted(ids)
        households = [item["household_id"] for item in data["affected_households"]["items"]]
        assert households == sorted({item["household_id"] for item in accounts["items"]})
        assert len(households) == 14
        # Recorded: the active households and their accounts; the run's receipt and the CRM
        # account and reporting records used for attribution.
        action, recorded_projection, scopes, account_scope, handles = recorded(
            service.database, result["result_id"])
        assert (action, recorded_projection) == ("feed_runs", projection)
        assert account_scope is not None and account_scope["accounts"]
        assert set(households) <= set(scopes)
        assert {item["account_id"] for item in account_scope["accounts"]} >= set(ids)
        with read_snapshot(service.database) as connection:
            receipt = connection.execute("SELECT receipt_source_id FROM feed_runs WHERE id=?",
                                         (run,)).fetchone()[0]
        (sources,) = handles
        assert receipt in sources and len(sources) == 1 + 15 + 15


@pytest.mark.anyio
async def test_a_feed_without_per_account_availability_has_null_counts_not_zero(demo):
    service, _ = demo
    for system, feed in (("CRM", "crm"), ("CUSTODY-B", "prices"), ("FX", "fx")):
        result = await service.execute(OPS, NOW, "feed_run_impact", {
            "run_id": run_id(service.database, system, feed, "2026-10-15")})
        data = result["data"]
        assert result["error"] is None and data["account_attribution"] == "NOT_ATTRIBUTABLE"
        for key in ("expected_account_count", "committed_account_count",
                    "affected_account_count", "affected_household_count",
                    "unattributed_account_count", "affected_accounts", "affected_households"):
            assert data[key] is None, key
        assert len(result["source_refs"]) == 1


@pytest.mark.anyio
async def test_an_account_the_manifest_omits_is_expected_and_affected(unit):
    """The unit fixture's PAS-A and custody manifests omit WC-AC-0003, which CRM manages."""
    service, _ = unit
    for system, feed in (("PAS-A", "pas_snapshots"), ("CUSTODY-A", "custody_snapshots")):
        result = await service.execute(OPS, NOW, "feed_run_impact", {
            "run_id": run_id(service.database, system, feed, "2026-09-30")})
        data = result["data"]
        assert result["error"] is None, result
        assert (data["expected_account_count"], data["committed_account_count"],
                data["affected_account_count"], data["affected_household_count"]) == (3, 2, 1, 1)
        assert data["affected_accounts"]["items"] == [{
            "account_id": "WC-AC-0003", "manifest_account_id": None,
            "reason": "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST", "household_id": "WC-HH-0003",
            "household_attribution": "RESOLVED"}]


@pytest.mark.anyio
async def test_any_other_stored_reason_is_returned_as_other(unit):
    service, _ = unit
    run = run_id(service.database, "PAS-A", "pas_snapshots", "2026-09-30")
    with write_transaction(service.database) as connection:
        connection.execute("UPDATE account_publications SET available=0,reason=? WHERE run_id=? "
                           "AND manifest_account_id='WC-AC-0001'", ("Private household 0001", run))
    result = await service.execute(OPS, NOW, "feed_run_impact", {"run_id": run})
    reasons = {item["manifest_account_id"]: item["reason"]
               for item in result["data"]["affected_accounts"]["items"]}
    assert reasons == {"WC-AC-0001": "OTHER", None: "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST"}
    assert "Private household" not in canonical(result)


def test_household_attribution_follows_the_crosswalk_and_the_reporting_fact():
    account = {"id": "WC-AC-1", "custodian": "A", "account_number": "1", "source_id": "S-A1"}
    views = {
        "account": FactView(values={"WC-AC-1": account, "WC-AC-2": {
            **account, "id": "WC-AC-2", "account_number": "2", "source_id": "S-A2"},
            "WC-AC-3": {**account, "id": "WC-AC-3", "account_number": "3", "source_id": "S-A3"},
            "WC-AC-4": {**account, "id": "WC-AC-4", "account_number": "4", "source_id": "S-A4"}}),
        "reporting": FactView(
            values={"WC-AC-1": {"household_id": "WC-HH-1", "source_id": "S-R1"},
                    "WC-AC-4": {"household_id": "WC-HH-9", "source_id": "S-R4"}},
            ambiguous={"WC-AC-2": [{"household_id": "WC-HH-1", "source_id": "S-R2a"},
                                   {"household_id": "WC-HH-2", "source_id": "S-R2b"}]}),
    }
    mapping = {("A", "1"): "WC-AC-1", ("A", "2"): "WC-AC-2", ("A", "3"): "WC-AC-3",
               ("A", "4"): "WC-AC-4", ("A", "5"): None}
    sources: set[str] = set()
    scopes = {"WC-HH-1", "WC-HH-2"}
    assert secondary._attribute(("A", "1"), mapping, views, scopes, sources) == (
        "WC-AC-1", "WC-HH-1", "RESOLVED")
    assert secondary._attribute(("A", "2"), mapping, views, scopes, sources) == (
        "WC-AC-2", None, "AMBIGUOUS")
    assert secondary._attribute(("A", "3"), mapping, views, scopes, sources) == (
        "WC-AC-3", None, "HOUSEHOLD_UNAVAILABLE")
    assert secondary._attribute(("A", "4"), mapping, views, scopes, sources) == (
        "WC-AC-4", None, "HOUSEHOLD_UNAVAILABLE"), "never a household outside the scope"
    assert secondary._attribute(("A", "5"), mapping, views, scopes, sources) == (
        None, None, "CROSSWALK_UNAVAILABLE")
    assert sources == {"S-A1", "S-R1", "S-A2", "S-R2a", "S-R2b", "S-A3", "S-A4", "S-R4"}


@pytest.mark.anyio
async def test_unknown_and_later_runs_are_one_not_found_for_an_allowed_caller(demo):
    service, _ = demo
    run = run_id(service.database, "PAS-B", "pas_snapshots", "2026-10-15")
    later = await service.execute(OPS, NOW, "feed_run_impact",
                                  {"run_id": run, "known_as_of": "2026-10-15T21:00:00Z"})
    unknown = await service.execute(OPS, NOW, "feed_run_impact",
                                    {"run_id": "RUN-" + "0" * 64,
                                     "known_as_of": "2026-10-15T21:00:00Z"})
    assert later["error"] == unknown["error"] and later["error"]["code"] == "NOT_FOUND"
    assert comparable(later) == comparable(unknown)


@pytest.mark.anyio
async def test_impact_denial_is_paired_for_an_existing_and_an_unknown_run(demo, monkeypatch):
    service, _ = demo
    run = run_id(service.database, "PAS-B", "pas_snapshots", "2026-10-15")

    def forbidden(*_args, **_kwargs):
        pytest.fail("households were read before an allow")

    monkeypatch.setattr(secondary, "household_views", forbidden)
    monkeypatch.setattr(secondary, "_ids", forbidden)
    for principal in (ADVISOR, Principal("ADV-03", "advisor"), ENGINEER):
        responses = []
        for identifier in (run, "RUN-" + "0" * 64):
            result = await service.execute(principal, NOW, "feed_run_impact",
                                           {"run_id": identifier})
            assert result["error"]["code"] == "NOT_AUTHORIZED" and result["data"] is None
            assert not result["source_refs"]
            responses.append(comparable(result))
        assert responses[0] == responses[1], principal
        with read_snapshot(service.database) as connection:
            decisions = {(row[0], row[1]) for row in connection.execute(
                "SELECT action,resources FROM decisions WHERE principal_id=?", (principal.id,))}
        assert decisions == {("feed_runs", "[]")}


@pytest.mark.anyio
async def test_impact_redisclosure_reauthorizes_the_recorded_action(demo):
    service, policy = demo
    run = run_id(service.database, "PAS-B", "pas_snapshots", "2026-10-15")
    impact = await service.execute(OPS, NOW, "feed_run_impact", {"run_id": run})
    _, _, scopes, _, _ = recorded(service.database, impact["result_id"])
    handle = {"reference": impact["source_refs"][0]}
    policy.calls.clear()
    opened = await service.execute(OPS, NOW, "get_data_provenance", handle)
    assert opened["error"] is None and opened["data"]["total_item_count"] == 31
    assert ("feed_runs", scopes) in policy.calls, "the recorded action over the whole scope"
    assert opened["data"]["items"], "the receipt and CRM records, projected"
    # Compliance reopens it, under the origin's no-PII projection.
    reopened = await service.execute(COMPLIANCE, NOW, "get_data_provenance", handle)
    assert reopened["error"] is None
    foreign = await service.execute(ADVISOR, NOW, "get_data_provenance", handle)
    unknown = await service.execute(ADVISOR, NOW, "get_data_provenance",
                                    {"reference": "h_unknown"})
    assert foreign["error"] == unknown["error"] and foreign["error"]["code"] == "NOT_AUTHORIZED"
    # Once the recorded action no longer allows, the handle no longer opens.
    policy.response = _refusing(policy.response, "feed_runs")
    refused = await service.execute(OPS, NOW, "get_data_provenance", handle)
    assert refused["error"]["code"] == "NOT_AUTHORIZED" and refused["data"] is None


@pytest.mark.anyio
async def test_both_results_reauthorize_feed_runs_over_their_scope_on_reuse(demo, monkeypatch):
    """A recorded health or impact result, cited again, re-authorizes `feed_runs` first."""
    service, policy = demo
    run = run_id(service.database, "PAS-B", "pas_snapshots", "2026-10-15")
    conversation = service.start_conversation(OPS, NOW)
    for tool, arguments in (("feed_health", WINDOW), ("feed_run_impact", {"run_id": run})):
        result = await service.execute(OPS, NOW, tool, arguments, conversation_id=conversation)
        assert result["error"] is None
        _, _, scopes, _, _ = recorded(service.database, result["result_id"])
        operation = Operation(OPS, NOW, "0" * 32, "r_check", "submit_answer", conversation)
        with read_snapshot(service.database) as connection:
            policy.calls.clear()
            await service.resolve_result(operation, connection, result["result_id"])
            assert ("feed_runs", scopes) in policy.calls
            assert operation.projection == "no_pii"
            # Once feed_runs no longer allows the operations role, reuse is refused.
            monkeypatch.setattr(policy, "response", _refusing(policy.response, "feed_runs"))
            refused = Operation(OPS, NOW, "0" * 32, "r_check2", "submit_answer", conversation)
            with pytest.raises(RequestFailure) as denied:
                await service.resolve_result(refused, connection, result["result_id"])
            assert denied.value.error.code == "NOT_AUTHORIZED"
            monkeypatch.undo()


def _refusing(response, action: str):
    def decide(principal, requested, resources, revision, filtered=False):
        decision = response(principal, requested, resources, revision, filtered)
        if requested != action:
            return decision
        return PolicyDecision(allow=False, allowed_ids=[], projection=decision.projection,
                              reason="NO_CURRENT_GRANT", policy_revision=decision.policy_revision,
                              entitlement_revision=decision.entitlement_revision)
    return decide


# --- No-PII projection, and the real policy ---------------------------------------------------


@pytest.mark.anyio
async def test_no_name_or_title_reaches_either_contract_for_any_role(demo):
    service, _ = demo
    private = private_strings(service.database)
    run = run_id(service.database, "PAS-B", "pas_snapshots", "2026-10-15")
    for principal in (OPS, COMPLIANCE):
        for tool, arguments in (("feed_health", WINDOW), ("feed_run_impact", {"run_id": run})):
            text = canonical(await service.execute(principal, NOW, tool, arguments))
            for value in private:
                assert value not in text and json.dumps(value)[1:-1] not in text, (tool, value)


@pytest.mark.anyio
async def test_real_policy_rest_routes_for_every_role(tmp_path, opa_server):
    async with app_client(tmp_path, opa_server) as (app, client, database):
        run = run_id(database, "PAS-B", "pas_snapshots", "2026-10-15")
        arguments = {"feed_health": {"date_from": "2026-10-12", "date_to": "2026-10-16"},
                     "feed_run_impact": {"run_id": run}}
        for persona, allowed in (("OPS-01", True), ("COMP-01", True), ("ADV-01", False),
                                 ("ENG-01", False)):
            for tool in TOOLS:
                response = await client.post(f"/api/{tool}", json=arguments[tool],
                                             headers=bearer(app, persona))
                envelope = response.json()
                decisions = audit(database, envelope, persona)
                if allowed:
                    assert response.status_code == 200 and envelope["error"] is None, envelope
                    assert all(row["allowed"] for row in decisions)
                    assert {row["action"] for row in decisions} == {"feed_runs"}
                else:
                    assert response.status_code == 403, (persona, tool, envelope)
                    assert envelope["error"]["message"] == (
                        "Caller is not authorized for the requested resource")
                    assert envelope["data"] is None and not envelope["source_refs"]
                    assert [(row["action"], row["resources"]) for row in decisions] == [
                        ("feed_runs", "[]")]
            unknown = await client.post("/api/feed_run_impact",
                                        json={"run_id": "RUN-" + "0" * 64},
                                        headers=bearer(app, persona))
            known = await client.post("/api/feed_run_impact", json={"run_id": run},
                                      headers=bearer(app, persona))
            if not allowed:
                assert unknown.status_code == known.status_code == 403
                assert unknown.json()["error"] == known.json()["error"]
            else:
                assert unknown.status_code == 404
        impact = (await client.post("/api/feed_run_impact", json={"run_id": run},
                                    headers=bearer(app, "OPS-01"))).json()
        assert impact["data"]["affected_account_count"] == 15
        health = (await client.post("/api/feed_health", json={
            "date_from": "2026-10-16", "date_to": "2026-10-15"},
            headers=bearer(app, "OPS-01")))
        assert health.status_code == 422
        anonymous = await client.post("/api/feed_health", json=arguments["feed_health"])
        assert anonymous.status_code == 401
