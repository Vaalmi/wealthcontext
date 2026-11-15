"""Equal-receipt evidence rules, from hand-authored bytes through both engines."""

import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest
from test_p3_oracle_agreement import compare_book, compare_household, import_export
from test_reference_oracle import A1, A2, DAY, HH, KNOWN, Export, cash, fix, position, quote, row
from test_reference_oracle import security as security_row

from reference_oracle import EvidenceError, evaluate, load_received_history
from reference_oracle.history import State
from wealthcontext.facts import account_snapshot, fact_view
from wealthcontext.storage import read_snapshot
from wealthcontext.valuation import resolve_received_valuation

CORRECTION = datetime(2026, 10, 2, 14, tzinfo=UTC)
BEFORE = CORRECTION - timedelta(microseconds=1)


def check(directory, *, known=KNOWN):
    database = directory / "received.sqlite"
    if not database.exists():
        import_export(directory, database, datetime(2055, 1, 1, tzinfo=UTC))
    history = load_received_history(directory)
    expected = evaluate(history, household_ids=[HH], as_of=DAY, known_as_of=known)
    with read_snapshot(database) as connection:
        actual = resolve_received_valuation(connection, HH, DAY, known)
        compare_household(actual, expected["households"][0])
        compare_book([actual.valuation], expected)
    return database, expected["households"][0], State(history, known)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("optional", ["isin", "source_asserted_at"])
def test_optional_null_defaults_are_equivalent_non_snapshot_assertions(tmp_path, reverse, optional):
    export = Export(tmp_path)
    export.crm()
    export.snapshots([position("1"), cash("7")])
    export.feed("CUSTODY-A", "prices", [quote("25")])
    explicit, omitted = security_row(), security_row()
    omitted.pop(optional)
    for definition in ([omitted, explicit] if reverse else [explicit, omitted]):
        export.feed("SECMASTER", "securities", [definition])
    database, expected, state = check(tmp_path)
    assert expected["status"] == "COMPLETE" and expected["assets_usd"] == "32.00"
    assert not state.view("security", DAY).ambiguous
    with read_snapshot(database) as connection:
        assert not fact_view(connection, "security", DAY, KNOWN).ambiguous
        # Raw evidence stays distinct although the validated assertions agree.
        assert connection.execute(
            "SELECT COUNT(*) FROM source_records WHERE source_system='SECMASTER' "
            "AND record_type='record'",
        ).fetchone()[0] == 2


