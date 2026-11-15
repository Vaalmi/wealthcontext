"""Hand-received feed evidence for atomic, temporal P2 ingestion."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import wealthcontext.ingestion as ingestion
from wealthcontext.clock import FrozenClock, utc_timestamp
from wealthcontext.database import migrate
from wealthcontext.facts import account_snapshot, fact_view, resolve_account
from wealthcontext.feeds import AccountReference, Delivery, FeedName, SourceSystem, build_delivery
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.storage import entitlements, migrate_business, read_snapshot

DAY = date(2026, 9, 30)
RECEIVED = datetime(2026, 9, 30, 22, tzinfo=UTC)
CRM_RECEIVED = RECEIVED - timedelta(days=1)
REFERENCES = [
    AccountReference(account_id="WC-AC-1", custodian="A", account_number="00420017"),
    AccountReference(account_id="WC-AC-2", custodian="A", account_number="00420018"),
    AccountReference(account_id="WC-AC-3", custodian="A", account_number="00420019"),
]
TABLES = (
    "feed_runs", "feed_manifests", "account_availability", "quarantine", "source_records",
    "facts", "entitlement_revisions", "entitlement_facts",
)


def row(kind: str, record_id: str, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, "source_record_id": record_id, "valid_from": "2026-09-14",
            "valid_to": "9999-12-31", **fields}


def position(
    account: AccountReference, security: str = "WC-SEC-1", *, day: date = DAY,
    quantity: Any = "10",
) -> dict[str, Any]:
    return row("position", f"pos-{account.custodian}-{account.account_number}-{security}",
               custodian=account.custodian, account_number=account.account_number,
               security_id=security, quantity=quantity, valid_from=day.isoformat(),
               valid_to=(day + timedelta(days=1)).isoformat())


def cash(account: AccountReference, *, day: date = DAY) -> dict[str, Any]:
    return row("cash", f"cash-{account.custodian}-{account.account_number}",
               custodian=account.custodian, account_number=account.account_number,
               currency="USD", amount="500.00", valid_from=day.isoformat(),
               valid_to=(day + timedelta(days=1)).isoformat())


def delivery(
    source: SourceSystem, feed: FeedName, records: list[dict], *, day: date = DAY,
    accounts: list[AccountReference] | None = None,
) -> Delivery:
    return build_delivery(source_system=source, feed=feed, business_date=day, records=records,
                          expected_accounts=accounts or [])


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "received.sqlite"
    migrate(path)
    migrate_business(path)
    migrate_feeds(path)
    return path


def initialize_accounts(
    database: Path, accounts: list[AccountReference] | None = None,
) -> None:
    records = [row("household", "household-1", id="WC-HH-1", name="Synthetic household")]
    for account in accounts or REFERENCES:
        records.extend([
            row("account", f"account-{account.account_id}", id=account.account_id,
                custodian=account.custodian, account_number=account.account_number,
                is_managed=True),
            row("reporting", f"reporting-{account.account_id}", account_id=account.account_id,
                household_id="WC-HH-1"),
        ])
    ingest_delivery(database, delivery("CRM", "crm", records), FrozenClock(CRM_RECEIVED))


def counts(database: Path) -> dict[str, int]:
    with read_snapshot(database) as connection:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in TABLES}


def test_same_external_number_at_two_custodians_never_merges(database: Path) -> None:
    accounts = [REFERENCES[0], AccountReference(
        account_id="WC-AC-B", custodian="B", account_number=REFERENCES[0].account_number,
    )]
    initialize_accounts(database, accounts)
    inputs: list[tuple[SourceSystem, AccountReference, str]] = [
        ("PAS-A", accounts[0], "10"), ("PAS-B", accounts[1], "20"),
    ]
    for source, account, quantity in inputs:
        ingest_delivery(database, delivery(source, "pas_snapshots",
                                           [position(account, quantity=quantity)],
                                           accounts=[account]), FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        for account, quantity in zip(accounts, ["10", "20"], strict=True):
            assert resolve_account(connection, account.custodian, account.account_number,
                                   DAY, RECEIVED) == account.account_id
            snapshot = account_snapshot(connection, "pas_snapshots", account.account_id,
                                        DAY, RECEIVED)
            assert snapshot.available
            assert snapshot.positions["WC-SEC-1"]["quantity"] == quantity
            assert snapshot.positions["WC-SEC-1"]["account_id"] == account.account_id


def test_truncation_salvages_only_complete_account_digests_and_keeps_zero_account(
    database: Path,
) -> None:
    initialize_accounts(database)
    first, second, zero = REFERENCES
    previous = delivery("PAS-A", "pas_snapshots", [position(zero)], accounts=REFERENCES)
    ingest_delivery(database, previous, FrozenClock(RECEIVED - timedelta(hours=1)))
    intended = [position(first), cash(first), position(second), cash(second)]
    partial = build_delivery(source_system="PAS-A", feed="pas_snapshots", business_date=DAY,
                             records=intended[:-1], intended_records=intended,
                             expected_accounts=REFERENCES, upstream_status="TIMEOUT")
    run = ingest_delivery(database, partial, FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        record = connection.execute("SELECT * FROM feed_runs WHERE id=?", (run,)).fetchone()
        assert record["upstream_status"] == "TIMEOUT"
        assert record["status"] == "PARTIAL"
        assert record["accepted_records"] == 2
        assert record["rejected_records"] == 1
        complete = account_snapshot(connection, "pas_snapshots", first.account_id, DAY, RECEIVED)
        assert complete.available and complete.cash["USD"]["amount"] == "500.00"
        truncated = account_snapshot(connection, "pas_snapshots", second.account_id, DAY, RECEIVED)
        assert not truncated.available and not truncated.positions and not truncated.cash
        assert truncated.reason == "ACCOUNT_MANIFEST_MISMATCH"
        empty = account_snapshot(connection, "pas_snapshots", zero.account_id, DAY, RECEIVED)
        assert empty.available and empty.positions == {} and empty.cash == {}
        assert empty.run_id == run  # Zero is proven by this manifest, not old holdings.
        rejected = connection.execute("SELECT * FROM quarantine WHERE run_id=?", (run,)).fetchall()
        assert [item["account_id"] for item in rejected] == [second.account_id]
        assert json.loads(rejected[0]["raw_payload"]) == intended[2]


def test_one_invalid_row_quarantines_every_row_of_its_account(database: Path) -> None:
    initialize_accounts(database, REFERENCES[:2])
    first, second, _ = REFERENCES
    rows = [position(first, quantity="-1"), cash(first), position(second), cash(second)]
    run = ingest_delivery(database, delivery("PAS-A", "pas_snapshots", rows,
                                             accounts=[first, second]), FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        assert not account_snapshot(connection, "pas_snapshots", first.account_id,
                                    DAY, RECEIVED).available
        assert account_snapshot(connection, "pas_snapshots", second.account_id,
                                DAY, RECEIVED).available
        quarantined = connection.execute("SELECT * FROM quarantine WHERE run_id=?", (run,)
                                          ).fetchall()
        assert len(quarantined) == 2
        assert {item["account_id"] for item in quarantined} == {first.account_id}
        assert {item["reason"] for item in quarantined} == {"INVALID_ACCOUNT_ROW"}
        published = connection.execute(
            "SELECT payload FROM source_records WHERE feed_run_id=? AND record_type='record'",
            (run,),
        ).fetchall()
        assert len(published) == 2
        assert all(json.loads(item["payload"])["account_number"] == second.account_number
                   for item in published)


def test_failed_authoritative_fetch_never_carries_forward_same_day_or_previous_day(
    database: Path,
) -> None:
    account = AccountReference(account_id="WC-AC-B", custodian="B", account_number="00420017")
    initialize_accounts(database, [account])
    good = delivery("PAS-B", "pas_snapshots", [position(account)], accounts=[account])
    ingest_delivery(database, good, FrozenClock(RECEIVED))
    failure = build_delivery(source_system="PAS-B", feed="pas_snapshots", business_date=DAY,
                             records=None, intended_records=[position(account)],
                             expected_accounts=[account], upstream_status="FAILED")
    failed_at = RECEIVED + timedelta(hours=1)
    failed_run = ingest_delivery(database, failure, FrozenClock(failed_at))
    with read_snapshot(database) as connection:
        assert account_snapshot(connection, "pas_snapshots", account.account_id,
                                DAY, RECEIVED).available
        failed = account_snapshot(connection, "pas_snapshots", account.account_id, DAY, failed_at)
        assert not failed.available and not failed.positions
        assert failed.run_id == failed_run
        assert connection.execute("SELECT content,status FROM feed_runs WHERE id=?", (failed_run,)
                                  ).fetchone()["content"] is None
        next_day = account_snapshot(connection, "pas_snapshots", account.account_id,
                                    DAY + timedelta(days=1), failed_at)
        assert not next_day.available


def test_run_and_record_idempotency_preserves_original_knowledge_and_removed_components(
    database: Path,
) -> None:
    initialize_accounts(database, REFERENCES[:1])
    account = REFERENCES[0]
    kept, removed, cash_row = position(account), position(account, "WC-SEC-2"), cash(account)
    original = delivery("PAS-A", "pas_snapshots", [kept, removed, cash_row], accounts=[account])
    first_run = ingest_delivery(database, original, FrozenClock(RECEIVED))
    before = counts(database)
    retry_clock = FrozenClock(RECEIVED + timedelta(hours=1))
    assert ingest_delivery(database, original, retry_clock) == first_run
    same_bytes = Delivery(original.manifest, original.content, "SUCCESS", "different-delivery-id")
    assert ingest_delivery(database, same_bytes, retry_clock) == first_run
    assert counts(database) == before
    corrected_at = RECEIVED + timedelta(hours=2)
    corrected_delivery = delivery("PAS-A", "pas_snapshots", [kept, cash_row], accounts=[account])
    corrected_run = ingest_delivery(database, corrected_delivery, FrozenClock(corrected_at))
    assert corrected_run != first_run
    with read_snapshot(database) as connection:
        prior = account_snapshot(connection, "pas_snapshots", account.account_id, DAY, RECEIVED)
        corrected = account_snapshot(connection, "pas_snapshots", account.account_id,
                                     DAY, corrected_at)
        assert set(prior.positions) == {"WC-SEC-1", "WC-SEC-2"}
        assert set(corrected.positions) == {"WC-SEC-1"}
        prior_source = prior.positions["WC-SEC-1"]["source_id"]
        assert corrected.positions["WC-SEC-1"]["source_id"] == prior_source
        assert corrected.positions["WC-SEC-1"]["recorded_at"] == utc_timestamp(RECEIVED)
        assert connection.execute(
            "SELECT COUNT(*) FROM source_records WHERE source_system='PAS-A' "
            "AND record_type='record'",
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM facts WHERE kind='position'",
        ).fetchone()[0] == 2  # The removed assertion remains immutable evidence.


class CountingClock:
    def __init__(self, instant: datetime):
        self.instant, self.calls = instant, 0

    def now(self) -> datetime:
        self.calls += 1
        return self.instant + timedelta(hours=self.calls - 1)


def test_live_ingestion_captures_one_clock_and_canonical_utc_without_accepting_recorded_at(
    database: Path,
) -> None:
    clock = CountingClock(datetime(2026, 9, 30, 18, 3, 4, 123456,
                                   tzinfo=timezone(timedelta(hours=-4))))
    good = row("household", "good", id="WC-HH-1", name="Synthetic household",
               source_asserted_at="2026-09-30T17:00:00-04:00")
    bad = row("household", "caller-clock", id="WC-HH-2", name="Synthetic other",
              recorded_at="2000-01-01T00:00:00Z")
    run = ingest_delivery(database, delivery("CRM", "crm", [good, bad]), clock)
    assert clock.calls == 1
    with read_snapshot(database) as connection:
        recorded = utc_timestamp(clock.instant)
        receipt = connection.execute("SELECT * FROM feed_runs WHERE id=?", (run,)).fetchone()
        assert receipt["recorded_at"] == receipt["imported_at"] == recorded
        sources = connection.execute("SELECT * FROM source_records WHERE feed_run_id=?", (run,)
                                     ).fetchall()
        assert {item["recorded_at"] for item in sources} == {recorded}
        assert {item["imported_at"] for item in sources} == {recorded}
        source = next(item for item in sources if item["source_record_id"] == "good")
        assert source["source_asserted_at"] == "2026-09-30T21:00:00.000000+00:00"
        view = fact_view(connection, "household", DAY, clock.instant)
        assert view.values.keys() == {"WC-HH-1"}
        assert view.values["WC-HH-1"]["recorded_at"] == recorded
        quarantine = connection.execute("SELECT raw_payload FROM quarantine WHERE run_id=?", (run,)
                                        ).fetchone()
        assert json.loads(quarantine[0])["recorded_at"] == bad["recorded_at"]


@pytest.mark.parametrize("failure_point", ["row", "entitlement_revision"])
def test_publication_failure_rolls_back_rows_run_and_revision_atomically(
    database: Path, monkeypatch, failure_point: str,
) -> None:
    records = [row("household", "household", id="WC-HH-1", name="Synthetic household"),
               row("advisor", "advisor", id="ADV-03", name="Synthetic advisor"),
               row("advisor_covers", "grant", advisor_id="ADV-03", household_id="WC-HH-1")]
    received = delivery("CRM", "crm", records)
    before = counts(database)
    publish_row = ingestion._publish_row
    publish_entitlements = ingestion._publish_entitlements
    calls = 0

    def broken_row(*args, **kwargs):
        nonlocal calls
        result = publish_row(*args, **kwargs)
        calls += 1
        if calls == 2:
            assert counts(database) == before  # A separate WAL read sees no partial run.
            raise sqlite3.OperationalError("injected mid-publication failure")
        return result

    def broken_revision(*args, **kwargs):
        publish_entitlements(*args, **kwargs)
        assert counts(database) == before  # Even the new grant revision is still uncommitted.
        raise sqlite3.OperationalError("injected post-revision failure")

    with monkeypatch.context() as patch:
        method = "_publish_row" if failure_point == "row" else "_publish_entitlements"
        patch.setattr(ingestion, method,
                      broken_row if failure_point == "row" else broken_revision)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            ingest_delivery(database, received, FrozenClock(RECEIVED))
    assert counts(database) == before
    run = ingest_delivery(database, received, FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        status = connection.execute("SELECT status FROM feed_runs WHERE id=?", (run,)).fetchone()[0]
        assert status == "SUCCESS"
        assert entitlements(connection)[0] == 1
        assert len(entitlements(connection)[1]) == 1


def test_entitlement_revision_commits_future_effective_bounds_without_repeated_revision(
    database: Path,
) -> None:
    initial = row("advisor_covers", "grant-old", advisor_id="ADV-03", household_id="WC-HH-1")
    ingest_delivery(database, delivery("CRM", "crm", [
        row("household", "household", id="WC-HH-1", name="Synthetic household"),
        row("advisor", "advisor-old", id="ADV-03", name="Synthetic advisor old"),
        row("advisor", "advisor-new", id="ADV-05", name="Synthetic advisor new"), initial,
    ]), FrozenClock(CRM_RECEIVED))
    corrected = {**initial, "valid_to": "2026-10-01"}
    replacement = row("advisor_covers", "grant-new", advisor_id="ADV-05", household_id="WC-HH-1",
                      valid_from="2026-10-01")
    committed = RECEIVED - timedelta(hours=2)
    run = ingest_delivery(database, delivery("CRM", "crm", [corrected, replacement]),
                          FrozenClock(committed))
    with read_snapshot(database) as connection:
        revision, grants = entitlements(connection)
        assert revision == 2
        assert connection.execute("SELECT recorded_at FROM entitlement_revisions WHERE revision=2"
                                  ).fetchone()[0] == utc_timestamp(committed)
        assert connection.execute("SELECT recorded_at FROM feed_runs WHERE id=?", (run,)
                                  ).fetchone()[0] == utc_timestamp(committed)
        for day, expected in [("2026-09-30", {"ADV-03"}), ("2026-10-01", {"ADV-05"})]:
            assert {grant["subject"] for grant in grants
                    if grant["valid_from"] <= day < grant["valid_to"]} == expected
        before = fact_view(connection, "advisor_covers", date(2026, 10, 2),
                           committed - timedelta(microseconds=1))
        after = fact_view(connection, "advisor_covers", date(2026, 10, 2), committed)
        assert set(before.values) == {"ADV-03:WC-HH-1"}
        assert set(after.values) == {"ADV-05:WC-HH-1"}
    # Different received content, same coverage. This must publish a run without
    # inventing another entitlement revision merely because CRM refreshed.
    ingest_delivery(database, delivery("CRM", "crm", [corrected, replacement,
        row("household", "household", id="WC-HH-1", name="Synthetic renamed household"),
    ]), FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        assert entitlements(connection)[0] == 2


def test_equal_time_membership_contradictions_have_no_lexical_source_winner(database: Path) -> None:
    first = row("membership", "AAAA", person_id="WC-PER-1", household_id="WC-HH-1")
    second = row("membership", "ZZZZ", person_id="WC-PER-1", household_id="WC-HH-2")
    ingest_delivery(database, delivery("CRM", "crm", [first, second]), FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        view = fact_view(connection, "membership", DAY, RECEIVED)
        assert "WC-PER-1" not in view.values
        households = {item["household_id"] for item in view.ambiguous["WC-PER-1"]}
        assert households == {"WC-HH-1", "WC-HH-2"}
        assert len({item["source_id"] for item in view.ambiguous["WC-PER-1"]}) == 2


def test_shortened_membership_end_supersedes_old_open_interval_at_later_knowledge(
    database: Path,
) -> None:
    original = row("membership", "member", person_id="WC-PER-1", household_id="WC-HH-1")
    ingest_delivery(database, delivery("CRM", "crm", [original]), FrozenClock(CRM_RECEIVED))
    ingest_delivery(database, delivery("CRM", "crm", [{**original, "valid_to": "2026-10-01"}]),
                    FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        after_end = date(2026, 10, 2)
        before = fact_view(connection, "membership", after_end,
                           RECEIVED - timedelta(microseconds=1))
        after = fact_view(connection, "membership", after_end, RECEIVED)
        assert before.values["WC-PER-1"]["household_id"] == "WC-HH-1"
        assert "WC-PER-1" not in after.values
        assert "WC-PER-1" not in after.ambiguous
        assert fact_view(connection, "membership", DAY, RECEIVED).values["WC-PER-1"]


def test_fact_cutoffs_compare_instants_including_offsets_and_exact_boundary(database: Path) -> None:
    ingest_delivery(database, delivery("CRM", "crm", [
        row("household", "household", id="WC-HH-1", name="Synthetic household"),
    ]), FrozenClock(RECEIVED))
    before = datetime(2026, 9, 30, 23, tzinfo=timezone(timedelta(hours=2)))
    after = datetime(2026, 9, 30, 19, tzinfo=timezone(timedelta(hours=-4)))
    with read_snapshot(database) as connection:
        assert not fact_view(connection, "household", DAY, before).values
        just_before = fact_view(connection, "household", DAY, RECEIVED - timedelta(microseconds=1))
        assert not just_before.values
        assert set(fact_view(connection, "household", DAY, RECEIVED).values) == {"WC-HH-1"}
        assert set(fact_view(connection, "household", DAY, after).values) == {"WC-HH-1"}


def test_omitted_expected_account_cannot_leave_managed_holdings_available(database: Path) -> None:
    initialize_accounts(database, REFERENCES[:1])
    account = REFERENCES[0]
    ingest_delivery(database, delivery("PAS-A", "pas_snapshots", [position(account)],
                                      accounts=[account]), FrozenClock(RECEIVED))
    omitted_at = RECEIVED + timedelta(hours=1)
    ingest_delivery(database, delivery("PAS-A", "pas_snapshots", []), FrozenClock(omitted_at))
    with read_snapshot(database) as connection:
        snapshot = account_snapshot(connection, "pas_snapshots", account.account_id,
                                    DAY, omitted_at)
        assert not snapshot.available, (
            "a later feed manifest omitted this managed account; earlier holdings must not survive"
        )
        assert not snapshot.positions and not snapshot.cash


def test_no_usable_account_publication_is_failed_not_partial(database: Path) -> None:
    initialize_accounts(database, REFERENCES[:1])
    account = REFERENCES[0]
    bad = delivery("PAS-A", "pas_snapshots", [position(account, quantity="-1")], accounts=[account])
    run = ingest_delivery(database, bad, FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        received = connection.execute("SELECT * FROM feed_runs WHERE id=?", (run,)).fetchone()
        assert received["accepted_records"] == 0
        assert received["rejected_records"] == 1
        assert not account_snapshot(connection, "pas_snapshots", account.account_id,
                                    DAY, RECEIVED).available
        assert received["status"] == "FAILED", "no account was accepted, even as a proven zero"


def test_partial_run_can_accept_a_proven_zero_even_when_accepted_record_count_is_zero(
    database: Path,
) -> None:
    zero, bad = REFERENCES[:2]
    initialize_accounts(database, [zero, bad])
    run = ingest_delivery(database, delivery("PAS-A", "pas_snapshots",
                                             [position(bad, quantity="-1")], accounts=[zero, bad]),
                          FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        received = connection.execute("SELECT * FROM feed_runs WHERE id=?", (run,)).fetchone()
        assert received["accepted_records"] == 0
        assert received["status"] == "PARTIAL"
        snapshot = account_snapshot(connection, "pas_snapshots", zero.account_id, DAY, RECEIVED)
        assert snapshot.available and not snapshot.positions and not snapshot.cash
        assert not account_snapshot(connection, "pas_snapshots", bad.account_id,
                                    DAY, RECEIVED).available


def test_delivery_id_reuse_cannot_silently_discard_changed_received_bytes(database: Path) -> None:
    original = delivery("CRM", "crm", [
        row("household", "household", id="WC-HH-1", name="Original synthetic name"),
    ])
    changed = delivery("CRM", "crm", [
        row("household", "household", id="WC-HH-1", name="Corrected synthetic name"),
    ])
    ingest_delivery(database, original, FrozenClock(RECEIVED))
    collision = Delivery(changed.manifest, changed.content, "SUCCESS", original.delivery_id)
    before = counts(database)
    with pytest.raises(ValueError, match="delivery"):
        ingest_delivery(database, collision, FrozenClock(RECEIVED + timedelta(hours=1)))
    assert counts(database) == before


@pytest.mark.parametrize("source,feed", [
    ("PAS-A", "pas_snapshots"), ("CUSTODY-A", "custody_snapshots"),
])
@pytest.mark.parametrize("invalid_interval", [
    {"valid_from": "2026-10-01", "valid_to": "2026-10-02"},
    {"valid_from": "2026-09-29", "valid_to": "2026-09-30"},
], ids=["starts-after-business-date", "exclusive-end-equals-business-date"])
def test_snapshot_validity_quarantines_whole_account_and_later_correction_restores_it(
    database: Path, source: SourceSystem, feed: FeedName, invalid_interval: dict,
) -> None:
    affected, valid_long = REFERENCES[:2]
    initialize_accounts(database, [affected, valid_long])
    long_interval = {"valid_from": "2026-09-14", "valid_to": "9999-12-31"}
    unaffected = [
        {**position(valid_long, quantity="20"), **long_interval},
        {**cash(valid_long), **long_interval},
    ]
    previous_at = RECEIVED - timedelta(hours=1)
    ingest_delivery(database, delivery(source, feed, [
        position(affected, quantity="7"), cash(affected), *unaffected,
    ], accounts=[affected, valid_long]), FrozenClock(previous_at))
    invalid_position = {**position(affected), **invalid_interval}
    bad_run = ingest_delivery(database, delivery(source, feed, [
        invalid_position, cash(affected), *unaffected,
    ], accounts=[affected, valid_long]), FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        previous = account_snapshot(connection, feed, affected.account_id, DAY, previous_at)
        assert previous.available and previous.positions["WC-SEC-1"]["quantity"] == "7"
        unavailable = account_snapshot(connection, feed, affected.account_id, DAY, RECEIVED)
        assert unavailable.run_id == bad_run
        assert not unavailable.available and unavailable.positions == unavailable.cash == {}
        assert unavailable.reason == "SNAPSHOT_VALIDITY_MISMATCH"
        publication = connection.execute(
            "SELECT available,source_ids FROM account_availability WHERE run_id=? AND account_id=?",
            (bad_run, affected.account_id),
        ).fetchone()
        assert publication["available"] == 0 and publication["source_ids"] == "[]"
        rejected = connection.execute("SELECT * FROM quarantine WHERE run_id=?",
                                       (bad_run,)).fetchall()
        assert len(rejected) == 2
        assert {item["account_id"] for item in rejected} == {affected.account_id}
        assert {item["reason"] for item in rejected} == {"SNAPSHOT_VALIDITY_MISMATCH"}
        rejected_payloads = {json.dumps(json.loads(item["raw_payload"]), sort_keys=True)
                             for item in rejected}
        assert rejected_payloads == {
            json.dumps(record, sort_keys=True) for record in [invalid_position, cash(affected)]
        }
        still_valid = account_snapshot(connection, feed, valid_long.account_id, DAY, RECEIVED)
        assert still_valid.available and still_valid.positions["WC-SEC-1"]["quantity"] == "20"
        run = connection.execute("SELECT status,accepted_records FROM feed_runs WHERE id=?",
                                 (bad_run,)).fetchone()
        assert tuple(run) == ("PARTIAL", 2)

    corrected_at = RECEIVED + timedelta(hours=1)
    corrected_run = ingest_delivery(database, delivery(source, feed, [
        position(affected), cash(affected), *unaffected,
    ], accounts=[affected, valid_long]), FrozenClock(corrected_at))
    with read_snapshot(database) as connection:
        corrected = account_snapshot(connection, feed, affected.account_id, DAY, corrected_at)
        assert corrected.run_id == corrected_run and corrected.available
        assert corrected.positions["WC-SEC-1"]["quantity"] == "10"
        assert corrected.cash["USD"]["amount"] == "500.00"
        assert not account_snapshot(connection, feed, affected.account_id, DAY, RECEIVED).available
        assert not account_snapshot(connection, feed, valid_long.account_id,
                                    DAY + timedelta(days=1), corrected_at).available


@pytest.mark.parametrize("source,feed", [
    ("PAS-A", "pas_snapshots"), ("CUSTODY-A", "custody_snapshots"),
])
@pytest.mark.parametrize("key", ["custodian", "account_number"])
@pytest.mark.parametrize("malformed", [[], {}, ["A"], {"number": "00420017"}])
def test_malformed_external_key_is_quarantined_without_losing_complete_account(
    database: Path, source: SourceSystem, feed: FeedName, key: str, malformed: Any,
) -> None:
    """A valid file can contain invalid business objects; reject them durably."""
    account = REFERENCES[0]
    initialize_accounts(database, [account])
    bad = {**position(account, "WC-SEC-BAD"), key: malformed}
    received = delivery(source, feed, [position(account), cash(account), bad], accounts=[account])
    run_id = ingest_delivery(database, received, FrozenClock(RECEIVED))
    with read_snapshot(database) as connection:
        run = connection.execute("SELECT * FROM feed_runs WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "PARTIAL"
        assert run["accepted_records"] == 2 and run["rejected_records"] == 1
        assert run["content"] == received.content
        assert connection.execute("SELECT 1 FROM feed_manifests WHERE run_id=?",
                                  (run_id,)).fetchone()
        assert connection.execute("SELECT 1 FROM source_records WHERE id=?",
                                  (run["receipt_source_id"],)).fetchone()
        rejects = connection.execute("SELECT * FROM quarantine WHERE run_id=?",
                                     (run_id,)).fetchall()
        assert len(rejects) == 1
        assert rejects[0]["reason"] == "UNEXPECTED_ACCOUNT"
        assert rejects[0]["account_id"] is None
        assert json.loads(rejects[0]["raw_payload"]) == bad
        published = account_snapshot(connection, feed, account.account_id, DAY, RECEIVED)
        assert published.available
        assert set(published.positions) == {"WC-SEC-1"}
        assert published.positions["WC-SEC-1"]["quantity"] == "10"
        assert published.cash["USD"]["amount"] == "500.00"
    before = counts(database)
    assert ingest_delivery(database, received, FrozenClock(RECEIVED)) == run_id
    assert counts(database) == before
