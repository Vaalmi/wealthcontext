"""The compliance contracts of ADR 0009 sections 5 to 7 (build step 5, C1 to C3), in secondary.py.

`decision_log_page`, `household_receipts` and `trace_spans` are REST routes for the
workbench only. Each takes its preflight on the empty scope before it decodes a cursor,
resolves an identifier or opens the span export; unknown and forbidden identifiers get one
identical response; each result is recorded as existing surfaces record theirs.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
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
)
from wealthcontext import secondary
from wealthcontext.bootstrap import bootstrap_history
from wealthcontext.clock import FrozenClock
from wealthcontext.context_service import ContextService, Operation, RequestFailure
from wealthcontext.conversation import MODEL_TOOLS
from wealthcontext.database import migrate, write_transaction
from wealthcontext.harness import tool_specs
from wealthcontext.identity import Principal
from wealthcontext.mcp_server import ARGUMENT_MODELS
from wealthcontext.policy import PolicyDecision
from wealthcontext.storage import canonical, migrate_business, read_snapshot
from wealthcontext.surface_contracts import SURFACE_ARGUMENT_MODELS, SURFACE_OUTPUT_MODELS
from wealthcontext.telemetry import create_provider

ROOT = Path(__file__).resolve().parents[1]
ENGINEER = Principal("ENG-01", "engineer")
OTHER_ADVISOR = Principal("ADV-02", "advisor")
TOOLS = ("decision_log_page", "household_receipts", "trace_spans")
NEW_ROLES = {
    "household_receipts_preflight": {"compliance"},
    "household_receipts": {"compliance"},
    "trace_spans_preflight": {"advisor", "compliance"},
    "trace_spans": {"advisor", "compliance"},
}
# The unit fixture has no snapshot rule; these tests read snapshots to make traces.
EXACT_ONE = {"snapshot": {"advisor", "compliance"}}


class CompliancePolicy(SurfacePolicy):
    """The unit fixture's decisions, plus ADR 0009's four new actions as the matrix sets them."""

    def response(self, principal, action, resources, revision, filtered=False):
        roles = NEW_ROLES.get(action, EXACT_ONE.get(action))
        if roles is None:
            return super().response(principal, action, resources, revision, filtered)
        self.calls.append((action, resources))
        covered = (set(resources) <= self.allowed if principal.role == "advisor" else True)
        allow = principal.role in roles and covered
        if action.endswith("_preflight") and resources:
            allow = False
        if action in {"household_receipts", "snapshot"} and len(resources) != 1:
            allow = False
        return PolicyDecision(
            allow=allow, allowed_ids=list(resources) if allow else [],
            projection="no_pii" if principal.role == "ops" else "full",
            reason="CURRENT_GRANT" if allow else "NO_CURRENT_GRANT",
            policy_revision=self.expected_revision, entitlement_revision=revision,
        )


def comparable(envelope: dict) -> dict:
    return {key: value for key, value in envelope.items()
            if key not in {"trace_id", "result_id", "calculated_at"}}


def recorded(database: Path, result_id: str) -> tuple[str, str, list[str], dict | None, list]:
    with read_snapshot(database) as connection:
        access = connection.execute("SELECT action,projection FROM result_access "
                                    "WHERE result_id=?", (result_id,)).fetchone()
        scopes = [row[0] for row in connection.execute(
            "SELECT household_id FROM result_scopes WHERE result_id=? ORDER BY household_id",
            (result_id,))]
        accounts = connection.execute("SELECT payload FROM result_account_scopes "
                                      "WHERE result_id=?", (result_id,)).fetchone()
        handles = [json.loads(row[0]) for row in connection.execute(
            "SELECT source_ids FROM provenance_handles WHERE result_id=?", (result_id,))]
    return (access["action"], access["projection"], scopes,
            json.loads(accounts[0]) if accounts else None, handles)


@pytest.fixture
def unit(tmp_path: Path):
    database = tmp_path / "surfaces.sqlite"
    prepare(database)
    spans = tmp_path / "trace.jsonl"
    provider = create_provider(spans)
    policy = CompliancePolicy()
    yield ContextService(database, policy, provider.get_tracer("test"), span_export=spans), policy
    provider.shutdown()


@pytest.fixture(scope="module")
def fixture_database(tmp_path_factory) -> Path:
    """The generated demo history, once: the C2 preset household and its two cutoffs."""
    directory = tmp_path_factory.mktemp("fixture")
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
    policy = CompliancePolicy()
    yield ContextService(database, policy, provider.get_tracer("test")), policy
    provider.shutdown()


# --- Shared: off MCP, schemas, matrix rows -------------------------------------------------


def test_they_stay_off_mcp_the_harness_and_the_evaluation_map():
    rows = {row.get("tool"): row for row in
            yaml.safe_load((ROOT / "policies/matrix.yaml").read_text())["routes"]}
    expected = {"decision_log_page": "view_decision_log",
                "household_receipts": "household_receipts", "trace_spans": "trace_spans"}
    for tool in TOOLS:
        assert tool not in ARGUMENT_MODELS and tool not in MODEL_TOOLS
        assert tool not in {spec.name for spec in tool_specs()}
        assert tool not in SURFACE_ARGUMENT_MODELS, "evaluation.py reads that map (Unit B)"
        assert tool in SURFACE_OUTPUT_MODELS
        row = rows[tool]
        assert (row["method"], row["path"], row["action"], row["business"]) == (
            "POST", f"/api/{tool}", expected[tool], True)
    actions = yaml.safe_load((ROOT / "policies/matrix.yaml").read_text())["actions"]
    assert actions["household_receipts_preflight"]["roles"] == {"compliance": "full"}
    assert actions["household_receipts"]["roles"] == {"compliance": "full"}
    assert actions["household_receipts"]["household_count"] == 1
    assert actions["trace_spans_preflight"]["roles"] == {"advisor": "full", "compliance": "full"}
    assert actions["trace_spans"]["roles"] == {"advisor": "full", "compliance": "full"}
    assert actions["trace_spans"]["advisor_requires_coverage"] is True


def test_published_schemas_close_their_shapes():
    page = json.loads((ROOT / "schemas/v1/decision-log-page.json").read_text())["$defs"]
    assert page["DecisionLogPageData"]["properties"]["items"]["maxItems"] == 20
    assert page["DecisionLogPageData"]["additionalProperties"] is False
    assert page["RevisionFacts"]["properties"]["facts"]["maxItems"] == 100
    assert page["RevisionFacts"]["properties"]["label"]["const"] == "FACTS_AT_STORED_REVISION"
    receipts = json.loads((ROOT / "schemas/v1/household-receipts.json").read_text())["$defs"]
    assert receipts["HouseholdReceiptsData"]["properties"]["items"]["maxItems"] == 20
    spans = json.loads((ROOT / "schemas/v1/trace-spans.json").read_text())["$defs"]
    span = spans["SpanItem"]
    assert set(span["properties"]) == {"span_id", "parent_span_id", "parent_omitted", "name",
                                       "start_time", "duration_ms", "status_code"}
    assert span["additionalProperties"] is False
    assert spans["TraceSpansData"]["properties"]["items"]["maxItems"] == 200
    arguments = json.loads((ROOT / "schemas/v1/decision-log-page-arguments.json").read_text())
    assert set(arguments["properties"]) == {"principal_id", "action", "outcome", "recorded_from",
                                            "recorded_before", "household_id", "cursor"}
    assert not (ROOT / "schemas/v1/trace-spans-arguments.json").exists(), (
        "trace_spans reuses trace-arguments.json")


# --- C1: decision_log_page -----------------------------------------------------------------


async def _decisions(service: ContextService, count: int, at: datetime) -> None:
    for offset in range(count):
        result = await service.execute(ADVISOR, at + timedelta(seconds=offset),
                                       "get_metric_definition", {"metric_id": "catalog"})
        assert result["error"] is None


@pytest.mark.anyio
async def test_refused_roles_get_one_denial_before_the_cursor_is_decoded(unit, monkeypatch):
    service, _ = unit
    # A real cursor, issued to compliance, is refused like a malformed one (step 7).
    await _decisions(service, 24, NOW - timedelta(hours=1))
    issued = (await service.execute(COMPLIANCE, NOW, "decision_log_page", {}))
    assert issued["error"] is None and issued["data"]["next_cursor"]
    real = issued["data"]["next_cursor"]

    def forbidden(*_args):
        pytest.fail("a cursor was decoded before an allow")

    monkeypatch.setattr(secondary, "open_cursor", forbidden)
    refusals = []
    for principal in (ADVISOR, OPS, ENGINEER):
        for arguments in ({}, {"cursor": "abc.def"}, {"cursor": real},
                          {"household_id": "WC-HH-0001"},
                          {"household_id": "WC-HH-9999"}):
            result = await service.execute(principal, NOW, "decision_log_page", arguments)
            assert result["error"]["code"] == "NOT_AUTHORIZED" and result["data"] is None
            refusals.append({key: value for key, value in comparable(result).items()
                             if key != "policy_revision"})
    assert all(item == refusals[0] for item in refusals)


@pytest.mark.anyio
async def test_the_cursor_holds_while_new_decisions_arrive(unit):
    service, _ = unit
    await _decisions(service, 24, NOW - timedelta(hours=1))
    first = await service.execute(COMPLIANCE, NOW, "decision_log_page", {})
    data = first["data"]
    assert first["status"] == "COMPLETE" and first["error"] is None, first
    with read_snapshot(service.database) as connection:
        before = connection.execute("SELECT COUNT(*) FROM decisions WHERE rowid<"
                                    "(SELECT MIN(rowid) FROM decisions WHERE trace_id=?)",
                                    (first["trace_id"],)).fetchone()[0]
    assert data["total_item_count"] == before
    assert (data["item_count"], data["truncated"]) == (20, True)
    assert data["next_cursor"] and data["browse_started_at"] == "2026-11-01T12:00:00Z"
    # Decisions arrive between pages, some recorded earlier than the browse began.
    await _decisions(service, 5, NOW - timedelta(minutes=30))
    second = await service.execute(COMPLIANCE, NOW + timedelta(minutes=5),
                                   "decision_log_page", {"cursor": data["next_cursor"]})
    page = second["data"]
    assert second["error"] is None, second
    assert page["total_item_count"] == data["total_item_count"], "the count is the watermark's"
    assert page["browse_started_at"] == data["browse_started_at"]
    seen = [item["id"] for item in data["items"]] + [item["id"] for item in page["items"]]
    assert len(seen) == len(set(seen)) == before == page["item_count"] + 20
    assert page["next_cursor"] is None
    with read_snapshot(service.database) as connection:
        expected = [row[0] for row in connection.execute(
            "SELECT id FROM decisions WHERE rowid<(SELECT MIN(rowid) FROM decisions "
            "WHERE trace_id=?) ORDER BY recorded_at DESC,id ASC", (first["trace_id"],))]
        browse_own = {row[0] for row in connection.execute(
            "SELECT id FROM decisions WHERE trace_id IN (?,?)",
            (first["trace_id"], second["trace_id"]))}
    assert seen == expected, "pages run newest first then by ID, without gaps or repeats"
    assert not browse_own & set(seen), "the browse's own decisions never enter its list"
    # A new browse sees what arrived.
    fresh = await service.execute(COMPLIANCE, NOW + timedelta(minutes=6),
                                  "decision_log_page", {})
    assert fresh["data"]["total_item_count"] > data["total_item_count"]


@pytest.mark.anyio
async def test_every_bad_cursor_gets_the_same_invalid_argument(unit, tmp_path):
    service, policy = unit
    await _decisions(service, 21, NOW - timedelta(hours=1))
    filters = {"action": "metric_definition"}
    issued = await service.execute(COMPLIANCE, NOW, "decision_log_page", filters)
    cursor = issued["data"]["next_cursor"]
    assert cursor
    body, _, tag = cursor.partition(".")
    other_compliance = Principal("COMP-02", "compliance")
    restarted = ContextService(service.database, policy, service.tracer)
    cases = [
        (service, COMPLIANCE, {**filters, "cursor": "AAAA.BBBB"}),          # malformed
        (service, COMPLIANCE, {**filters, "cursor": body + "." + tag[::-1]}),  # forged
        (service, COMPLIANCE, {**filters, "cursor": body[:-2] + "." + tag}),   # tampered
        (service, other_compliance, {**filters, "cursor": cursor}),       # another principal
        (service, COMPLIANCE, {"cursor": cursor}),                        # different filters
        (service, COMPLIANCE, {**filters, "outcome": "allow", "cursor": cursor}),
        (restarted, COMPLIANCE, {**filters, "cursor": cursor}),           # before a restart
    ]
    responses = []
    for owner, principal, arguments in cases:
        result = await owner.execute(principal, NOW, "decision_log_page", arguments)
        assert result["error"]["code"] == "INVALID_ARGUMENT", (arguments, result)
        assert result["data"] is None
        responses.append(comparable(result))
    assert all(item == responses[0] for item in responses)
    # Equal filters written with another offset are the same filters.
    shifted = await service.execute(COMPLIANCE, NOW, "decision_log_page", {
        "recorded_from": "2026-10-01T00:00:00Z", "cursor": None})
    second = shifted["data"]["next_cursor"]
    again = await service.execute(COMPLIANCE, NOW, "decision_log_page", {
        "recorded_from": "2026-09-30T20:00:00-04:00", "cursor": second})
    assert again["error"] is None, again
    assert (await service.execute(COMPLIANCE, NOW, "decision_log_page", {
        **filters, "cursor": cursor}))["error"] is None


@pytest.mark.anyio
async def test_filters_and_the_household_rule(unit):
    service, policy = unit
    policy.allowed = {"WC-HH-0001"}
    # A filtered allow: search lists all three candidates, authorizes WC-HH-0001 only.
    search = await service.execute(ADVISOR, NOW, "list_households", {})
    # An exact allow and an exact denial.
    snapshot = await service.execute(ADVISOR, NOW, "get_household_snapshot",
                                     {"household_id": "WC-HH-0001", "as_of": "2026-09-30"})
    denied = await service.execute(ADVISOR, NOW, "get_household_snapshot",
                                   {"household_id": "WC-HH-0002", "as_of": "2026-09-30"})
    assert search["error"] is None and snapshot["error"] is None
    assert denied["error"]["code"] == "NOT_AUTHORIZED"
    later = NOW + timedelta(minutes=1)

    async def ids(arguments: dict) -> set[str]:
        result = await service.execute(COMPLIANCE, later, "decision_log_page", arguments)
        assert result["error"] is None, result
        assert result["data"]["total_item_count"] == result["data"]["item_count"]
        return {item["trace_id"] for item in result["data"]["items"]}

    two = await ids({"household_id": "WC-HH-0002"})
    assert denied["trace_id"] in two
    assert search["trace_id"] not in two, "a filtered allow matches its authorized households"
    one = await ids({"household_id": "WC-HH-0001"})
    assert {search["trace_id"], snapshot["trace_id"]} <= one and denied["trace_id"] not in one
    assert await ids({"household_id": "WC-HH-0003"}) == set()
    assert await ids({"outcome": "deny", "principal_id": "ADV-01"}) == {denied["trace_id"]}
    assert search["trace_id"] in await ids({"action": "search_households"})
    assert await ids({"recorded_before": "2026-11-01T12:00:00Z"}) == set()
    page = await service.execute(COMPLIANCE, later, "decision_log_page",
                                 {"principal_id": "ADV-01"})
    items = {item["action"]: item for item in page["data"]["items"]}
    assert items["search_households"]["resources"] == ["WC-HH-0001", "WC-HH-0002",
                                                       "WC-HH-0003"]
    assert items["search_households"]["authorized_resources"] == ["WC-HH-0001"]
    deny = [item for item in page["data"]["items"] if not item["allowed"]]
    assert deny and all(item["authorized_resources"] is None for item in deny)
    # Recorded scope: every household any matching decision names, before pagination.
    action, projection, scopes, accounts, handles = recorded(service.database,
                                                             page["result_id"])
    assert (action, projection, scopes) == (
        "view_decision_log", "full", ["WC-HH-0001", "WC-HH-0002", "WC-HH-0003"])
    assert accounts is not None and accounts["accounts"] == []
    assert accounts["household_ids"] == scopes
    assert handles == [] and page["source_refs"] == []
    invalid = await service.execute(COMPLIANCE, later, "decision_log_page", {
        "recorded_from": "2026-11-02T00:00:00Z", "recorded_before": "2026-11-01T00:00:00Z"})
    assert invalid["error"]["code"] == "INVALID_ARGUMENT"
    assert (await service.execute(ADVISOR, later, "decision_log_page", {"outcome": "maybe"}))[
        "error"]["code"] == "INVALID_ARGUMENT", "shape is checked first, for any caller"


OUT_OF_UTC_YEARS = ["0001-01-01T00:00:00+00:01", "9999-12-31T23:59:59-00:01"]


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["recorded_from", "recorded_before"])
@pytest.mark.parametrize("instant", OUT_OF_UTC_YEARS)
async def test_a_filter_instant_outside_utc_years_is_a_recorded_invalid_argument(
    unit, field, instant,
):
    """SOL-UX2-R1-FILTER-001: the offset moves the instant outside years 1 to 9999."""
    service, _ = unit
    out = await service.execute(COMPLIANCE, NOW, "decision_log_page", {field: instant})
    assert out["error"]["code"] == "INVALID_ARGUMENT" and out["data"] is None
    with read_snapshot(service.database) as connection:
        assert connection.execute("SELECT 1 FROM results WHERE id=?",
                                  (out["result_id"],)).fetchone(), "durably recorded"
    # The representable endpoints, and any other caller, still get the same answer shape.
    edge = "0001-01-01T00:00:00Z" if field == "recorded_from" else "9999-12-31T23:59:59Z"
    assert (await service.execute(COMPLIANCE, NOW, "decision_log_page",
                                  {field: edge}))["error"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["recorded_from", "recorded_before"])
@pytest.mark.parametrize("instant", OUT_OF_UTC_YEARS)
async def test_a_filter_instant_outside_utc_years_is_refused_through_rest(
    tmp_path, opa_server, field, instant,
):
    async with app_client(tmp_path, opa_server) as (app, client, database):
        response = await client.post("/api/decision_log_page", json={field: instant},
                                     headers=bearer(app, "COMP-01"))
        envelope = response.json()
        assert envelope["error"]["code"] == "INVALID_ARGUMENT", envelope
        assert envelope["data"] is None
        with read_snapshot(database) as connection:
            assert connection.execute("SELECT 1 FROM results WHERE id=?",
                                      (envelope["result_id"],)).fetchone()


@pytest.mark.anyio
async def test_decision_log_page_redisclosure_reauthorizes_the_recorded_scope(unit,
                                                                              monkeypatch):
    """SOL-UX2-R1-REDISCLOSURE-001: a recorded C1 page, reused, re-authorizes its scope."""
    service, policy = unit
    search = await service.execute(ADVISOR, NOW, "list_households", {})
    denied = await service.execute(ADVISOR, NOW, "get_household_snapshot",
                                   {"household_id": "WC-HH-0002", "as_of": "2026-09-30"})
    assert search["error"] is None and denied["error"]["code"] == "NOT_AUTHORIZED"
    later = NOW + timedelta(minutes=1)
    conversation = service.start_conversation(COMPLIANCE, later)
    page = await service.execute(COMPLIANCE, later, "decision_log_page", {},
                                 conversation_id=conversation)
    assert page["error"] is None
    action, _, scopes, _, _ = recorded(service.database, page["result_id"])
    assert action == "view_decision_log"
    assert scopes == ["WC-HH-0001", "WC-HH-0002", "WC-HH-0003"]
    with read_snapshot(service.database) as connection:
        policy.calls.clear()
        operation = Operation(COMPLIANCE, later, "0" * 32, "r_check", "submit_answer",
                              conversation)
        reused = await service.resolve_result(operation, connection, page["result_id"])
        assert reused == page
        assert ("view_decision_log", scopes) in policy.calls, "over the whole recorded scope"
        # Another principal cannot reuse it, and gets the unknown result's refusal.
        for principal in (ADVISOR, OPS):
            foreign = Operation(principal, later, "0" * 32, "r_foreign", "submit_answer",
                                conversation)
            with pytest.raises(RequestFailure) as refused:
                await service.resolve_result(foreign, connection, page["result_id"])
            assert refused.value.error.code == "NOT_AUTHORIZED"
        # Once view_decision_log no longer allows compliance, reuse is refused.
        original = policy.response

        def refusing(principal, requested, resources, revision, filtered=False):
            decision = original(principal, requested, resources, revision, filtered)
            if requested != "view_decision_log":
                return decision
            return PolicyDecision(
                allow=False, allowed_ids=[], projection=decision.projection,
                reason="NO_CURRENT_GRANT", policy_revision=decision.policy_revision,
                entitlement_revision=decision.entitlement_revision)

        monkeypatch.setattr(policy, "response", refusing)
        operation = Operation(COMPLIANCE, later, "0" * 32, "r_check2", "submit_answer",
                              conversation)
        with pytest.raises(RequestFailure) as refused:
            await service.resolve_result(operation, connection, page["result_id"])
        assert refused.value.error.code == "NOT_AUTHORIZED"
    monkeypatch.undo()
    # Reading the page's own trace re-authorizes trace_scope over the recorded scope.
    policy.calls.clear()
    traced = await service.execute(COMPLIANCE, later, "trace", {"trace_id": page["trace_id"]})
    assert traced["error"] is None
    assert any(action == "trace_scope" and set(scopes) <= set(resources)
               for action, resources in policy.calls), policy.calls


@pytest.mark.anyio
async def test_entitlement_facts_are_those_at_the_stored_revision(unit):
    service, _ = unit
    await _decisions(service, 1, NOW - timedelta(minutes=1))
    with write_transaction(service.database) as connection:
        connection.execute("INSERT INTO entitlement_revisions VALUES (7, ?)",
                           ("2026-10-31T12:00:00.000000+00:00",))
        connection.executemany("INSERT INTO entitlement_facts VALUES (?,?,?,?,?,?)", [
            (7, "advisor_covers", "ADV-01", f"WC-HH-{number:04}", "2026-09-14", "9999-12-31")
            for number in range(1, 106)])
    await _decisions(service, 1, NOW)
    result = await service.execute(COMPLIANCE, NOW + timedelta(minutes=1),
                                   "decision_log_page", {})
    revisions = {item["revision"]: item for item in result["data"]["revisions"]}
    assert result["data"]["revision_count"] == len(revisions) == 2
    stored = revisions[7]
    assert stored["label"] == "FACTS_AT_STORED_REVISION"
    assert (stored["item_count"], stored["total_item_count"], stored["truncated"]) == (
        100, 105, True)
    assert stored["recorded_at"] == "2026-10-31T12:00:00Z"
    assert stored["facts"][0] == {"kind": "advisor_covers", "subject": "ADV-01",
                                  "object": "WC-HH-0001", "valid_from": "2026-09-14",
                                  "valid_to": "9999-12-31"}
    cited = {item["entitlement_revision"] for item in result["data"]["items"]}
    assert set(revisions) == cited - {None}


# --- C2: household_receipts ----------------------------------------------------------------


RECEIPTS = {"household_id": "WC-HH-0009", "as_of": "2026-09-30"}
EARLIER = "2026-10-06T13:00:00Z"
LATER = "2026-10-31T16:00:00Z"


@pytest.mark.anyio
async def test_receipts_group_the_snapshot_cited_records_by_first_run(demo):
    service, _ = demo
    lists = {}
    for cutoff, cited in ((EARLIER, 27), (LATER, 29)):
        arguments = {**RECEIPTS, "known_as_of": cutoff}
        receipts = await service.execute(COMPLIANCE, NOW, "household_receipts", arguments)
        snapshot = await service.execute(COMPLIANCE, NOW, "get_household_snapshot", arguments)
        data = receipts["data"]
        assert receipts["error"] is None, receipts
        assert (data["cited_record_count"], data["records_without_run_count"]) == (cited, 0)
        assert sum(item["record_count"] for item in data["items"]) == cited
        assert data["total_item_count"] == data["item_count"] <= 20
        assert (receipts["status"], receipts["quality_flags"]) == (
            snapshot["status"], snapshot["quality_flags"]), "every warning is disclosed with it"
        assert (receipts["as_of"], receipts["valuation_date"], receipts["known_as_of"]) == (
            snapshot["as_of"], snapshot["valuation_date"], snapshot["known_as_of"])
        assert "assets_usd" not in canonical(data) and "_usd" not in canonical(data)
        order = [(item["recorded_at"], item["run_id"]) for item in data["items"]]
        assert order == sorted(order, key=lambda item: (-datetime.fromisoformat(
            item[0]).timestamp(), item[1]))
        # Recorded scope: the household, its accounts, and the cited records as sources.
        action, projection, scopes, accounts, handles = recorded(service.database,
                                                                 receipts["result_id"])
        _, _, _, snapshot_accounts, snapshot_handles = recorded(service.database,
                                                                snapshot["result_id"])
        assert (action, projection, scopes) == ("household_receipts", "full", ["WC-HH-0009"])
        assert accounts is not None and snapshot_accounts is not None
        assert accounts["accounts"] == snapshot_accounts["accounts"] != []
        assert handles == snapshot_handles and len(handles[0]) == cited
        lists[cutoff] = {item["run_id"] for item in data["items"]}
    # A receipt in one list and not the other is found by run ID.
    assert lists[EARLIER] - lists[LATER] and lists[LATER] - lists[EARLIER]
    assert lists[EARLIER] & lists[LATER]


@pytest.mark.anyio
async def test_receipts_are_refused_before_any_lookup_and_paired(demo, monkeypatch):
    service, _ = demo

    def forbidden(*_args, **_kwargs):
        pytest.fail("the household was resolved before an allow")

    monkeypatch.setattr(secondary, "calculate_snapshot", forbidden)
    for principal in (ADVISOR, Principal("ADV-03", "advisor"), OPS, ENGINEER):
        responses = []
        for household in ("WC-HH-0009", "WC-HH-0001", "WC-HH-9999"):
            result = await service.execute(principal, NOW, "household_receipts",
                                           {**RECEIPTS, "household_id": household})
            assert result["error"]["code"] == "NOT_AUTHORIZED" and result["data"] is None
            assert not result["source_refs"] and not result["quality_flags"]
            responses.append(comparable(result))
        assert responses[0] == responses[1] == responses[2], principal
        with read_snapshot(service.database) as connection:
            actions = [row[0] for row in connection.execute(
                "SELECT action FROM decisions WHERE principal_id=?", (principal.id,))]
        assert set(actions) == {"household_receipts_preflight"}


@pytest.mark.anyio
async def test_receipts_errors_after_an_allow(demo):
    service, _ = demo
    unknown = await service.execute(COMPLIANCE, NOW, "household_receipts",
                                    {**RECEIPTS, "household_id": "WC-HH-9999"})
    assert unknown["error"]["code"] == "NOT_FOUND"
    out = await service.execute(COMPLIANCE, NOW, "household_receipts",
                                {**RECEIPTS, "as_of": "2025-01-01"})
    assert out["error"]["code"] == "AS_OF_OUT_OF_RANGE"


@pytest.mark.anyio
async def test_receipts_redisclosure_reauthorizes_the_recorded_action(demo):
    service, policy = demo
    receipts = await service.execute(COMPLIANCE, NOW, "household_receipts", RECEIPTS)
    handle = {"reference": receipts["source_refs"][0]}
    policy.calls.clear()
    opened = await service.execute(COMPLIANCE, NOW, "get_data_provenance", handle)
    assert opened["error"] is None and opened["data"]["total_item_count"] == 29
    assert ("household_receipts", ["WC-HH-0009"]) in policy.calls
    for principal in (ADVISOR, OPS):
        foreign = await service.execute(principal, NOW, "get_data_provenance", handle)
        unknown = await service.execute(principal, NOW, "get_data_provenance",
                                        {"reference": "h_unknown"})
        assert foreign["error"] == unknown["error"]
        assert foreign["error"]["code"] == "NOT_AUTHORIZED"
    # Once the recorded action no longer allows, the handle no longer opens.
    NEW_ROLES["household_receipts"] = set()
    try:
        refused = await service.execute(COMPLIANCE, NOW, "get_data_provenance", handle)
    finally:
        NEW_ROLES["household_receipts"] = {"compliance"}
    assert refused["error"]["code"] == "NOT_AUTHORIZED" and refused["data"] is None


# --- C3: trace_spans -----------------------------------------------------------------------


SNAPSHOT = {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}


def _later() -> datetime:
    """Spans carry wall-clock times; ask after they ended."""
    return datetime.now(UTC) + timedelta(seconds=1)


@pytest.mark.anyio
async def test_the_owner_gets_an_allowlisted_span_tree(unit):
    service, policy = unit
    snapshot = await service.execute(ADVISOR, NOW, "get_household_snapshot", SNAPSHOT)
    assert snapshot["error"] is None
    policy.calls.clear()
    result = await service.execute(ADVISOR, _later(), "trace_spans",
                                   {"trace_id": snapshot["trace_id"]})
    data = result["data"]
    assert result["error"] is None and result["status"] == "COMPLETE", result
    assert [call[0] for call in policy.calls] == ["trace_spans_preflight", "trace_spans"]
    assert policy.calls[1][1] == ["WC-HH-0001"]
    assert data["span_source"] == "EXPORT" and data["scan_truncated"] is False
    assert data["trace_id"] == snapshot["trace_id"]
    names = [item["name"] for item in data["items"]]
    assert sorted(names) == ["authz", "calc", "query", "tool"]
    roots = [item for item in data["items"] if item["parent_span_id"] is None]
    assert [item["name"] for item in roots] == ["tool"] and not roots[0]["parent_omitted"]
    by_id = {item["span_id"]: item for item in data["items"]}
    assert by_id[next(item["parent_span_id"] for item in data["items"]
                      if item["name"] == "calc")]["name"] == "query"
    assert all(item["status_code"] in {"UNSET", "OK", "ERROR"} for item in data["items"])
    assert all(isinstance(item["duration_ms"], str) for item in data["items"])
    text = canonical(result)
    assert "attributes" not in text and "wealthcontext.tool" not in text
    assert "get_household_snapshot" not in text and "telemetry" not in text
    # Recorded: the trace's households, no accounts, no sources.
    action, projection, scopes, accounts, handles = recorded(service.database,
                                                             result["result_id"])
    assert (action, projection, scopes, handles) == ("trace_spans", "full", ["WC-HH-0001"], [])
    assert accounts is not None and accounts["accounts"] == []
    # The bare, upper-case and 0x-prefixed forms name the same trace.
    for form in (snapshot["trace_id"].upper(), "0x" + snapshot["trace_id"]):
        again = await service.execute(ADVISOR, _later(), "trace_spans", {"trace_id": form})
        assert again["data"]["items"] == data["items"]
    # Compliance may read any trace.
    assert (await service.execute(COMPLIANCE, _later(), "trace_spans",
                                  {"trace_id": snapshot["trace_id"]}))["error"] is None


@pytest.mark.anyio
async def test_spans_are_refused_to_engineers_operations_others_and_revoked_advisors(
    unit, monkeypatch,
):
    service, policy = unit
    policy.allowed = {"WC-HH-0001", "WC-HH-0002"}
    owned = await service.execute(ADVISOR, NOW, "get_household_snapshot", SNAPSHOT)
    engineer_own = await service.execute(ENGINEER, NOW, "get_metric_definition",
                                         {"metric_id": "catalog"})
    ops_own = await service.execute(OPS, NOW, "get_data_quality_report", {"as_of": "2026-09-30"})

    def forbidden(*_args):
        pytest.fail("the span export was opened before an allow")

    monkeypatch.setattr(secondary, "read_spans", forbidden)
    cases = [
        (ENGINEER, owned["trace_id"]), (ENGINEER, engineer_own["trace_id"]),
        (OPS, owned["trace_id"]), (OPS, ops_own["trace_id"]),
        (OTHER_ADVISOR, owned["trace_id"]), (ADVISOR, "0" * 32), (ADVISOR, "not-a-trace"),
        (ADVISOR, engineer_own["trace_id"]),
    ]
    responses = []
    for principal, trace_id in cases:
        result = await service.execute(principal, _later(), "trace_spans",
                                       {"trace_id": trace_id})
        assert result["error"]["code"] == "NOT_AUTHORIZED", (principal, trace_id, result)
        assert result["data"] is None and not result["source_refs"]
        responses.append({key: value for key, value in comparable(result).items()
                          if key != "policy_revision"})
    assert all(item == responses[0] for item in responses)
    # Engineers and operations stop at the preflight: no owner or result was read.
    with read_snapshot(service.database) as connection:
        for principal in (ENGINEER, OPS):
            actions = {row[0] for row in connection.execute(
                "SELECT d.action FROM decisions d JOIN results r ON r.trace_id=d.trace_id "
                "WHERE d.principal_id=? AND json_extract(r.payload,'$.error.code')="
                "'NOT_AUTHORIZED' AND r.recorded_at>?",
                (principal.id, "2026-11-01T12:00:00.000000+00:00"))}
            assert actions == {"trace_spans_preflight"}, principal
    # Revoked: the owner no longer covers the trace's household.
    policy.allowed = {"WC-HH-0002"}
    revoked = await service.execute(ADVISOR, _later(), "trace_spans",
                                    {"trace_id": owned["trace_id"]})
    assert revoked["error"]["code"] == "NOT_AUTHORIZED" and revoked["data"] is None
    assert ("trace_spans", ["WC-HH-0001"]) in policy.calls


@pytest.mark.anyio
async def test_a_trace_with_decisions_only_and_corrupt_evidence_are_rejected(unit):
    service, _ = unit
    snapshot = await service.execute(ADVISOR, NOW, "get_household_snapshot", SNAPSHOT)
    with write_transaction(service.database) as connection:
        connection.execute(
            "INSERT INTO decisions VALUES ('d_only','ADV-01','snapshot','[]',0,'X',NULL,NULL,"
            "?, '2026-10-01T00:00:00.000000+00:00')", ("a" * 32,))
    expected = {}
    for tool in ("trace", "trace_spans"):
        result = await service.execute(ADVISOR, _later(), tool, {"trace_id": "a" * 32})
        expected[tool] = result["error"]
    assert expected["trace_spans"] == expected["trace"]
    assert expected["trace_spans"]["code"] == "INTERNAL"
    with write_transaction(service.database) as connection:
        connection.execute("UPDATE results SET payload_hash=? WHERE id=?",
                           ("0" * 64, snapshot["result_id"]))
    for tool in ("trace", "trace_spans"):
        result = await service.execute(ADVISOR, _later(), tool,
                                       {"trace_id": snapshot["trace_id"]})
        expected[tool] = result["error"]
    assert expected["trace_spans"] == expected["trace"]
    assert expected["trace_spans"]["code"] == "INTERNAL"


@pytest.mark.anyio
async def test_spans_redisclosure_reauthorizes_the_recorded_scope(unit):
    service, policy = unit
    snapshot = await service.execute(ADVISOR, NOW, "get_household_snapshot", SNAPSHOT)
    spans = await service.execute(ADVISOR, _later(), "trace_spans",
                                  {"trace_id": snapshot["trace_id"]})
    assert spans["error"] is None
    # The recorded spans result is part of its own trace; reading that trace
    # re-authorizes its whole recorded scope.
    assert (await service.execute(ADVISOR, _later(), "trace",
                                  {"trace_id": spans["trace_id"]}))["error"] is None
    policy.allowed = set()
    refused = await service.execute(ADVISOR, _later(), "trace", {"trace_id": spans["trace_id"]})
    assert refused["error"]["code"] == "NOT_AUTHORIZED" and refused["data"] is None


def _line(trace: str, span: str, name: str, start: datetime, end: datetime,
          parent: str | None = None, **extra) -> str:
    return json.dumps({
        "name": name, "context": {"trace_id": "0x" + trace, "span_id": "0x" + span},
        "parent_id": None if parent is None else "0x" + parent,
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time": end.isoformat().replace("+00:00", "Z"),
        "status": {"status_code": "OK"}, "attributes": {"secret": "PRIVATE"}, **extra,
    })


def test_the_reader_is_bounded_and_allowlisted(tmp_path, monkeypatch):
    trace = "b" * 32
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)
    start = now - timedelta(seconds=5)
    path = tmp_path / "traces.jsonl"
    lines = [
        _line(trace, "1" * 16, "tool", start, start + timedelta(microseconds=12345)),
        _line(trace, "2" * 16, "invented", start + timedelta(seconds=1),
              start + timedelta(seconds=2), parent="1" * 16),
        _line(trace, "3" * 16, "authz", start + timedelta(seconds=2),
              start + timedelta(seconds=3), parent="9" * 16),
        _line(trace, "4" * 16, "calc", start, now + timedelta(seconds=1)),  # ended later
        _line("c" * 32, "5" * 16, "tool", start, start + timedelta(seconds=1)),  # other trace
        "{not json " + trace,
        json.dumps({"name": "tool", "context": {"trace_id": trace}}),
        _line(trace, "6" * 16, "query", start + timedelta(seconds=4),
              start + timedelta(seconds=3)),  # ends before it starts
    ]
    path.write_text("\n".join(lines) + "\n")
    read = secondary.read_spans(path, trace, now)
    assert (read.source, read.scan_truncated) == ("EXPORT", False)
    assert [span["span_id"] for span in read.spans] == ["1" * 16, "2" * 16, "3" * 16]
    assert [span["name"] for span in read.spans] == ["tool", "other", "authz"]
    assert read.spans[0]["duration_ms"] == "12.345"
    assert read.spans[1]["duration_ms"] == "1000.000"
    assert all("attributes" not in span and "secret" not in canonical(
        {key: str(value) for key, value in span.items()}) for span in read.spans)
    # Missing or unreadable: UNAVAILABLE and empty.
    assert secondary.read_spans(tmp_path / "absent.jsonl", trace, now) == (
        secondary.SpanRead([], "UNAVAILABLE", False))
    assert secondary.read_spans(tmp_path, trace, now).source == "UNAVAILABLE"
    assert secondary.read_spans(None, trace, now).source == "UNAVAILABLE"
    # Only the newest bytes are scanned; older spans are not shown and the result says so.
    newest = _line(trace, "7" * 16, "tool", start, start + timedelta(seconds=1))
    monkeypatch.setattr(secondary, "SCAN_BYTES", len(newest) + 1)
    path.write_text("\n".join(lines) + "\n" + newest + "\n")
    bounded = secondary.read_spans(path, trace, now)
    assert bounded.scan_truncated is True
    assert [span["span_id"] for span in bounded.spans] == ["7" * 16], (
        "a scan that starts on a line boundary keeps that line")
    monkeypatch.setattr(secondary, "SCAN_BYTES", len(newest) - 3)
    assert secondary.read_spans(path, trace, now).spans == [], (
        "a partial first line is skipped")


@pytest.mark.anyio
async def test_parent_omitted_cap_and_unavailable_export(unit, monkeypatch):
    service, _ = unit
    snapshot = await service.execute(ADVISOR, NOW, "get_household_snapshot", SNAPSHOT)
    trace = snapshot["trace_id"]
    now = _later()
    start = now - timedelta(minutes=1)
    assert service.span_export is not None
    with service.span_export.open("a") as stream:
        for index in range(205):
            stream.write(_line(trace, f"{index + 1:016x}", "query",
                               start + timedelta(microseconds=index),
                               start + timedelta(microseconds=index + 1),
                               parent="f" * 16) + "\n")
    result = await service.execute(ADVISOR, now, "trace_spans", {"trace_id": trace})
    data = result["data"]
    assert (data["item_count"], data["truncated"]) == (200, True)
    assert data["total_item_count"] == 205 + 4
    orphan = data["items"][0]
    assert orphan["parent_span_id"] is None and orphan["parent_omitted"] is True
    monkeypatch.setattr(service, "span_export", service.span_export.parent / "missing.jsonl")
    missing = await service.execute(ADVISOR, now, "trace_spans", {"trace_id": trace})
    assert missing["error"] is None and missing["status"] == "COMPLETE"
    assert (missing["data"]["span_source"], missing["data"]["items"],
            missing["data"]["total_item_count"]) == ("UNAVAILABLE", [], 0)


# --- Real policy: the REST routes for every role ------------------------------------------


@pytest.mark.anyio
async def test_real_policy_rest_routes_for_every_role(tmp_path, opa_server):
    async with app_client(tmp_path, opa_server) as (app, client, database):
        traces = {}
        for persona in ("ADV-01", "OPS-01", "COMP-01", "ENG-01"):
            seed = await client.post("/api/get_metric_definition", json={"metric_id": "catalog"},
                                     headers=bearer(app, persona))
            traces[persona] = seed.json()["trace_id"]
        owned = (await client.post("/api/get_household_snapshot", json={
            "household_id": "WC-HH-0001", "as_of": "2026-09-30"},
            headers=bearer(app, "ADV-01"))).json()
        assert owned["error"] is None, owned
        expected = {"ADV-01": {"trace_spans"}, "COMP-01": set(TOOLS), "OPS-01": set(),
                    "ENG-01": set()}
        preflights = {"decision_log_page": "view_decision_log",
                      "household_receipts": "household_receipts_preflight",
                      "trace_spans": "trace_spans_preflight"}
        for persona, allowed in expected.items():
            for tool in TOOLS:
                arguments = {"decision_log_page": {}, "household_receipts": {
                    "household_id": "WC-HH-0009", "as_of": "2026-09-30",
                    "known_as_of": "2026-10-06T13:00:00Z"},
                    "trace_spans": {"trace_id": traces[persona]}}[tool]
                response = await client.post(f"/api/{tool}", json=arguments,
                                             headers=bearer(app, persona))
                envelope = response.json()
                decisions = audit(database, envelope, persona)
                if tool in allowed:
                    assert response.status_code == 200 and envelope["error"] is None, envelope
                    assert all(row["allowed"] for row in decisions)
                else:
                    assert response.status_code == 403, (persona, tool, envelope)
                    assert envelope["error"]["message"] == (
                        "Caller is not authorized for the requested resource")
                    assert envelope["data"] is None and not envelope["source_refs"]
                    assert [row["action"] for row in decisions] == [preflights[tool]]
        spans = (await client.post("/api/trace_spans", json={"trace_id": owned["trace_id"]},
                                   headers=bearer(app, "ADV-01"))).json()
        assert spans["error"] is None and spans["data"]["span_source"] == "EXPORT"
        assert {"tool", "authz", "query", "calc"} <= {
            item["name"] for item in spans["data"]["items"]}
        other = (await client.post("/api/trace_spans", json={"trace_id": owned["trace_id"]},
                                   headers=bearer(app, "ADV-02"))).json()
        unknown = (await client.post("/api/trace_spans", json={"trace_id": "0" * 32},
                                     headers=bearer(app, "ADV-02"))).json()
        assert other["error"] == unknown["error"] and other["error"]["code"] == "NOT_AUTHORIZED"
        receipts = (await client.post("/api/household_receipts", json={
            "household_id": "WC-HH-0009", "as_of": "2026-09-30"},
            headers=bearer(app, "COMP-01"))).json()
        assert receipts["data"]["cited_record_count"] == 29
        page = (await client.post("/api/decision_log_page", json={},
                                  headers=bearer(app, "COMP-01"))).json()
        assert page["data"]["revisions"][0]["label"] == "FACTS_AT_STORED_REVISION"
        bad = await client.post("/api/decision_log_page", json={"cursor": "x.y"},
                                headers=bearer(app, "COMP-01"))
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_ARGUMENT"
        anonymous = await client.post("/api/trace_spans", json={"trace_id": "0" * 32})
        assert anonymous.status_code == 401
