import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from wealthcontext.clock import utc_timestamp
from wealthcontext.conflicts import (
    conflicts_at,
    migrate_conflicts,
    observed_conflicts_at,
    persist_conflicts,
)
from wealthcontext.database import connect, migrate, write_transaction
from wealthcontext.ingestion import migrate_feeds
from wealthcontext.storage import canonical, digest, migrate_business

HH = "WC-HH-0001"
OTHER_HH = "WC-HH-0002"
PERSON = "WC-PER-0001"
DAY = date(2026, 9, 30)
EARLY = datetime(2026, 10, 1, 12, tzinfo=UTC)
LATER = datetime(2026, 10, 2, 14, tzinfo=UTC)


def run(connection: sqlite3.Connection, run_id: str, known: datetime, *, day=DAY) -> None:
    recorded = utc_timestamp(known)
    connection.execute("INSERT INTO feed_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        run_id, "CRM", "crm", day.isoformat(), run_id, None, None, "SUCCESS", "SUCCESS",
        recorded, recorded, 0, 0, f"SOURCE-{run_id}",
    ))


def fact(connection: sqlite3.Connection, run_id: str, kind: str, entity: str,
         payload: dict, known: datetime, *, suffix="") -> str:
    source = f"SOURCE-{run_id}-{kind}-{entity}{suffix}"
    text = canonical(payload)
    recorded = utc_timestamp(known)
    connection.execute("INSERT INTO source_records "
                       "(id,source_system,source_record_id,business_date,recorded_at,imported_at,"
                       "payload,payload_hash,feed_run_id) VALUES (?,?,?,?,?,?,?,?,?)", (
        source, "CRM", source, DAY.isoformat(), recorded, recorded, text, digest(text), run_id,
    ))
    connection.execute("INSERT INTO facts VALUES (?,?,?,?,?,?,?)", (
        kind, entity, "2026-09-14", "9999-12-31", recorded, source, text,
    ))
    return source


def household(connection: sqlite3.Connection, run_id: str, known: datetime, *, household_id=HH):
    fact(connection, run_id, "household", household_id,
         {"id": household_id, "name": f"Synthetic {household_id}"}, known)


def conflicting_person(connection: sqlite3.Connection, run_id: str, known: datetime):
    fact(connection, run_id, "membership", PERSON,
         {"person_id": PERSON, "household_id": HH}, known)
    first = fact(connection, run_id, "person", PERSON,
                 {"id": PERSON, "name": "Synthetic first", "external_person_key": "EXT-1"}, known)
    second = fact(connection, run_id, "person", PERSON,
                  {"id": PERSON, "name": "Synthetic conflicting", "external_person_key": "EXT-1"},
                  known, suffix="-conflict")
    return first, second


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "conflicts.sqlite"
    migrate(path)
    migrate_business(path)
    migrate_feeds(path)
    with write_transaction(path) as connection:
        migrate_conflicts(connection)
        migrate_conflicts(connection)
    return path


def test_conflict_appearance_resolution_and_history_are_append_only(database):
    with write_transaction(database) as connection:
        run(connection, "RUN-EARLY", EARLY)
        household(connection, "RUN-EARLY", EARLY)
        source_ids = conflicting_person(connection, "RUN-EARLY", EARLY)
        persist_conflicts(connection, "RUN-EARLY", DAY, EARLY)
    with write_transaction(database) as connection:
        run(connection, "RUN-LATER", LATER)
        fact(connection, "RUN-LATER", "person", PERSON,
             {"id": PERSON, "name": "Resolved synthetic", "external_person_key": "EXT-1"}, LATER)
        persist_conflicts(connection, "RUN-LATER", DAY, LATER)
        before = conflicts_at(connection, HH, DAY, EARLY - timedelta(microseconds=1))
        early = conflicts_at(connection, HH, DAY, EARLY)
        later = conflicts_at(connection, HH, DAY, LATER)
        assert before == later == []
        assert observed_conflicts_at(connection, HH, DAY, EARLY - timedelta(microseconds=1)) is None
        assert observed_conflicts_at(connection, HH, DAY, LATER) == []
        assert len(early) == 1 and early[0]["code"] == "AMBIGUOUS_ENTITY"
        assert set(source_ids) <= set(early[0]["source_ids"])
        assert connection.execute("SELECT COUNT(*) FROM conflict_snapshots").fetchone()[0] == 2
        assert connection.execute(
            "SELECT payload FROM conflict_snapshots WHERE run_id='RUN-LATER'",
        ).fetchone()[0] == "[]"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE conflict_snapshots SET payload='[]' WHERE run_id='RUN-EARLY'",
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM conflict_snapshots WHERE run_id='RUN-EARLY'")


def test_household_scope_is_isolated_and_derived_dates_do_not_require_an_observation(database):
    with write_transaction(database) as connection:
        run(connection, "RUN-ONE", EARLY)
        household(connection, "RUN-ONE", EARLY)
        household(connection, "RUN-ONE", EARLY, household_id=OTHER_HH)
        conflicting_person(connection, "RUN-ONE", EARLY)
        persist_conflicts(connection, "RUN-ONE", DAY, EARLY)
        assert conflicts_at(connection, HH, DAY, EARLY)
        assert conflicts_at(connection, OTHER_HH, DAY, EARLY) == []
        assert conflicts_at(connection, HH, DAY + timedelta(days=1), EARLY)[0]["code"] == (
            "AMBIGUOUS_ENTITY"
        )
        assert observed_conflicts_at(connection, HH, DAY + timedelta(days=1), EARLY) is None
        assert conflicts_at(connection, "WC-HH-UNKNOWN", DAY, EARLY) == []


