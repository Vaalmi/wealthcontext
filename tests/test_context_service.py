import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wealthcontext.clock import utc_timestamp
from wealthcontext.context_service import DENIAL, ContextService
from wealthcontext.database import migrate, write_transaction
from wealthcontext.harness import one_claim_demo
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient
from wealthcontext.storage import digest, migrate_business, read_snapshot, seed_vertical_slice
from wealthcontext.telemetry import create_provider
from wealthcontext.verifier import TEMPLATE

NOW = datetime(2035, 1, 1, tzinfo=UTC)
ARGS = {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}


@pytest.fixture
def context(tmp_path: Path, opa_server: PolicyClient):
    database = tmp_path / "db.sqlite"
    migrate(database)
    migrate_business(database)
    seed_vertical_slice(database, NOW)
    provider = create_provider(tmp_path / "traces.jsonl")
    yield ContextService(database, opa_server, provider.get_tracer("wealthcontext"))
    provider.shutdown()


@pytest.mark.anyio
async def test_real_opa_snapshot_records_exact_response_and_resolves_provenance(context) -> None:
    principal = Principal("ADV-01", "advisor")
    result = await context.execute(principal, NOW, "get_household_snapshot", ARGS)
    assert result["status"] == "COMPLETE", result
    assert result["data"]["assets_usd"] == "3000.00"
    assert result["entitlement_revision"] == 1
    reference = result["source_refs"][0]
    provenance = await context.execute(
        principal, NOW, "get_data_provenance", {"reference": reference},
    )
    assert provenance["status"] == "COMPLETE", provenance
    assert provenance["data"]["item_count"] == 6
    systems = {row["source_system"] for row in provenance["data"]["items"]}
    assert systems == {"CRM", "PAS", "CUSTODY"}
    with read_snapshot(context.database) as connection:
        recorded = connection.execute(
            "SELECT * FROM results WHERE id=?", (result["result_id"],),
        ).fetchone()
        assert json.loads(recorded["payload"]) == result
        assert digest(recorded["payload"]) == recorded["payload_hash"]
        assert [row[0] for row in connection.execute("SELECT action FROM decisions")] == [
            "snapshot", "provenance_preflight", "snapshot", "provenance",
        ]


