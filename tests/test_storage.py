import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from wealthcontext.clock import utc_timestamp
from wealthcontext.database import migrate, write_transaction
from wealthcontext.storage import entitlements, facts_at, migrate_business, read_snapshot
from wealthcontext.storage import seed_vertical_slice as seed
from wealthcontext.telemetry import create_provider


def test_fixture_import_is_idempotent_and_preserves_simulated_clock(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    migrate(db)
    migrate_business(db)
    late_clock = datetime(2035, 1, 1, tzinfo=UTC)
    seed(db, late_clock)
    seed(db, late_clock)
    with read_snapshot(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 7
        assert entitlements(connection)[0] == 1
        assert facts_at(connection, "position", date(2026, 9, 30), late_clock)
        assert not facts_at(connection, "position", date(2026, 10, 1), late_clock)
        assert not facts_at(connection, "position", date(2026, 9, 30),
                            datetime(2026, 9, 30, 21, tzinfo=UTC))
        source = connection.execute("SELECT * FROM source_records LIMIT 1").fetchone()
        assert source["recorded_at"] == "2026-09-30T22:00:00.000000+00:00"
        assert source["imported_at"] == utc_timestamp(late_clock)


def test_otel_exports_linked_spans_as_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    provider = create_provider(path)
    tracer = provider.get_tracer("wealthcontext.test")
    with tracer.start_as_current_span("tool"):
        with tracer.start_as_current_span("authz"):
            pass
    provider.shutdown()
    spans = [json.loads(line) for line in path.read_text().splitlines()]
    assert [span["name"] for span in spans] == ["authz", "tool"]
    assert spans[0]["parent_id"] == spans[1]["context"]["span_id"]
    assert spans[0]["context"]["trace_id"] == spans[1]["context"]["trace_id"]


def test_knowledge_cutoff_compares_instants_across_offsets_and_precision(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    migrate(db)
    migrate_business(db)
    seed(db, datetime(2035, 1, 1, tzinfo=UTC))
    with read_snapshot(db) as connection:
        for cutoff, present in [
            ("2026-09-30T19:00:00-04:00", True),
            ("2026-09-30T23:00:00+02:00", False),
            ("2026-09-30T21:59:59.999999Z", False),
            ("2026-09-30T22:00:00Z", True),
            ("2026-09-30T22:00:00.000001Z", True),
        ]:
            assert bool(facts_at(connection, "position", date(2026, 9, 30),
                                 datetime.fromisoformat(cutoff))) is present
        with pytest.raises(ValueError, match="timezone"):
            facts_at(connection, "position", date(2026, 9, 30), datetime(2026, 10, 1))


def test_timestamp_migration_preserves_instants_and_recorded_payloads(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    migrate(db)
    migrate_business(db)
    seed(db, datetime(2035, 1, 1, tzinfo=UTC))
    with write_transaction(db) as connection:
        connection.execute("DELETE FROM schema_version WHERE version=3")
        for table in ("facts", "source_records", "entitlement_revisions"):
            connection.execute(f"UPDATE {table} SET recorded_at='2026-09-30T18:00:00-04:00'")
        connection.execute("INSERT INTO results VALUES ('r_test','t_test',NULL,NULL,?,?,?)",
                           ('{"original":"unchanged"}', 'original-hash', '2026-09-30T22:00:00Z'))
    migrate_business(db)
    migrate_business(db)
    with read_snapshot(db) as connection:
        for table in ("facts", "source_records", "entitlement_revisions", "results"):
            assert {row[0] for row in connection.execute(f"SELECT recorded_at FROM {table}")} == {
                "2026-09-30T22:00:00.000000+00:00",
            }
        row = connection.execute("SELECT payload,payload_hash FROM results").fetchone()
        assert tuple(row) == ('{"original":"unchanged"}', 'original-hash')