def fx_export(directory):
    export = Export(directory)
    export.crm()
    export.feed("SECMASTER", "securities", [security_row(currency="GBP", country="GB")])
    export.snapshots([position("1"), cash("7")])
    export.feed("CUSTODY-A", "prices", [quote("25", currency="GBP")])
    return export


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("left,right", [
    ("2026-09-30T15:00:00Z", "2026-09-30T15:00:00+00:00"),
    ("2026-09-30T15:00:00Z", "2026-09-30T15:00:00.000Z"),
    ("2026-09-30T15:00:00.1Z", "2026-09-30T15:00:00.100000+00:00"),
    ("2026-09-30T15:00:00.1234567Z", "2026-09-30T15:00:00.123456Z"),
    ("2026-09-30t15:00:00z", "2026-09-30 15:00:00+00:00"),
    ("2026-09-30T16:00:00.1+01:00", "2026-09-30T16:00:00.100000+01:00"),
    ("1790780400", "2026-09-30T15:00:00Z"),
    (1790780400, "2026-09-30T15:00:00Z"),
    ("1790780400000", "2026-09-30T15:00:00Z"),
    (1790780400000, "2026-09-30T15:00:00Z"),
    ("+01790780400", "2026-09-30T15:00:00Z"),
    ("1.7907804e9", "2026-09-30T15:00:00Z"),
    ("1790780400.1234567", "2026-09-30T15:00:00.123457Z"),
    ("1790780400000.123456", "2026-09-30T15:00:00.000124Z"),
    ("1790780400.9999999", "2026-09-30T15:00:01Z"),
    ("2026-09-30_15:00Z", "2026-09-30T15:00:00Z"),
    ("2026-09-30T15:00:00,1+0000", "2026-09-30T15:00:00.100000Z"),
    ("2026-09-30T14:00:00−01:00", "2026-09-30T14:00:00-01:00"),
])
def test_equivalent_fx_timestamp_spellings_and_correction_cutoffs(tmp_path, reverse, left, right):
    export = fx_export(tmp_path)
    for spelling in ([right, left] if reverse else [left, right]):
        row = {**fix("GBP", "1.25"), "rate_at": spelling}
        export.feed("FX", "fx", [row], at="2026-09-30T22:00:00Z")
    before_history = load_received_history(tmp_path)
    before = evaluate(before_history, household_ids=[HH], as_of=DAY, known_as_of=BEFORE)
    export.feed("FX", "fx", [fix("GBP", "1.5")], at=CORRECTION.isoformat())
    database, result, state = check(tmp_path, known=BEFORE)
    assert result["status"] == "COMPLETE" and result["assets_usd"] == "38.25"
    assert not state.view("fx", DAY).ambiguous
    assert evaluate(load_received_history(tmp_path), household_ids=[HH], as_of=DAY,
                    known_as_of=BEFORE) == before
    with read_snapshot(database) as connection:
        assert not fact_view(connection, "fx", DAY, BEFORE).ambiguous
        assert connection.execute(
            "SELECT COUNT(*) FROM source_records WHERE source_system='FX' "
            "AND record_type='record' AND recorded_at<?", (CORRECTION.isoformat(),),
        ).fetchone()[0] == 2
    _database, corrected, _state = check(tmp_path, known=CORRECTION)
    assert corrected["assets_usd"] == "44.50"


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_instant_with_different_reported_offsets_remains_distinct(tmp_path, reverse):
    export = fx_export(tmp_path)
    spellings = ["2026-09-30T15:00:00Z", "2026-09-30T16:00:00+01:00"]
    for spelling in reversed(spellings) if reverse else spellings:
        export.feed("FX", "fx", [{**fix("GBP", "1.25"), "rate_at": spelling}],
                    at="2026-09-30T22:00:00Z")
    _database, result, state = check(tmp_path)
    assert result["status"] == "INCOMPLETE" and result["fresh_subtotal_usd"] == "7.00"
    assert "GBP" in state.view("fx", DAY).ambiguous


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("earlier_start", [False, True])
def test_tied_fx_different_file_dates_are_ambiguous_until_later_received_correction(
    tmp_path, reverse, earlier_start,
):
    export = fx_export(tmp_path)
    correct, wrong = fix("GBP", "1.25"), fix("GBP", "1.25")
    if earlier_start:
        wrong["valid_from"] = "2026-09-29"
    publications = [(DAY, correct), (date(2026, 9, 29), wrong)]
    for file_day, observation in reversed(publications) if reverse else publications:
        export.feed("FX", "fx", [observation], day=file_day, at="2026-09-30T22:00:00Z")
    before_history = load_received_history(tmp_path)
    before_value = evaluate(before_history, household_ids=[HH], as_of=DAY, known_as_of=BEFORE)
    # Reassert the eligible raw GBP row in a genuinely new file, not a duplicate
    # delivery. The unrelated EUR row changes file bytes without changing GBP.
    export.feed("FX", "fx", [correct, fix("EUR", "1.1")], at=CORRECTION.isoformat())
    database, expected, state = check(tmp_path, known=BEFORE)
    assert evaluate(load_received_history(tmp_path), household_ids=[HH], as_of=DAY,
                    known_as_of=BEFORE) == before_value
    assert expected["status"] == "INCOMPLETE" and expected["fresh_subtotal_usd"] == "7.00"
    assert state.fx("GBP", DAY) == (None, "AMBIGUOUS_FX")
    with read_snapshot(database) as connection:
        unresolved = fact_view(connection, "fx", DAY, BEFORE)
        assert "GBP" in unresolved.ambiguous and "GBP" not in unresolved.values
        assert all(not any(key.startswith("_") for key in item)
                   for item in unresolved.ambiguous["GBP"])
    _database, corrected, state = check(tmp_path, known=CORRECTION)
    assert corrected["status"] == "COMPLETE" and corrected["assets_usd"] == "38.25"
    assert not state.view("fx", DAY).ambiguous


