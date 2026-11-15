"""Tied interval ends must not manufacture a winner at an exclusive boundary."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from wealthcontext.clock import FrozenClock
from wealthcontext.database import migrate
from wealthcontext.facts import account_snapshot, fact_view, resolve_account
from wealthcontext.feeds import AccountReference, FeedName, SourceSystem, build_delivery
from wealthcontext.ingestion import ingest_delivery, migrate_feeds
from wealthcontext.snapshot import CompleteSnapshot, IncompleteSnapshot, calculate_snapshot
from wealthcontext.storage import migrate_business, read_snapshot

FIRST_HH, SECOND_HH = "WC-HH-0001", "WC-HH-0002"
PERSON = "WC-PER-0001"
ACCOUNT = AccountReference(account_id="WC-AC-0001", custodian="A", account_number="00420017")
EXTERNAL = {"custodian": ACCOUNT.custodian, "account_number": ACCOUNT.account_number}
DAYS = [date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)]
TIED = datetime(2026, 10, 3, 12, tzinfo=UTC)
CORRECTED = datetime(2026, 10, 3, 13, tzinfo=UTC)
Kind = Literal["membership", "reporting", "account"]


def row(kind: str, key: str, **fields: Any) -> dict:
    return {"kind": kind, "source_record_id": key, "valid_from": "2026-09-14",
            "valid_to": "9999-12-31", **fields}


def receive(
    database: Path, source: SourceSystem, feed: FeedName, records: list[dict],
    *, day: date, known: datetime,
) -> str:
    packet = build_delivery(
        source_system=source, feed=feed, business_date=day, records=records,
        expected_accounts=[ACCOUNT] if feed in {"pas_snapshots", "custody_snapshots"} else [],
    )
    return ingest_delivery(database, packet, FrozenClock(known))


@pytest.fixture
def cash_database(tmp_path: Path) -> Path:
    database = tmp_path / "intervals.sqlite"
    migrate(database)
    migrate_business(database)
    migrate_feeds(database)
    initial = datetime(2026, 9, 29, 22, tzinfo=UTC)
    receive(database, "CRM", "crm", [
        row("household", "household-one", id=FIRST_HH, name="Synthetic first household"),
        row("household", "household-two", id=SECOND_HH, name="Synthetic second household"),
        row("person", "person", id=PERSON, name="Synthetic person", external_person_key="P1"),
        row("membership", "membership", person_id=PERSON, household_id=FIRST_HH),
        row("account", "account", id=ACCOUNT.account_id, **EXTERNAL, is_managed=True),
        row("reporting", "reporting", account_id=ACCOUNT.account_id, household_id=FIRST_HH),
    ], day=DAYS[0], known=initial)
    receive(database, "CUSTODY-A", "custody_accounts", [
        row("ownership", "ownership", **EXTERNAL, title="Synthetic individual",
            registration="INDIVIDUAL", status="OPEN", owners=[{
                "entity_id": PERSON, "entity_type": "person", "role": "owner",
            }]),
    ], day=DAYS[0], known=initial)
    snapshot_feeds: list[tuple[SourceSystem, FeedName]] = [
        ("PAS-A", "pas_snapshots"), ("CUSTODY-A", "custody_snapshots"),
    ]
    for day in DAYS:
        daily_cash = row("cash", f"cash-{day}", **EXTERNAL, currency="USD", amount="1000.00",
                         valid_from=day.isoformat(), valid_to=(day + timedelta(days=1)).isoformat())
        received = datetime(day.year, day.month, day.day, 22, tzinfo=UTC)
        for source, feed in snapshot_feeds:
            receive(database, source, feed, [daily_cash], day=day, known=received)
    return database


def assertion(kind: Kind, key: str, *, closed: bool) -> dict:
    end = "2026-10-01" if closed else "9999-12-31"
    if kind == "membership":
        return row(kind, key, person_id=PERSON, household_id=FIRST_HH, valid_to=end)
    if kind == "reporting":
        return row(kind, key, account_id=ACCOUNT.account_id, household_id=FIRST_HH, valid_to=end)
    return row(kind, key, id=ACCOUNT.account_id, **EXTERNAL, is_managed=True, valid_to=end)


@pytest.mark.parametrize("kind,closed_resolution", [
    ("membership", False), ("membership", True),
    ("reporting", False), ("reporting", True), ("account", False),
])
def test_tied_ends_block_totals_only_where_applicability_differs_until_later_resolution(
    cash_database: Path, kind: Kind, closed_resolution: bool,
) -> None:
    database = cash_database
    entity = PERSON if kind == "membership" else ACCOUNT.account_id
    run = receive(database, "CRM", "crm", [
        assertion(kind, "closed-assertion", closed=True),
        assertion(kind, "open-assertion", closed=False),
    ], day=DAYS[-1], known=TIED)
    with read_snapshot(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 0
        publication = connection.execute("SELECT status,accepted_records FROM feed_runs WHERE id=?",
                                         (run,)).fetchone()
        assert tuple(publication) == ("SUCCESS", 2)
        # Sep30 is inside both intervals and their non-temporal payload is the
        # same: no artificial ambiguity before the first exclusive end.
        before_end = fact_view(connection, kind, DAYS[0], TIED)
        assert entity in before_end.values and entity not in before_end.ambiguous
        clean, clean_flags, _ = calculate_snapshot(connection, FIRST_HH, DAYS[0], TIED)
        assert isinstance(clean, CompleteSnapshot)
        assert clean.assets_usd == clean.cash_usd == "1000.00" and clean_flags == []
        for day in DAYS[1:]:
            previously_clean, _, _ = calculate_snapshot(
                connection, FIRST_HH, day, TIED - timedelta(microseconds=1),
            )
            assert isinstance(previously_clean, CompleteSnapshot)
            assert previously_clean.assets_usd == "1000.00"
            uncertain = fact_view(connection, kind, day, TIED)
            assert entity not in uncertain.values and len(uncertain.ambiguous[entity]) == 2
            if kind == "account":
                assert resolve_account(connection, "A", ACCOUNT.account_number, day, TIED) is None
                assert not account_snapshot(connection, "pas_snapshots", ACCOUNT.account_id,
                                            day, TIED).available
            else:
                # The ambiguity cannot be blamed on missing financial feeds.
                assert account_snapshot(connection, "pas_snapshots", ACCOUNT.account_id,
                                        day, TIED).available
                assert account_snapshot(connection, "custody_snapshots", ACCOUNT.account_id,
                                        day, TIED).available
            data, flags, _ = calculate_snapshot(connection, FIRST_HH, day, TIED)
            assert isinstance(data, IncompleteSnapshot)
            assert "assets_usd" not in data.model_dump(mode="json")
            assert entity in data.missing_components
            assert any(flag.code == "AMBIGUOUS_ENTITY" and flag.subject == entity for flag in flags)

    corrected = [assertion(kind, "resolved-assertion", closed=closed_resolution)]
    if closed_resolution:
        # A closed reporting relationship transfers to the second household,
        # preserving exactly one reporting household per account in v1. A person
        # can likewise move while the custody legal ownership stays unchanged.
        if kind == "reporting":
            corrected.append(row(kind, "new-reporting", account_id=ACCOUNT.account_id,
                                 household_id=SECOND_HH, valid_from="2026-10-01"))
        else:
            corrected.append(row(kind, "new-membership", person_id=PERSON,
                                 household_id=SECOND_HH, valid_from="2026-10-01"))
    receive(database, "CRM", "crm", corrected, day=DAYS[-1], known=CORRECTED)
    with read_snapshot(database) as connection:
        for day in DAYS[1:]:
            historical, historical_flags, _ = calculate_snapshot(connection, FIRST_HH, day, TIED)
            assert isinstance(historical, IncompleteSnapshot)
            assert any(flag.code == "AMBIGUOUS_ENTITY" and flag.subject == entity
                       for flag in historical_flags)
            still_uncertain = fact_view(connection, kind, day, TIED)
            assert entity in still_uncertain.ambiguous and entity not in still_uncertain.values
            just_before = fact_view(connection, kind, day, CORRECTED - timedelta(microseconds=1))
            assert entity in just_before.ambiguous
            resolved = fact_view(connection, kind, day, CORRECTED)
            assert entity in resolved.values and entity not in resolved.ambiguous
            data, flags, _ = calculate_snapshot(connection, FIRST_HH, day, CORRECTED)
            assert isinstance(data, CompleteSnapshot)
            expected_assets = "0.00" if kind == "reporting" and closed_resolution else "1000.00"
            assert data.assets_usd == expected_assets
            assert not any(flag.code == "AMBIGUOUS_ENTITY" for flag in flags)
            if closed_resolution:
                assert resolved.values[entity]["household_id"] == SECOND_HH
            if kind == "reporting" and closed_resolution:
                receiving_household, _, _ = calculate_snapshot(connection, SECOND_HH,
                                                               day, CORRECTED)
                assert isinstance(receiving_household, CompleteSnapshot)
                assert receiving_household.assets_usd == "1000.00"
