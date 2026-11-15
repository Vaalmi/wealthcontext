"""Versioned facts and durable response evidence, separate from telemetry."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from wealthcontext.clock import utc_timestamp
from wealthcontext.database import connect, write_transaction

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS source_records (
        id TEXT PRIMARY KEY, source_system TEXT NOT NULL, source_record_id TEXT NOT NULL,
        business_date TEXT NOT NULL, recorded_at TEXT NOT NULL, imported_at TEXT NOT NULL,
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS facts (
        kind TEXT NOT NULL, entity_id TEXT NOT NULL, valid_from TEXT NOT NULL,
        valid_to TEXT NOT NULL, recorded_at TEXT NOT NULL,
        source_id TEXT NOT NULL REFERENCES source_records(id), payload TEXT NOT NULL,
        PRIMARY KEY(kind, entity_id, valid_from, recorded_at, source_id),
        CHECK(valid_from < valid_to))""",
    """CREATE TABLE IF NOT EXISTS entitlement_revisions (
        revision INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS entitlement_facts (
        revision INTEGER NOT NULL REFERENCES entitlement_revisions(revision),
        kind TEXT NOT NULL, subject TEXT NOT NULL, object TEXT NOT NULL,
        valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
        PRIMARY KEY(revision, kind, subject, object, valid_from))""",
    """CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS conversation_creation_integrity (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
        schema_version INTEGER NOT NULL CHECK(schema_version=1),
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS conversation_message_integrity (
        conversation_id TEXT NOT NULL REFERENCES conversations(id), sequence INTEGER NOT NULL,
        schema_version INTEGER NOT NULL CHECK(schema_version=1),
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
        PRIMARY KEY(conversation_id,sequence))""",
    """CREATE TABLE IF NOT EXISTS decisions (
        id TEXT PRIMARY KEY, principal_id TEXT, action TEXT NOT NULL,
        resources TEXT NOT NULL, allowed INTEGER NOT NULL, reason TEXT NOT NULL,
        policy_revision TEXT, entitlement_revision INTEGER,
        trace_id TEXT NOT NULL, recorded_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS results (
        id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, principal_id TEXT,
        conversation_id TEXT REFERENCES conversations(id), payload TEXT NOT NULL,
        payload_hash TEXT NOT NULL, recorded_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS result_scopes (
        result_id TEXT NOT NULL REFERENCES results(id), household_id TEXT NOT NULL,
        PRIMARY KEY(result_id, household_id))""",
    """CREATE TABLE IF NOT EXISTS result_access (
        result_id TEXT PRIMARY KEY REFERENCES results(id), action TEXT NOT NULL,
        projection TEXT NOT NULL CHECK(projection IN ('full','no_pii')),
        metadata_version INTEGER NOT NULL CHECK(metadata_version=1))""",
    """CREATE TABLE IF NOT EXISTS result_scope_integrity (
        result_id TEXT PRIMARY KEY REFERENCES results(id), schema_version INTEGER NOT NULL,
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL, CHECK(schema_version=1))""",
    """CREATE TABLE IF NOT EXISTS result_issuance_integrity (
        result_id TEXT PRIMARY KEY REFERENCES results(id), schema_version INTEGER NOT NULL,
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL, CHECK(schema_version=1))""",
    """CREATE TABLE IF NOT EXISTS decision_projections (
        decision_id TEXT PRIMARY KEY REFERENCES decisions(id),
        projection TEXT NOT NULL CHECK(projection IN ('full','no_pii')))""",
    """CREATE TABLE IF NOT EXISTS conversation_messages (
        conversation_id TEXT NOT NULL REFERENCES conversations(id), sequence INTEGER NOT NULL,
        kind TEXT NOT NULL
            CHECK(kind IN ('user','assistant_call','tool_result','assistant_answer')),
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY(conversation_id,sequence))""",
    """CREATE TABLE IF NOT EXISTS conversation_message_results (
        conversation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        result_id TEXT NOT NULL REFERENCES results(id),
        PRIMARY KEY(conversation_id,sequence,result_id),
        FOREIGN KEY(conversation_id,sequence)
            REFERENCES conversation_messages(conversation_id,sequence))""",
    """CREATE TABLE IF NOT EXISTS decision_authorized_resources (
        decision_id TEXT NOT NULL REFERENCES decisions(id), household_id TEXT NOT NULL,
        PRIMARY KEY(decision_id, household_id))""",
    """CREATE TABLE IF NOT EXISTS result_account_scopes (
        result_id TEXT PRIMARY KEY REFERENCES results(id), schema_version INTEGER NOT NULL,
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
        CHECK(schema_version = 1))""",
    """CREATE TABLE IF NOT EXISTS provenance_handles (
        id TEXT PRIMARY KEY, result_id TEXT NOT NULL REFERENCES results(id),
        principal_id TEXT NOT NULL, source_ids TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS evaluation_runs (
        id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, mode TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','completed','failed','deferred_not_run')),
        recorded_at TEXT NOT NULL, completed_at TEXT,
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
        cases_json TEXT NOT NULL, cases_hash TEXT NOT NULL,
        manifest_json TEXT NOT NULL, manifest_hash TEXT NOT NULL,
        final_payload TEXT, final_hash TEXT,
        CHECK((status='running' AND completed_at IS NULL AND final_payload IS NULL
               AND final_hash IS NULL) OR (status<>'running' AND completed_at IS NOT NULL
               AND final_payload IS NOT NULL AND final_hash IS NOT NULL)))""",
    """CREATE TABLE IF NOT EXISTS evaluation_trials (
        run_id TEXT NOT NULL REFERENCES evaluation_runs(id), case_id TEXT NOT NULL,
        trial INTEGER NOT NULL CHECK(trial BETWEEN 1 AND 3),
        started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
        PRIMARY KEY(run_id,case_id,trial))""",
    "CREATE INDEX IF NOT EXISTS evaluation_run_order ON evaluation_runs(recorded_at,id)",
    """CREATE TRIGGER IF NOT EXISTS evaluation_runs_no_delete BEFORE DELETE ON evaluation_runs
        BEGIN SELECT RAISE(ABORT, 'evaluation runs are durable evidence'); END""",
    """CREATE TRIGGER IF NOT EXISTS evaluation_runs_terminal_transition
        BEFORE UPDATE ON evaluation_runs WHEN OLD.status<>'running' OR NEW.status='running'
        OR NEW.id IS NOT OLD.id OR NEW.provider IS NOT OLD.provider OR NEW.model IS NOT OLD.model
        OR NEW.mode IS NOT OLD.mode OR NEW.recorded_at IS NOT OLD.recorded_at
        OR NEW.payload IS NOT OLD.payload OR NEW.payload_hash IS NOT OLD.payload_hash
        OR NEW.cases_json IS NOT OLD.cases_json OR NEW.cases_hash IS NOT OLD.cases_hash
        OR NEW.manifest_json IS NOT OLD.manifest_json OR NEW.manifest_hash IS NOT OLD.manifest_hash
        BEGIN SELECT RAISE(ABORT, 'only one evaluation terminal transition is permitted'); END""",
    """CREATE TRIGGER IF NOT EXISTS evaluation_trials_no_update BEFORE UPDATE ON evaluation_trials
        BEGIN SELECT RAISE(ABORT, 'evaluation trials are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS evaluation_trials_no_delete BEFORE DELETE ON evaluation_trials
        BEGIN SELECT RAISE(ABORT, 'evaluation trials are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS evaluation_trials_running_only
        BEFORE INSERT ON evaluation_trials
        WHEN (SELECT status FROM evaluation_runs WHERE id=NEW.run_id)<>'running'
        BEGIN SELECT RAISE(ABORT, 'evaluation run is terminal'); END""",
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def migrate_business(path: Path) -> None:
    with write_transaction(path) as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        connection.execute("INSERT OR IGNORE INTO schema_version VALUES (2)")
        connection.execute("INSERT OR IGNORE INTO schema_version VALUES (6)")
        connection.execute("INSERT OR IGNORE INTO schema_version VALUES (7)")
        # Old index rows cannot prove completeness. Do not bless potentially
        # damaged legacy scopes by deriving integrity evidence during migration.
        connection.execute("INSERT OR IGNORE INTO schema_version VALUES (8)")
        # Ownership/source membership cannot be reconstructed from mutable indexes.
        # Legacy results without issuance proof require fresh governed retrieval.
        connection.execute("INSERT OR IGNORE INTO schema_version VALUES (9)")
        # Original conversation/message associations cannot be rebuilt from mutable rows.
        connection.execute("INSERT OR IGNORE INTO schema_version VALUES (10)")
        if not connection.execute("SELECT 1 FROM schema_version WHERE version=3").fetchone():
            # Preserve the instants in an existing P1 database; payloads and their hashes
            # are immutable evidence and retain their originally serialized form.
            for table, columns in {
                "source_records": ("recorded_at", "imported_at"),
                "facts": ("recorded_at",), "entitlement_revisions": ("recorded_at",),
                "conversations": ("created_at",), "decisions": ("recorded_at",),
                "results": ("recorded_at",),
            }.items():
                rows = connection.execute(
                    f"SELECT rowid AS migration_rowid, * FROM {table}",
                ).fetchall()
                for row in rows:
                    for column in columns:
                        connection.execute(
                            f"UPDATE {table} SET {column}=? WHERE rowid=?",
                            (utc_timestamp(datetime.fromisoformat(row[column])),
                             row["migration_rowid"]),
                        )
            connection.execute("INSERT INTO schema_version VALUES (3)")
        if not connection.execute("SELECT 1 FROM schema_version WHERE version=5").fetchone():
            # Existing exact decisions authorized every resource on allow.
            # New filtered decisions record their actual subset in the companion table.
            for decision in connection.execute(
                "SELECT id,resources FROM decisions WHERE allowed=1",
            ):
                for resource in json.loads(decision["resources"]):
                    connection.execute(
                        "INSERT OR IGNORE INTO decision_authorized_resources VALUES (?, ?)",
                        (decision["id"], resource),
                    )
            connection.execute("INSERT INTO schema_version VALUES (5)")


