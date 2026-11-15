"""Received timestamp admission at UTC's representable endpoints, with valid peers."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_p3_tied_evidence import BEFORE, CORRECTION, check
from test_reference_oracle import A1, A2, DAY, HH, Export, cash, fix, position, quote, security

from reference_oracle import evaluate, load_received_history
from wealthcontext.facts import account_snapshot, fact_view
from wealthcontext.feeds import FXRow, parse_row
from wealthcontext.storage import read_snapshot

INVALID_TIMESTAMPS = [
    "0001-01-01T00:00:00+00:01",
    "9999-12-31T23:59:59-00:01",
]
VALID_TIMESTAMPS = [
    "0001-01-01T00:01:00+00:01",
    "9999-12-31T23:58:59.999999-00:01",
    "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59.999999Z",
    "2026-09-30T16:00:00.123456+01:00",
]


@pytest.mark.parametrize("field", ["source_asserted_at", "rate_at"])
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
def test_feed_timestamps_reject_only_unrepresentable_utc_instants(field, timestamp):
    with pytest.raises(ValidationError, match="UTC instant is outside years 1 through 9999"):
        parse_row("fx", {**fix("GBP", "1.25"), field: timestamp}, source_system="FX")


@pytest.mark.parametrize("field", ["source_asserted_at", "rate_at"])
@pytest.mark.parametrize("timestamp", VALID_TIMESTAMPS)
def test_valid_feed_timestamp_keeps_original_offset_and_microseconds(field, timestamp):
    parsed = parse_row("fx", {**fix("GBP", "1.25"), field: timestamp}, source_system="FX")
    assert isinstance(parsed, FXRow)
    value = getattr(parsed, field)
    expected = datetime.fromisoformat(timestamp)
    assert value.isoformat() == expected.isoformat()
    assert value.utcoffset() == expected.utcoffset()
    assert value.microsecond == expected.microsecond
    assert value.astimezone(UTC) == expected.astimezone(UTC)


def mixed_currency_export(directory):
    export = Export(directory)
    export.crm()
    export.feed("SECMASTER", "securities", [
        security(currency="GBP", country="GB"),
        security("WC-SEC-0002", issuer="WC-ISS-0002"),
    ])
    export.snapshots([position("1"), position("2", sec="WC-SEC-0002"), cash("7")])
    export.feed("CUSTODY-A", "prices", [
        quote("25", currency="GBP"), quote("5", sec="WC-SEC-0002"),
    ])
    return export


@pytest.mark.parametrize("field", ["source_asserted_at", "rate_at"])
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
def test_received_invalid_fx_timestamp_quarantines_row_preserves_peer_and_cutoffs(
    tmp_path, field, timestamp,
):
    export = mixed_currency_export(tmp_path)
    bad = {**fix("GBP", "1.25"), field: timestamp}
    export.feed("FX", "fx", [bad, fix("EUR", "1.1")])
    original = evaluate(load_received_history(tmp_path), household_ids=[HH],
                        as_of=DAY, known_as_of=BEFORE)
    export.feed("FX", "fx", [fix("GBP", "1.25")], at=CORRECTION.isoformat())
    database, before, state = check(tmp_path, known=BEFORE)
    # The valid USD peer contributes 2*5+7=17; GBP cannot contribute without a fix.
    assert before["status"] == "INCOMPLETE" and before["fresh_subtotal_usd"] == "17.00"
    assert before.get("assets_usd") is None
    assert set(state.view("fx", DAY).values) == {"EUR"}
    assert evaluate(load_received_history(tmp_path), household_ids=[HH],
                    as_of=DAY, known_as_of=BEFORE) == original
    with read_snapshot(database) as connection:
        assert set(fact_view(connection, "fx", DAY, BEFORE).values) == {"EUR"}
        rejected = connection.execute("SELECT reason,raw_payload FROM quarantine").fetchall()
        assert len(rejected) == 1 and rejected[0]["reason"] == "INVALID_ROW"
        assert timestamp in rejected[0]["raw_payload"]
    _, corrected, _ = check(tmp_path, known=CORRECTION)
    assert corrected["status"] == "COMPLETE" and corrected["assets_usd"] == "48.25"


@pytest.mark.parametrize("field", ["source_asserted_at", "rate_at"])
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
def test_invalid_later_fx_timestamp_does_not_displace_prior_valid_fix(tmp_path, field, timestamp):
    export = mixed_currency_export(tmp_path)
    export.feed("FX", "fx", [fix("GBP", "1.25")])
    export.feed("FX", "fx", [{**fix("GBP", "9"), field: timestamp}, fix("EUR", "1.1")],
                at=CORRECTION.isoformat())
    for cutoff in (BEFORE, CORRECTION):
        database, result, _ = check(tmp_path, known=cutoff)
        assert result["status"] == "COMPLETE" and result["assets_usd"] == "48.25"
        with read_snapshot(database) as connection:
            assert fact_view(connection, "fx", DAY, cutoff).values["GBP"]["usd_rate"] == "1.25"


@pytest.mark.parametrize("field", ["source_asserted_at", "rate_at"])
@pytest.mark.parametrize("timestamp", VALID_TIMESTAMPS[:2])
def test_received_valid_utc_endpoints_are_admitted_without_offset_rewriting(
    tmp_path, field, timestamp,
):
    export = mixed_currency_export(tmp_path)
    export.feed("FX", "fx", [{**fix("GBP", "1.25"), field: timestamp}, fix("EUR", "1.1")])
    database, result, state = check(tmp_path, known=BEFORE)
    assert "GBP" in state.view("fx", DAY).values
    # A representable extreme rate time is admitted, but is not this day's FX fix.
    assert result["status"] == ("COMPLETE" if field == "source_asserted_at" else "INCOMPLETE")
    assert result.get("assets_usd") == ("48.25" if field == "source_asserted_at" else None)
    with read_snapshot(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 0
        value = fact_view(connection, "fx", DAY, BEFORE).values["GBP"]
        if field == "rate_at":
            assert value[field] == timestamp


@pytest.mark.parametrize("feed,source", [
    ("pas_snapshots", "PAS-A"), ("custody_snapshots", "CUSTODY-A"),
])
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
def test_received_invalid_asserted_time_quarantines_whole_account_but_not_valid_peer(
    tmp_path, feed, source, timestamp,
):
    export = Export(tmp_path)
    export.crm(accounts=(A1, A2))
    export.feed("SECMASTER", "securities", [security()])
    good = [position("1"), cash("7"), position("2", account=A2), cash("3", account=A2)]
    bad = [{**good[0], "source_asserted_at": timestamp}, *good[1:]]
    for current_feed, current_source in (
        ("pas_snapshots", "PAS-A"), ("custody_snapshots", "CUSTODY-A"),
    ):
        export.feed(current_source, current_feed, bad if current_feed == feed else good,
                    accounts=(A1, A2))
    export.feed("CUSTODY-A", "prices", [quote("25")])
    original = evaluate(load_received_history(tmp_path), household_ids=[HH],
                        as_of=DAY, known_as_of=BEFORE)
    export.feed(source, feed, good, accounts=(A1, A2), at=CORRECTION.isoformat())
    database, before, _ = check(tmp_path, known=BEFORE)
    # A2 is 2*25+3=53; A1 is 1*25+7=32. Only PAS loss blocks total valuation.
    if feed == "pas_snapshots":
        assert before["status"] == "INCOMPLETE" and before["fresh_subtotal_usd"] == "53.00"
    else:
        assert before["status"] == "COMPLETE" and before["assets_usd"] == "85.00"
    assert evaluate(load_received_history(tmp_path), household_ids=[HH],
                    as_of=DAY, known_as_of=BEFORE) == original
    with read_snapshot(database) as connection:
        quarantined = connection.execute("SELECT account_id,reason FROM quarantine").fetchall()
        assert len(quarantined) == 2
        assert {(item["account_id"], item["reason"]) for item in quarantined} == {
            (A1["account_id"], "INVALID_ACCOUNT_ROW"),
        }
        assert not account_snapshot(connection, feed, A1["account_id"], DAY, BEFORE).available
        assert account_snapshot(connection, feed, A2["account_id"], DAY, BEFORE).available
        assert account_snapshot(connection, feed, A1["account_id"], DAY, CORRECTION).available
    _, corrected, _ = check(tmp_path, known=CORRECTION)
    assert corrected["status"] == "COMPLETE" and corrected["assets_usd"] == "85.00"
    _, still_before, _ = check(tmp_path, known=CORRECTION - timedelta(microseconds=1))
    assert still_before == before
