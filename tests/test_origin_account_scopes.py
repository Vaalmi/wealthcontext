"""Book lineage is owned by all evaluated households, independent of visible breaches."""

import json
from datetime import UTC, datetime

import pytest

from wealthcontext.clock import utc_timestamp
from wealthcontext.database import migrate, write_transaction
from wealthcontext.source_projection import OriginAccountScope, SourceScope, project_source_record
from wealthcontext.storage import canonical, digest, migrate_business, read_snapshot


def metadata() -> dict:
    return {
        "schema_version": 1, "result_id": "r_book", "household_ids": ["WC-HH-1", "WC-HH-2"],
        "accounts": [
            {"household_id": household, "account_id": account, "custodian": "A",
             "account_number": number}
            for household, account, number in [
                ("WC-HH-1", "WC-AC-1", "0001"), ("WC-HH-2", "WC-AC-2", "0002"),
            ]
        ],
    }


def test_scope_metadata_keeps_nonbreacher_account_without_exposing_other_receipt_accounts():
    scope = SourceScope.from_account_scope(metadata(), ["WC-HH-1", "WC-HH-2"], "r_book")
    received = {"manifest": {
        "feed": "pas_snapshots", "record_count": 103, "checksum": "f" * 64,
        "expected_account_ids": ["WC-AC-1", "WC-AC-2", "WC-AC-3"],
        "accounts": [
            {"account_id": account, "custodian": "A", "account_number": number,
             "record_count": count, "digest": "e" * 64}
            for account, number, count in [("WC-AC-1", "0001", 1), ("WC-AC-2", "0002", 2),
                                            ("WC-AC-3", "0003", 100)]
        ],
    }, "note": "PRIVATE-NOTE"}
    payload = canonical(received)
    record = {"id": "SRC-1", "payload": payload, "payload_hash": digest(payload)}
    reply = project_source_record(record, scope)
    projected = reply["payload"]["manifest"]
    assert projected["expected_account_ids"] == ["WC-AC-1", "WC-AC-2"]
    assert projected["record_count"] == 3
    assert "WC-AC-3" not in canonical(reply) and "PRIVATE-NOTE" not in canonical(reply)
    assert reply["payload_hash"] == digest(payload)
    assert record["payload"] == payload


@pytest.mark.parametrize("change", [
    {"result_id": "r_other"}, {"household_ids": ["WC-HH-1"]},
    {"schema_version": 2}, {"accounts": [{"household_id": "WC-HH-foreign",
                                       "account_id": "WC-AC-3"}]},
    {"accounts": [{"household_id": "WC-HH-1", "account_id": "WC-AC-1",
                   "custodian": "A"}]},
])
def test_invalid_account_scope_never_falls_back_to_business_facts(change):
    with pytest.raises(ValueError):
        SourceScope.from_account_scope({**metadata(), **change}, ["WC-HH-1", "WC-HH-2"], "r_book")


def test_explicit_empty_metadata_scope_is_authoritative():
    scope = SourceScope.from_account_scope(
        {**metadata(), "accounts": []}, ["WC-HH-1", "WC-HH-2"], "r_book",
    )
    assert not scope.account_ids and not scope.external_accounts
    assert OriginAccountScope.model_validate(metadata()).schema_version == 1


def test_companion_table_migration_preserves_exact_schema_and_backfills_once(tmp_path):
    database = tmp_path / "legacy.sqlite"
    migrate(database)
    migrate_business(database)
    at = utc_timestamp(datetime(2035, 1, 1, tzinfo=UTC))
    with write_transaction(database) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(decisions)")]
        connection.execute("DELETE FROM schema_version WHERE version=5")
        for decision, allowed, resources in [("d_allow", 1, ["WC-HH-1", "WC-HH-2"]),
                                              ("d_deny", 0, ["WC-HH-3"])]:
            connection.execute("INSERT INTO decisions VALUES (?, 'ADV-1', 'snapshot', ?, ?, "
                               "'LEGACY', NULL, 1, 'trace', ?)",
                               (decision, json.dumps(resources), allowed, at))
    migrate_business(database)
    migrate_business(database)
    with read_snapshot(database) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(decisions)")] == columns
        authorized = [tuple(row) for row in connection.execute(
            "SELECT * FROM decision_authorized_resources ORDER BY decision_id,household_id",
        )]
        assert authorized == [("d_allow", "WC-HH-1"), ("d_allow", "WC-HH-2")]
        assert not connection.execute("SELECT * FROM result_account_scopes").fetchall()
