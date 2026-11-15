"""Point-in-time canonical assertions and manifest-backed account snapshots."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from wealthcontext.clock import utc_timestamp
from wealthcontext.storage import canonical


@dataclass
class FactView:
    values: dict[str, dict] = field(default_factory=dict)
    ambiguous: dict[str, list[dict]] = field(default_factory=dict)


def _applicable_versions(rows: Sequence[sqlite3.Row], day: date) -> list[sqlite3.Row]:
    """Supersede intervals before activity checks, retaining tied applicability."""
    versions: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        group = versions.setdefault((row["entity_id"], row["valid_from"]), [])
        if not group or group[0]["recorded_at"] == row["recorded_at"]:
            group.append(row)
    return sorted((row for group in versions.values()
                   if any(day.isoformat() < item["valid_to"] for item in group)
                   for row in group), key=lambda row: row["recorded_at"], reverse=True)


def active_household_ids(connection: sqlite3.Connection, day: date, known: datetime) -> list[str]:
    """Received active/ambiguous IDs only; never load private names before OPA."""
    rows = connection.execute(
        "SELECT f.entity_id,f.valid_from,f.valid_to,f.recorded_at FROM facts f "
        "JOIN source_records s ON s.id=f.source_id WHERE f.kind='household' "
        "AND f.valid_from<=? AND f.recorded_at<=? AND s.feed_run_id IS NOT NULL "
        "ORDER BY f.entity_id,f.valid_from,f.recorded_at DESC,f.source_id",
        (day.isoformat(), utc_timestamp(known)),
    ).fetchall()
    return sorted({row["entity_id"] for row in _applicable_versions(rows, day)})


def _crosswalk(view: FactView) -> dict[tuple[str, str], str | None]:
    result: dict[tuple[str, str], str | None] = {}
    for item in view.values.values():
        key = item["custodian"], item["account_number"]
        result[key] = item["id"] if key not in result else None
    for group in view.ambiguous.values():
        for item in group:
            result[item["custodian"], item["account_number"]] = None
    return result


def fact_view(
    connection: sqlite3.Connection, kind: str, day: date, known: datetime,
    *, entity_ids: Sequence[str] | None = None,
) -> FactView:
    """Equal knowledge-time contradictions have no lexical source-ID winner.

    P2 canonical reads use received-file-backed assertions. P1's small direct seed
    remains available to its isolated hand-fixture tests and recorded provenance.
    """
    # Household search filters canonical identifiers through OPA before loading
    # names. Keep the same interval/tie resolver, with the restriction in SQL.
    # Relationship/crosswalk views still require their complete assertion graph.
    restriction = ""
    parameters: tuple = (kind, day.isoformat(), utc_timestamp(known))
    if entity_ids is not None:
        if kind != "household":
            raise ValueError("scoped identity reads currently support households only")
        if not entity_ids:
            return FactView()
        restriction = "AND f.entity_id IN (" + ",".join("?" for _ in entity_ids) + ") "
        parameters += tuple(entity_ids)
    rows = connection.execute(
        "SELECT f.entity_id,f.payload,f.source_id,f.recorded_at,f.valid_from,f.valid_to,"
        "s.source_system,s.business_date "
        "FROM facts f JOIN source_records s ON s.id=f.source_id "
        "WHERE f.kind=? AND f.valid_from<=? AND f.recorded_at<=? "
        "AND s.feed_run_id IS NOT NULL "
        + restriction +
        "ORDER BY f.entity_id,f.valid_from,f.recorded_at DESC,f.source_id",
        parameters,
    ).fetchall()
    # A received correction that shortens valid_to must supersede the old interval
    # before filtering for the valuation date, or ended memberships would reappear.
    candidates: dict[str, list[dict]] = {}
    # Tied assertions can disagree whether an interval is still active. Keep
    # both sides when at least one applies; filtering the closed side first
    # would silently turn an unresolved relationship into an open one.
    current = _applicable_versions(rows, day)
    for row in current:
        group = candidates.setdefault(row["entity_id"], [])
        if not group or group[0]["recorded_at"] == row["recorded_at"]:
            group.append({**json.loads(row["payload"]), "source_id": row["source_id"],
                          "recorded_at": row["recorded_at"], "source_system": row["source_system"],
                          "_valid_at_date": day.isoformat() < row["valid_to"],
                          **({"_fx_file_date": row["business_date"]} if kind == "fx" else {})})
    if kind in {"ownership", "crm_ownership", "transaction", "lot", "position",
                "cash", "custody_position", "custody_cash", "pas_grouping"}:
        mapping = _crosswalk(fact_view(connection, "account", day, known))
        rebound: dict[str, list[dict]] = {}
        for entity, group in candidates.items():
            for item in group:
                target = entity
                if "custodian" in item and "account_number" in item:
                    account = mapping.get((item["custodian"], item["account_number"]))
                    item = {**item, "account_id": account}
                    if kind in {"ownership", "crm_ownership", "pas_grouping"}:
                        target = account or entity
                    elif kind in {"position", "cash", "custody_position", "custody_cash"}:
                        component = item.get("security_id", item.get("currency"))
                        target = f"{account}:{component}"
                        item["id"] = target
                rebound.setdefault(target, []).append(item)
        candidates = {}
        for entity, group in rebound.items():
            latest = max(item["recorded_at"] for item in group)
            candidates[entity] = [item for item in group if item["recorded_at"] == latest]
    result = FactView()
    for entity, group in candidates.items():
        activity = [item.pop("_valid_at_date", True) for item in group]
        interval_ambiguous = not all(activity)
        payloads = {canonical({k: v for k, v in item.items()
                               if k not in {"source_id", "recorded_at", "source_system"}})
                    for item in group}
        # A fix's file date controls daily eligibility. Equal-time fixes from
        # different file dates cannot collapse before that eligibility check.
        # Keep this comparison state internal, outside returned business facts.
        for item in group:
            item.pop("_fx_file_date", None)
        if interval_ambiguous or len(payloads) > 1:
            result.ambiguous[entity] = group
        else:
            result.values[entity] = group[0]
    return result


def resolve_account(
    connection: sqlite3.Connection, custodian: str, number: str, day: date, known: datetime,
) -> str | None:
    return _crosswalk(fact_view(connection, "account", day, known)).get((custodian, number))


@dataclass
class AccountSnapshot:
    available: bool
    positions: dict[str, dict] = field(default_factory=dict)
    cash: dict[str, dict] = field(default_factory=dict)
    groupings: list[dict] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    run_id: str | None = None
    reason: str | None = None


def account_snapshot(
    connection: sqlite3.Connection, feed: str, account_id: str, day: date, known: datetime,
    *, accounts_view: FactView | None = None,
) -> AccountSnapshot:
    """Only rows belonging to the selected complete account publication may value it."""
    accounts = (fact_view(connection, "account", day, known)
                if accounts_view is None else accounts_view)
    account = accounts.values.get(account_id)
    if account is None or _crosswalk(accounts).get(
        (account["custodian"], account["account_number"])
    ) != account_id:
        return AccountSnapshot(False, reason="ACCOUNT_CROSSWALK_UNAVAILABLE")
    if not account["is_managed"]:
        return AccountSnapshot(False, reason="HELD_AWAY_EXCLUDED")
    system = ("PAS-" if feed == "pas_snapshots" else "CUSTODY-") + account["custodian"]
    received = connection.execute(
        "SELECT r.id,r.recorded_at,r.receipt_source_id,p.available,p.reason,p.source_ids "
        "FROM feed_runs r JOIN feed_manifests m ON m.run_id=r.id "
        "LEFT JOIN account_publications p ON p.run_id=r.id AND p.custodian=? "
        "AND p.account_number=? WHERE r.feed=? AND r.source_system=? "
        "AND r.business_date=? AND r.recorded_at<=? ORDER BY r.recorded_at DESC,r.id",
        (account["custodian"], account["account_number"], feed, system,
         day.isoformat(), utc_timestamp(known)),
    ).fetchall()
    if received:
        latest = [item for item in received if item["recorded_at"] == received[0]["recorded_at"]]
        sources = sorted({item["receipt_source_id"] for item in latest})
        if len({(item["available"], item["source_ids"]) for item in latest}) > 1:
            return AccountSnapshot(False, source_ids=sources, reason="AMBIGUOUS_SNAPSHOT")
        chosen = latest[0]
        result = AccountSnapshot(bool(chosen["available"]), source_ids=sources,
                                 run_id=chosen["id"], reason=chosen["reason"])
        if chosen["available"] is None:
            result.reason = "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST"
        elif not result.available and len({item["reason"] for item in latest}) > 1:
            result.reason = "MULTIPLE_UNAVAILABLE_PUBLICATIONS"
        if result.available:
            _snapshot_records(connection, result, json.loads(chosen["source_ids"]),
                              account_id, day, known)
        return result
    # Legacy hand-authored fixtures have no received manifest. Production feed
    # publications always take the external-account path above.
    rows = connection.execute(
        "SELECT a.*,r.recorded_at,r.receipt_source_id FROM account_availability a "
        "JOIN feed_runs r ON r.id=a.run_id LEFT JOIN feed_manifests m ON m.run_id=r.id "
        "WHERE m.run_id IS NULL AND r.feed=? AND a.account_id=? "
        "AND r.business_date=? AND r.recorded_at<=? ORDER BY r.recorded_at DESC,r.id",
        (feed, account_id, day.isoformat(), utc_timestamp(known)),
    ).fetchall()
    if not rows:
        return AccountSnapshot(False, reason="MISSING_RUN")
    row = rows[0]
    concurrent = [item for item in rows if item["recorded_at"] == row["recorded_at"]]
    sources = sorted({item["receipt_source_id"] for item in concurrent})
    if len({(item["available"], item["source_ids"]) for item in concurrent}) > 1:
        return AccountSnapshot(False, source_ids=sources, reason="AMBIGUOUS_SNAPSHOT")
    result = AccountSnapshot(bool(row["available"]), source_ids=sources,
                             run_id=row["run_id"], reason=row["reason"])
    if not result.available:
        if len({item["reason"] for item in concurrent}) > 1:
            result.reason = "MULTIPLE_UNAVAILABLE_PUBLICATIONS"
        return result
    _snapshot_records(connection, result, json.loads(row["source_ids"]), account_id, day, known)
    return result


def _snapshot_records(
    connection: sqlite3.Connection, result: AccountSnapshot, source_ids: list[str],
    account_id: str, day: date, known: datetime,
) -> None:
    for source_id in source_ids:
        # A record can be idempotently shared by multiple received files. Selecting
        # exact source IDs from the manifest publication avoids old-row carry-forward.
        for fact in connection.execute(
            "SELECT kind,entity_id,payload,source_id,recorded_at FROM facts WHERE source_id=? "
            "AND valid_from<=? AND ?<valid_to AND recorded_at<=? ORDER BY recorded_at",
            (source_id, day.isoformat(), day.isoformat(), utc_timestamp(known)),
        ):
            value = {**json.loads(fact["payload"]), "source_id": fact["source_id"],
                     "recorded_at": fact["recorded_at"], "account_id": account_id}
            if fact["kind"] in {"position", "custody_position"}:
                value["id"] = f"{account_id}:{value['security_id']}"
                result.positions[value["security_id"]] = value
            elif fact["kind"] in {"cash", "custody_cash"}:
                value["id"] = f"{account_id}:{value['currency']}"
                result.cash[value["currency"]] = value
            elif fact["kind"] == "pas_grouping":
                result.groupings.append(value)
        result.source_ids.append(source_id)
    result.source_ids = sorted(set(result.source_ids))
