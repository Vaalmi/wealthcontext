import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from wealthcontext.clock import utc_timestamp
from wealthcontext.database import connect, migrate
from wealthcontext.households import household_context
from wealthcontext.ingestion import migrate_feeds
from wealthcontext.storage import canonical, digest, migrate_business

HH = "WC-HH-0001"
OTHER_HH = "WC-HH-0002"
ACCOUNT = "WC-AC-0001"
PERSON = "WC-PER-0001"
SECURITY = "WC-SEC-0001"
DAY = date(2026, 9, 30)
KNOWN = datetime(2026, 10, 5, tzinfo=UTC)
RECORDED = "2026-09-30T22:00:00.000000+00:00"


class ReceivedFacts:
    """Hand-built received assertions, independent of the synthetic generator."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.sequence = 0

    def source(self, payload: dict, *, system="CRM", run_id="HAND-RAW",
               recorded=RECORDED, day=DAY) -> str:
        self.sequence += 1
        source_id = f"SRC-HAND-{self.sequence}"
        text = canonical(payload)
        self.connection.execute(
            "INSERT INTO source_records "
            "(id,source_system,source_record_id,business_date,recorded_at,imported_at,payload,"
            "payload_hash,feed_run_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (source_id, system, source_id, day.isoformat(), recorded, recorded,
             text, digest(text), run_id),
        )
        return source_id

    def fact(self, kind: str, entity: str, payload: dict, *, start="2026-09-14",
             end="9999-12-31", recorded=RECORDED, system="CRM", run_id="HAND-RAW") -> str:
        source_id = self.source(payload, system=system, run_id=run_id, recorded=recorded)
        self.connection.execute("INSERT INTO facts VALUES (?,?,?,?,?,?,?)",
                                (kind, entity, start, end, recorded, source_id, canonical(payload)))
        return source_id

    def household(self, household=HH, person=PERSON, *, member_end="9999-12-31"):
        self.fact("household", household, {"id": household, "name": f"Synthetic {household}"})
        self.fact("person", person, {"id": person, "name": f"Synthetic {person}",
                                     "external_person_key": f"EXT-{person}"})
        self.fact("membership", person, {"person_id": person, "household_id": household},
                  end=member_end)

    def account(self, account=ACCOUNT, household=HH, *, managed=True, owner=PERSON,
                status="OPEN", custodian="A", account_number="00420017", with_ownership=True):
        self.fact("account", account, {"id": account, "custodian": custodian,
                                       "account_number": account_number, "is_managed": managed})
        self.fact("reporting", account, {"account_id": account, "household_id": household})
        if managed and with_ownership:
            self.fact("ownership", account, {
                "account_id": account, "title": "Synthetic account", "registration": "INDIVIDUAL",
                "status": status,
                "owners": [{"entity_id": owner, "entity_type": "person", "role": "owner"}],
            }, system=f"CUSTODY-{custodian}")

    def snapshot(self, feed: str, *, account=ACCOUNT, day=DAY, positions=None, cash=None,
                 groupings=None, available=True, recorded=RECORDED) -> list[str]:
        run_id = f"RUN-HAND-{self.sequence + 1}"
        system = "PAS-A" if feed == "pas_snapshots" else "CUSTODY-A"
        receipt = self.source({"manifest_account": account}, system=system, run_id=run_id,
                              recorded=recorded, day=day)
        sources = []
        for security, quantity in (positions or {}).items():
            sources.append(self.fact("position" if feed == "pas_snapshots" else "custody_position",
                                     f"{account}:{security}", {"id": f"{account}:{security}",
                                     "account_id": account, "security_id": security,
                                     "quantity": quantity}, start=day.isoformat(),
                                     run_id=run_id, recorded=recorded, system=system))
        for currency, amount in (cash or {}).items():
            sources.append(self.fact("cash" if feed == "pas_snapshots" else "custody_cash",
                                     f"{account}:{currency}", {"id": f"{account}:{currency}",
                                     "account_id": account, "currency": currency, "amount": amount},
                                     start=day.isoformat(), run_id=run_id,
                                     recorded=recorded, system=system))
        for household in groupings or []:
            sources.append(self.fact("pas_grouping", account, {
                "account_id": account, "household_id": household,
            }, start=day.isoformat(), run_id=run_id, recorded=recorded, system=system))
        self.connection.execute("INSERT INTO feed_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            run_id, system, feed, day.isoformat(), run_id, None, None, "SUCCESS",
            "SUCCESS" if available else "FAILED", recorded, recorded, len(sources), 0, receipt,
        ))
        self.connection.execute("INSERT INTO account_availability VALUES (?,?,?,?,?)", (
            run_id, account, int(available), None if available else "MANIFEST_MISMATCH",
            canonical(sources if available else []),
        ))
        return [receipt, *sources]


@pytest.fixture
def received(tmp_path: Path):
    path = tmp_path / "households.sqlite"
    migrate(path)
    migrate_business(path)
    migrate_feeds(path)
    connection = connect(path)
    yield ReceivedFacts(connection)
    connection.close()


def test_crm_reporting_wins_over_pas_grouping_with_redacted_disclosed_conflict(received):
    received.household()
    received.account()
    pas_sources = received.snapshot("pas_snapshots", positions={SECURITY: "10"},
                                    cash={"USD": "500"}, groupings=[OTHER_HH])
    received.snapshot("custody_snapshots", positions={SECURITY: "10"}, cash={"USD": "500"})
    result = household_context(received.connection, HH, DAY, KNOWN)
    assert result.missing_components == []
    assert len(result.accounts) == 1
    assert result.accounts[0].account["id"] == ACCOUNT
    assert result.accounts[0].pas.groupings[0]["household_id"] == "[REDACTED]"
    assert [flag.code for flag in result.quality_flags] == ["REPORTING_HOUSEHOLD_CONFLICT"]
    assert result.conflicts[0]["detail"]["authoritative_source"] == "CRM"
    assert result.conflicts[0]["current_household_id"] == HH
    assert set(pas_sources) <= set(result.source_ids)
    assert OTHER_HH not in json.dumps(asdict(result), default=str)


@pytest.mark.parametrize("different_canonical_id", [False, True])
def test_duplicate_person_assertions_block_affected_membership(received, different_canonical_id):
    received.household()
    duplicate = "WC-PER-OTHER" if different_canonical_id else PERSON
    received.fact("person", duplicate, {"id": duplicate, "name": "Conflicting identity",
                                       "external_person_key": f"EXT-{PERSON}"})
    result = household_context(received.connection, HH, DAY, KNOWN)
    assert result.members == []
    assert result.missing_components == [PERSON]
    assert result.quality_flags[0].code == "AMBIGUOUS_ENTITY"
    assert result.conflicts[0]["detail"] == {"entity_type": "person"}


def test_ambiguous_membership_blocks_each_candidate_household_without_disclosing_other(received):
    received.household()
    received.fact("household", OTHER_HH, {"id": OTHER_HH, "name": "Other synthetic household"})
    counterpart_source = received.fact("membership", PERSON,
                                       {"person_id": PERSON, "household_id": OTHER_HH})
    first = household_context(received.connection, HH, DAY, KNOWN)
    second = household_context(received.connection, OTHER_HH, DAY, KNOWN)
    assert first.missing_components == second.missing_components == [PERSON]
    assert first.members == second.members == []
    assert counterpart_source not in first.source_ids
    assert OTHER_HH not in json.dumps(asdict(first), default=str)


def test_ambiguous_reporting_does_not_choose_or_double_count_account(received):
    received.household()
    received.account()
    counterpart = received.fact("reporting", ACCOUNT,
                                {"account_id": ACCOUNT, "household_id": OTHER_HH})
    result = household_context(received.connection, HH, DAY, KNOWN)
    assert result.accounts == []
    assert result.missing_components == [ACCOUNT]
    assert result.quality_flags[0].code == "AMBIGUOUS_ENTITY"
    assert counterpart not in result.source_ids


def test_ex_spouse_membership_ends_but_jtwros_legal_ownership_remains(received):
    spouse = "WC-PER-0002"
    received.household()
    received.fact("person", spouse, {"id": spouse, "name": "Synthetic former spouse",
                                     "external_person_key": "EXT-SPOUSE"})
    received.fact("membership", spouse, {"person_id": spouse, "household_id": HH},
                  end="2026-10-01")
    received.account()
    owners = [{"entity_id": entity, "entity_type": "person", "role": "joint_owner"}
              for entity in [PERSON, spouse]]
    received.fact("ownership", ACCOUNT, {"account_id": ACCOUNT, "title": "Joint account",
                  "registration": "JTWROS", "status": "OPEN", "owners": owners},
                  recorded="2026-09-30T22:01:00.000000+00:00", system="CUSTODY-A")
    day = date(2026, 10, 1)
    received.snapshot("pas_snapshots", day=day, cash={"USD": "100"})
    received.snapshot("custody_snapshots", day=day, cash={"USD": "100"})
    result = household_context(received.connection, HH, day, KNOWN)
    assert [member["id"] for member in result.members] == [PERSON]
    assert result.accounts[0].ownership is not None
    assert result.accounts[0].ownership["owners"] == owners
    assert result.missing_components == []
    assert [flag.code for flag in result.quality_flags] == ["OWNERSHIP_CONFLICT"]
    assert result.conflicts[0]["detail"]["legal_owner_ids"] == [spouse]
    assert "percentage" not in json.dumps(asdict(result), default=str)


def test_missing_pas_blocks_without_reconciliation_from_invented_zero(received):
    received.household()
    received.account()
    # Yesterday's complete publication cannot fill today's missing PAS feed.
    received.snapshot("pas_snapshots", day=date(2026, 9, 29), cash={"USD": "100"})
    received.snapshot("custody_snapshots", cash={"USD": "100"})
    result = household_context(received.connection, HH, DAY, KNOWN)
    assert result.missing_components == [ACCOUNT]
    assert result.accounts[0].pas.available is False
    assert result.accounts[0].reconciliation is None
    assert [flag.code for flag in result.quality_flags] == ["SOURCE_UNAVAILABLE"]


def test_partial_custody_is_secondary_warning_and_never_matches(received):
    received.household()
    received.account()
    received.snapshot("pas_snapshots", positions={SECURITY: "10"}, cash={"USD": "100"})
    received.snapshot("custody_snapshots", positions={SECURITY: "10"}, available=False)
    result = household_context(received.connection, HH, DAY, KNOWN)
    assert result.missing_components == []
    assert [flag.code for flag in result.quality_flags] == ["RECONCILIATION_UNAVAILABLE"]
    recon = result.accounts[0].reconciliation
    assert recon is not None
    assert {row.reconciliation_status for row in recon.rows} == {"RECONCILIATION_UNAVAILABLE"}
    assert all(row.settled is None for row in recon.rows)


def test_missing_custody_ownership_warns_without_inventing_metadata_or_blocking_assets(received):
    received.household()
    received.account(with_ownership=False)
    received.snapshot("pas_snapshots", cash={"USD": "500"})
    received.snapshot("custody_snapshots", cash={"USD": "500"})
    result = household_context(received.connection, HH, DAY, KNOWN)
    assert result.accounts[0].ownership is None
    assert result.missing_components == []
    assert result.conflicts == []
    assert len(result.quality_flags) == 1
    flag = result.quality_flags[0]
    assert flag.code == "OWNERSHIP_UNAVAILABLE" and flag.severity == "warning"
    assert flag.subject == ACCOUNT
    assert flag.detail == {
        "source": "CUSTODY", "entity_type": "ownership",
        "reason": "MISSING_CUSTODY_OWNERSHIP", "affects_valuation": False,
    }
    reconciliation = result.accounts[0].reconciliation
    assert reconciliation is not None
    assert reconciliation.rows[0].reconciliation_status == "MATCHED"


def test_closed_account_balance_is_included_and_held_away_is_listed_without_feed_failure(received):
    received.household()
    received.account(status="CLOSED")
    held_away = "WC-AC-HELD"
    received.account(held_away, managed=False, account_number="HELD")
    received.snapshot("pas_snapshots", positions={SECURITY: "1"})
    received.snapshot("custody_snapshots", positions={SECURITY: "1"})
    result = household_context(received.connection, HH, DAY, KNOWN)
    assert [account.account["id"] for account in result.accounts] == [ACCOUNT]
    assert [account["id"] for account in result.excluded_accounts] == [held_away]
    assert {flag.code for flag in result.quality_flags} == {
        "CLOSED_ACCOUNT_WITH_BALANCE", "HELD_AWAY_EXCLUDED",
    }
    assert result.missing_components == []


def test_custody_registration_wins_over_contradictory_crm_assertion(received):
    received.household()
    received.account()
    received.fact("crm_ownership", ACCOUNT, {
        "account_id": ACCOUNT, "title": "CRM title", "registration": "ROTH_IRA", "status": "OPEN",
        "owners": [{"entity_id": PERSON, "entity_type": "person", "role": "owner"}],
    })
    received.snapshot("pas_snapshots")
    received.snapshot("custody_snapshots")
    result = household_context(received.connection, HH, DAY, KNOWN)
    assert result.accounts[0].ownership is not None
    assert result.accounts[0].ownership["registration"] == "INDIVIDUAL"
    assert result.missing_components == []
    assert result.conflicts[0]["detail"] == {
        "reason": "CRM_CUSTODY_DISAGREEMENT", "authoritative_source": "CUSTODY",
        "fields": ["title", "registration"],
    }


def test_received_transactions_lots_and_sources_flow_into_reconciliation(received):
    received.household()
    received.account()
    tx = {"id": "WC-TX-0001", "account_id": ACCOUNT, "security_id": SECURITY,
          "trade_date": "2026-09-30", "settle_date": "2026-10-01", "quantity_delta": "10",
          "cash_delta_local": "-251", "currency": "USD", "status": "ACTIVE"}
    tx_source = received.fact("transaction", tx["id"], tx, system="PAS-A")
    lot_source = received.fact("lot", "WC-LOT-0001", {"id": "WC-LOT-0001", "account_id": ACCOUNT,
                                                    "security_id": SECURITY, "quantity": "10"})
    received.snapshot("pas_snapshots", positions={SECURITY: "10"}, cash={"USD": "9749"})
    received.snapshot("custody_snapshots", cash={"USD": "10000"})
    result = household_context(received.connection, HH, DAY, KNOWN)
    account = result.accounts[0]
    assert account.transactions[0]["id"] == tx["id"]
    assert account.lots[0]["id"] == "WC-LOT-0001"
    assert {tx_source, lot_source} <= set(result.source_ids)
    assert account.reconciliation is not None
    assert {row.reconciliation_status for row in account.reconciliation.rows} == {
        "PENDING_SETTLEMENT",
    }


def test_relationship_knowledge_cutoff_does_not_include_later_corrections(received):
    received.household()
    corrected_at = datetime(2026, 10, 6, 14, tzinfo=UTC)
    received.fact("membership", PERSON, {"person_id": PERSON, "household_id": OTHER_HH},
                  recorded=utc_timestamp(corrected_at))
    before = household_context(received.connection, HH, DAY, KNOWN)
    after = household_context(received.connection, HH, DAY, corrected_at)
    assert [member["id"] for member in before.members] == [PERSON]
    assert after.members == []


def test_absent_household_returns_no_invented_context(received):
    result = household_context(received.connection, HH, DAY, KNOWN)
    assert result.household is None
    assert result.accounts == result.members == result.source_ids == []
