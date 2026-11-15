"""Plan-derived role boundaries exercised against the actual OPA/service boundary."""

import hashlib
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
import yaml

from wealthcontext.app import create_app
from wealthcontext.bootstrap import bootstrap_history
from wealthcontext.clock import FrozenClock, utc_timestamp
from wealthcontext.context_service import ContextService
from wealthcontext.database import migrate, write_transaction
from wealthcontext.harness import one_claim_demo
from wealthcontext.identity import Principal
from wealthcontext.settings import Settings
from wealthcontext.storage import migrate_business, read_snapshot
from wealthcontext.telemetry import create_provider

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2035, 1, 1, 15, tzinfo=UTC)
ARGS = {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}
PERSONAS = {
    "advisor": Principal("ADV-01", "advisor"),
    "ops": Principal("OPS-01", "ops"),
    "compliance": Principal("COMP-01", "compliance"),
    "engineer": Principal("ENG-01", "engineer"),
}
BUSINESS_TOOLS = {
    "get_household_snapshot", "get_portfolio_exposure", "get_concentrated_positions",
    "get_data_provenance", "search_households", "get_metric_definition",
    "get_data_quality_report", "feed_runs", "conflicts", "decision_log", "eval_reports", "trace",
    "submit_answer", "list_households", "decision_log_page", "household_receipts", "trace_spans",
    "feed_health", "feed_run_impact",
}


def matrix() -> dict:
    return yaml.safe_load((ROOT / "policies/matrix.yaml").read_text())


def recorded(service, envelope, principal):
    with read_snapshot(service.database) as connection:
        row = connection.execute(
            "SELECT * FROM results WHERE id=?", (envelope["result_id"],),
        ).fetchone()
        assert row is not None and row["principal_id"] == principal.id
        assert json.loads(row["payload"]) == envelope
        assert hashlib.sha256(row["payload"].encode()).hexdigest() == row["payload_hash"]
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM decisions WHERE trace_id=?", (envelope["trace_id"],),
        )]
        assert decisions
        assert all(item["principal_id"] == principal.id and item["id"] for item in decisions)
        return decisions


def replace_grants(database: Path, facts: list[tuple[str, str, str]]) -> int:
    with write_transaction(database) as connection:
        revision = connection.execute(
            "SELECT MAX(revision)+1 FROM entitlement_revisions",
        ).fetchone()[0]
        connection.execute("INSERT INTO entitlement_revisions VALUES (?,?)",
                           (revision, utc_timestamp(NOW)))
        for kind, owner, resource in facts:
            connection.execute("INSERT INTO entitlement_facts VALUES (?,?,?,?,?,?)", (
                revision, kind, owner, resource, "2026-09-14", "9999-12-31",
            ))
    return revision


@pytest.fixture
def service(tmp_path, opa_server):
    database = tmp_path / "security.sqlite"
    migrate(database)
    migrate_business(database)
    bootstrap_history(database, tmp_path / "received", FrozenClock(NOW))
    replace_grants(database, [
        ("advisor_covers", "ADV-01", "WC-HH-0001"),
        ("advisor_covers", "ADV-01", "WC-HH-0005"),
        ("advisor_member", "ADV-02", "TEAM-ACCEPTANCE"),
        ("team_covers", "TEAM-ACCEPTANCE", "WC-HH-0001"),
    ])
    provider = create_provider(tmp_path / "traces.jsonl")
    yield ContextService(database, opa_server, provider.get_tracer("wealthcontext-p4-security"))
    provider.shutdown()


def test_matrix_covers_real_routes_and_preserves_plan_role_boundaries():
    spec = matrix()
    assert spec["version"] == 1
    assert set(spec["roles"]) == set(PERSONAS)
    routes = {(row["method"], row["path"]): row for row in spec["routes"]}
    assert len(routes) == len(spec["routes"])
    app = create_app()
    for route in app.routes:
        for method in getattr(route, "methods", ()):
            if method == "HEAD":
                continue
            path = getattr(route, "path", None)
            assert (method, path) in routes, (method, path)
    assert BUSINESS_TOOLS <= {row.get("tool") for row in spec["routes"] if row["business"]}
    for row in spec["routes"]:
        if row["business"]:
            assert row["authentication"] == "required"
            assert row["action"] in spec["actions"]
    actions = spec["actions"]
    # Independent plan assertions prevent a permissive matrix and permissive
    # policy from agreeing with each other while violating role separation.
    for action in ("snapshot", "portfolio_exposure", "concentrated_positions",
                   "concentration_book", "search_households"):
        assert actions[action]["roles"] == {"advisor": "full", "compliance": "full"}
        assert actions[action]["advisor_requires_coverage"] is True
    for action in ("view_quality", "feed_runs"):
        assert actions[action]["roles"] == {"ops": "no_pii", "compliance": "full"}
    for action in ("view_decision_log", "view_eval_reports"):
        assert actions[action]["roles"] == {"compliance": "full"}
    assert actions["metric_definition"]["roles"] == dict.fromkeys(PERSONAS, "full")
    assert actions["provenance"]["roles"]["ops"] == "no_pii"
    assert actions["verify_claim"]["requires_original_result_authorization"] is True
    assert actions["verify_claim"]["roles"] == {
        "advisor": "full", "compliance": "full", "ops": "no_pii", "engineer": "full",
    }
    assert actions["verify_claim"]["engineer_empty_scope_only"] is True


