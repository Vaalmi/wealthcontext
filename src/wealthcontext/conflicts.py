"""Canonical cutoff conflict views and append-only feed-publication observations."""

import json
import sqlite3
from datetime import date, datetime

from wealthcontext.clock import utc_timestamp
from wealthcontext.households import household_context, household_views
from wealthcontext.storage import canonical


def migrate_conflicts(connection: sqlite3.Connection) -> None:
    """Create storage using the caller's connection and transaction."""
    connection.execute("""CREATE TABLE IF NOT EXISTS conflict_snapshots (
        run_id TEXT NOT NULL REFERENCES feed_runs(id), household_id TEXT NOT NULL,
        business_date TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(run_id, household_id))""")
    connection.execute("CREATE INDEX IF NOT EXISTS conflict_snapshot_cutoffs "
                       "ON conflict_snapshots(household_id,business_date,recorded_at)")
    for operation in ("UPDATE", "DELETE"):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS
            conflict_snapshots_no_{operation.lower()} BEFORE {operation} ON conflict_snapshots
            BEGIN SELECT RAISE(ABORT, 'conflict snapshots are append-only'); END""")


def persist_conflicts(
    connection: sqlite3.Connection, run_id: str, day: date, known: datetime,
) -> None:
    """Observe the publishing run's business date, including explicit resolutions.

    These immutable audit observations share the feed transaction. They record
    what this run observed for its own business date; they are not a cache for
    arbitrary valuation dates. Repeated calls never rewrite a run's observations.
    """
    if not connection.in_transaction:
        raise RuntimeError("conflict observations require the feed publication transaction")
    recorded = utc_timestamp(known)
    run = connection.execute("SELECT business_date,recorded_at FROM feed_runs WHERE id=?",
                             (run_id,)).fetchone()
    if run is None:
        raise ValueError("conflict observations require an existing feed run")
    if (run["business_date"] != day.isoformat()
            or utc_timestamp(datetime.fromisoformat(run["recorded_at"])) != recorded):
        raise ValueError("conflict cutoffs must match the publishing feed run")
    views = household_views(connection, day, known)
    households = views["household"]
    previous = {row[0] for row in connection.execute(
        "SELECT DISTINCT household_id FROM conflict_snapshots WHERE recorded_at<=?", (recorded,),
    )}
    for household_id in sorted(households.values.keys() | households.ambiguous.keys() | previous):
        if connection.execute(
            "SELECT 1 FROM conflict_snapshots WHERE run_id=? AND household_id=?",
            (run_id, household_id),
        ).fetchone():
            continue
        context = household_context(connection, household_id, day, known, views=views)
        connection.execute("INSERT INTO conflict_snapshots VALUES (?,?,?,?,?)", (
            run_id, household_id, day.isoformat(), recorded, canonical(context.conflicts),
        ))


def conflicts_at(
    connection: sqlite3.Connection, household_id: str, day: date, known: datetime,
) -> list[dict]:
    """Reconstruct conflicts from canonical facts at both requested cutoffs.

    Late received records can affect a different valuation date from their file's
    business date. Derivation also covers future-effective facts before that
    date's first run, and households whose valid interval has since ended. The
    caller owns authorization and the read snapshot, just as for household_context.
    """
    return household_context(connection, household_id, day, known).conflicts


def observed_conflicts_at(
    connection: sqlite3.Connection, household_id: str, day: date, known: datetime,
) -> list[dict] | None:
    """Return the latest publication observation, not a reconstructed cutoff view.

    Different feeds may share one fixture instant. Each observation is computed
    after its run publishes; later feed insertion represents combined state then.
    None means no run observed this date by the cutoff; [] is a recorded absence
    of conflicts. An older observation can differ from conflicts_at after backfill.
    """
    row = connection.execute(
        "SELECT c.payload FROM conflict_snapshots c JOIN feed_runs r ON r.id=c.run_id "
        "WHERE c.household_id=? AND c.business_date=? AND c.recorded_at<=? "
        "ORDER BY c.recorded_at DESC,r.rowid DESC LIMIT 1",
        (household_id, day.isoformat(), utc_timestamp(known)),
    ).fetchone()
    return json.loads(row["payload"]) if row else None