@pytest.mark.anyio
async def test_denial_is_neutral_and_precedes_existence_lookup(context, monkeypatch) -> None:
    def forbidden_query(*args):
        pytest.fail("business lookup ran before authorization")
    monkeypatch.setattr("wealthcontext.context_service.calculate_snapshot", forbidden_query)
    for household in ["WC-HH-0001", "WC-HH-unknown"]:
        result = await context.execute(
            Principal("ADV-02", "advisor"), NOW, "get_household_snapshot",
            {**ARGS, "household_id": household},
        )
        assert result["error"]["code"] == "NOT_AUTHORIZED", result
        assert result["error"]["message"] == DENIAL
        assert result["data"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("principal", [Principal("ADV-02", "advisor"),
                                        Principal("ENG-01", "engineer"),
                                        Principal("OPS-01", "ops")])
async def test_denial_precedes_date_resolution_and_records_no_resource_scope(context, principal):
    for day in ("2026-09-30", "2026-09-01"):
        result = await context.execute(principal, NOW, "get_household_snapshot",
                                       {**ARGS, "as_of": day})
        assert result["error"]["code"] == "NOT_AUTHORIZED"
        assert result["valuation_date"] is None and result["known_as_of"] is None
        with read_snapshot(context.database) as connection:
            decisions = connection.execute("SELECT allowed,reason FROM decisions WHERE trace_id=?",
                                           (result["trace_id"],)).fetchall()
            assert [tuple(row) for row in decisions] == [(0, "NO_CURRENT_GRANT")]
            assert not connection.execute("SELECT 1 FROM result_scopes WHERE result_id=?",
                                          (result["result_id"],)).fetchall()


@pytest.mark.anyio
@pytest.mark.parametrize("principal", [Principal("ENG-01", "engineer"),
                                        Principal("OPS-01", "ops")])
async def test_metadata_preflights_do_not_grant_financial_answer_or_foreign_handle(
    context, principal,
):
    provenance = await context.execute(principal, NOW, "get_data_provenance",
                                       {"reference": "h_unknown"})
    assert provenance["error"]["code"] == "NOT_AUTHORIZED"
    owner = Principal("ADV-01", "advisor")
    owner_conversation = context.start_conversation(owner, NOW)
    financial = await context.execute(owner, NOW, "get_household_snapshot", ARGS,
                                      owner_conversation)
    conversation = context.start_conversation(principal, NOW)
    answer = await context.execute(principal, NOW, "submit_answer", {
        "answer_markdown": TEMPLATE, "claims": [{
            "id": "c1", "result_id": financial["result_id"],
            "entity": {"type": "household", "id": "WC-HH-0001"}, "field": "assets_usd",
            "household_id": "WC-HH-0001", "valuation_date": "2026-09-30",
            "unit": "USD", "format": "value_only",
        }], "predicates": [],
        "caveats": [], "refusal": None,
    }, conversation)
    assert answer["error"]["code"] == "NOT_AUTHORIZED"
    with read_snapshot(context.database) as connection:
        actions = [tuple(row) for row in connection.execute(
            "SELECT action,allowed FROM decisions WHERE trace_id=?", (answer["trace_id"],),
        )]
        assert actions == [("result_preflight", 1), ("verify_claim", 1),
                           ("result_preflight", 1), ("submit_answer", 0)]


@pytest.mark.anyio
async def test_handles_recheck_current_grants_and_issuing_principal(context) -> None:
    principal = Principal("ADV-01", "advisor")
    result = await context.execute(principal, NOW, "get_household_snapshot", ARGS)
    reference = result["source_refs"][0]
    for handle in [reference, "h_unknown"]:
        denied = await context.execute(Principal("ADV-02", "advisor"), NOW,
                                       "get_data_provenance", {"reference": handle})
        assert denied["error"]["code"] == "NOT_AUTHORIZED"
        assert denied["error"]["message"] == DENIAL
    with write_transaction(context.database) as connection:
        connection.execute("INSERT INTO entitlement_revisions VALUES (2, ?)", (utc_timestamp(NOW),))
    revoked = await context.execute(principal, NOW, "get_data_provenance", {"reference": reference})
    assert revoked["error"]["code"] == "NOT_AUTHORIZED"
    assert revoked["entitlement_revision"] == 2


@pytest.mark.anyio
async def test_metric_catalogue_and_invalid_arguments_have_typed_envelopes(context) -> None:
    principal = Principal("ENG-01", "engineer")
    result = await context.execute(
        principal, NOW, "get_metric_definition", {"metric_id": "catalog"},
    )
    assert result["status"] == "COMPLETE"
    assert result["data"]["item_count"] == 5
    assert [item["id"] for item in result["data"]["items"]] == [
        "CASH_USD@1", "CONCENTRATION@1", "HOUSEHOLD_ASSETS@1", "ISSUER_EXPOSURE@1",
        "MV_POSITION@1",
    ]
    # Nonhistorical counts carry the request's New York business day and instant.
    assert result["valuation_date"] == "2034-12-31" and result["as_of"] is None  # New York day
    assert datetime.fromisoformat(result["known_as_of"]) == NOW
    bad = await context.execute(
        principal, NOW, "get_household_snapshot", {**ARGS, "known_as_of": "bad"},
    )
    assert bad["error"]["code"] == "INVALID_ARGUMENT"
    assert bad["data"] is None


@pytest.mark.anyio
async def test_harness_renders_one_server_resolved_claim_with_parent_trace(context) -> None:
    answer = await one_claim_demo(context, Principal("ADV-01", "advisor"), NOW)
    assert answer["status"] == "COMPLETE", answer
    assert answer["data"]["markdown"].startswith(
        "Household assets in included accounts: USD 3000.00.\n\n> Source:",
    )
    assert answer["data"]["mode"] == "verified"
    assert answer["data"]["sources"][0]["valuation_date"] == "2026-09-30"
    assert answer["data"]["claims"][0]["value"] == "3000.00"
    with read_snapshot(context.database) as connection:
        traces = {row[0] for row in connection.execute("SELECT trace_id FROM results")}
    assert traces == {answer["trace_id"]}


@pytest.mark.anyio
async def test_claim_cannot_cross_conversations_or_survive_revocation(context) -> None:
    principal = Principal("ADV-01", "advisor")
    conversation = context.start_conversation(principal, NOW)
    result = await context.execute(principal, NOW, "get_household_snapshot", ARGS, conversation)
    candidate = {
        "answer_markdown": TEMPLATE, "claims": [{
            "id": "c1", "result_id": result["result_id"],
            "entity": {"type": "household", "id": "WC-HH-0001"}, "field": "assets_usd",
            "household_id": "WC-HH-0001", "valuation_date": "2026-09-30", "unit": "USD",
            "format": "value_only",
        }], "predicates": [], "caveats": [], "refusal": None,
    }
    other = context.start_conversation(principal, NOW)
    wrong_conversation = await context.execute(principal, NOW, "submit_answer", candidate, other)
    assert wrong_conversation["error"]["code"] == "NOT_AUTHORIZED"
    candidate["claims"][0]["unit"] = "pct"
    invalid = await context.execute(principal, NOW, "submit_answer", candidate, conversation)
    assert invalid["error"]["code"] == "INVALID_ARGUMENT"
    candidate["claims"][0]["unit"] = "USD"
    with write_transaction(context.database) as connection:
        connection.execute("INSERT INTO entitlement_revisions VALUES (2, ?)", (utc_timestamp(NOW),))
    revoked = await context.execute(principal, NOW, "submit_answer", candidate, conversation)
    assert revoked["error"]["code"] == "NOT_AUTHORIZED"


@pytest.mark.anyio
@pytest.mark.parametrize("role,principal_id", [("ops", "OPS-01"), ("engineer", "ENG-01")])
async def test_general_verifier_can_render_own_neutral_denial_without_financial_access(
    context, role, principal_id,
) -> None:
    principal = Principal(principal_id, role)
    conversation = context.start_conversation(principal, NOW)
    denied = await context.execute(principal, NOW, "get_household_snapshot", ARGS, conversation)
    candidate = {"answer_markdown": "This household does not exist.", "claims": [],
                 "predicates": [], "caveats": [],
                 "refusal": {"code": "NOT_AUTHORIZED", "result_id": denied["result_id"]}}
    answer = await context.execute(principal, NOW, "submit_answer", candidate, conversation)
    assert answer["status"] == "COMPLETE", answer
    assert answer["data"]["markdown"].startswith("Access denied.")
    assert "does not exist" not in answer["data"]["markdown"]
    assert answer["data"]["claims"] == []
    with read_snapshot(context.database) as connection:
        assert connection.execute(
            "SELECT action FROM result_access WHERE result_id=?", (denied["result_id"],),
        ).fetchone()[0] == "error_result"
        assert not connection.execute(
            "SELECT 1 FROM result_scopes WHERE result_id=?", (answer["result_id"],),
        ).fetchall()
    foreign = context.start_conversation(principal, NOW)
    assert (await context.execute(principal, NOW, "submit_answer", candidate, foreign))[
        "error"]["code"] == "NOT_AUTHORIZED"


@pytest.mark.anyio
async def test_submit_answer_requires_conversation_and_returns_bounded_structured_diagnostics(
    context,
) -> None:
    principal = Principal("ADV-01", "advisor")
    invalid = {"answer_markdown": " ".join(str(value) for value in range(200)),
               "claims": [], "predicates": [], "caveats": [], "refusal": None}
    without_conversation = await context.execute(principal, NOW, "submit_answer", invalid)
    assert without_conversation["error"]["code"] == "NOT_AUTHORIZED"
    conversation = context.start_conversation(principal, NOW)
    result = await context.execute(principal, NOW, "submit_answer", invalid, conversation)
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert len(result["error"]["message"]) <= 500
    diagnostics = result["error"]["details"]["diagnostics"]
    assert diagnostics and all(set(item) == {"code", "path", "message"} for item in diagnostics)
    assert "199" in json.dumps(diagnostics)


@pytest.mark.anyio
async def test_provenance_preserves_origin_cutoffs_for_verified_record_count(context) -> None:
    principal = Principal("ADV-01", "advisor")
    conversation = context.start_conversation(principal, NOW)
    snapshot = await context.execute(principal, NOW, "get_household_snapshot", ARGS, conversation)
    source = await context.execute(principal, NOW, "get_data_provenance",
                                   {"reference": snapshot["source_refs"][0]}, conversation)
    for field in ("as_of", "valuation_date", "known_as_of"):
        assert source[field] == snapshot[field]
    answer = await context.execute(principal, NOW, "submit_answer", {
        "answer_markdown": "Received records: {{c1}}.", "claims": [{
            "id": "c1", "result_id": source["result_id"],
            "entity": {"type": "result_summary", "id": source["result_id"]},
            "field": "item_count", "household_id": None,
            "valuation_date": source["valuation_date"], "unit": "count", "format": "value_only",
        }], "predicates": [], "caveats": [], "refusal": None,
    }, conversation)
    assert answer["status"] == "COMPLETE", answer
    assert answer["data"]["claims"][0]["value"] == source["data"]["item_count"]
    records = answer["data"]["sources"][0]["records"]
    assert len(records) == source["data"]["item_count"]
    assert all(item["payload_hash"] in answer["data"]["markdown"] for item in records)


@pytest.mark.anyio
async def test_invalid_arguments_are_audited_and_authentication_clock_is_utc(context):
    result = await context.execute(Principal("ADV-01", "advisor"), NOW,
                                   "get_household_snapshot", None)
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    instant = datetime.fromisoformat("2026-10-01T01:00:00.000001+02:00")
    denied = context.authentication_error(instant)
    assert denied["calculated_at"] == "2026-09-30T23:00:00.000001Z"
    with read_snapshot(context.database) as connection:
        row = connection.execute("SELECT reason FROM decisions WHERE trace_id=?",
                                 (result["trace_id"],)).fetchone()
        assert row[0] == "INVALID_ARGUMENT"
        row = connection.execute("SELECT recorded_at FROM results WHERE id=?",
                                 (denied["result_id"],)).fetchone()
        assert row[0] == "2026-09-30T23:00:00.000001+00:00"