@pytest.mark.anyio
async def test_matrix_drives_every_business_surface_for_four_roles(service):
    spec = matrix()
    for role, principal in PERSONAS.items():
        seed = await service.execute(principal, NOW, "get_metric_definition",
                                     {"metric_id": "catalog"})
        recorded(service, seed, principal)
        reference = "h_unknown"
        if role in {"advisor", "compliance", "ops"}:
            seed_tool = "get_data_quality_report" if role == "ops" else "get_household_snapshot"
            seed_args = {"as_of": "2026-09-30"} if role == "ops" else ARGS
            seed_data = await service.execute(principal, NOW, seed_tool, seed_args)
            assert seed_data["error"] is None, seed_data
            assert seed_data["source_refs"], "Authorized received evidence must issue lineage"
            reference = seed_data["source_refs"][0]
            recorded(service, seed_data, principal)
        with read_snapshot(service.database) as connection:
            failed_run = connection.execute(
                "SELECT id FROM feed_runs WHERE source_system='PAS-B' AND feed='pas_snapshots' "
                "AND business_date='2026-10-15'").fetchone()[0]
        arguments = {
            "get_household_snapshot": ARGS,
            "get_portfolio_exposure": {**ARGS, "group_by": "issuer"},
            "get_concentrated_positions": {**ARGS, "threshold_pct": "10.00"},
            "get_data_provenance": {"reference": reference},
            "search_households": {"name_query": "Synthetic"},
            "list_households": {},
            "get_metric_definition": {"metric_id": "catalog"},
            "get_data_quality_report": {"as_of": "2026-09-30"},
            "feed_runs": {"as_of": "2026-10-09"},
            "conflicts": {"household_id": "WC-HH-0005", "as_of": "2026-09-30"},
            "decision_log": {}, "eval_reports": {}, "trace": {"trace_id": seed["trace_id"]},
            "decision_log_page": {}, "household_receipts": ARGS,
            "trace_spans": {"trace_id": seed["trace_id"]},
            "feed_health": {"date_from": "2026-10-12", "date_to": "2026-10-16"},
            "feed_run_impact": {"run_id": failed_run},
        }
        for route in spec["routes"]:
            if (route["method"] != "POST" or not route["business"]
                    or route.get("tool") not in BUSINESS_TOOLS):
                continue
            tool = route["tool"]
            if tool == "submit_answer":
                result = await one_claim_demo(service, principal, NOW)
            else:
                result = await service.execute(principal, NOW, tool, arguments[tool])
            expected_allow = role in spec["actions"][route["action"]]["roles"]
            assert (result["error"] is None) is expected_allow, (role, tool, result)
            decisions = recorded(service, result, principal)
            if expected_allow:
                assert any(item["allowed"] for item in decisions)
                assert result["policy_revision"] == service.policy.expected_revision
                assert result["entitlement_revision"] is not None
            else:
                assert result["error"]["code"] == "NOT_AUTHORIZED"
                assert result["error"]["message"] == (
                    "Caller is not authorized for the requested resource"
                )
                assert result["data"] is None and not result["source_refs"]


@pytest.mark.anyio
async def test_direct_team_current_grants_and_handle_ownership_are_independent(service):
    direct = PERSONAS["advisor"]
    teammate = Principal("ADV-02", "advisor")
    outsider = Principal("ADV-03", "advisor")
    for principal in (direct, teammate):
        snapshot = await service.execute(principal, NOW, "get_household_snapshot", ARGS)
        assert snapshot["data"]["assets_usd"] == "3000.00"
        recorded(service, snapshot, principal)
        search = await service.execute(principal, NOW, "search_households",
                                       {"name_query": "household"})
        expected_ids = ({"WC-HH-0001", "WC-HH-0005"} if principal == direct else {"WC-HH-0001"})
        assert {row["household_id"] for row in search["data"]["items"]} == expected_ids
        recorded(service, search, principal)
    reference = snapshot["source_refs"][0]  # Issued to teammate, not merely the same household.
    for principal in (direct, outsider):
        failures = []
        for handle in (reference, "h_unknown"):
            denied = await service.execute(principal, NOW, "get_data_provenance",
                                           {"reference": handle})
            assert denied["error"]["code"] == "NOT_AUTHORIZED"
            recorded(service, denied, principal)
            failures.append(denied["error"])
        assert failures[0] == failures[1]
    compliant = await service.execute(PERSONAS["compliance"], NOW, "get_data_provenance",
                                     {"reference": reference})
    assert compliant["error"] is None
    recorded(service, compliant, PERSONAS["compliance"])
    conflicts = await service.execute(direct, NOW, "conflicts",
                                      {"household_id": "WC-HH-0005", "as_of": "2026-09-30"})
    assert conflicts["error"] is None and conflicts["data"]["item_count"] > 0
    assert "WC-HH-0006" not in json.dumps(conflicts)
    recorded(service, conflicts, direct)
    changed = replace_grants(service.database, [("advisor_covers", "ADV-03", "WC-HH-0001")])
    revoked = await service.execute(teammate, NOW, "get_data_provenance", {"reference": reference})
    assert revoked["error"]["code"] == "NOT_AUTHORIZED"
    assert revoked["entitlement_revision"] == changed
    recorded(service, revoked, teammate)
    historical = await service.execute(outsider, NOW, "get_household_snapshot", ARGS)
    assert historical["data"]["assets_usd"] == "3000.00"
    assert historical["entitlement_revision"] == changed
    recorded(service, historical, outsider)
    for resource in ("WC-HH-0001", "WC-HH-9999"):
        denied = await service.execute(direct, NOW, "get_household_snapshot",
                                       {**ARGS, "household_id": resource})
        assert denied["error"]["code"] == "NOT_AUTHORIZED"
        assert denied["error"]["message"] == "Caller is not authorized for the requested resource"
        recorded(service, denied, direct)


def personal_strings(database: Path) -> set[str]:
    values = set()
    with read_snapshot(database) as connection:
        for row in connection.execute("SELECT kind,payload FROM facts WHERE kind IN "
                                      "('household','person','ownership','crm_ownership')"):
            payload = json.loads(row["payload"])
            values.update(payload[key] for key in ("name", "title")
                          if isinstance(payload.get(key), str))
    assert values
    return values


