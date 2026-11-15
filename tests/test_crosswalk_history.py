"""External received evidence resolves through CRM at both query cutoffs."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wealthcontext.clock import FrozenClock
from wealthcontext.database import migrate
from wealthcontext.facts import account_snapshot, fact_view, resolve_account
from wealthcontext.feeds import AccountReference, FeedName, SourceSystem, build_delivery
from wealthcontext.households import household_context
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.storage import migrate_business, read_snapshot

DAY = date(2026, 9, 30)
NEXT_DAY = DAY + timedelta(days=1)
INITIAL = datetime(2026, 9, 30, 12, tzinfo=UTC)
RECEIVED = datetime(2026, 9, 30, 22, tzinfo=UTC)
CORRECTED = datetime(2026, 10, 2, 14, tzinfo=UTC)
FIRST = "WC-AC-1"
SECOND = "WC-AC-2"
SECURITY = "WC-SEC-1"
X = "00420017"
Y = "00420018"
REFERENCES = [AccountReference(account_id=FIRST, custodian="A", account_number=X),
              AccountReference(account_id=SECOND, custodian="A", account_number=Y)]
SNAPSHOT_FEEDS: list[tuple[SourceSystem, FeedName]] = [
    ("PAS-A", "pas_snapshots"), ("CUSTODY-A", "custody_snapshots"),
]


def row(kind: str, key: str, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, "source_record_id": key, "valid_from": "2026-09-14",
            "valid_to": "9999-12-31", **fields}


def account_rows(
    mapping: dict[str, str], *, version: str = "initial", start: str = "2026-09-14",
    end: str = "9999-12-31",
) -> list[dict]:
    return [row("account", f"{version}-{account}", id=account, custodian="A",
                account_number=number, is_managed=True, valid_from=start, valid_to=end)
            for account, number in mapping.items()]


def crm_rows(mapping: dict[str, str]) -> list[dict]:
    records = account_rows(mapping)
    for account in mapping:
        suffix = "1" if account == FIRST else "2"
        household, person = f"WC-HH-{suffix}", f"WC-PER-{suffix}"
        records.extend([
            row("household", household, id=household, name=f"Synthetic household {suffix}"),
            row("person", person, id=person, name=f"Synthetic person {suffix}",
                external_person_key=f"EXT-{suffix}"),
            row("membership", f"member-{suffix}", person_id=person, household_id=household),
            row("reporting", f"report-{account}", account_id=account, household_id=household),
        ])
    return records


def receive(
    database: Path, source: SourceSystem, feed: FeedName, records: list[dict],
    *, at: datetime = RECEIVED, day: date = DAY,
    references: list[AccountReference] | None = None,
) -> str:
    return ingest_delivery(database, build_delivery(
        source_system=source, feed=feed, business_date=day, records=records,
        expected_accounts=references or [],
    ), FrozenClock(at))


def snapshot_rows(*, day: date = DAY, settled: bool = False) -> list[dict]:
    records = []
    for reference, label, quantity, cash in zip(
        REFERENCES, ("X", "Y"), ("9", "18") if settled else ("10", "20"),
        ("110", "220") if settled else ("100", "200"), strict=True,
    ):
        interval = {"valid_from": day.isoformat(),
                    "valid_to": (day + timedelta(days=1)).isoformat()}
        records.extend([
            row("position", f"position-{label}", custodian="A",
                account_number=reference.account_number, security_id=SECURITY,
                quantity=quantity, **interval),
            row("cash", f"cash-{label}", custodian="A",
                account_number=reference.account_number, currency="USD", amount=cash, **interval),
        ])
    return records


def receive_snapshots(
    database: Path, *, day: date = DAY, at: datetime = RECEIVED,
) -> dict[str, str]:
    return {feed: receive(database, source, feed,
                          snapshot_rows(day=day, settled=feed == "custody_snapshots"),
                          at=at, day=day, references=REFERENCES)
            for source, feed in SNAPSHOT_FEEDS}


def receive_external_evidence(database: Path) -> dict[str, str]:
    ownership, transactions, lots = [], [], []
    for reference, label, person, quantity, cost in zip(
        REFERENCES, ("X", "Y"), ("WC-PER-1", "WC-PER-2"), ("1", "2"), ("10", "20"),
        strict=True,
    ):
        external = {"custodian": "A", "account_number": reference.account_number}
        ownership.append(row(
            "ownership", f"ownership-{label}", title=f"Synthetic external {label}",
            registration="INDIVIDUAL", status="OPEN", owners=[{
                "entity_id": person, "entity_type": "person", "role": "owner",
            }], **external,
        ))
        transactions.append(row(
            "transaction", f"transaction-{label}", id=f"WC-TX-{label}", txn_type="BUY",
            security_id=SECURITY, trade_date=DAY.isoformat(), settle_date=NEXT_DAY.isoformat(),
            quantity_delta=quantity, cash_delta_local=f"-{cost}", currency="USD",
            status="ACTIVE", **external,
        ))
        lots.append(row(
            "lot", f"lot-{label}", id=f"WC-LOT-{label}", transaction_id=f"WC-TX-{label}",
            security_id=SECURITY, open_date=DAY.isoformat(), quantity=quantity,
            cost_basis_local=cost, currency="USD", **external,
        ))
    runs = {
        "custody_accounts": receive(database, "CUSTODY-A", "custody_accounts", ownership),
        "transactions": receive(database, "PAS-A", "transactions", transactions),
        "lots": receive(database, "PAS-A", "lots", lots),
    }
    return {**runs, **receive_snapshots(database)}


def assert_resolved(
    connection: sqlite3.Connection, account: str, label: str, *, day: date = DAY,
    known: datetime = RECEIVED, runs: dict[str, str],
) -> None:
    number, quantity, settled, cash = ((X, "10", "9", "100") if label == "X"
                                        else (Y, "20", "18", "200"))
    assert resolve_account(connection, "A", number, day, known) == account
    ownership = fact_view(connection, "ownership", day, known).values[account]
    assert ownership["account_id"] == account and ownership["account_number"] == number
    assert ownership["title"] == f"Synthetic external {label}"
    for kind, entity in (("transaction", f"WC-TX-{label}"), ("lot", f"WC-LOT-{label}")):
        record = fact_view(connection, kind, day, known).values[entity]
        assert record["account_id"] == account and record["account_number"] == number
    for feed, expected in (("pas_snapshots", quantity), ("custody_snapshots", settled)):
        snapshot = account_snapshot(connection, feed, account, day, known)
        assert snapshot.available and snapshot.run_id == runs[feed]
        position = snapshot.positions[SECURITY]
        assert position["id"] == f"{account}:{SECURITY}" and position["account_id"] == account
        assert position["account_number"] == number and position["quantity"] == expected
        assert snapshot.cash["USD"]["id"] == f"{account}:USD"
        if feed == "pas_snapshots":
            assert snapshot.cash["USD"]["amount"] == cash
    household = household_context(connection, "WC-HH-1" if account == FIRST else "WC-HH-2",
                                  day, known)
    assert household.missing_components == [] and len(household.accounts) == 1
    resolved = household.accounts[0]
    assert resolved.account["id"] == account and resolved.ownership is not None
    assert resolved.ownership["title"] == f"Synthetic external {label}"
    assert [record["id"] for record in resolved.transactions] == [f"WC-TX-{label}"]
    assert [record["id"] for record in resolved.lots] == [f"WC-LOT-{label}"]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "crosswalk.sqlite"
    migrate(path)
    migrate_business(path)
    migrate_feeds(path)
    return path


def test_external_feeds_received_before_crm_resolve_without_reingestion(database):
    runs = receive_external_evidence(database)
    with read_snapshot(database) as connection:
        original = [tuple(record) for record in connection.execute(
            "SELECT id,recorded_at,payload_hash,feed_run_id FROM source_records ORDER BY id",
        )]
        assert connection.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 0
        assert {record[0] for record in connection.execute("SELECT status FROM feed_runs")} == {
            "SUCCESS",
        }
        for account in (FIRST, SECOND):
            for _, feed in SNAPSHOT_FEEDS:
                unavailable = account_snapshot(connection, feed, account, DAY, RECEIVED)
                assert not unavailable.available and unavailable.positions == unavailable.cash == {}
                assert unavailable.reason == "ACCOUNT_CROSSWALK_UNAVAILABLE"
        for kind in ("ownership", "transaction", "lot"):
            assert all(record["account_id"] is None
                       for record in fact_view(connection, kind, DAY, RECEIVED).values.values())

    receive(database, "CRM", "crm", crm_rows({FIRST: X, SECOND: Y}), at=CORRECTED,
            day=CORRECTED.date())
    with read_snapshot(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM feed_runs").fetchone()[0] == 6
        retained = [tuple(record) for record in connection.execute(
            "SELECT id,recorded_at,payload_hash,feed_run_id FROM source_records "
            "WHERE source_system!='CRM' ORDER BY id",
        )]
        assert retained == original
        assert_resolved(connection, FIRST, "X", known=CORRECTED, runs=runs)
        assert_resolved(connection, SECOND, "Y", known=CORRECTED, runs=runs)
        assert not account_snapshot(connection, "pas_snapshots", FIRST, DAY, RECEIVED).available
        assert fact_view(connection, "transaction", DAY, RECEIVED).values["WC-TX-X"][
            "account_id"
        ] is None


def test_retrospective_swap_rebinds_external_facts_without_rewriting_old_knowledge(database):
    receive(database, "CRM", "crm", crm_rows({FIRST: X, SECOND: Y}), at=INITIAL)
    runs = receive_external_evidence(database)
    with read_snapshot(database) as connection:
        assert_resolved(connection, FIRST, "X", runs=runs)
        assert_resolved(connection, SECOND, "Y", runs=runs)
        old_x_sources = account_snapshot(connection, "pas_snapshots", FIRST, DAY,
                                         RECEIVED).source_ids
        old_y_sources = account_snapshot(connection, "pas_snapshots", SECOND, DAY,
                                         RECEIVED).source_ids
    receive(database, "CRM", "crm", account_rows({FIRST: Y, SECOND: X}, version="corrected"),
            at=CORRECTED, day=CORRECTED.date())
    with read_snapshot(database) as connection:
        assert_resolved(connection, FIRST, "Y", known=CORRECTED, runs=runs)
        assert_resolved(connection, SECOND, "X", known=CORRECTED, runs=runs)
        assert_resolved(connection, FIRST, "X", runs=runs)
        assert_resolved(connection, SECOND, "Y", runs=runs)
        assert account_snapshot(connection, "pas_snapshots", FIRST, DAY,
                                CORRECTED).source_ids == old_y_sources
        assert account_snapshot(connection, "pas_snapshots", SECOND, DAY,
                                CORRECTED).source_ids == old_x_sources


def test_valid_date_crosswalk_change_preserves_each_days_external_account_attribution(database):
    receive(database, "CRM", "crm", crm_rows({FIRST: X, SECOND: Y}), at=INITIAL)
    first_runs = receive_external_evidence(database)
    next_runs = receive_snapshots(database, day=NEXT_DAY, at=RECEIVED + timedelta(days=1))
    corrected = [
        *account_rows({FIRST: X, SECOND: Y}, version="ended", end=NEXT_DAY.isoformat()),
        *account_rows({FIRST: Y, SECOND: X}, version="changed", start=NEXT_DAY.isoformat()),
    ]
    receive(database, "CRM", "crm", corrected, at=CORRECTED, day=CORRECTED.date())
    with read_snapshot(database) as connection:
        assert_resolved(connection, FIRST, "X", known=CORRECTED, runs=first_runs)
        assert_resolved(connection, SECOND, "Y", known=CORRECTED, runs=first_runs)
        assert_resolved(connection, FIRST, "Y", day=NEXT_DAY, known=CORRECTED, runs=next_runs)
        assert_resolved(connection, SECOND, "X", day=NEXT_DAY, known=CORRECTED, runs=next_runs)
        assert not account_snapshot(connection, "pas_snapshots", FIRST, NEXT_DAY,
                                    RECEIVED).available


@pytest.mark.parametrize("case", ["missing", "ambiguous_account", "duplicate_external_key"])
def test_unresolved_crosswalk_makes_affected_snapshots_unavailable_not_zero(database, case):
    records = crm_rows({FIRST: X, SECOND: Y})
    if case == "missing":
        records = [record for record in records
                   if not (record["kind"] == "account" and record["id"] == FIRST)]
    elif case == "ambiguous_account":
        records.extend(account_rows({FIRST: "00420019"}, version="contradiction"))
    else:
        records.extend(account_rows({"WC-AC-3": X}, version="collision"))
    receive(database, "CRM", "crm", records, at=INITIAL)
    runs = receive_external_evidence(database)
    with read_snapshot(database) as connection:
        assert resolve_account(connection, "A", X, DAY, RECEIVED) is None
        for _, feed in SNAPSHOT_FEEDS:
            unavailable = account_snapshot(connection, feed, FIRST, DAY, RECEIVED)
            assert not unavailable.available and unavailable.positions == unavailable.cash == {}
            assert unavailable.reason == "ACCOUNT_CROSSWALK_UNAVAILABLE"
        assert fact_view(connection, "transaction", DAY, RECEIVED).values["WC-TX-X"][
            "account_id"
        ] is None
        assert fact_view(connection, "lot", DAY, RECEIVED).values["WC-LOT-X"]["account_id"] is None
        context = household_context(connection, "WC-HH-1", DAY, RECEIVED)
        assert context.missing_components == [FIRST]
        assert_resolved(connection, SECOND, "Y", runs=runs)


@pytest.mark.parametrize("source,feed", SNAPSHOT_FEEDS)
def test_latest_file_omission_blocks_newly_learned_account_without_older_fallback(
    database, source: SourceSystem, feed: FeedName,
):
    receive(database, "CRM", "crm", crm_rows({FIRST: X}), at=INITIAL)
    older = receive(database, source, feed, snapshot_rows(), references=REFERENCES)
    latest_at = RECEIVED + timedelta(hours=1)
    latest = receive(database, source, feed,
                     [record for record in snapshot_rows() if record["account_number"] == X],
                     at=latest_at, references=REFERENCES[:1])
    receive(database, "CRM", "crm", crm_rows({SECOND: Y}), at=CORRECTED,
            day=CORRECTED.date())
    with read_snapshot(database) as connection:
        # Both files were valid when received. Y's older manifest proves that its
        # absence in the latest source file must not resurrect its earlier rows.
        assert {record[0] for record in connection.execute(
            "SELECT status FROM feed_runs WHERE id IN (?,?)", (older, latest),
        )} == {"SUCCESS"}
        assert connection.execute(
            "SELECT available FROM account_publications WHERE run_id=? AND account_number=?",
            (older, Y),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT 1 FROM account_publications WHERE run_id=? AND account_number=?", (latest, Y),
        ).fetchone() is None
        unavailable = account_snapshot(connection, feed, SECOND, DAY, CORRECTED)
        assert not unavailable.available and unavailable.positions == unavailable.cash == {}
        assert unavailable.run_id == latest
        assert unavailable.reason == "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST"
        receipt = connection.execute("SELECT receipt_source_id FROM feed_runs WHERE id=?",
                                      (latest,)).fetchone()[0]
        assert unavailable.source_ids == [receipt]
        retained = account_snapshot(connection, feed, FIRST, DAY, CORRECTED)
        assert retained.available and retained.run_id == latest
        assert retained.positions[SECURITY]["quantity"] == "10"
