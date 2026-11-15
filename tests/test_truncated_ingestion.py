"""Real byte truncation is salvaged only where account manifests prove completeness."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from wealthcontext.clock import FrozenClock
from wealthcontext.database import migrate
from wealthcontext.facts import account_snapshot, fact_view
from wealthcontext.feeds import AccountReference, build_delivery, content_hash, decode_content
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.storage import migrate_business, read_snapshot

DAY = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 22, tzinfo=UTC)
ACCOUNTS = [AccountReference(account_id=f"WC-AC-{index}", custodian="A",
                             account_number=f"0042001{index}") for index in (1, 2, 3)]


def raw(kind: str, source_id: str, **fields) -> dict:
    return {"kind": kind, "source_record_id": source_id, "valid_from": "2026-09-14",
            "valid_to": "9999-12-31", **fields}


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "truncation.sqlite"
    migrate(path)
    migrate_business(path)
    migrate_feeds(path)
    records = [raw("account", f"CRM-{account.account_id}", id=account.account_id,
                   custodian=account.custodian, account_number=account.account_number,
                   is_managed=True) for account in ACCOUNTS]
    ingest_delivery(path, build_delivery(source_system="CRM", feed="crm", business_date=DAY,
                                         records=records), FrozenClock(NOW))
    return path


def test_mid_record_timeout_publishes_complete_account_and_retains_original_bytes(database):
    records = []
    for account in ACCOUNTS[:2]:
        records.extend([
            raw("position", f"POS-{account.account_id}", custodian="A",
                account_number=account.account_number, security_id="WC-SEC-1", quantity="10"),
            raw("cash", f"CASH-{account.account_id}", custodian="A",
                account_number=account.account_number, currency="USD", amount="500.00"),
        ])
    intended = build_delivery(source_system="CUSTODY-A", feed="custody_snapshots",
                              business_date=DAY, records=records, expected_accounts=ACCOUNTS)
    assert intended.content is not None
    received = intended.content[:-18]
    partial = replace(intended, content=received, upstream_status="TIMEOUT",
                      delivery_id="custody-actual-byte-timeout")
    with pytest.raises(ValueError):
        decode_content(received)
    fragment = decode_content(received, allow_partial=True).records[-1]
    assert isinstance(fragment, str)
    run_id = ingest_delivery(database, partial, FrozenClock(NOW))
    with read_snapshot(database) as connection:
        stored = connection.execute("SELECT * FROM feed_runs WHERE id=?", (run_id,)).fetchone()
        assert stored["status"] == "PARTIAL" and stored["upstream_status"] == "TIMEOUT"
        assert stored["accepted_records"] == 2
        assert stored["content"] == received
        assert stored["content_hash"] == content_hash(received) != intended.manifest.checksum
        first = account_snapshot(connection, "custody_snapshots", ACCOUNTS[0].account_id, DAY, NOW)
        second = account_snapshot(connection, "custody_snapshots", ACCOUNTS[1].account_id, DAY, NOW)
        zero = account_snapshot(connection, "custody_snapshots", ACCOUNTS[2].account_id, DAY, NOW)
        assert first.available and first.positions["WC-SEC-1"]["quantity"] == "10"
        assert first.cash["USD"]["amount"] == "500.00"
        assert not second.available and second.positions == second.cash == {}
        assert second.reason == "ACCOUNT_MANIFEST_MISMATCH"
        assert zero.available and zero.positions == zero.cash == {}
        rejected = connection.execute("SELECT * FROM quarantine WHERE run_id=?",
                                       (run_id,)).fetchall()
        assert len(rejected) == 2
        assert any(json.loads(row["raw_payload"]) == fragment for row in rejected)
        assert any(row["account_id"] == ACCOUNTS[1].account_id
                   and json.loads(row["raw_payload"]) == records[2] for row in rejected)
        published = connection.execute(
            "SELECT payload FROM source_records WHERE feed_run_id=? AND record_type='record'",
            (run_id,),
        ).fetchall()
        assert len(published) == 2
        assert all(json.loads(row["payload"])["account_number"] == ACCOUNTS[0].account_number
                   for row in published)


def test_non_snapshot_feed_cannot_publish_prefix_without_account_completeness_proof(database):
    rows = [raw("price", f"PRICE-{index}", security_id=f"WC-SEC-{index}", currency="USD",
                price="25.00", price_date=DAY.isoformat()) for index in (1, 2)]
    intended = build_delivery(source_system="CUSTODY-A", feed="prices", business_date=DAY,
                              records=rows)
    assert intended.content is not None
    received = intended.content[:-18]
    partial = replace(intended, content=received, upstream_status="TIMEOUT",
                      delivery_id="price-byte-timeout")
    run_id = ingest_delivery(database, partial, FrozenClock(NOW))
    with read_snapshot(database) as connection:
        stored = connection.execute("SELECT * FROM feed_runs WHERE id=?", (run_id,)).fetchone()
        assert stored["accepted_records"] == 0 and stored["content"] == received
        assert stored["status"] == "TIMEOUT"
        assert fact_view(connection, "price", DAY, NOW).values == {}
        rejected = connection.execute("SELECT reason FROM quarantine WHERE run_id=?",
                                       (run_id,)).fetchall()
        assert [row["reason"] for row in rejected] == ["INVALID_FILE"]