@pytest.mark.anyio
async def test_ops_secondary_surfaces_and_lineage_never_redisclose_personal_text(service):
    private = personal_strings(service.database) | {"PII-REJECT-SENTINEL"}
    with write_transaction(service.database) as connection:
        run = connection.execute(
            "SELECT id FROM feed_runs WHERE business_date='2026-10-09' "
            "AND feed='custody_snapshots' AND source_system='CUSTODY-B'",
        ).fetchone()[0]
        connection.execute("INSERT INTO quarantine VALUES (?,?,?,?,?)", (
            "REJECT-PII-ACCEPTANCE", run, "WC-AC-0016", "SCHEMA_VALIDATION",
            json.dumps({"name": "PII-REJECT-SENTINEL", "title": "PII-REJECT-SENTINEL"}),
        ))
    ops = PERSONAS["ops"]
    for tool, arguments in (
        ("get_data_quality_report", {"as_of": "2026-09-30"}),
        ("feed_runs", {"as_of": "2026-10-09"}),
        ("conflicts", {"household_id": "WC-HH-0002", "as_of": "2026-09-30"}),
    ):
        result = await service.execute(ops, NOW, tool, arguments)
        assert result["error"] is None, result
        assert result["data"]["item_count"] > 0, "A vacuous empty response cannot prove projection"
        encoded = json.dumps(result)
        assert not any(value in encoded for value in private)
        recorded(service, result, ops)
        assert result["source_refs"], (tool, result)
        lineage = await service.execute(ops, NOW, "get_data_provenance",
                                        {"reference": result["source_refs"][0]})
        assert lineage["error"] is None and lineage["data"]["item_count"] > 0
        encoded = json.dumps(lineage)
        assert not any(value in encoded for value in private)
        assert "[REDACTED]" in encoded
        recorded(service, lineage, ops)
        trace = await service.execute(ops, NOW, "trace", {"trace_id": result["trace_id"]})
        assert trace["error"] is None
        assert not any(value in json.dumps(trace) for value in private)
        recorded(service, trace, ops)
        foreign = await service.execute(PERSONAS["advisor"], NOW, "trace",
                                        {"trace_id": result["trace_id"]})
        assert foreign["error"]["code"] == "NOT_AUTHORIZED"
        recorded(service, foreign, PERSONAS["advisor"])


def test_workbench_page_rows_name_governed_actions_and_menus_within_policy():
    spec = matrix()
    rows = {(row["method"], row["path"]): row for row in spec["routes"]
            if row["path"].startswith("/workbench")}
    assert ("POST", "/workbench/inspect") not in rows
    evidence = rows[("GET", "/workbench/evidence")]
    assert (evidence["action"], evidence["business"], evidence["authentication"]) == (
        "provenance", True, "required")
    assert "menu" not in evidence
    # Step 2's household tabs are page routes without a menu entry of their own.
    for path, action in (("/workbench/household/reconciliation", "snapshot"),
                         ("/workbench/household/exposure", "portfolio_exposure")):
        tab = rows[("GET", path)]
        assert (tab["action"], tab["business"], tab["authentication"]) == (
            action, True, "required") and "menu" not in tab
    # Step 3's A7 routes: entered by a link from the "Current access" scenario, no menu.
    for method, path, action in (("GET", "/workbench/revocation", "conversation_context"),
                                 ("POST", "/workbench/revocation/answer", "conversation_context"),
                                 ("GET", "/workbench/revocation/evidence", "provenance")):
        row = rows[(method, path)]
        assert (row["action"], row["business"], row["authentication"]) == (
            action, True, "required") and "menu" not in row
    pages = {path: row for (method, path), row in rows.items() if "menu" in row}
    assert set(pages) == {"/workbench", "/workbench/book", "/workbench/household",
                          "/workbench/metrics", "/workbench/contracts", "/workbench/decisions",
                          "/workbench/point-in-time", "/workbench/trace",
                          "/workbench/data-health", "/workbench/feed-run"} | {
        f"/workbench/inspector/{name}" for name in (
            "snapshot", "quality", "conflicts", "feeds", "eval", "trace")}
    # Owner decision, 2026-11-11 ("Retire both"): the decisions inspector left the
    # compliance menu, its only one, once C1 was built; the route stays.
    assert "menu" not in rows[("GET", "/workbench/inspector/decisions")]
    for path, row in pages.items():
        assert row["method"] == "GET" and row["business"] and row["kind"] == "business"
        assert row["authentication"] == "required"
        allowed = set(spec["actions"][row["action"]]["roles"])
        assert set(row["menu"]["roles"]) <= allowed, path
        assert set(row["menu"]) == {"label", "group", "roles"}
    assert {path: row["action"] for path, row in pages.items()} == {
        "/workbench": "conversation_context",
        "/workbench/book": "search_households",
        "/workbench/household": "snapshot",
        "/workbench/metrics": "metric_definition",
        "/workbench/contracts": "metric_definition",
        "/workbench/inspector/snapshot": "snapshot",
        "/workbench/inspector/quality": "view_quality",
        "/workbench/inspector/conflicts": "view_conflicts",
        "/workbench/inspector/feeds": "feed_runs",
        "/workbench/inspector/eval": "view_eval_reports",
        "/workbench/inspector/trace": "trace_scope",
        "/workbench/decisions": "view_decision_log",
        "/workbench/point-in-time": "household_receipts",
        "/workbench/trace": "trace_spans",
        "/workbench/data-health": "feed_runs",
        "/workbench/feed-run": "feed_runs",
    }
    # Plan role limits, stated independently of the policy the menu is checked against.
    # Engineers see contracts only (brief guardrail 9; build plan decision 13: no trace
    # page for engineers). Owner decision, 2026-11-10: the trace inspector and Ask left
    # the engineer's menu when step 4 built E1 and E2; their routes stay.
    menus = {role: {path for path, row in pages.items() if role in row["menu"]["roles"]}
             for role in PERSONAS}
    assert menus["engineer"] == {"/workbench/metrics", "/workbench/contracts"}
    for path in ("/workbench/metrics", "/workbench/contracts"):
        assert path not in menus["advisor"] | menus["ops"] | menus["compliance"]
    assert "/workbench/inspector/feeds" not in menus["advisor"]
    # Owner decision, 2026-11-12 ("Retire Feed runs"): O1 and O2 replace the feed runs
    # inspector in the operations menu; compliance keeps it and the route stays.
    assert "/workbench/inspector/feeds" not in menus["ops"]
    assert "/workbench/inspector/feeds" in menus["compliance"]
    assert "/workbench/inspector/decisions" not in menus["advisor"] | menus["ops"]
    assert "/workbench/inspector/snapshot" not in menus["ops"]
    # Owner decision, 2026-11-10: retired from the advisor menu once A2 shows the snapshot.
    assert "/workbench/inspector/snapshot" not in menus["advisor"]
    assert "/workbench/inspector/snapshot" in menus["compliance"]
    # The advisor screens of step 2 are listed for advisors only.
    for path in ("/workbench/book", "/workbench/household"):
        assert path not in menus["ops"] | menus["engineer"] | menus["compliance"]
        assert path in menus["advisor"]
    # ADR 0009 point 13: C1 to C3 are in the compliance menu alone; an advisor reaches
    # their own run's C3 from their answer only.
    for path in ("/workbench/decisions", "/workbench/point-in-time", "/workbench/trace"):
        assert path not in menus["advisor"] | menus["ops"] | menus["engineer"]
        assert path in menus["compliance"]
    # Owner decision, 2026-11-11 ("Retire both"): C1 and C3 replace the decisions and trace
    # inspectors in the compliance menu.
    assert "/workbench/inspector/trace" not in menus["compliance"]
    assert "/workbench/inspector/trace" in menus["advisor"] & menus["ops"]
    # ADR 0009 point 13: O1 and O2 are in the operations menu alone; advisors do not see
    # feed runs (brief guardrail 9), and the engineer's menu lists neither.
    for path in ("/workbench/data-health", "/workbench/feed-run"):
        assert path not in menus["advisor"] | menus["compliance"] | menus["engineer"]
        assert path in menus["ops"]
    assert "advisor" not in spec["actions"]["feed_runs"]["roles"]
    for row in rows.values():
        if row["authentication"] == "public":
            assert not row["business"], "a public workbench route carries no business data"


