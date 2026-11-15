"""Household relationships, exclusions and reconciliation over received assertions."""

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal

from pydantic import JsonValue

from wealthcontext.contracts import QualityCode, QualityFlag
from wealthcontext.facts import AccountSnapshot, FactView, account_snapshot, fact_view
from wealthcontext.reconciliation import AccountReconciliation, reconcile_account


@dataclass
class AccountContext:
    account: dict
    ownership: dict | None
    pas: AccountSnapshot
    custody: AccountSnapshot
    transactions: list[dict]
    lots: list[dict]
    reconciliation: AccountReconciliation | None


@dataclass
class HouseholdContext:
    household: dict | None
    members: list[dict] = field(default_factory=list)
    accounts: list[AccountContext] = field(default_factory=list)
    excluded_accounts: list[dict] = field(default_factory=list)
    quality_flags: list[QualityFlag] = field(default_factory=list)
    missing_components: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)


def _sources(rows: list[dict]) -> list[str]:
    return sorted({row["source_id"] for row in rows if row.get("source_id")})


def _assertions(view: FactView) -> list[dict]:
    return [*view.values.values(), *(row for group in view.ambiguous.values() for row in group)]


def _ownership_fields(value: dict) -> dict:
    return {key: sorted(value[key], key=lambda owner: owner["entity_id"]) if key == "owners"
            else value[key] for key in ("title", "registration", "status", "owners")}


def household_views(
    connection: sqlite3.Connection, day: date, known: datetime,
) -> dict[str, FactView]:
    return {kind: fact_view(connection, kind, day, known) for kind in (
        "household", "person", "membership", "account", "reporting", "ownership",
        "crm_ownership", "transaction", "lot",
    )}


