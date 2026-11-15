"""Manifest verification and atomic publication of received, versioned facts."""

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from wealthcontext.clock import Clock, utc_timestamp
from wealthcontext.conflicts import migrate_conflicts, persist_conflicts
from wealthcontext.database import write_transaction
from wealthcontext.facts import fact_view
from wealthcontext.storage import canonical, digest

if TYPE_CHECKING:
    from wealthcontext.feeds import Delivery, FeedRow
    from wealthcontext.generator import FixtureEvent

FEED_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS feed_runs (
        id TEXT PRIMARY KEY, source_system TEXT NOT NULL, feed TEXT NOT NULL,
        business_date TEXT NOT NULL, delivery_id TEXT NOT NULL, content_hash TEXT,
        content BLOB, upstream_status TEXT NOT NULL, status TEXT NOT NULL,
        recorded_at TEXT NOT NULL, imported_at TEXT NOT NULL,
        accepted_records INTEGER NOT NULL, rejected_records INTEGER NOT NULL,
        receipt_source_id TEXT NOT NULL,
        UNIQUE(source_system,content_hash), UNIQUE(source_system,delivery_id))""",
    """CREATE TABLE IF NOT EXISTS feed_manifests (
        run_id TEXT PRIMARY KEY REFERENCES feed_runs(id), payload TEXT NOT NULL,
        payload_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS account_availability (
        run_id TEXT NOT NULL REFERENCES feed_runs(id), account_id TEXT NOT NULL,
        available INTEGER NOT NULL, reason TEXT, source_ids TEXT NOT NULL,
        PRIMARY KEY(run_id,account_id))""",
    """CREATE TABLE IF NOT EXISTS account_publications (
        run_id TEXT NOT NULL REFERENCES feed_runs(id), custodian TEXT NOT NULL,
        account_number TEXT NOT NULL, manifest_account_id TEXT NOT NULL,
        available INTEGER NOT NULL, reason TEXT, source_ids TEXT NOT NULL,
        PRIMARY KEY(run_id,custodian,account_number))""",
    """CREATE TABLE IF NOT EXISTS quarantine (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES feed_runs(id),
        account_id TEXT, reason TEXT NOT NULL, raw_payload TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS source_record_identity ON source_records "
    "(source_system,source_record_id,business_date,payload_hash)",
    "CREATE INDEX IF NOT EXISTS fact_history_lookup ON facts "
    "(kind,entity_id,valid_from,recorded_at)",
    "CREATE INDEX IF NOT EXISTS snapshot_run_lookup ON feed_runs "
    "(feed,business_date,recorded_at)",
)


def migrate_feeds(path: Path) -> None:
    with write_transaction(path) as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(source_records)")}
        for name, declaration in {
            "feed_run_id": "TEXT", "source_asserted_at": "TEXT",
            "record_type": "TEXT NOT NULL DEFAULT 'record'",
        }.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE source_records ADD COLUMN {name} {declaration}")
        for statement in FEED_SCHEMA:
            connection.execute(statement)
        migrate_conflicts(connection)
        connection.execute("INSERT OR IGNORE INTO entitlement_revisions VALUES (0, ?)",
                           ("0001-01-01T00:00:00.000000+00:00",))
        connection.execute("INSERT OR IGNORE INTO schema_version VALUES (4)")


def _source(
    connection: sqlite3.Connection, run_id: str, system: str, record_id: str, day: date,
    raw: dict, recorded: str, imported: str, asserted: datetime | None,
    record_type: str = "record",
) -> str:
    payload = canonical(raw)
    hashed = digest(payload)
    source_id = "SRC-" + digest(canonical([system, record_id, day.isoformat(), hashed]))
    connection.execute(
        "INSERT OR IGNORE INTO source_records "
        "(id,source_system,source_record_id,business_date,recorded_at,imported_at,payload,"
        "payload_hash,feed_run_id,source_asserted_at,record_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (source_id, system, record_id, day.isoformat(), recorded, imported, payload, hashed,
         run_id, utc_timestamp(asserted) if asserted else None, record_type),
    )
    return source_id