@pytest.mark.anyio
async def test_matrix_drives_every_workbench_page_and_the_evidence_route_for_four_roles(
    tmp_path, opa_server,
):
    """The new page routes against the real policy: allow and deny as the matrix says,
    neutral denials, and evidence only for a pair issued to the asking session."""
    spec = matrix()
    (tmp_path / "revision.txt").write_text(opa_server.expected_revision)
    app = create_app(Settings(tmp_path, tmp_path, opa_server.url), FrozenClock(NOW))
    origin = {"Origin": "http://localhost:8000"}
    denial = "You are not authorized for the requested resource."

    def envelope(page) -> dict:
        found = re.search(r"<summary>Governed result envelope</summary><pre>(.*?)</pre>",
                          page.text, re.S)
        assert found is not None, page.text[-1500:]
        return json.loads(html.unescape(found.group(1)))

    def evidence_urls(page) -> list[str]:
        return [html.unescape(url)
                for url in re.findall(r'href="(/workbench/evidence[^"]+)"', page.text)]

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://localhost:8000") as client:
            async def enter(persona: str, household: str = "WC-HH-0001") -> None:
                landing = await client.get("/")
                token = re.search(r'name="csrf" value="([^"]+)"', landing.text)
                assert token is not None
                applied = await client.post("/workbench/session", headers=origin, data={
                    "csrf": token.group(1), "persona": persona, "as_of": "2026-09-30",
                    "known_as_of": "2026-10-31T16:00:00Z", "mode": "replay",
                    "provider": "openai", "household_id": household})
                assert applied.status_code == 303, applied.text

            issued: dict[str, str] = {}
            unknown: dict[str, str] = {}
            for role, principal in PERSONAS.items():
                await enter(principal.id)
                refused = await client.get("/workbench/evidence?result=r_unknown&source=h_unknown")
                assert refused.status_code == 403 and denial in refused.text
                unknown[role] = refused.text
                for row in spec["routes"]:
                    if row["method"] != "GET" or not row["path"].startswith(
                            "/workbench/inspector/") or row["path"].endswith("/trace"):
                        continue
                    page = await client.get(row["path"])
                    assert page.status_code == 200, (role, row["path"])
                    result = envelope(page)
                    expected_allow = role in spec["actions"][row["action"]]["roles"]
                    assert (result["error"] is None) is expected_allow, (role, row["path"], result)
                    decisions = recorded(app.state.service, result, principal)
                    if expected_allow:
                        assert any(item["allowed"] for item in decisions)
                        for url in evidence_urls(page)[:1]:
                            opened = await client.get(url)
                            assert opened.status_code == 200, (role, row["path"], opened.text)
                            assert "Source records" in opened.text
                            issued.setdefault(role, url)
                    else:
                        assert result["error"]["code"] == "NOT_AUTHORIZED"
                        assert result["error"]["message"] == (
                            "Caller is not authorized for the requested resource")
                        assert result["data"] is None and not result["source_refs"]
                        assert denial in page.text and not evidence_urls(page)
                # No conversation yet: the trace page states its precondition and reads nothing.
                trace = await client.get("/workbench/inspector/trace")
                assert trace.status_code == 200 and "Run a verified answer" in trace.text
                workspace = await client.get("/workbench")
                assert workspace.status_code == 200
            assert set(issued) == set(spec["actions"]["provenance"]["roles"]), (
                "evidence opened for exactly the roles the provenance action allows")
            assert len(set(unknown.values())) == 1, "one neutral denial for every role"
            # A pair issued to another session is refused like an unknown one, for every
            # role, including the role it was issued to.
            for role, principal in PERSONAS.items():
                await enter(principal.id)
                for url in issued.values():
                    foreign = await client.get(url)
                    assert foreign.status_code == 403
                    assert foreign.text == unknown[role], (role, url)
            # Request-time entitlements: a pair issued while covered refuses once the
            # household is revoked, with the same neutral page and no earlier figure.
            await enter("ADV-01")
            page = await client.get("/workbench/inspector/snapshot")
            url = evidence_urls(page)[0]
            assert (await client.get(url)).status_code == 200
            replace_grants(app.state.service.database, [("advisor_covers", "ADV-03", "WC-HH-0001")])
            revoked = await client.get(url)
            assert revoked.status_code == 403 and revoked.text == unknown["advisor"]
            assert "3000.00" not in revoked.text and "source_record_id" not in revoked.text
            # Existing and non-existent households give the same denial page.
            pages = []
            for household in ("WC-HH-0001", "WC-HH-9999"):
                await enter("ADV-01", household)
                view = await client.get("/workbench/inspector/snapshot")
                assert envelope(view)["error"]["code"] == "NOT_AUTHORIZED"
                text = re.sub(r'name="csrf" value="[^"]+"', "", view.text)
                text = re.sub(r"\b(r_[0-9a-f]{32}|[0-9a-f]{32})\b", "ID", text)
                pages.append(text.replace(household, "HOUSEHOLD"))
            assert pages[0] == pages[1]