def household_context(
    connection: sqlite3.Connection, household_id: str, day: date, known: datetime,
    *, views: dict[str, FactView] | None = None,
) -> HouseholdContext:
    """Read one already-authorized household without conflating membership and ownership.

    Counterpart household identifiers are redacted here until an authorization-aware
    projection is supplied. Source IDs retain lineage; the provenance surface must
    independently project raw records before releasing them to a caller.
    """
    views = household_views(connection, day, known) if views is None else views
    result = HouseholdContext(views["household"].values.get(household_id))
    sources: set[str] = set()
    missing: set[str] = set()

    def issue(
        code: QualityCode, subject: str, detail: dict[str, JsonValue], records: list[dict],
        *, blocking: bool = False, conflict: bool = True,
    ) -> None:
        evidence = _sources(records)
        sources.update(evidence)
        result.quality_flags.append(QualityFlag(code=code, severity="warning", subject=subject,
                                               detail=detail))
        if blocking:
            missing.add(subject)
        if conflict:
            result.conflicts.append({"code": code, "subject": subject,
                                     "current_household_id": household_id, "detail": detail,
                                     "source_ids": evidence})

    if household_id in views["household"].ambiguous:
        issue("AMBIGUOUS_ENTITY", household_id, {"entity_type": "household"},
              views["household"].ambiguous[household_id], blocking=True)
    elif result.household is None:
        return result
    else:
        sources.add(result.household["source_id"])

    memberships = views["membership"]
    persons = views["person"]
    external_ids: dict[str, set[str]] = {}
    for person in _assertions(persons):
        external_ids.setdefault(person["external_person_key"], set()).add(person["id"])
    duplicate_persons = {person_id for group in external_ids.values() if len(group) > 1
                         for person_id in group}
    current_members: set[str] = set()
    for person_id, assertions in sorted(memberships.ambiguous.items()):
        relevant = [row for row in assertions if row["household_id"] == household_id]
        if relevant:
            issue("AMBIGUOUS_ENTITY", person_id, {"entity_type": "membership"}, relevant,
                  blocking=True)
    for person_id, membership in sorted(memberships.values.items()):
        if membership["household_id"] != household_id:
            continue
        current_members.add(person_id)
        sources.add(membership["source_id"])
        if person_id in persons.ambiguous or person_id in duplicate_persons:
            own_assertions = persons.ambiguous.get(person_id, [])
            if person_id in persons.values:
                own_assertions = [persons.values[person_id]]
            issue("AMBIGUOUS_ENTITY", person_id, {"entity_type": "person"},
                  [membership, *own_assertions], blocking=True)
        elif person_id not in persons.values:
            issue("SOURCE_UNAVAILABLE", person_id, {"source": "CRM", "entity_type": "person"},
                  [membership], blocking=True, conflict=False)
        else:
            person = persons.values[person_id]
            sources.add(person["source_id"])
            result.members.append({**person, "membership_source_id": membership["source_id"]})

    reporting = views["reporting"]
    for account_id, assertions in sorted(reporting.ambiguous.items()):
        relevant = [row for row in assertions if row["household_id"] == household_id]
        if relevant:
            issue("AMBIGUOUS_ENTITY", account_id, {"entity_type": "reporting_household"},
                  relevant, blocking=True)
    for account_id, assignment in sorted(reporting.values.items()):
        if assignment["household_id"] != household_id:
            continue
        sources.add(assignment["source_id"])
        account = views["account"].values.get(account_id)
        if account_id in views["account"].ambiguous:
            issue("AMBIGUOUS_ENTITY", account_id, {"entity_type": "account"},
                  [assignment, *views["account"].ambiguous[account_id]], blocking=True)
            continue
        if account is None:
            issue("SOURCE_UNAVAILABLE", account_id, {"source": "CRM", "entity_type": "account"},
                  [assignment], blocking=True, conflict=False)
            continue
        sources.add(account["source_id"])
        if not account["is_managed"]:
            result.excluded_accounts.append({**account, "reason": "HELD_AWAY_EXCLUDED"})
            result.quality_flags.append(QualityFlag(code="HELD_AWAY_EXCLUDED", severity="info",
                                                   subject=account_id))
            continue

        ownership = views["ownership"].values.get(account_id)
        if ownership is not None:
            sources.add(ownership["source_id"])
            nonmembers = sorted(owner["entity_id"] for owner in ownership["owners"]
                                if owner["entity_type"] == "person"
                                and owner["entity_id"] not in current_members)
            if nonmembers:
                issue("OWNERSHIP_CONFLICT", account_id,
                      {"reason": "LEGAL_OWNER_NOT_CURRENT_MEMBER",
                       "legal_owner_ids": list[JsonValue](nonmembers),
                       "membership_source": "CRM", "ownership_source": "CUSTODY"},
                      [assignment, ownership])
            crm = views["crm_ownership"].values.get(account_id)
            if crm and _ownership_fields(crm) != _ownership_fields(ownership):
                changed = [key for key, value in _ownership_fields(crm).items()
                           if value != _ownership_fields(ownership)[key]]
                issue("OWNERSHIP_CONFLICT", account_id,
                      {"reason": "CRM_CUSTODY_DISAGREEMENT", "authoritative_source": "CUSTODY",
                       "fields": list[JsonValue](changed)}, [ownership, crm])
            if account_id in views["crm_ownership"].ambiguous:
                issue("OWNERSHIP_CONFLICT", account_id,
                      {"reason": "AMBIGUOUS_CRM_OWNERSHIP", "authoritative_source": "CUSTODY"},
                      [ownership, *views["crm_ownership"].ambiguous[account_id]])
        elif account_id in views["ownership"].ambiguous:
            issue("OWNERSHIP_CONFLICT", account_id, {"reason": "AMBIGUOUS_CUSTODY_OWNERSHIP"},
                  views["ownership"].ambiguous[account_id])
        else:
            issue("OWNERSHIP_UNAVAILABLE", account_id,
                  {"source": "CUSTODY", "entity_type": "ownership",
                   "reason": "MISSING_CUSTODY_OWNERSHIP", "affects_valuation": False},
                  [account], conflict=False)

        pas = account_snapshot(connection, "pas_snapshots", account_id, day, known,
                               accounts_view=views["account"])
        custody = account_snapshot(connection, "custody_snapshots", account_id, day, known,
                                   accounts_view=views["account"])
        sources.update(pas.source_ids)
        sources.update(custody.source_ids)
        for grouping in pas.groupings:
            if grouping["household_id"] != household_id:
                issue("REPORTING_HOUSEHOLD_CONFLICT", account_id,
                      {"authoritative_source": "CRM", "household_id": household_id,
                       "counterpart_household_id": "[REDACTED]"}, [assignment, grouping])
        pas = replace(pas, groupings=[
            {**grouping, "household_id": (household_id if grouping["household_id"] == household_id
                                          else "[REDACTED]")}
            for grouping in pas.groupings
        ])
        transactions = sorted((row for row in views["transaction"].values.values()
                               if row["account_id"] == account_id), key=lambda row: row["id"])
        lots = sorted((row for row in views["lot"].values.values()
                       if row["account_id"] == account_id), key=lambda row: row["id"])
        sources.update(_sources([*transactions, *lots]))
        ambiguous_transactions = [row for group in views["transaction"].ambiguous.values()
                                  for row in group if row["account_id"] == account_id]
        reconciliation = None
        if not pas.available:
            issue("SOURCE_UNAVAILABLE", account_id, {"source": "PAS", "reason": pas.reason},
                  [], blocking=True, conflict=False)
            if not custody.available:
                issue("RECONCILIATION_UNAVAILABLE", account_id,
                      {"source": "CUSTODY", "reason": custody.reason}, [], conflict=False)
        elif ambiguous_transactions:
            issue("RECONCILIATION_UNAVAILABLE", account_id,
                  {"reason": "AMBIGUOUS_TRANSACTIONS"}, ambiguous_transactions)
        else:
            reconciliation = reconcile_account(
                account_id, day, known,
                {key: row["quantity"] for key, row in pas.positions.items()},
                {key: row["amount"] for key, row in pas.cash.items()},
                ({key: row["quantity"] for key, row in custody.positions.items()}
                 if custody.available else None),
                ({key: row["amount"] for key, row in custody.cash.items()}
                 if custody.available else None), transactions,
            )
            result.quality_flags.extend(reconciliation.quality_flags)
            for flag in reconciliation.quality_flags:
                if flag.code in {"QUANTITY_BREAK", "CASH_BREAK"}:
                    result.conflicts.append({
                        "code": flag.code, "subject": flag.subject,
                        "current_household_id": household_id, "detail": flag.detail,
                        "source_ids": sorted(set(pas.source_ids + custody.source_ids
                                                 + _sources(transactions))),
                    })
        if pas.available and ownership and ownership["status"] == "CLOSED" and (
            any(Decimal(row["quantity"]) != 0 for row in pas.positions.values())
            or any(Decimal(row["amount"]) != 0 for row in pas.cash.values())
        ):
            result.quality_flags.append(QualityFlag(
                code="CLOSED_ACCOUNT_WITH_BALANCE", severity="warning", subject=account_id,
            ))
        result.accounts.append(AccountContext(account, ownership, pas, custody,
                                               transactions, lots, reconciliation))
    result.missing_components = sorted(missing)
    result.source_ids = sorted(sources)
    return result