def _canonical_fact(row: "FeedRow", feed: str) -> tuple[str, str, dict]:
    payload = row.model_dump(mode="json")
    for key in ("source_record_id", "source_asserted_at", "valid_from", "valid_to", "note"):
        payload.pop(key, None)
    kind = payload.pop("kind")
    # Upstream account evidence belongs to the external account, not the CRM
    # mapping known on arrival. Resolve its canonical account at query cutoffs.
    external = (f"{payload['custodian']}:{payload['account_number']}"
                if "custodian" in payload and kind != "account" else None)
    if kind in {"position", "cash"}:
        component = payload["security_id"] if kind == "position" else payload["currency"]
        entity = f"{external}:{component}"
        payload["id"] = entity
        if feed == "custody_snapshots":
            kind = "custody_" + kind
    elif kind in {"ownership", "pas_grouping"}:
        entity = str(external)
        if kind == "ownership" and feed == "crm":
            kind = "crm_ownership"
    elif kind == "membership":
        entity = payload["person_id"]
    elif kind == "reporting":
        entity = payload["account_id"]
    elif kind == "advisor_member":
        entity = f"{payload['advisor_id']}:{payload['team_id']}"
    elif kind == "advisor_covers":
        entity = f"{payload['advisor_id']}:{payload['household_id']}"
    elif kind == "team_covers":
        entity = f"{payload['team_id']}:{payload['household_id']}"
    elif kind == "price":
        entity = payload["security_id"]
    elif kind == "fx":
        entity = payload["currency"]
    else:
        entity = payload["id"]
    return kind, entity, payload


def _publish_row(
    connection: sqlite3.Connection, run_id: str, delivery: "Delivery", raw: dict,
    row: "FeedRow", recorded: str, imported: str,
) -> str:
    source_id = _source(connection, run_id, delivery.manifest.source_system,
                        row.source_record_id, delivery.manifest.business_date, raw,
                        recorded, imported, row.source_asserted_at)
    kind, entity, payload = _canonical_fact(row, delivery.manifest.feed)
    original_time = connection.execute("SELECT recorded_at FROM source_records WHERE id=?",
                                       (source_id,)).fetchone()[0]
    payload_text = canonical(payload)
    # Raw-record idempotency does not erase a new assertion of an old value.
    # V1 -> V2 -> V1 must supersede V2, including when V1 revokes a grant.
    # Every non-snapshot receipt asserts its complete set at one instant, so
    # replayed V1 alongside new V2 in the same file also remains ambiguous.
    # Snapshots instead select exact raw records through account_publications.
    assertion_time = (original_time if delivery.manifest.feed in {
        "pas_snapshots", "custody_snapshots",
    } else recorded)
    connection.execute("INSERT OR IGNORE INTO facts VALUES (?,?,?,?,?,?,?)",
                       (kind, entity, row.valid_from.isoformat(), row.valid_to.isoformat(),
                        assertion_time, source_id, payload_text))
    return source_id


def _publish_entitlements(connection: sqlite3.Connection, recorded: str) -> None:
    rows = connection.execute(
        "SELECT f.* FROM facts f JOIN source_records s ON s.id=f.source_id "
        "WHERE kind IN ('advisor_member','advisor_covers','team_covers') "
        "AND s.feed_run_id IS NOT NULL ORDER BY recorded_at DESC,source_id",
    ).fetchall()
    latest: dict[tuple, list[sqlite3.Row]] = {}
    for row in rows:
        group = latest.setdefault((row["kind"], row["entity_id"], row["valid_from"]), [])
        if not group or group[0]["recorded_at"] == row["recorded_at"]:
            group.append(row)
    grants = []
    for group in latest.values():
        if len({(row["payload"], row["valid_to"]) for row in group}) != 1:
            continue  # conflicting CRM grants fail closed instead of picking an ID
        row = group[0]
        data = json.loads(row["payload"])
        subject = data.get("advisor_id", data.get("team_id"))
        target = data["team_id"] if row["kind"] == "advisor_member" else data["household_id"]
        grants.append((row["kind"], subject, target, row["valid_from"], row["valid_to"]))
    revision = connection.execute("SELECT MAX(revision) FROM entitlement_revisions").fetchone()[0]
    old = [tuple(row) for row in connection.execute(
        "SELECT kind,subject,object,valid_from,valid_to FROM entitlement_facts WHERE revision=?",
        (revision,),
    )]
    if sorted(old) == sorted(grants):
        return
    revision += 1
    connection.execute("INSERT INTO entitlement_revisions VALUES (?,?)", (revision, recorded))
    connection.executemany("INSERT INTO entitlement_facts VALUES (?,?,?,?,?,?)",
                           [(revision, *grant) for grant in grants])


