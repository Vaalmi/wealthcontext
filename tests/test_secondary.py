"""Hand-received evidence exercises secondary reads without generator truth."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import wealthcontext.context_service as service_module
from wealthcontext.clock import FrozenClock
from wealthcontext.context_service import ContextService
from wealthcontext.conversation import TOMBSTONE, ConversationManager
from wealthcontext.database import migrate
from wealthcontext.facts import fact_view
from wealthcontext.feeds import AccountReference, FeedName, SourceSystem, build_delivery
from wealthcontext.identity import Principal
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.policy import PolicyClient, PolicyDecision
from wealthcontext.storage import canonical, migrate_business, read_snapshot
from wealthcontext.surface_contracts import SearchArguments, SearchData, SurfaceList
from wealthcontext.telemetry import create_provider

DAY = date(2026, 9, 30)
RECEIVED = datetime(2026, 9, 30, 22, tzinfo=UTC)
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
REVISION = "sha256:" + "a" * 64
OPS = Principal("OPS-01", "ops")
ADVISOR = Principal("ADV-01", "advisor")
COMPLIANCE = Principal("COMP-01", "compliance")


def row(kind: str, key: str, **values: Any) -> dict:
    return {"kind": kind, "source_record_id": key, "valid_from": "2026-09-14",
            "valid_to": "9999-12-31", **values}


class SurfacePolicy(PolicyClient):
    """Unit decision fixture; the independent suite uses real OPA for the matrix."""

    def __init__(self):
        super().__init__("http://not-contacted.invalid", REVISION)
        self.allowed = {"WC-HH-0001"}
        self.calls: list[tuple[str, list[str]]] = []

    def response(self, principal, action, resources, revision, filtered=False):
        self.calls.append((action, resources))
        roles = {
            "search_preflight": {"advisor", "compliance"},
            "search_households": {"advisor", "compliance"},
            "quality_preflight": {"ops", "compliance"},
            "view_quality": {"ops", "compliance"},
            "feed_runs": {"ops", "compliance"},
            "view_conflicts": {"advisor", "ops", "compliance"},
            "view_decision_log": {"compliance"}, "view_eval_reports": {"compliance"},
            "trace_preflight": {"advisor", "ops", "compliance", "engineer"},
            "trace_scope": {"advisor", "ops", "compliance", "engineer"},
            "provenance_preflight": {"advisor", "ops", "compliance"},
            "provenance": {"advisor", "ops", "compliance"},
            "metric_definition": {"advisor", "ops", "compliance", "engineer"},
            "result_preflight": {"advisor", "ops", "compliance", "engineer"},
            "conversation_preflight": {"advisor", "ops", "compliance", "engineer"},
            "conversation_context": {"advisor", "ops", "compliance", "engineer"},
        }
        selected = (set(resources) & self.allowed if principal.role == "advisor"
                    else set(resources))
        allow = principal.role in roles.get(action, set()) and (
            filtered or selected == set(resources)
        )
        return PolicyDecision(
            allow=allow, allowed_ids=sorted(selected) if allow else [],
            projection="no_pii" if principal.role == "ops" and action != "metric_definition"
            else "full",
            reason="CURRENT_GRANT" if allow else "NO_CURRENT_GRANT", policy_revision=REVISION,
            entitlement_revision=revision,
        )

    async def decide(self, principal, action, resources, facts, revision, request_business_date):
        return self.response(principal, action, resources, revision)

    async def filter_resources(
        self, principal, action, candidates, facts, revision, request_business_date,
    ):
        return self.response(principal, action, candidates, revision, filtered=True)


def prepare(database: Path, count: int = 3) -> None:
    migrate(database)
    migrate_business(database)
    migrate_feeds(database)

    def receive(system: SourceSystem, feed: FeedName, records: list[dict], **kwargs):
        delivery = build_delivery(source_system=system, feed=feed, business_date=DAY,
                                  records=records, **kwargs)
        return ingest_delivery(database, delivery, FrozenClock(RECEIVED))

    records, owners, accounts = [], [], []
    for number in range(1, count + 1):
        household, account, person = (f"WC-{kind}-{number:04}" for kind in ("HH", "AC", "PER"))
        reference = AccountReference(account_id=account, custodian="A",
                                     account_number=f"0042{number:04}")
        accounts.append(reference)
        records.extend([
            row("household", f"hh-{number}", id=household, name=f"Private household {number:04}"),
            row("person", f"person-{number}", id=person, name=f"Private person {number:04}",
                external_person_key=f"external-{number}", note="PRIVATE-NOTE"),
            row("membership", f"member-{number}", person_id=person, household_id=household),
            row("account", f"account-{number}", id=account, custodian="A",
                account_number=reference.account_number, is_managed=True),
            row("reporting", f"report-{number}", account_id=account, household_id=household),
        ])
        owners.append(row("ownership", f"owner-{number}", custodian="A",
                          account_number=reference.account_number, title=f"Private title {number}",
                          registration="INDIVIDUAL", status="OPEN",
                          owners=[{"entity_id": person, "entity_type": "person", "role": "owner"}]))
    receive("CRM", "crm", records)
    receive("CUSTODY-A", "custody_accounts", owners)
    # The final account is absent from the manifest, not a confirmed zero balance.
    cash = [row("cash", f"cash-{index}", custodian="A", account_number=item.account_number,
                currency="USD", amount="100.00", valid_from="2026-09-30", valid_to="2026-10-01")
            for index, item in enumerate(accounts[:-1])]
    receive("PAS-A", "pas_snapshots", cash, expected_accounts=accounts[:-1])
    custody = [{**item, "source_record_id": f"custody-{index}", "amount": "101.00"}
               for index, item in enumerate(cash)]
    receive("CUSTODY-A", "custody_snapshots", custody, expected_accounts=accounts[:-1])


@pytest.fixture
def secondary(tmp_path: Path):
    database = tmp_path / "surfaces.sqlite"
    prepare(database)
    provider = create_provider(tmp_path / "trace.jsonl")
    policy = SurfacePolicy()
    yield ContextService(database, policy, provider.get_tracer("test")), policy
    provider.shutdown()


@pytest.mark.anyio
async def test_search_filters_before_name_matching_and_retains_all_evaluated_scope(secondary):
    service, policy = secondary
    result = await service.execute(ADVISOR, NOW, "search_households", {"name_query": "PRIVATE"})
    assert result["status"] == "COMPLETE", result
    assert result["data"]["items"] == [{"household_id": "WC-HH-0001",
                                         "household_name": "Private household 0001"}]
    assert result["data"]["evaluated_household_count"] == 1
    assert "Private household 0002" not in canonical(result)
    assert [item[0] for item in policy.calls] == ["search_preflight", "search_households"]
    unmatched = await service.execute(ADVISOR, NOW, "search_households", {"name_query": "absent"})
    assert unmatched["data"]["items"] == []
    with read_snapshot(service.database) as connection:
        scopes = connection.execute("SELECT household_id FROM result_scopes WHERE result_id=?",
                                    (unmatched["result_id"],)).fetchall()
        assert [item[0] for item in scopes] == ["WC-HH-0001"]


@pytest.mark.anyio
async def test_quality_reports_diagnostics_and_no_pii_provenance(secondary):
    service, _ = secondary
    result = await service.execute(OPS, NOW, "get_data_quality_report", {"as_of": str(DAY)})
    assert result["status"] == "INCOMPLETE", result
    assert result["data"]["evaluated_household_count"] == 3
    assert result["data"]["incomplete_household_count"] == 1
    assert result["data"]["quality_flag_count"] == sum(
        item["flags"]["total_item_count"] for item in result["data"]["items"]
    )
    assert "CASH_BREAK" in {flag["code"] for flag in result["quality_flags"]}
    assert "assets_usd" not in canonical(result)
    provenance = await service.execute(OPS, NOW, "get_data_provenance",
                                       {"reference": result["source_refs"][0]})
    assert provenance["status"] == "COMPLETE", provenance
    serialized = canonical(provenance)
    for marker in ("Private household", "Private person", "Private title", "PRIVATE-NOTE"):
        assert marker not in serialized
    with read_snapshot(service.database) as connection:
        stored = connection.execute("SELECT payload FROM result_account_scopes WHERE result_id=?",
                                    (result["result_id"],)).fetchone()
        assert len(json.loads(stored[0])["accounts"]) == 3


@pytest.mark.anyio
async def test_search_top_twenty_does_not_shrink_durable_scope(tmp_path: Path):
    database = tmp_path / "many.sqlite"
    prepare(database, count=23)
    policy = SurfacePolicy()
    policy.allowed = {f"WC-HH-{number:04}" for number in range(1, 24)}
    provider = create_provider(tmp_path / "trace.jsonl")
    try:
        service = ContextService(database, policy, provider.get_tracer("test"))
        result = await service.execute(ADVISOR, NOW, "search_households", {"name_query": "private"})
        assert result["status"] == "COMPLETE", result
        data = result["data"]
        assert (data["item_count"], data["total_item_count"], data["truncated"]) == (20, 23, True)
        assert data["next_cursor"] is None
        with read_snapshot(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM result_scopes WHERE result_id=?",
                                      (result["result_id"],)).fetchone()[0] == 23
    finally:
        provider.shutdown()


@pytest.mark.anyio
async def test_feed_rejects_keep_real_counts_but_never_unstructured_received_text(secondary):
    service, _ = secondary
    received = build_delivery(source_system="CRM", feed="crm", business_date=DAY,
                              records=["Private person hidden in malformed fragment", {
                                  "kind": "unknown", "Private household key": "PRIVATE-NOTE",
                              }])
    run_id = ingest_delivery(service.database, received, FrozenClock(RECEIVED))
    for principal in (OPS, COMPLIANCE):
        result = await service.execute(principal, NOW, "feed_runs", {"as_of": str(DAY)})
        assert result["status"] == "COMPLETE", result
        rejected = [item for item in result["data"]["items"] if item["id"] == run_id]
        assert len(rejected) == 1
        assert rejected[0]["rejects"]["total_item_count"] == 2
        assert all(item["raw_payload"] == "[REDACTED]" for item in rejected[0]["rejects"]["items"])
        assert "Private" not in canonical(result) and "PRIVATE-NOTE" not in canonical(result)


@pytest.mark.anyio
async def test_conflicts_are_real_received_breaks_and_other_households_are_denied(secondary):
    service, _ = secondary
    result = await service.execute(ADVISOR, NOW, "conflicts",
                                   {"household_id": "WC-HH-0001", "as_of": str(DAY)})
    assert result["status"] == "COMPLETE", result
    # Reconciliation flags are reported even though they are not relationship conflicts.
    assert "CASH_BREAK" in {flag["code"] for flag in result["quality_flags"]}
    denied = await service.execute(ADVISOR, NOW, "conflicts",
                                   {"household_id": "WC-HH-0002", "as_of": str(DAY)})
    assert denied["error"]["code"] == "NOT_AUTHORIZED"


@pytest.mark.anyio
async def test_trace_checks_owner_and_current_scopes_and_exposes_only_durable_metadata(secondary):
    service, policy = secondary
    origin = await service.execute(ADVISOR, NOW, "search_households", {"name_query": "private"})
    args = {"trace_id": origin["trace_id"]}
    result = await service.execute(ADVISOR, NOW, "trace", args)
    assert result["status"] == "COMPLETE", result
    assert result["data"]["results"]["items"][0]["result_id"] == origin["result_id"]
    assert result["data"]["decisions"]["total_item_count"] == 2
    assert "Private household" not in canonical(result)
    denied = await service.execute(OPS, NOW, "trace", args)
    assert denied["error"]["code"] == "NOT_AUTHORIZED"
    policy.allowed.clear()
    revoked = await service.execute(ADVISOR, NOW, "trace", args)
    assert revoked["error"]["code"] == "NOT_AUTHORIZED"
    compliance = await service.execute(COMPLIANCE, NOW, "trace", args)
    assert compliance["status"] == "COMPLETE"


@pytest.mark.anyio
async def test_compliance_decision_log_and_real_empty_evaluation_inventory(secondary):
    service, _ = secondary
    await service.execute(ADVISOR, NOW, "search_households", {"name_query": "private"})
    result = await service.execute(COMPLIANCE, NOW, "decision_log", {})
    assert result["data"]["total_item_count"] == 2
    assert {item["action"] for item in result["data"]["items"]} == {
        "search_preflight", "search_households",
    }
    evaluations = await service.execute(COMPLIANCE, NOW, "eval_reports", {})
    assert evaluations["status"] == "COMPLETE" and evaluations["data"]["items"] == []
    denied = await service.execute(OPS, NOW, "decision_log", {})
    assert denied["error"]["code"] == "NOT_AUTHORIZED"


@pytest.mark.anyio
async def test_preflight_denial_never_reads_business_inventory(secondary, monkeypatch):
    service, _ = secondary

    def forbidden(*args):
        pytest.fail("business identifiers were retrieved before an allow")

    monkeypatch.setattr("wealthcontext.secondary._ids", forbidden)
    for principal, tool, arguments in [
        (OPS, "search_households", {"name_query": "private"}),
        (ADVISOR, "get_data_quality_report", {"as_of": str(DAY)}),
        (ADVISOR, "feed_runs", {"as_of": str(DAY)}),
    ]:
        result = await service.execute(principal, NOW, tool, arguments)
        assert result["error"]["code"] == "NOT_AUTHORIZED"


@pytest.mark.anyio
async def test_search_name_select_is_restricted_to_authorized_identifiers(secondary, monkeypatch):
    service, _ = secondary
    statements = []

    def observed(connection, kind, day, known, *, entity_ids=None):
        connection.set_trace_callback(statements.append)
        return fact_view(connection, kind, day, known, entity_ids=entity_ids)

    monkeypatch.setattr("wealthcontext.secondary.fact_view", observed)
    result = await service.execute(ADVISOR, NOW, "search_households", {"name_query": "private"})
    assert result["status"] == "COMPLETE", result
    name_queries = [statement for statement in statements
                    if "SELECT f.entity_id,f.payload" in statement]
    assert len(name_queries) == 1
    assert "AND f.entity_id IN ('WC-HH-0001')" in name_queries[0]
    assert "WC-HH-0002" not in name_queries[0]


def test_closed_surface_contracts_validate_honest_list_counts_and_queries():
    assert SearchArguments(name_query="  Example  ").name_query == "Example"
    with pytest.raises(ValidationError):
        SearchArguments(name_query="  ")
    with pytest.raises(ValidationError):
        SearchArguments.model_validate({"name_query": "name", "principal": "COMP-01"})
    with pytest.raises(ValidationError):
        SurfaceList(items=["one"], item_count=1, total_item_count=2, ordering="id", truncated=False)
    assert SearchData.model_json_schema()["additionalProperties"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("metric_sort", ["a", "z"])
async def test_ops_conversation_retains_full_metric_and_no_pii_quality_results(
    secondary, monkeypatch, metric_sort,
):
    service, _ = secondary
    manager = ConversationManager(service)
    conversation = manager.start(OPS, NOW)
    # Reauthorization sorts result IDs. Exercise both orders deterministically,
    # including no_pii quality resolved before the full metric catalogue.
    ids = iter([f"r_{metric_sort}_metric", "r_m_quality"])
    original_identifier = service_module.identifier
    monkeypatch.setattr(service_module, "identifier", lambda prefix: next(ids)
                        if prefix == "r" else original_identifier(prefix))
    metric = await manager.execute_tool(
        OPS, NOW, conversation, "call_metric", "get_metric_definition", {"metric_id": "catalog"},
    )
    quality = await manager.execute_tool(
        OPS, NOW, conversation, "call_quality", "get_data_quality_report", {"as_of": str(DAY)},
    )
    assert metric["status"] == "COMPLETE" and quality["status"] == "INCOMPLETE"
    with read_snapshot(service.database) as connection:
        stored = dict(connection.execute("SELECT result_id,projection FROM result_access"))
        assert stored[metric["result_id"]] == "full"
        assert stored[quality["result_id"]] == "no_pii"
    view = await manager.prepare_turn(OPS, NOW, conversation)
    assert view["status"] == "COMPLETE", view
    assert view["data"]["redacted_result_ids"] == []
    messages = view["data"]["messages"]
    assert [item["role"] for item in messages] == ["assistant", "tool", "assistant", "tool"]
    assert messages[0]["tool_calls"][0]["id"] == messages[1]["tool_call_id"] == "call_metric"
    assert messages[2]["tool_calls"][0]["id"] == messages[3]["tool_call_id"] == "call_quality"
    assert json.loads(messages[1]["content"])["result_id"] == metric["result_id"]
    assert json.loads(messages[3]["content"])["result_id"] == quality["result_id"]
    for marker in (TOMBSTONE, "Private household", "Private person", "Private title",
                   "PRIVATE-NOTE"):
        assert marker not in canonical(view)