@contextmanager
def read_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(path)
    try:
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.rollback()
        connection.close()


def entitlements(connection: sqlite3.Connection) -> tuple[int, list[dict]]:
    row = connection.execute("SELECT MAX(revision) FROM entitlement_revisions").fetchone()
    if row[0] is None:
        raise RuntimeError("no committed entitlement revision")
    revision = int(row[0])
    facts = [dict(row) for row in connection.execute(
        "SELECT kind, subject, object, valid_from, valid_to FROM entitlement_facts "
        "WHERE revision = ? ORDER BY kind, subject, object", (revision,),
    )]
    return revision, facts


def facts_at(
    connection: sqlite3.Connection, kind: str, valuation_date: date, known_as_of: datetime,
) -> dict[str, dict]:
    rows = connection.execute(
        "SELECT entity_id, payload, source_id FROM facts "
        "WHERE kind = ? AND valid_from <= ? AND ? < valid_to AND recorded_at <= ? "
        "ORDER BY recorded_at DESC, source_id",
        (kind, valuation_date.isoformat(), valuation_date.isoformat(), utc_timestamp(known_as_of)),
    )
    result: dict[str, dict] = {}
    for row in rows:
        result.setdefault(row["entity_id"], {**json.loads(row["payload"]),
                                              "source_id": row["source_id"]})
    return result


