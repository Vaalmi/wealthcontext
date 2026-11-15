"""Legacy persisted FX defenses; distinct from current received-row admission."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_p3_oracle_agreement import import_export
from test_p3_timestamp_bounds import INVALID_TIMESTAMPS, mixed_currency_export
from test_reference_oracle import DAY, HH, fix

from wealthcontext import feeds
from wealthcontext.clock import utc_timestamp
from wealthcontext.context_service import ContextService
from wealthcontext.database import write_transaction
from wealthcontext.identity import Principal
from wealthcontext.storage import canonical, digest, read_snapshot
from wealthcontext.telemetry import create_provider
from wealthcontext.valuation import resolve_received_valuation

NOW = datetime(2035, 1, 1, tzinfo=UTC)
BAD_RECEIVED = datetime(2026, 10, 2, 13, tzinfo=UTC)
CORRECTED = BAD_RECEIVED + timedelta(hours=1)


@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
def test_timestamp_storage_reports_out_of_range_as_value_error(timestamp):
    with pytest.raises(ValueError, match="UTC instant is outside years 1 through 9999"):
        utc_timestamp(datetime.fromisoformat(timestamp))


@pytest.mark.parametrize("timestamp,expected", [
    ("0001-01-01T00:01:00+00:01", "0001-01-01T00:00:00.000000+00:00"),
    ("9999-12-31T23:58:59.999999-00:01", "9999-12-31T23:59:59.999999+00:00"),
    ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000000+00:00"),
    ("9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59.999999+00:00"),
    ("2026-09-30T16:00:00.123456+01:00", "2026-09-30T15:00:00.123456+00:00"),
])
def test_timestamp_storage_keeps_valid_fixed_width_utc_encoding(timestamp, expected):
    assert utc_timestamp(datetime.fromisoformat(timestamp)) == expected


def received_fixture(directory, timestamp, *, field="rate_at", prior=True):
    export = mixed_currency_export(directory)
    if prior:
        export.feed("FX", "fx", [fix("GBP", "1.1")])
    bad = {**fix("GBP", "9"), "source_record_id": "LEGACY-FX", field: timestamp}
    export.feed("FX", "fx", [bad, fix("EUR", "1.1")], at=BAD_RECEIVED.isoformat())
    export.feed("FX", "fx", [fix("GBP", "1.25")], at=CORRECTED.isoformat())
    return directory / "received.sqlite"


def legacy_database(directory, timestamp, monkeypatch):
    """Emulate the old AwareDatetime admission for one received endpoint row only.

    The bypass exists solely in this test setup. Actual old ingestion persisted
    these aware endpoint values before the representability validator was added.
    Restore normal parsing before any valuation/service call; retain exact raw
    received payloads, file manifests and receipt times, not fabricated answers.
    """
    database = received_fixture(directory, timestamp)
    parse = feeds.parse_row

    def old_admission(feed, raw, *, source_system=None):
        if isinstance(raw, dict) and raw.get("source_record_id") == "LEGACY-FX":
            typed = parse(feed, {**raw, "rate_at": "2026-09-30T15:00:00Z"},
                          source_system=source_system)
            assert isinstance(typed, feeds.FXRow)
            return typed.model_copy(update={"rate_at": datetime.fromisoformat(timestamp)})
        return parse(feed, raw, source_system=source_system)

    with monkeypatch.context() as patch:
        patch.setattr(feeds, "parse_row", old_admission)
        import_export(directory, database, NOW)
    with read_snapshot(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 0
        source = connection.execute(
            "SELECT payload,payload_hash FROM source_records "
            "WHERE source_record_id='LEGACY-FX' AND record_type='record'",
        ).fetchone()
        assert json.loads(source["payload"])["rate_at"] == timestamp
        assert digest(source["payload"]) == source["payload_hash"]
    return database


def assert_durable(database, result):
    with read_snapshot(database) as connection:
        stored = connection.execute("SELECT * FROM results WHERE id=?", (result["result_id"],))
        row = stored.fetchone()
        assert row and json.loads(row["payload"]) == result
        assert digest(row["payload"]) == row["payload_hash"]
        assert row["trace_id"] == result["trace_id"]
        decisions = connection.execute(
            "SELECT allowed FROM decisions WHERE trace_id=?", (result["trace_id"],),
        ).fetchall()
        assert decisions and all(item["allowed"] for item in decisions)


@pytest.mark.anyio
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
@pytest.mark.parametrize("tool", ["get_household_snapshot", "get_portfolio_exposure",
                                  "get_concentrated_positions"])
async def test_legacy_endpoint_fix_returns_durable_incomplete_without_older_fallback(
    tmp_path, monkeypatch, opa_server, timestamp, tool,
):
    database = legacy_database(tmp_path, timestamp, monkeypatch)
    provider = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, opa_server, provider.get_tracer("legacy-boundary"))
    try:
        for known, total in [
            (BAD_RECEIVED - timedelta(microseconds=1), "44.50"),
            (BAD_RECEIVED, None),
            (CORRECTED - timedelta(microseconds=1), None),
            (CORRECTED, "48.25"),
            (BAD_RECEIVED, None),
        ]:
            result = await service.execute(Principal("COMP-01", "compliance"), NOW, tool, {
                "household_id": HH, "as_of": DAY.isoformat(), "known_as_of": known.isoformat(),
            })
            assert result["status"] == ("COMPLETE" if total else "INCOMPLETE"), result
            assert result["error"] is None
            assert datetime.fromisoformat(result["known_as_of"]) == known
            if tool != "get_concentrated_positions":
                assert result["data"].get("assets_usd") == total
                if total is None:
                    assert result["data"]["fresh_subtotal_usd"] == "17.00"
            else:
                assert bool(result["data"]["indeterminate"]) is (total is None)
                if total is None:
                    assert not result["data"]["breaches"]
            if total is None:
                assert any(flag["code"] == "FX_UNAVAILABLE"
                           and flag["detail"]["reason"] == "INVALID_FIX_TIMESTAMP"
                           for flag in result["quality_flags"])
            assert_durable(database, result)
        with read_snapshot(database) as connection:
            # The historical bad fact remains evidence; reads must never rewrite it.
            payload = connection.execute(
                "SELECT payload FROM facts WHERE kind='fx' AND recorded_at=? "
                "AND entity_id='GBP'", (utc_timestamp(BAD_RECEIVED),),
            ).fetchone()[0]
            assert json.loads(payload)["rate_at"] == timestamp
    finally:
        provider.shutdown()


@pytest.mark.parametrize("value", [None, "not-a-time", 17, {}, "2026-09-30T15:00:00", "MISSING"])
def test_malformed_persisted_fix_fails_closed_without_rewriting_or_older_fallback(
    tmp_path, monkeypatch, value,
):
    database = legacy_database(tmp_path, INVALID_TIMESTAMPS[0], monkeypatch)
    # Explicit malformed persisted-fact setup, not a claim that current or old
    # wire validation admitted these non-datetime values.
    with write_transaction(database) as connection:
        source, text = connection.execute(
            "SELECT source_id,payload FROM facts WHERE kind='fx' AND entity_id='GBP' "
            "AND recorded_at=?", (utc_timestamp(BAD_RECEIVED),),
        ).fetchone()
        payload = json.loads(text)
        if value == "MISSING":
            payload.pop("rate_at")
        else:
            payload["rate_at"] = value
        modified = canonical(payload)
        connection.execute("UPDATE facts SET payload=? WHERE source_id=?", (modified, source))
    with read_snapshot(database) as connection:
        before = resolve_received_valuation(connection, HH, DAY, BAD_RECEIVED)
        assert before.valuation.assets_usd is None
        assert before.valuation.fresh_subtotal_usd == Decimal("17.00")
        assert any(flag.code == "FX_UNAVAILABLE"
                   and flag.detail["reason"] == "INVALID_FIX_TIMESTAMP"
                   for flag in before.quality_flags)
        corrected = resolve_received_valuation(connection, HH, DAY, CORRECTED)
        assert corrected.valuation.assets_usd == Decimal("48.25")
        assert connection.execute("SELECT payload FROM facts WHERE source_id=?", (source,)) \
            .fetchone()[0] == modified


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["source_asserted_at", "rate_at"])
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
async def test_current_ingestion_quarantine_returns_durable_service_response(
    tmp_path, opa_server, field, timestamp,
):
    database = received_fixture(tmp_path, timestamp, field=field, prior=False)
    import_export(tmp_path, database, NOW)
    provider = create_provider(tmp_path / "traces.jsonl")
    service = ContextService(database, opa_server, provider.get_tracer("received-boundary"))
    try:
        for known, total in [(BAD_RECEIVED, None), (CORRECTED, "48.25"), (BAD_RECEIVED, None)]:
            result = await service.execute(Principal("COMP-01", "compliance"), NOW,
                "get_household_snapshot", {
                    "household_id": HH, "as_of": DAY.isoformat(), "known_as_of": known.isoformat(),
                })
            assert result["status"] == ("COMPLETE" if total else "INCOMPLETE")
            assert result["error"] is None and result["data"].get("assets_usd") == total
            assert datetime.fromisoformat(result["known_as_of"]) == known
            if total is None:
                assert result["data"]["fresh_subtotal_usd"] == "17.00"
            assert_durable(database, result)
        with read_snapshot(database) as connection:
            rejected = connection.execute("SELECT reason FROM quarantine").fetchall()
            assert len(rejected) == 1 and rejected[0][0] == "INVALID_ROW"
            assert connection.execute(
                "SELECT COUNT(*) FROM facts WHERE kind='fx' AND entity_id='EUR'",
            ).fetchone()[0] == 1
    finally:
        provider.shutdown()