@pytest.mark.parametrize("reverse", [False, True])
def test_tied_fx_same_file_date_and_rate_stays_usable_across_active_start_intervals(
    tmp_path, reverse,
):
    export = fx_export(tmp_path)
    observations = [fix("GBP", "1.25"), {**fix("GBP", "1.25"), "valid_from": "2026-09-29"}]
    for observation in reversed(observations) if reverse else observations:
        export.feed("FX", "fx", [observation])
    _database, expected, state = check(tmp_path)
    assert expected["assets_usd"] == "38.25"
    assert state.fx("GBP", DAY)[0] is not None


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("feed", ["pas_snapshots", "custody_snapshots"])
@pytest.mark.parametrize("difference", ["source_record_id", "valid_to", "source_asserted_at",
                                      "pas_price"])
def test_tied_snapshot_publications_require_exact_raw_record_identity(
    tmp_path, reverse, feed, difference,
):
    export = Export(tmp_path)
    export.crm(accounts=(A1, A2))
    export.feed("SECMASTER", "securities", [security_row()])
    export.feed("CUSTODY-A", "prices", [quote("25")])
    original = [position("1"), cash("7")]
    changed = deepcopy(original)
    if difference == "source_record_id":
        changed[0][difference] += "-rekeyed"
    elif difference == "valid_to":
        changed[0][difference] = "2026-10-02"
    elif difference == "source_asserted_at":
        changed[0].pop(difference)
    else:
        changed[0][difference] = None
    for source, selected_feed in (("PAS-A", "pas_snapshots"), ("CUSTODY-A", "custody_snapshots")):
        rows = ([changed, original] if reverse else [original, changed]) if selected_feed == feed \
            else [original]
        for publication in rows:
            export.feed(source, selected_feed, publication, accounts=(A1, A2))
    source = "PAS-A" if feed == "pas_snapshots" else "CUSTODY-A"
    export.feed(source, feed, [*original, cash("0", account=A2)], accounts=(A1, A2),
                at=CORRECTION.isoformat())
    database, expected, state = check(tmp_path, known=BEFORE)
    publication, reason = state.snapshot(feed, A1["account_id"], DAY)
    assert publication is None and reason == "AMBIGUOUS_PUBLICATION"
    with read_snapshot(database) as connection:
        actual = account_snapshot(connection, feed, A1["account_id"], DAY, BEFORE)
        assert not actual.available and actual.reason == "AMBIGUOUS_SNAPSHOT"
    if feed == "pas_snapshots":
        assert expected["status"] == "INCOMPLETE" and expected["fresh_subtotal_usd"] == "0.00"
    else:
        assert expected["status"] == "COMPLETE" and expected["assets_usd"] == "32.00"
        assert {flag["code"] for flag in expected["quality_flags"]} == {
            "RECONCILIATION_UNAVAILABLE",
        }
    _database, corrected, state = check(tmp_path, known=CORRECTION)
    assert corrected["assets_usd"] == "32.00"
    assert state.snapshot(feed, A1["account_id"], DAY)[1] is None