def test_feed_publication_rollback_also_rolls_back_conflict_observation(database):
    with pytest.raises(RuntimeError, match="abort publication"):
        with write_transaction(database) as connection:
            run(connection, "RUN-ROLLBACK", EARLY)
            household(connection, "RUN-ROLLBACK", EARLY)
            conflicting_person(connection, "RUN-ROLLBACK", EARLY)
            persist_conflicts(connection, "RUN-ROLLBACK", DAY, EARLY)
            assert conflicts_at(connection, HH, DAY, EARLY)
            raise RuntimeError("abort publication")
    with write_transaction(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM feed_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conflict_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_later_publication_at_same_instant_wins_without_lexical_run_id_order(database):
    with write_transaction(database) as connection:
        run(connection, "RUN-Z-FIRST", EARLY)
        household(connection, "RUN-Z-FIRST", EARLY)
        persist_conflicts(connection, "RUN-Z-FIRST", DAY, EARLY)
    with write_transaction(database) as connection:
        run(connection, "RUN-A-SECOND", EARLY)
        conflicting_person(connection, "RUN-A-SECOND", EARLY)
        persist_conflicts(connection, "RUN-A-SECOND", DAY, EARLY)
        assert conflicts_at(connection, HH, DAY, EARLY)[0]["code"] == "AMBIGUOUS_ENTITY"
        assert observed_conflicts_at(connection, HH, DAY, EARLY) == conflicts_at(
            connection, HH, DAY, EARLY,
        )
        first_payload = connection.execute(
            "SELECT payload FROM conflict_snapshots WHERE run_id='RUN-Z-FIRST'",
        ).fetchone()[0]
        assert first_payload == "[]"


def test_same_run_repeated_after_context_change_cannot_rewrite_original_observation(database):
    with write_transaction(database) as connection:
        run(connection, "RUN-ONE", EARLY)
        household(connection, "RUN-ONE", EARLY)
        persist_conflicts(connection, "RUN-ONE", DAY, EARLY)
        conflicting_person(connection, "RUN-ONE", EARLY)
        persist_conflicts(connection, "RUN-ONE", DAY, EARLY)
        assert observed_conflicts_at(connection, HH, DAY, EARLY) == []
        assert conflicts_at(connection, HH, DAY, EARLY)[0]["code"] == "AMBIGUOUS_ENTITY"
        assert connection.execute("SELECT COUNT(*) FROM conflict_snapshots").fetchone()[0] == 1


def test_no_known_households_creates_no_invented_conflicts(database):
    with write_transaction(database) as connection:
        run(connection, "RUN-EMPTY", EARLY)
        persist_conflicts(connection, "RUN-EMPTY", DAY, EARLY)
        assert connection.execute("SELECT COUNT(*) FROM conflict_snapshots").fetchone()[0] == 0
        assert conflicts_at(connection, HH, DAY, EARLY) == []


def test_ambiguous_household_id_itself_is_included(database):
    with write_transaction(database) as connection:
        run(connection, "RUN-AMBIGUOUS", EARLY)
        household(connection, "RUN-AMBIGUOUS", EARLY)
        fact(connection, "RUN-AMBIGUOUS", "household", HH,
             {"id": HH, "name": "Conflicting synthetic name"}, EARLY, suffix="-other")
        persist_conflicts(connection, "RUN-AMBIGUOUS", DAY, EARLY)
        assert conflicts_at(connection, HH, DAY, EARLY)[0]["subject"] == HH


def test_cutoffs_are_aware_normalized_and_must_match_the_publishing_run(database):
    with write_transaction(database) as connection:
        run(connection, "RUN-ONE", EARLY)
        household(connection, "RUN-ONE", EARLY)
        conflicting_person(connection, "RUN-ONE", EARLY)
        equivalent = datetime.fromisoformat("2026-10-01T08:00:00-04:00")
        persist_conflicts(connection, "RUN-ONE", DAY, equivalent)
        assert conflicts_at(connection, HH, DAY, equivalent)
        for day, known in [(DAY + timedelta(days=1), EARLY), (DAY, LATER)]:
            with pytest.raises(ValueError, match="match"):
                persist_conflicts(connection, "RUN-ONE", day, known)
        with pytest.raises(ValueError, match="timezone"):
            conflicts_at(connection, HH, DAY, datetime(2026, 10, 1))
        with pytest.raises(ValueError, match="existing feed"):
            persist_conflicts(connection, "RUN-UNKNOWN", DAY, EARLY)


def test_persistence_requires_callers_atomic_publication_transaction(database):
    connection = connect(database)
    try:
        with pytest.raises(RuntimeError, match="publication transaction"):
            persist_conflicts(connection, "RUN-UNKNOWN", DAY, EARLY)
    finally:
        connection.close()
