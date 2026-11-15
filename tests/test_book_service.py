"""Received feeds exercise book scope, filtering and immutable origin provenance."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import wealthcontext.context_service as service_module
from wealthcontext.clock import FrozenClock, utc_timestamp
from wealthcontext.context_service import ConcentrationArguments, ContextService, ExposureArguments
from wealthcontext.conversation import ConversationManager
from wealthcontext.database import migrate, write_transaction
from wealthcontext.feeds import AccountReference, FeedName, SourceSystem, build_delivery
from wealthcontext.identity import Principal
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.policy import PolicyClient, PolicyDecision
from wealthcontext.storage import digest, migrate_business, read_snapshot
from wealthcontext.telemetry import create_provider

DAY = date(2026, 9, 30)
RECEIVED = datetime(2026, 9, 30, 22, tzinfo=UTC)
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
REVISION = "sha256:" + "a" * 64
ALLOWED = [f"WC-HH-{number}" for number in range(1, 5)]
REFERENCES = [AccountReference(account_id=f"WC-AC-{number}", custodian="A",
                               account_number=f"004200{number:02}") for number in range(1, 6)]


def row(kind: str, key: str, **values: Any) -> dict:
    return {"kind": kind, "source_record_id": key, "valid_from": "2026-09-14",
            "valid_to": "9999-12-31", **values}


def receive(database: Path, system: SourceSystem, feed: FeedName, records: list[dict], **kw):
    delivery = build_delivery(source_system=system, feed=feed, business_date=DAY,
                              records=records, **kw)
    return ingest_delivery(database, delivery, FrozenClock(RECEIVED))


class RecordedPolicy(PolicyClient):
    """Fixed test policy; real subset wire validation and Rego have separate tests."""

    def __init__(self):
        super().__init__("http://not-contacted.invalid", REVISION)
        self.allowed = set(ALLOWED)
        self.calls: list[tuple[str, list[str], int, date]] = []

    def decision(self, principal, resources, revision, *, filtered=False):
        permitted = principal.role in {"advisor", "compliance"}
        selected = (set(resources) if principal.role == "compliance"
                    else self.allowed & set(resources))
        allow = permitted and (filtered or selected == set(resources))
        return PolicyDecision(
            allow=allow, allowed_ids=sorted(selected) if allow else [],
            projection="no_pii" if principal.role == "ops" else "full",
            reason="CURRENT_GRANT" if allow else "NO_CURRENT_GRANT", policy_revision=REVISION,
            entitlement_revision=revision,
        )

    async def decide(self, principal, action, resources, facts, revision, request_business_date):
        self.calls.append((action, resources, revision, request_business_date))
        return self.decision(principal, resources, revision)

    async def filter_candidates(
        self, principal, candidates, facts, revision, request_business_date,
    ):
        self.calls.append(("concentration_book", candidates, revision, request_business_date))
        return self.decision(principal, candidates, revision, filtered=True)


@pytest.fixture
def received_book(tmp_path: Path):
    database = tmp_path / "book.sqlite"
    migrate(database)
    migrate_business(database)
    migrate_feeds(database)
    crm = []
    for number, account in enumerate(REFERENCES, 1):
        household, person = f"WC-HH-{number}", f"WC-PER-{number}"
        crm.extend([
            row("household", f"household-{number}", id=household,
                name=f"Synthetic household {number}"),
            row("person", f"person-{number}", id=person, external_person_key=f"external-{number}",
                name=f"Synthetic person {number}", note="PRIVATE-NOTE"),
            row("membership", f"member-{number}", person_id=person, household_id=household),
            row("account", f"account-{number}", id=account.account_id, custodian="A",
                account_number=account.account_number, is_managed=True),
            row("reporting", f"report-{number}", account_id=account.account_id,
                household_id=household),
        ])
    receive(database, "CRM", "crm", crm)
    ownership = [row(
        "ownership", f"owner-{number}", custodian="A", account_number=account.account_number,
        title=f"Synthetic title {number}", registration="INDIVIDUAL", status="OPEN",
        owners=[{"entity_id": f"WC-PER-{number}", "entity_type": "person", "role": "owner"}],
    ) for number, account in enumerate(REFERENCES, 1)]
    receive(database, "CUSTODY-A", "custody_accounts", ownership)
    records = [row(
        "cash", f"cash-{number}", custodian="A", account_number=account.account_number,
        currency="USD", amount={1: "100", 2: "200", 3: "25", 4: "0", 5: "1000"}[number],
        valid_from=DAY.isoformat(), valid_to="2026-10-01",
    ) for number, account in enumerate(REFERENCES, 1)]
    records.append(row("position", "position-1", custodian="A",
                       account_number=REFERENCES[0].account_number, security_id="WC-SEC-1",
                       quantity="10", valid_from=DAY.isoformat(), valid_to="2026-10-01"))
    receive(database, "CUSTODY-A", "custody_snapshots", records, expected_accounts=REFERENCES)
    receive(database, "PAS-A", "pas_snapshots",
            [item for item in records if item["source_record_id"] != "cash-3"],
            intended_records=records, expected_accounts=REFERENCES, upstream_status="PARTIAL")
    receive(database, "SECMASTER", "securities", [row(
        "security", "security-1", id="WC-SEC-1", issuer_id="WC-ISS-1",
        issuer_name="Synthetic issuer", name="Synthetic security", ticker="SYN1",
        type="COMMON_STOCK", currency="USD", cusip="990000101", isin="US9900001010",
        identifier_status="synthetic_unallocated", listing_country="US",
    )])
    receive(database, "CUSTODY-A", "prices", [row(
        "price", "price-1", security_id="WC-SEC-1", currency="USD", price="10",
        price_date=DAY.isoformat(),
    )])
    policy = RecordedPolicy()
    provider = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, policy, provider.get_tracer("test"))
    yield service, policy
    provider.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["get_portfolio_exposure", "get_concentrated_positions"])
async def test_denied_or_invalid_financial_calls_have_no_query_or_calc_spans(received_book, tool):
    service, _policy = received_book
    for principal, arguments in (
        (Principal("ENG-01", "engineer"), {"as_of": "2026-09-30",
                                          "household_id": "WC-HH-1"}),
        (Principal("ADV-1", "advisor"), {"as_of": "bad-date"}),
    ):
        result = await service.execute(principal, NOW, tool, arguments)
        assert result["status"] == "ERROR"
        spans = [json.loads(line) for line in (service.database.parent / "traces.jsonl")
                 .read_text().splitlines()]
        names = {span["name"] for span in spans
                 if span["context"]["trace_id"].removeprefix("0x") == result["trace_id"]}
        assert "tool" in names
        assert not names & {"query", "calc"}


@pytest.mark.anyio
async def test_book_preserves_every_evaluated_owner_and_projects_raw_origin(
    received_book, monkeypatch,
):
    service, policy = received_book
    result = await service.execute(Principal("ADV-1", "advisor"), NOW,
                                   "get_concentrated_positions", {"as_of": DAY.isoformat()})
    assert result["status"] == "INCOMPLETE", result
    data = result["data"]
    assert data["evaluated_household_count"] == 4
    assert data["determinate_household_count"] == 2
    assert data["breaching_household_count"] == 1
    assert [item["household_id"] for item in data["breaches"]] == ["WC-HH-1"]
    assert [(item["household_id"], item["reason"]) for item in data["indeterminate"]] == [
        ("WC-HH-3", "INCOMPLETE_ASSETS"), ("WC-HH-4", "NON_POSITIVE_ASSETS"),
    ]
    assert "WC-HH-5" not in json.dumps(result)
    assert [call[0] for call in policy.calls] == [
        "concentration_book_preflight", "concentration_book",
    ]
    assert policy.calls[1][1] == [*ALLOWED, "WC-HH-5"]
    assert policy.calls[0][2:] == policy.calls[1][2:]
    with read_snapshot(service.database) as connection:
        assert [row[0] for row in connection.execute(
            "SELECT household_id FROM result_scopes WHERE result_id=? ORDER BY household_id",
            (result["result_id"],),
        )] == ALLOWED
        evidence = connection.execute("SELECT * FROM result_account_scopes WHERE result_id=?",
                                      (result["result_id"],)).fetchone()
        assert digest(evidence["payload"]) == evidence["payload_hash"]
        assert {row["account_id"] for row in json.loads(evidence["payload"])["accounts"]} == {
            "WC-AC-1", "WC-AC-2", "WC-AC-3", "WC-AC-4",
        }
        allowed = [row[0] for row in connection.execute(
            "SELECT a.household_id FROM decision_authorized_resources a "
            "JOIN decisions d ON d.id=a.decision_id WHERE d.trace_id=? ORDER BY a.household_id",
            (result["trace_id"],),
        )]
        assert allowed == ALLOWED
        raw_before = [tuple(row) for row in connection.execute(
            "SELECT id,payload,payload_hash FROM source_records ORDER BY id",
        )]
    provenance = await service.execute(
        Principal("ADV-1", "advisor"), NOW, "get_data_provenance",
        {"reference": result["source_refs"][0]},
    )
    assert provenance["status"] == "COMPLETE", provenance
    serialized = json.dumps(provenance)
    assert "WC-HH-5" not in serialized and "WC-AC-5" not in serialized
    assert "PRIVATE-NOTE" not in serialized
    manifests = [item["payload"]["manifest"] for item in provenance["data"]["items"]
                 if isinstance(item["payload"], dict) and "manifest" in item["payload"]]
    assert any("WC-AC-2" in manifest["expected_account_ids"] for manifest in manifests)
    with read_snapshot(service.database) as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT id,payload,payload_hash FROM source_records ORDER BY id",
        )] == raw_before
    policy.allowed.remove("WC-HH-2")  # A nonbreacher still owns the recorded result.

    def forbidden_projection(*args):
        pytest.fail("revoked origin released source evidence")

    monkeypatch.setattr(service_module, "project_source_record", forbidden_projection)
    denied = await service.execute(Principal("ADV-1", "advisor"), NOW,
                                   "get_data_provenance", {"reference": result["source_refs"][0]})
    assert denied["error"]["code"] == "NOT_AUTHORIZED"


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["get_portfolio_exposure", "get_concentrated_positions"])
async def test_explicit_denial_precedes_business_lookup_and_date_range(
    received_book, monkeypatch, tool,
):
    service, _ = received_book

    def forbidden(*args, **kw):
        pytest.fail("denied request reached received business facts")

    monkeypatch.setattr(service_module, "resolve_received_valuation", forbidden)
    for household in ("WC-HH-5", "WC-HH-missing"):
        for day in (DAY.isoformat(), "2026-09-01"):
            denied = await service.execute(Principal("ADV-1", "advisor"), NOW, tool,
                                           {"household_id": household, "as_of": day})
            assert denied["error"]["code"] == "NOT_AUTHORIZED"
            assert denied["valuation_date"] is None


@pytest.mark.anyio
async def test_book_preflight_denies_before_inventory_and_valid_empty_book_is_complete(
    received_book, monkeypatch,
):
    service, policy = received_book
    inventory = service.inventory

    def forbidden(*args):
        pytest.fail("unpermitted role reached inventory")

    monkeypatch.setattr(service, "inventory", forbidden)
    denied = await service.execute(Principal("OPS-1", "ops"), NOW,
                                   "get_concentrated_positions", {"as_of": DAY.isoformat()})
    assert denied["error"]["code"] == "NOT_AUTHORIZED"
    monkeypatch.setattr(service, "inventory", inventory)
    policy.allowed.clear()
    empty = await service.execute(Principal("ADV-1", "advisor"), NOW,
                                  "get_concentrated_positions", {"as_of": DAY.isoformat()})
    assert empty["status"] == "COMPLETE", empty
    assert empty["data"]["evaluated_household_count"] == 0
    assert empty["data"]["breaches"] == empty["data"]["indeterminate"] == []
    with read_snapshot(service.database) as connection:
        metadata = connection.execute("SELECT payload FROM result_account_scopes WHERE result_id=?",
                                      (empty["result_id"],)).fetchone()
        assert json.loads(metadata[0])["accounts"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("damage", ["hash", "delete"])
async def test_origin_metadata_corruption_errors_instead_of_using_visible_rows(
    received_book, damage,
):
    service, _ = received_book
    result = await service.execute(Principal("ADV-1", "advisor"), NOW, "get_portfolio_exposure",
                                   {"household_id": "WC-HH-1", "as_of": DAY.isoformat()})
    assert result["status"] == "COMPLETE", result
    with write_transaction(service.database) as connection:
        if damage == "hash":
            connection.execute("UPDATE result_account_scopes SET payload='{}' WHERE result_id=?",
                               (result["result_id"],))
        else:
            connection.execute("DELETE FROM result_account_scopes WHERE result_id=?",
                               (result["result_id"],))
    reply = await service.execute(Principal("ADV-1", "advisor"), NOW, "get_data_provenance",
                                  {"reference": result["source_refs"][0]})
    assert reply["error"]["code"] == "INTERNAL"


@pytest.mark.anyio
async def test_received_book_scope_loss_blocks_history_after_partial_revocation(received_book):
    service, policy = received_book
    principal = Principal("ADV-1", "advisor")
    manager = ConversationManager(service)
    conversation = manager.start(principal, NOW)
    result = await manager.execute_tool(
        principal, NOW, conversation, "book", "get_concentrated_positions",
        {"as_of": DAY.isoformat()},
    )
    assert result["status"] == "INCOMPLETE"
    # HH2 is evaluated but has no breach/indeterminate row. Its scope remains
    # part of the original complete observation even when absent from the list.
    assert "WC-HH-2" not in json.dumps(result["data"])
    with write_transaction(service.database) as connection:
        connection.execute("DELETE FROM result_scopes WHERE result_id=? AND household_id=?",
                           (result["result_id"], "WC-HH-2"))
    policy.allowed.remove("WC-HH-2")
    for view in (manager.prepare_turn, manager.render_history):
        current = await view(principal, NOW, conversation)
        assert current["status"] == "ERROR" and current["error"]["code"] == "INTERNAL"
        assert current["data"] is None


@pytest.mark.anyio
async def test_book_missing_historical_household_is_indeterminate_not_silently_omitted(
    received_book,
):
    service, _ = received_book
    result = await service.execute(Principal("ADV-1", "advisor"), NOW,
                                   "get_concentrated_positions", {
                                       "as_of": DAY.isoformat(),
                                       "known_as_of": "2026-09-30T21:59:59Z",
                                   })
    assert result["status"] == "INCOMPLETE", result
    assert result["data"]["evaluated_household_count"] == 4
    assert [item["household_id"] for item in result["data"]["indeterminate"]] == ALLOWED
    assert all(item["reason"] == "INCOMPLETE_ASSETS" for item in result["data"]["indeterminate"])
    assert result["data"]["breaches"] == []
    assert {flag["subject"] for flag in result["quality_flags"]} == set(ALLOWED)


@pytest.mark.anyio
async def test_book_compliance_inventory_contains_published_uncovered_household(received_book):
    service, _ = received_book
    result = await service.execute(Principal("CMP-1", "compliance"), NOW,
                                   "get_concentrated_positions", {"as_of": DAY.isoformat()})
    assert result["status"] == "INCOMPLETE", result
    assert result["data"]["evaluated_household_count"] == 5
    with read_snapshot(service.database) as connection:
        assert [row[0] for row in connection.execute(
            "SELECT household_id FROM result_scopes WHERE result_id=? ORDER BY household_id",
            (result["result_id"],),
        )] == [*ALLOWED, "WC-HH-5"]


@pytest.mark.anyio
async def test_new_account_evidence_failure_rolls_back_whole_response_and_decisions(received_book):
    service, _ = received_book
    with write_transaction(service.database) as connection:
        connection.execute(
            "CREATE TRIGGER fail_account_scope BEFORE INSERT ON result_account_scopes "
            "BEGIN SELECT RAISE(ABORT, 'injected evidence persistence failure'); END",
        )
    result = await service.execute(Principal("ADV-1", "advisor"), NOW,
                                   "get_concentrated_positions", {"as_of": DAY.isoformat()})
    assert result["error"]["code"] == "INTERNAL"
    assert result["data"] is None and result["source_refs"] == []
    with read_snapshot(service.database) as connection:
        for table in ("results", "decisions", "result_scopes", "provenance_handles",
                      "result_account_scopes", "decision_authorized_resources"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.anyio
async def test_book_keeps_one_entitlement_snapshot_when_revision_commits_mid_request(received_book):
    service, policy = received_book
    original_decide = policy.decide
    changed = False

    async def decide(principal, action, resources, facts, revision, request_business_date):
        nonlocal changed
        decision = await original_decide(principal, action, resources, facts, revision,
                                         request_business_date)
        if action == "concentration_book_preflight" and not changed:
            with write_transaction(service.database) as connection:
                connection.execute("INSERT INTO entitlement_revisions VALUES (1, ?)",
                                   (utc_timestamp(NOW),))
                connection.execute(
                    "INSERT INTO entitlement_facts VALUES (1, 'advisor_covers', 'ADV-1', "
                    "'WC-HH-target-only', '2026-09-14', '9999-12-31')",
                )
            changed = True
        return decision

    policy.decide = decide
    first = await service.execute(Principal("ADV-1", "advisor"), NOW,
                                  "get_concentrated_positions", {"as_of": DAY.isoformat()})
    assert first["entitlement_revision"] == 0
    assert [call[2] for call in policy.calls] == [0, 0]
    assert "WC-HH-target-only" not in policy.calls[1][1]
    policy.calls.clear()
    second = await service.execute(Principal("ADV-1", "advisor"), NOW,
                                   "get_concentrated_positions", {"as_of": DAY.isoformat()})
    assert second["entitlement_revision"] == 1
    assert [call[2] for call in policy.calls] == [1, 1]
    assert "WC-HH-target-only" in policy.calls[1][1]


@pytest.mark.parametrize("value", ["0.99", "50.01", "51", "NaN", "Infinity", "1e1", 10, True,
                                   "10.001", " 10.00", "01.00"])
def test_shared_threshold_contract_rejects_unsafe_or_out_of_range_values(value):
    with pytest.raises(ValidationError):
        ConcentrationArguments.model_validate({"as_of": DAY.isoformat(), "threshold_pct": value})


@pytest.mark.parametrize("value,normalized", [("1", "1.00"), ("10.1", "10.10"),
                                             ("50.00", "50.00")])
def test_shared_arguments_normalize_percent_and_publish_group_default(value, normalized):
    args = ConcentrationArguments.model_validate({"as_of": DAY.isoformat(), "threshold_pct": value})
    assert args.threshold_pct == normalized
    assert ExposureArguments.model_validate({"household_id": "WC-HH-1",
                                             "as_of": DAY.isoformat()}).group_by == "issuer"