@pytest.mark.parametrize("feed", ["pas_snapshots", "custody_snapshots"])
def test_tied_snapshot_row_order_does_not_change_exact_received_record_set(tmp_path, feed):
    export = Export(tmp_path)
    export.crm()
    export.feed("SECMASTER", "securities", [security_row()])
    export.feed("CUSTODY-A", "prices", [quote("25")])
    rows = [position("1"), cash("7")]
    export.snapshots(rows)
    source = "PAS-A" if feed == "pas_snapshots" else "CUSTODY-A"
    export.feed(source, feed, list(reversed(rows)), accounts=(A1,))
    database, expected, state = check(tmp_path)
    assert expected["assets_usd"] == "32.00"
    assert state.snapshot(feed, A1["account_id"], DAY)[1] is None
    with read_snapshot(database) as connection:
        assert account_snapshot(connection, feed, A1["account_id"], DAY, KNOWN).available


@pytest.mark.parametrize("reverse", [False, True])
def test_tied_unavailable_publications_do_not_become_available_or_ambiguous_due_to_reason(
    tmp_path, reverse,
):
    export = Export(tmp_path)
    export.crm()
    export.feed("CUSTODY-A", "custody_snapshots", [], accounts=(A1,))
    for failure in reversed(("absent", "invalid")) if reverse else ("absent", "invalid"):
        export.feed("PAS-A", "pas_snapshots", [] if failure == "absent" else [cash("-1")],
                    accounts=(A1,), absent=failure == "absent",
                    status="FAILED" if failure == "absent" else "SUCCESS")
    database, expected, state = check(tmp_path)
    publication, reason = state.snapshot("pas_snapshots", A1["account_id"], DAY)
    assert publication is not None and not publication.available
    assert reason == "MULTIPLE_UNAVAILABLE_PUBLICATIONS"
    assert expected["fresh_subtotal_usd"] == "0.00"
    with read_snapshot(database) as connection:
        actual = account_snapshot(connection, "pas_snapshots", A1["account_id"], DAY, KNOWN)
        assert not actual.available and actual.reason == reason


@pytest.mark.parametrize("timestamp", [
    "20260930T150000Z", "2026-09-30X15:00:00Z", "2026-09-30T15:00:00+00",
    "2026-09-30T15:00:00+00:00:00", " 1790780400 ", "1790780400e0", True, None,
])
def test_rejected_timestamp_spelling_does_not_restore_missing_fx(tmp_path, timestamp):
    export = fx_export(tmp_path)
    export.feed("FX", "fx", [{**fix("GBP", "1.25"), "rate_at": timestamp}])
    _database, result, _state = check(tmp_path)
    assert result["status"] == "INCOMPLETE"
    assert result["fresh_subtotal_usd"] == "7.00"


@pytest.mark.parametrize("field", ["rate_at", "source_asserted_at", "valid_from", "valid_to"])
@pytest.mark.parametrize("offset,accepted", [
    ("+00:60", False), ("+01:99", False), ("-00:60", False),
    ("−01:99", False), ("+0060", False), ("-0199", False),
    ("+00:59", True), ("+01:59", True), ("−01:59", True),
])
def test_received_timezone_minute_bounds_do_not_create_financial_evidence(
    tmp_path, field, offset, accepted,
):
    export = fx_export(tmp_path)
    local_time = "2026-10-01T00:00:00" if field == "valid_to" else "2026-09-30T00:00:00"
    if field in {"rate_at", "source_asserted_at"}:
        local_time = "2026-09-30T15:00:00"
    export.feed("FX", "fx", [{**fix("GBP", "1.25"), field: local_time + offset}])
    _database, result, _state = check(tmp_path)
    assert result["status"] == ("COMPLETE" if accepted else "INCOMPLETE")
    assert result.get("assets_usd") == ("38.25" if accepted else None)
    if not accepted:
        assert result["fresh_subtotal_usd"] == "7.00"