@pytest.mark.anyio
async def test_list_households_records_its_whole_scope_and_redisclosure_reauthorizes_it(service):
    """ADR 0009 section 1 against the real policy: the recorded result's handle opens only
    while the caller still covers every household the list evaluated."""
    advisor, outsider = PERSONAS["advisor"], Principal("ADV-03", "advisor")
    listed = await service.execute(advisor, NOW, "list_households", {})
    assert listed["error"] is None and listed["status"] == "COMPLETE", listed
    assert [item["household_id"] for item in listed["data"]["items"]] == [
        "WC-HH-0001", "WC-HH-0005"]
    assert listed["data"]["evaluated_household_count"] == 2
    decisions = recorded(service, listed, advisor)
    assert [(item["action"], item["allowed"]) for item in decisions] == [
        ("search_preflight", 1), ("search_households", 1)]
    with read_snapshot(service.database) as connection:
        access = connection.execute("SELECT action, projection FROM result_access "
                                    "WHERE result_id=?", (listed["result_id"],)).fetchone()
        assert tuple(access) == ("search_households", "full")
        scopes = [row[0] for row in connection.execute(
            "SELECT household_id FROM result_scopes WHERE result_id=? ORDER BY household_id",
            (listed["result_id"],))]
        assert scopes == ["WC-HH-0001", "WC-HH-0005"]
        accounts = json.loads(connection.execute(
            "SELECT payload FROM result_account_scopes WHERE result_id=?",
            (listed["result_id"],)).fetchone()[0])
        assert accounts["accounts"] == []
    handle = listed["source_refs"][0]
    reopened = await service.execute(advisor, NOW, "get_data_provenance", {"reference": handle})
    assert reopened["error"] is None and reopened["data"]["item_count"] > 0, reopened
    recorded(service, reopened, advisor)
    failures = []
    for principal, reference in ((outsider, handle), (outsider, "h_unknown")):
        denied = await service.execute(principal, NOW, "get_data_provenance",
                                       {"reference": reference})
        assert denied["error"]["code"] == "NOT_AUTHORIZED"
        recorded(service, denied, principal)
        failures.append(denied["error"])
    assert failures[0] == failures[1], "a foreign handle is refused like an unknown one"
    compliant = await service.execute(PERSONAS["compliance"], NOW, "get_data_provenance",
                                      {"reference": handle})
    assert compliant["error"] is None
    # Coverage of one listed household ends: the whole recorded scope is checked again.
    changed = replace_grants(service.database, [("advisor_covers", "ADV-01", "WC-HH-0001")])
    for tool, arguments in (("get_data_provenance", {"reference": handle}),
                            ("trace", {"trace_id": listed["trace_id"]})):
        revoked = await service.execute(advisor, NOW, tool, arguments)
        assert revoked["error"]["code"] == "NOT_AUTHORIZED", (tool, revoked)
        assert revoked["entitlement_revision"] == changed
        assert revoked["data"] is None and not revoked["source_refs"]
        recorded(service, revoked, advisor)
    narrowed = await service.execute(advisor, NOW, "list_households", {})
    assert [item["household_id"] for item in narrowed["data"]["items"]] == ["WC-HH-0001"]
    for principal in (PERSONAS["ops"], PERSONAS["engineer"]):
        refused = await service.execute(principal, NOW, "list_households", {})
        assert refused["error"]["code"] == "NOT_AUTHORIZED" and refused["data"] is None
        assert [(item["action"], item["allowed"])
                for item in recorded(service, refused, principal)] == [("search_preflight", 0)]