def seed_vertical_slice(path: Path, imported_at: datetime) -> None:
    """Idempotent received-record fixture for P1; full manifest ingestion starts in P2."""
    recorded = utc_timestamp(datetime.fromisoformat("2026-09-30T22:00:00Z"))
    household, account, security = "WC-HH-0001", "WC-AC-0001", "WC-SEC-0001"
    rows = [
        ("household", household, "CRM", {"id": household, "name": "Okafor household"}),
        ("account", account, "CRM", {"id": account, "household_id": household,
                                     "title": "Okafor individual", "is_managed": True}),
        ("security", security, "CUSTODY", {"id": security, "issuer_id": "WC-ISS-0001",
                                            "name": "Northstar Example", "currency": "USD"}),
        ("position", "WC-POS-0001", "PAS", {"id": "WC-POS-0001", "account_id": account,
                                              "security_id": security, "quantity": "100"}),
        ("cash", "WC-CASH-0001", "PAS", {"id": "WC-CASH-0001", "account_id": account,
                                          "currency": "USD", "amount": "500.00"}),
        ("price", security, "CUSTODY", {"security_id": security, "price": "25.00",
                                        "currency": "USD", "price_date": "2026-09-30"}),
        ("pas_coverage", account, "PAS", {"account_id": account,
                                          "position_ids": ["WC-POS-0001"],
                                          "cash_ids": ["WC-CASH-0001"]}),
    ]
    with write_transaction(path) as connection:
        if connection.execute("SELECT 1 FROM source_records LIMIT 1").fetchone():
            return
        for kind, entity, source, payload in rows:
            source_id = f"SRC-P1-{kind}"
            text = canonical(payload)
            connection.execute("INSERT INTO source_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (source_id, source, source_id, "2026-09-30", recorded,
                                utc_timestamp(imported_at), text, digest(text)))
            daily = kind in {"position", "cash", "price", "pas_coverage"}
            connection.execute("INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (kind, entity, "2026-09-30" if daily else "2026-09-14",
                                "2026-10-01" if daily else "9999-12-31", recorded,
                                source_id, text))
        connection.execute("INSERT INTO entitlement_revisions VALUES (1, ?)", (recorded,))
        connection.execute("INSERT INTO entitlement_facts VALUES (1, ?, ?, ?, ?, ?)",
                           ("advisor_covers", "ADV-01", household, "2026-09-14", "9999-12-31"))