@pytest.mark.parametrize("field", ["price_date", "pas_price_date"])
@pytest.mark.parametrize("offset,accepted", [
    ("+00:60", False), ("+01:99", False), ("-00:60", False),
    ("−01:99", False), ("+0060", False), ("-0199", False),
    ("+00:59", True), ("+01:59", True), ("−01:59", True),
])
def test_received_price_dates_enforce_timezone_bounds_and_account_quarantine(
    tmp_path, field, offset, accepted,
):
    export = Export(tmp_path)
    export.crm()
    export.feed("SECMASTER", "securities", [security_row(currency="GBP", country="GB")])
    holding = position("1")
    closing_price = quote("25", currency="GBP")
    timestamp = "2026-09-30T00:00:00" + offset
    if field == "pas_price_date":
        holding["pas_price"] = {"price": "25", "price_date": timestamp, "currency": "GBP"}
    else:
        closing_price["price_date"] = timestamp
    export.snapshots([holding, cash("7")])
    export.feed("CUSTODY-A", "prices", [closing_price])
    export.feed("FX", "fx", [fix("GBP", "1.25")])
    _database, result, _state = check(tmp_path)
    assert result["status"] == ("COMPLETE" if accepted else "INCOMPLETE")
    assert result.get("assets_usd") == ("38.25" if accepted else None)
    if not accepted:
        # Malformed PAS input withholds the entire authoritative account, including
        # cash; a malformed custody price only withholds its position's valuation.
        expected_subtotal = "0.00" if field == "pas_price_date" else "7.00"
        assert result["fresh_subtotal_usd"] == expected_subtotal


@pytest.mark.parametrize("timestamp", [
    "1790780400", 1790780400, "1790780400000", "1790780400.1234567", "1.7907804e9",
    1790780400.5,
])
def test_numeric_source_asserted_time_preserves_received_financial_evidence(tmp_path, timestamp):
    export = fx_export(tmp_path)
    export.feed("FX", "fx", [{**fix("GBP", "1.25"), "source_asserted_at": timestamp}])
    _database, result, _state = check(tmp_path)
    assert result["status"] == "COMPLETE" and result["assets_usd"] == "38.25"


@pytest.mark.parametrize("timestamp,expected", [
    ("-0.0000005", "1970-01-01T00:00:00Z"),
    ("0.0000005", "1970-01-01T00:00:00.000001Z"),
    ("20000000000", "2603-10-11T11:33:20Z"),
    ("20000000000.1", "1970-08-20T11:33:20.000100Z"),
    ("-20000000000.1", "1969-05-14T12:26:39.999900Z"),
    ("1790780400000.123456", "2026-09-30T15:00:00.000124Z"),
])
def test_numeric_timestamp_calendar_and_precision_boundaries(timestamp, expected):
    from reference_oracle.wire import timestamp_assertion

    assert timestamp_assertion(timestamp) == expected


@pytest.mark.parametrize("timestamp", [
    "1790780400", 1790780400, "1790780400000", 1790780400000, "+01790780400",
    "1.7907804e9", "1790780400.1234567", "1790780400000.123456",
    "1790780400.9999999", "2026-09-30T14:00:00−01:00", 1790780400.5,
])
def test_single_received_timestamp_representation_values_the_foreign_position(tmp_path, timestamp):
    export = fx_export(tmp_path)
    export.feed("FX", "fx", [{**fix("GBP", "1.25"), "rate_at": timestamp}])
    _database, result, _state = check(tmp_path)
    assert result["status"] == "COMPLETE" and result["assets_usd"] == "38.25"


@pytest.mark.parametrize("entry", [None, [], 7, {}])
def test_malformed_export_event_reports_evidence_error(tmp_path, entry):
    (tmp_path / "index.json").write_text(json.dumps({"version": 1, "events": [entry]}))
    with pytest.raises(EvidenceError, match="invalid export event entry"):
        load_received_history(tmp_path)