@pytest.mark.anyio
async def test_matrix_drives_the_advisor_pages_for_four_roles(tmp_path, opa_server):
    """Step 2's pages against the real policy: allow and deny as the matrix says, one
    neutral denial for an existing and an unknown household, and request-time access."""
    spec = matrix()
    rows = {row["path"]: row for row in spec["routes"]}
    (tmp_path / "revision.txt").write_text(opa_server.expected_revision)
    app = create_app(Settings(tmp_path, tmp_path, opa_server.url), FrozenClock(NOW))
    origin = {"Origin": "http://localhost:8000"}
    denial = "You are not authorized for the requested resource."
    # Every business page route that is not an inspector, a conversation view or the
    # evidence route: a new data page joins this suite through its matrix row.
    pages = tuple(row["path"] for row in spec["routes"]
                  if row["method"] == "GET" and row["business"]
                  and row["path"].startswith("/workbench/")
                  and not row["path"].startswith(("/workbench/inspector/", "/workbench/claim",
                                                  "/workbench/evidence",
                                                  "/workbench/revocation",
                                                  "/workbench/metrics",
                                                  "/workbench/contracts",
                                                  "/workbench/decisions",
                                                  "/workbench/point-in-time",
                                                  "/workbench/trace",
                                                  "/workbench/data-health",
                                                  "/workbench/feed-run")))
    assert pages == ("/workbench/book", "/workbench/household",
                     "/workbench/household/reconciliation", "/workbench/household/exposure")

    def envelopes(page) -> list[dict]:
        return [json.loads(html.unescape(text))
                for text in re.findall(r"<pre>(\{.*?)</pre>", page.text, re.S)]

    def body(page, *requested: str) -> str:
        found = re.search(r'<main id="main".*?</main>', page.text, re.S)
        assert found is not None, page.text[-1500:]
        text = re.sub(r"\b(r_[0-9a-f]{32}|[0-9a-f]{32})\b", "ID", found.group(0))
        for value in requested:
            text = text.replace(value, "REQUESTED")
        return text

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://localhost:8000") as client:
            async def enter(persona: str, household: str = "WC-HH-0001") -> None:
                landing = await client.get("/")
                token = re.search(r'name="csrf" value="([^"]+)"', landing.text)
                assert token is not None
                applied = await client.post("/workbench/session", headers=origin, data={
                    "csrf": token.group(1), "persona": persona, "as_of": "2026-09-30",
                    "known_as_of": "2026-10-31T16:00:00Z", "mode": "replay",
                    "provider": "openai", "household_id": household})
                assert applied.status_code == 303, applied.text

            for role, principal in PERSONAS.items():
                await enter(principal.id)
                for path in pages:
                    page = await client.get(path)
                    expected_allow = role in spec["actions"][rows[path]["action"]]["roles"]
                    if not expected_allow:
                        assert page.status_code == 403 and denial in page.text, (role, path)
                        assert "Synthetic household" not in page.text
                        assert "/workbench/evidence" not in page.text
                        continue
                    assert page.status_code == 200, (role, path, page.text[-1500:])
                    shown = envelopes(page)
                    assert shown and all(item["error"] is None for item in shown), (role, path)
                    for item in shown:
                        decisions = recorded(app.state.service, item, principal)
                        assert any(decision["allowed"] for decision in decisions)
                    link = re.search(r'href="(/workbench/evidence[^"]+)"', page.text)
                    assert link is not None, (role, path)
                    opened = await client.get(html.unescape(link.group(1)))
                    assert opened.status_code == 200 and "Source records" in opened.text
                    if path == "/workbench/book":
                        listed = shown[0]["data"]["items"]
                        if role == "advisor":
                            assert [item["household_id"] for item in listed] == [
                                "WC-HH-0001", "WC-HH-0005", "WC-HH-0006", "WC-HH-0011"]
                        assert "No book figure is shown" in page.text
            # One neutral denial: a household that exists but is not covered, one whose
            # coverage ended (S04) and one that does not exist.
            await enter("ADV-03")
            for path in pages[1:]:
                variants = set()
                for household in ("WC-HH-0002", "WC-HH-0004", "WC-HH-9999"):
                    page = await client.get(f"{path}?household_id={household}")
                    assert page.status_code == 403 and denial in page.text, (path, household)
                    assert envelopes(page) == [], "the denial discloses no envelope"
                    assert "Signed in as" in page.text and "/workbench/evidence" not in page.text
                    variants.add(body(page, household))
                assert len(variants) == 1, path
            # Request-time entitlements: a figure's evidence and its page refuse once the
            # household is revoked, with the same neutral responses as an unknown one.
            await enter("ADV-01")
            page = await client.get("/workbench/household")
            link = re.search(r'href="(/workbench/evidence[^"]+)"', page.text)
            assert link is not None
            evidence = html.unescape(link.group(1))
            assert (await client.get(evidence)).status_code == 200
            unknown = await client.get("/workbench/household?household_id=WC-HH-9999")
            replace_grants(app.state.service.database,
                           [("advisor_covers", "ADV-03", "WC-HH-0001")])
            revoked = await client.get("/workbench/household")
            assert revoked.status_code == 403 and "3000.00" not in revoked.text
            assert body(revoked, "WC-HH-0001") == body(unknown, "WC-HH-9999")
            refused = await client.get(evidence)
            assert refused.status_code == 403 and "source_record_id" not in refused.text


@pytest.mark.anyio
async def test_matrix_drives_the_seeded_revocation_for_four_roles_and_the_seeded_advisor(
    tmp_path, opa_server,
):
    """A7 against the real policy: the earlier turn is paired tombstones, the follow-up
    abstains with no new retrieval and no key, an old handle and an unknown one get one
    neutral denial, no other persona gets a store, and the demo database is untouched."""
    from tests.test_workbench_revocation import ABSTENTION, RETRIEVAL, digest_database, issued
    from wealthcontext.conversation import TOMBSTONE

    (tmp_path / "revision.txt").write_text(opa_server.expected_revision)
    app = create_app(Settings(tmp_path, tmp_path, opa_server.url), FrozenClock(NOW))
    origin = {"Origin": "http://localhost:8000"}
    denial = "You are not authorized for the requested resource."
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://localhost:8000") as client:
            async def enter(persona: str, **extra: str) -> str:
                landing = await client.get("/")
                token = re.search(r'name="csrf" value="([^"]+)"', landing.text)
                assert token is not None
                applied = await client.post("/workbench/session", headers=origin, data={
                    "csrf": token.group(1), "persona": persona, "as_of": "2026-09-30",
                    "known_as_of": "2026-10-31T16:00:00Z", "mode": "replay",
                    "provider": "openai", "household_id": "WC-HH-0004", **extra})
                assert applied.status_code == 303, applied.text
                return applied.headers["location"]

            database = app.state.service.database
            assert app.state.workbench.followup is not None
            before = digest_database(database)
            unknown_pair = "/workbench/revocation/evidence?result=r_unknown&source=h_unknown"
            refusals = set()
            for principal in PERSONAS.values():
                await enter(principal.id)
                page = await client.get("/workbench/revocation")
                assert page.status_code == 200 and "This seeded conversation is ADV-03" in page.text
                assert TOMBSTONE not in page.text and len(app.state.workbench.seeded) == 0
                refused = await client.get(unknown_pair)
                assert refused.status_code == 403 and denial in refused.text
                refusals.add(refused.text)
            await enter("ADV-03")
            page = await client.get("/workbench/revocation")
            assert page.status_code == 200, page.text[-2000:]
            store = app.state.workbench.seeded.get(client.cookies["wc_demo_session"])
            assert store is not None
            assert "Access changed" in page.text and TOMBSTONE in page.text
            assert page.text.count("Redacted: not authorized") == 2
            taken = issued(store.database)
            token = re.search(r'name="csrf" value="([^"]+)"', page.text)
            assert token is not None
            answered = await client.post("/workbench/revocation/answer", headers=origin,
                                         data={"csrf": token.group(1)})
            assert answered.status_code == 303
            page = await client.get("/workbench/revocation")
            assert "Abstained · no new retrieval" in page.text and ABSTENTION in page.text
            assert not set(issued(store.database)[len(taken):]) & RETRIEVAL
            link = re.search(r'href="(/workbench/revocation/evidence[^"]+)"', page.text)
            assert link is not None
            old = await client.get(html.unescape(link.group(1)))
            unknown = await client.get(unknown_pair)
            assert old.status_code == unknown.status_code == 403
            assert old.text == unknown.text and refusals == {unknown.text}
            assert digest_database(database) == before