def ingest_delivery(path: Path, delivery: "Delivery", clock: Clock) -> str:
    """Live ingestion has no caller-supplied knowledge time."""
    now = clock.now()
    return _ingest(path, delivery, recorded_at=now, imported_at=now)


def import_fixture_history(path: Path, events: Iterable["FixtureEvent"], clock: Clock) -> list[str]:
    """Trusted synthetic bootstrap preserves fixture knowledge times, never exposed as an API."""
    imported = clock.now()
    return [_ingest(path, event.delivery, recorded_at=event.recorded_at, imported_at=imported)
            for event in events]


def _ingest(
    path: Path, delivery: "Delivery", *, recorded_at: datetime, imported_at: datetime,
) -> str:
    from wealthcontext.feeds import account_key, decode_content, parse_row, records_digest

    manifest = delivery.manifest
    recorded, imported = utc_timestamp(recorded_at), utc_timestamp(imported_at)
    content_hash = (hashlib.sha256(delivery.content).hexdigest()
                    if delivery.content is not None else None)
    run_key = [manifest.source_system, content_hash or delivery.delivery_id]
    run_id = "RUN-" + digest(canonical(run_key))
    receipt_id = "pending"
    with write_transaction(path) as connection:
        prior_delivery = connection.execute(
            "SELECT r.*,m.payload AS manifest FROM feed_runs r "
            "JOIN feed_manifests m ON m.run_id=r.id WHERE r.source_system=? AND r.delivery_id=?",
            (manifest.source_system, delivery.delivery_id),
        ).fetchone()
        if prior_delivery:
            if (prior_delivery["content_hash"] != content_hash
                    or prior_delivery["manifest"] != canonical(manifest.model_dump(mode="json"))
                    or prior_delivery["upstream_status"] != delivery.upstream_status):
                raise ValueError("delivery_id cannot be reused for different received evidence")
            return prior_delivery["id"]
        existing = connection.execute(
            "SELECT id FROM feed_runs WHERE source_system=? AND content_hash=?",
            (manifest.source_system, content_hash),
        ).fetchone()
        if existing:
            return existing[0]
        connection.execute("INSERT INTO feed_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (run_id, manifest.source_system, manifest.feed,
                            manifest.business_date.isoformat(), delivery.delivery_id, content_hash,
                            delivery.content, delivery.upstream_status, "FAILED", recorded,
                            imported,
                            0, 0, receipt_id))
        manifest_payload = canonical(manifest.model_dump(mode="json"))
        connection.execute("INSERT INTO feed_manifests VALUES (?,?,?)",
                           (run_id, manifest_payload, digest(manifest_payload)))
        receipt_id = _source(
            connection, run_id, manifest.source_system, f"manifest:{delivery.delivery_id}",
            manifest.business_date, {"manifest": manifest.model_dump(mode="json"),
                                     "file_received": delivery.content is not None,
                                     "upstream_status": delivery.upstream_status},
            recorded, imported, None, "availability_evidence",
        )
        connection.execute("UPDATE feed_runs SET receipt_source_id=? WHERE id=?",
                           (receipt_id, run_id))
        rejected = 0

        def reject(raw: object, reason: str, account_id: str | None = None) -> None:
            nonlocal rejected
            rejected += 1
            connection.execute("INSERT INTO quarantine VALUES (?,?,?,?,?)",
                               (f"{run_id}:{rejected}", run_id, account_id, reason, canonical(raw)))

        raw_rows: list = []
        file_valid = False
        file_matches = False
        if delivery.content is not None:
            try:
                content = decode_content(
                    delivery.content,
                    allow_partial=manifest.feed in {"pas_snapshots", "custody_snapshots"},
                )
                file_valid = (content.source_system == manifest.source_system
                              and content.feed == manifest.feed
                              and content.business_date == manifest.business_date)
                if not file_valid:
                    reject({"delivery_id": delivery.delivery_id}, "FILE_IDENTITY_MISMATCH")
                else:
                    raw_rows = content.records
                    file_matches = (content_hash == manifest.checksum
                                    and len(raw_rows) == manifest.record_count)
            except (ValidationError, ValueError, UnicodeError):
                reject({"delivery_id": delivery.delivery_id}, "INVALID_FILE")
        snapshot = manifest.feed in {"pas_snapshots", "custody_snapshots"}
        accepted = 0
        accepted_accounts = 0
        if snapshot:
            accounts = fact_view(connection, "account", manifest.business_date, recorded_at)
            source_custodian = manifest.source_system.rsplit("-", 1)[-1]
            managed_ids = {item["id"] for item in accounts.values.values()
                           if item["is_managed"] and item["custodian"] == source_custodian}
            managed_ids.update(item["id"] for group in accounts.ambiguous.values()
                               for item in group if item["is_managed"]
                               and item["custodian"] == source_custodian)
            for missing_id in sorted(managed_ids - set(manifest.expected_account_ids)):
                reject({"expected_account_id": missing_id},
                       "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST", missing_id)
                connection.execute("INSERT INTO account_availability VALUES (?,?,?,?,?)",
                                   (run_id, missing_id, 0,
                                    "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST", "[]"))
            expected_keys = {(item.custodian, item.account_number) for item in manifest.accounts}
            for raw in raw_rows:
                if account_key(raw) not in expected_keys:
                    reject(raw, "UNEXPECTED_ACCOUNT")
            for expected in manifest.accounts:
                group = [raw for raw in raw_rows
                         if account_key(raw) == (expected.custodian, expected.account_number)]
                reason = None
                parsed = []
                if (not file_valid or delivery.upstream_status == "FAILED"
                        or len(group) != expected.record_count
                        or records_digest(group) != expected.digest):
                    reason = "ACCOUNT_MANIFEST_MISMATCH"
                else:
                    try:
                        parsed = [parse_row(manifest.feed, raw,
                                            source_system=manifest.source_system) for raw in group]
                        if any(not row.valid_from <= manifest.business_date < row.valid_to
                               for row in parsed):
                            reason = "SNAPSHOT_VALIDITY_MISMATCH"
                        components = [(_canonical_fact(row, manifest.feed)[:2])
                                      for row in parsed]
                        if len(components) != len(set(components)):
                            reason = "DUPLICATE_ACCOUNT_COMPONENT"
                    except (ValidationError, ValueError):
                        reason = "INVALID_ACCOUNT_ROW"
                source_ids = []
                if reason is not None:
                    for raw in group or [{"expected_account_id": expected.account_id}]:
                        reject(raw, reason, expected.account_id)
                else:
                    accepted_accounts += 1
                    for raw, row in zip(group, parsed, strict=True):
                        source_ids.append(_publish_row(connection, run_id, delivery, raw, row,
                                                       recorded, imported))
                        accepted += 1
                connection.execute("INSERT INTO account_availability VALUES (?,?,?,?,?)",
                                   (run_id, expected.account_id, int(reason is None), reason,
                                    canonical(sorted(source_ids))))
                connection.execute("INSERT INTO account_publications VALUES (?,?,?,?,?,?,?)",
                                   (run_id, expected.custodian, expected.account_number,
                                    expected.account_id, int(reason is None), reason,
                                    canonical(sorted(source_ids))))
        elif file_valid and file_matches and delivery.upstream_status != "FAILED":
            # CRM crosswalk identities publish first inside this same transaction.
            ordered = sorted(raw_rows, key=lambda raw: 0 if isinstance(raw, dict)
                             and raw.get("kind") == "account" else 1)
            for raw in ordered:
                try:
                    row = parse_row(manifest.feed, raw, source_system=manifest.source_system)
                    _publish_row(connection, run_id, delivery, raw, row, recorded, imported)
                    accepted += 1
                except (ValidationError, ValueError):
                    reject(raw, "INVALID_ROW")
        elif delivery.content is not None and file_valid:
            for raw in raw_rows or [{"delivery_id": delivery.delivery_id}]:
                reject(raw, "FILE_MANIFEST_MISMATCH")
        if manifest.feed == "crm" and accepted:
            _publish_entitlements(connection, recorded)
        if delivery.content is None:
            status = "TIMEOUT" if delivery.upstream_status == "TIMEOUT" else "FAILED"
        elif rejected or not file_matches or delivery.upstream_status != "SUCCESS":
            status = ("PARTIAL" if accepted or accepted_accounts else
                      "TIMEOUT" if delivery.upstream_status == "TIMEOUT" else "FAILED")
        else:
            status = "SUCCESS"
        connection.execute("UPDATE feed_runs SET status=?,accepted_records=?,rejected_records=? "
                           "WHERE id=?", (status, accepted, rejected, run_id))
        if manifest.feed in {"crm", "custody_accounts", "pas_snapshots",
                             "custody_snapshots", "transactions"}:
            persist_conflicts(connection, run_id, manifest.business_date, recorded_at)
    return run_id