@pytest.mark.parametrize("spelling", [
    "2026-09-30T00:00:00Z", "2026-09-30T00:00:00", "2026-09-30T00:00:00+01:00",
    1790726400, "1790726400", "1790726400000",
])
def test_received_date_spellings_keep_fx_interval_and_price_evidence(tmp_path, spelling):
    export = Export(tmp_path)
    export.crm()
    export.feed("SECMASTER", "securities", [security_row(currency="GBP", country="GB")])
    export.snapshots([{**position("1"), "pas_price": {
        "price": "25", "price_date": spelling, "currency": "GBP",
    }}, cash("7")])
    export.feed("CUSTODY-A", "prices", [{**quote("25", currency="GBP"),
                                        "price_date": spelling, "valid_from": spelling}])
    export.feed("FX", "fx", [{**fix("GBP", "1.25"), "valid_from": spelling}])
    _database, result, _state = check(tmp_path)
    assert result["status"] == "COMPLETE" and result["assets_usd"] == "38.25"
    assert result["positions"][0]["price_date"] == "2026-09-30"
    assert not any(flag["code"] == "PRICE_SOURCE_MISMATCH" for flag in result["quality_flags"])


@pytest.mark.parametrize("amount", ["01", "00.0", "-0", "-0.00", "١", "1.١", 1.5, "1e1"])
def test_noncanonical_financial_strings_quarantine_the_authoritative_account(tmp_path, amount):
    export = Export(tmp_path)
    export.crm()
    export.snapshots([cash(amount)])
    _database, result, _state = check(tmp_path)
    assert result["status"] == "INCOMPLETE" and result["fresh_subtotal_usd"] == "0.00"
    assert result["missing_account_ids"] == [A1["account_id"]]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("other,complete", [("25", True), ("30", False)])
def test_price_assertion_dates_normalize_before_equal_receipt_grouping(
    tmp_path, reverse, other, complete,
):
    export = Export(tmp_path)
    export.crm()
    export.feed("SECMASTER", "securities", [security_row()])
    export.snapshots([position("1"), cash("7")])
    rows = [quote("25"), {**quote(other), "price_date": 1790726400}]
    for item in reversed(rows) if reverse else rows:
        export.feed("CUSTODY-A", "prices", [item])
    _database, result, _state = check(tmp_path)
    assert result["status"] == ("COMPLETE" if complete else "INCOMPLETE")
    assert result.get("assets_usd") == ("32.00" if complete else None)


@pytest.mark.parametrize("case", ["missing_account", "missing_person", "account", "reporting"])
def test_missing_account_diagnostics_use_relationship_kind_and_actual_blocker(tmp_path, case):
    export = Export(tmp_path)
    if case == "missing_account":
        account_id = "WC-ACCT-0001"
        export.crm(accounts=(), extra=[row("reporting", "assignment", account_id=account_id,
                                          household_id=HH)])
        expected_accounts, expected_code = [account_id], "SOURCE_UNAVAILABLE"
    elif case == "missing_person":
        export.crm(extra=[row("membership", "membership", person_id="WC-AC-PER-0001",
                              household_id=HH)])
        export.snapshots([cash("7")])
        expected_accounts, expected_code = [], "SOURCE_UNAVAILABLE"
    elif case == "account":
        export.crm()
        export.crm(accounts=({**A1, "managed": False},))
        expected_accounts, expected_code = [A1["account_id"]], "AMBIGUOUS_ENTITY"
    else:
        export.crm(extra=[row("reporting", "other-assignment", account_id=A1["account_id"],
                              household_id="WC-HH-0002")])
        expected_accounts, expected_code = [A1["account_id"]], "AMBIGUOUS_ENTITY"
    _database, result, _state = check(tmp_path)
    assert result["status"] == "INCOMPLETE"
    assert result["missing_account_ids"] == expected_accounts
    assert {flag["code"] for flag in result["blocking_flags"]} == {expected_code}