@pytest.mark.anyio
async def test_matrix_drives_the_engineer_pages_for_four_roles(tmp_path, opa_server):
    """Step 4's pages against the real policy: each maps to metric_definition, which admits
    all four roles; each governed read is recorded; only the engineer's menu lists them;
    and an engineer on them reaches no trace page, span, household page or evidence."""
    spec = matrix()
    rows = {row["path"]: row for row in spec["routes"]}
    pages = ("/workbench/metrics", "/workbench/contracts")
    (tmp_path / "revision.txt").write_text(opa_server.expected_revision)
    app = create_app(Settings(tmp_path, tmp_path, opa_server.url), FrozenClock(NOW))
    origin = {"Origin": "http://localhost:8000"}

    def envelopes(page) -> list[dict]:
        return [json.loads(html.unescape(text))
                for text in re.findall(r"<pre>(\{.*?)</pre>", page.text, re.S)]

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://localhost:8000") as client:
            for role, principal in PERSONAS.items():
                landing = await client.get("/")
                token = re.search(r'name="csrf" value="([^"]+)"', landing.text)
                assert token is not None
                applied = await client.post("/workbench/session", headers=origin, data={
                    "csrf": token.group(1), "persona": principal.id, "as_of": "2026-09-30",
                    "known_as_of": "2026-10-31T16:00:00Z", "mode": "replay",
                    "provider": "openai", "household_id": "WC-HH-0001"})
                assert applied.status_code == 303
                for path in pages:
                    assert rows[path]["action"] == "metric_definition"
                    expected_allow = role in spec["actions"]["metric_definition"]["roles"]
                    for query in ("", "?metric_id=HOUSEHOLD_ASSETS@1" if "metrics" in path
                                  else "?schema_id=snapshot"):
                        page = await client.get(path + query)
                        assert expected_allow and page.status_code == 200, (role, path)
                        shown = envelopes(page)
                        assert shown and all(item["error"] is None for item in shown)
                        for item in shown:
                            decisions = recorded(app.state.service, item, principal)
                            assert {decision["action"] for decision in decisions} == {
                                "metric_definition"}
                            assert all(decision["allowed"] for decision in decisions)
                        nav = re.search(r'<nav class="side".*?</nav>', page.text, re.S)
                        assert nav is not None
                        listed = set(re.findall(r'href="([^"]+)"', nav.group(0)))
                        assert (set(pages) <= listed) is (role == "engineer"), role
                        if role == "engineer":
                            assert listed == set(pages)
                            assert not re.search(
                                r'href="/workbench/(inspector|household|book|evidence|claim)',
                                page.text)
                            # The page body only: the context form after it holds the
                            # session's own household field, which every persona has.
                            body = re.search(r'<main id="main".*?</main>', page.text, re.S)
                            assert body is not None and "WC-HH-" not in body.group(0)
                            assert "span_id" not in page.text


@pytest.mark.anyio
async def test_matrix_drives_the_compliance_pages_for_four_roles(tmp_path, opa_server):
    """Step 5's pages against the real policy: C1 and C2 for compliance alone; C3 for
    compliance and for the advisor who owns the run; one neutral denial for an existing
    and an unknown household, trace and cursor; each governed read recorded."""
    spec = matrix()
    rows = {row["path"]: row for row in spec["routes"]}
    assert {path: rows[path]["action"] for path in (
        "/workbench/decisions", "/workbench/point-in-time", "/workbench/trace")} == {
        "/workbench/decisions": "view_decision_log",
        "/workbench/point-in-time": "household_receipts", "/workbench/trace": "trace_spans"}
    (tmp_path / "revision.txt").write_text(opa_server.expected_revision)
    app = create_app(Settings(tmp_path, tmp_path, opa_server.url), FrozenClock(NOW))
    origin = {"Origin": "http://localhost:8000"}
    denial = "You are not authorized for the requested resource."

    def envelopes(page) -> list[dict]:
        return [json.loads(html.unescape(text))
                for text in re.findall(r"<pre>(\{.*?)</pre>", page.text, re.S)]

    def body(page, requested: str) -> str:
        found = re.search(r'<main id="main".*?</main>', page.text, re.S)
        assert found is not None, page.text[-1500:]
        text = re.sub(r"\b(r_[0-9a-f]{32}|[0-9a-f]{32})\b", "ID", found.group(0))
        return text.replace(requested, "REQUESTED") if requested else text

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://localhost:8000") as client:
            async def enter(persona: str) -> None:
                landing = await client.get("/")
                token = re.search(r'name="csrf" value="([^"]+)"', landing.text)
                assert token is not None
                applied = await client.post("/workbench/session", headers=origin, data={
                    "csrf": token.group(1), "persona": persona, "as_of": "2026-09-30",
                    "known_as_of": "2026-10-31T16:00:00Z", "mode": "replay",
                    "provider": "openai", "household_id": "WC-HH-0009"})
                assert applied.status_code == 303, applied.text

            # ADV-01's own run: a household page it covers, read by itself.
            await enter("ADV-01")
            owned = envelopes(await client.get("/workbench/household?household_id=WC-HH-0001"))
            trace = owned[0]["trace_id"]
            await enter("COMP-01")
            for _ in range(21):
                await client.get("/workbench/inspector/eval")
            first = await client.get("/workbench/decisions")
            (page_one,) = envelopes(first)
            cursor = page_one["data"]["next_cursor"]
            assert cursor, page_one
            cases = {
                "/workbench/decisions": [("", ""), ("?cursor=" + cursor, cursor),
                                         ("?cursor=AAAA.BBBB", "AAAA.BBBB")],
                "/workbench/point-in-time": [
                    ("?household_id=WC-HH-0009&compare_known_as_of=2026-10-06T13:00:00Z",
                     "WC-HH-0009"),
                    ("?household_id=WC-HH-9999&compare_known_as_of=2026-10-06T13:00:00Z",
                     "WC-HH-9999")],
                "/workbench/trace": [("?trace_id=" + trace, trace),
                                     ("?trace_id=" + "0" * 32, "0" * 32)],
            }
            shown = {("compliance", path, requested) for path, queries in cases.items()
                     for _, requested in queries if requested != "0" * 32}
            shown.add(("advisor", "/workbench/trace", trace))
            for role, principal in PERSONAS.items():
                await enter(principal.id)
                for path, queries in cases.items():
                    denials = []
                    for query, requested in queries:
                        page = await client.get(path + query)
                        if (role, path, requested) in shown:
                            assert page.status_code == 200, (role, path, query)
                            for item in envelopes(page):
                                decisions = recorded(app.state.service, item, principal)
                                assert decisions
                            continue
                        assert page.status_code == 403, (role, path, query, page.text[-500:])
                        assert denial in page.text
                        denials.append(body(page, requested))
                    assert all(item == denials[0] for item in denials), (role, path)
            # The late-trade household at both cutoffs: 29 and 27 cited records.
            await enter("COMP-01")
            pit = envelopes(await client.get(
                "/workbench/point-in-time?compare_known_as_of=2026-10-06T13:00:00Z"))
            assert [item["data"]["cited_record_count"] for item in pit
                    if "cited_record_count" in item["data"]] == [29, 27]
            # Spans: the real export holds this run's spans.
            spans = envelopes(await client.get("/workbench/trace?trace_id=" + trace))[0]
            assert spans["data"]["span_source"] == "EXPORT"
            assert {"tool", "authz"} <= {item["name"] for item in spans["data"]["items"]}