@pytest.mark.parametrize("country", [
    "USA", "U", "us", "gb", "G1", "GB ", " GB", "GB\n", "ＧＢ", "",
    None, [], {}, "US", "GB", "ZZ",
])
def test_received_listing_country_admission_controls_price_freshness(tmp_path, country):
    export = Export(tmp_path)
    export.crm()
    # One definition only: a valid companion must not mask rejected evidence.
    export.feed("SECMASTER", "securities", [security_row(country=country)])
    export.snapshots([position("1"), cash("7")])
    export.feed("CUSTODY-A", "prices", [
        {**quote("25"), "price_date": (DAY - timedelta(days=1)).isoformat()},
    ])
    _database, actual, _state = check(tmp_path)
    if country in ("GB", "ZZ"):
        assert actual["status"] == "COMPLETE" and actual["assets_usd"] == "32.00"
    else:
        assert actual["status"] == "INCOMPLETE" and actual["fresh_subtotal_usd"] == "7.00"


def ownership_export(directory):
    export = Export(directory)
    export.crm(extra=[
        row("person", "owner-person", id="WC-PER-1", external_person_key="synthetic-owner",
            name="Synthetic owner"),
        row("membership", "owner-membership", person_id="WC-PER-1", household_id=HH),
    ])
    export.feed("SECMASTER", "securities", [security_row()])
    export.snapshots([position("1"), cash("7")])
    export.feed("CUSTODY-A", "prices", [quote("25")])
    ownership = row(
        "ownership", "ownership-1", custodian="A", account_number=A1["account_number"],
        status="CLOSED", title="Synthetic closed account", registration="INDIVIDUAL",
        owners=[{"entity_id": "WC-PER-1", "entity_type": "person", "role": "owner"}],
    )
    return export, ownership


@pytest.mark.parametrize("status", [
    "INVALID", "closed", " CLOSED ", "", None, [], {}, "OPEN", "CLOSED",
])
def test_received_ownership_status_correction_preserves_closed_balance_warning(tmp_path, status):
    export, ownership = ownership_export(tmp_path)
    export.feed("CUSTODY-A", "custody_accounts", [ownership])
    export.feed("CUSTODY-A", "custody_accounts", [{**ownership, "status": status}],
                at=CORRECTION.isoformat())
    for known in (BEFORE, CORRECTION):
        database, actual, state = check(tmp_path, known=known)
        expected_status = "OPEN" if status == "OPEN" and known == CORRECTION else "CLOSED"
        assert actual["status"] == "COMPLETE" and actual["assets_usd"] == "32.00"
        assert {item["code"] for item in actual["quality_flags"]} == (
            {"CLOSED_ACCOUNT_WITH_BALANCE"} if expected_status == "CLOSED" else set()
        )
        assert state.view("ownership", DAY).values[A1["account_id"]].row["status"] == (
            expected_status
        )
        with read_snapshot(database) as connection:
            assert fact_view(connection, "ownership", DAY, known).values[
                A1["account_id"]
            ]["status"] == expected_status
            rejects = connection.execute(
                "SELECT reason FROM quarantine WHERE json_extract(raw_payload, '$.kind')="
                "'ownership'",
            ).fetchall()
            assert [item["reason"] for item in rejects] == (
                [] if status in ("OPEN", "CLOSED") else ["INVALID_ROW"]
            )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("status", ["INVALID", "closed", " CLOSED "])
def test_tied_invalid_ownership_status_does_not_displace_valid_closed_evidence(
    tmp_path, reverse, status,
):
    export, ownership = ownership_export(tmp_path)
    assertions = [ownership, {**ownership, "status": status}]
    for assertion in reversed(assertions) if reverse else assertions:
        export.feed("CUSTODY-A", "custody_accounts", [assertion])
    database, actual, state = check(tmp_path)
    assert actual["assets_usd"] == "32.00"
    assert {item["code"] for item in actual["quality_flags"]} == {
        "CLOSED_ACCOUNT_WITH_BALANCE",
    }
    assert not state.view("ownership", DAY).ambiguous
    with read_snapshot(database) as connection:
        assert not fact_view(connection, "ownership", DAY, KNOWN).ambiguous