@pytest.mark.anyio
async def test_matrix_drives_the_operations_pages_for_four_roles(tmp_path, opa_server):
    """Step 6's pages against the real policy: O1 and O2 for operations and compliance;
    one neutral denial for an existing and an unknown run, before any lookup; the S07
    preset's cell gives its run; each governed read recorded; no name or title."""
    spec = matrix()
    rows = {row["path"]: row for row in spec["routes"]}
    assert {rows[path]["action"] for path in (
        "/workbench/data-health", "/workbench/feed-run")} == {"feed_runs"}
    (tmp_path / "revision.txt").write_text(opa_server.expected_revision)
    app = create_app(Settings(tmp_path, tmp_path, opa_server.url), FrozenClock(NOW))
    origin = {"Origin": "http://localhost:8000"}
    denial = "You are not authorized for the requested resource."

    def envelopes(page) -> list[dict]:
        return [json.loads(html.unescape(text))
                for text in re.findall(r"<pre>(\{.*?)</pre>", page.text, re.S)]

    def body(page, requested: str) -> str:
        found = re.search(r'<main id="main".*?</main>', page.text, re.S)
        assert found is not None, page.text[-1500:]
        text = re.sub(r"\b(r_[0-9a-f]{32}|[0-9a-f]{32})\b", "ID", found.group(0))
        return text.replace(requested, "REQUESTED") if requested else text

    async with app.router.lifespan_context(app):
        database = tmp_path / "wealthcontext.sqlite"
        with read_snapshot(database) as connection:
            run = connection.execute(
                "SELECT id FROM feed_runs WHERE source_system='PAS-B' AND feed='pas_snapshots' "
                "AND business_date='2026-10-15'").fetchone()[0]
            private = set()
            for row in connection.execute("SELECT payload FROM facts WHERE kind IN "
                                          "('household','person','ownership','crm_ownership')"):
                payload = json.loads(row["payload"])
                private.update(payload[key] for key in ("name", "title")
                               if isinstance(payload.get(key), str) and payload[key])
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://localhost:8000") as client:
            async def enter(persona: str) -> None:
                landing = await client.get("/")
                token = re.search(r'name="csrf" value="([^"]+)"', landing.text)
                assert token is not None
                applied = await client.post("/workbench/session", headers=origin, data={
                    "csrf": token.group(1), "persona": persona, "as_of": "2026-10-15",
                    "known_as_of": "2026-10-31T16:00:00Z", "mode": "live",
                    "provider": "openai", "household_id": "WC-HH-0001"})
                assert applied.status_code == 303, applied.text

            unknown = "RUN-" + "0" * 64
            cases = {"/workbench/data-health": [("", "")],
                     "/workbench/feed-run": [("?run_id=" + run, run),
                                             ("?run_id=" + unknown, unknown)]}
            for role, principal in PERSONAS.items():
                await enter(principal.id)
                allowed = role in spec["actions"]["feed_runs"]["roles"]
                for path, queries in cases.items():
                    denials = []
                    for query, requested in queries:
                        page = await client.get(path + query)
                        if allowed:
                            assert page.status_code == 200, (role, path, query)
                            assert denial not in page.text
                            for item in envelopes(page):
                                assert recorded(app.state.service, item, principal)
                            continue
                        assert page.status_code == 403, (role, path, query, page.text[-500:])
                        assert denial in page.text
                        # The denial shows the request's trace, never the envelope.
                        trace = re.search(r"trace ([0-9a-f]{32})", page.text)
                        assert trace is not None, page.text[-500:]
                        with read_snapshot(database) as connection:
                            decisions = [(row["principal_id"], row["action"],
                                          json.loads(row["resources"])) for row in
                                         connection.execute(
                                             "SELECT * FROM decisions WHERE trace_id=?",
                                             (trace.group(1),))]
                        assert decisions == [(principal.id, "feed_runs", [])], (
                            "refused before any lookup")
                        denials.append(body(page, requested))
                    assert all(item == denials[0] for item in denials), (role, path)
            # The S07 preset names a cell; the run comes from the authorized feed_health.
            landing = await client.get("/")
            token = re.search(r'name="csrf" value="([^"]+)"', landing.text)
            assert token is not None
            applied = await client.post("/workbench/session", headers=origin, data={
                "csrf": token.group(1), "apply_preset": "true", "preset": "failed_feed"})
            page = await client.get(applied.headers["location"])
            impact = next(item for item in envelopes(page)
                          if "account_attribution" in (item["data"] or {}))
            assert impact["data"]["run_id"] == run
            assert impact["data"]["affected_account_count"] == 15
            for text in (page.text, html.unescape(page.text)):
                assert not [value for value in private if value in text]
