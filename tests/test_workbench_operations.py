"""Build plan step 6 exit tests: the operations screens O1 and O2.

O1 Data health draws every expected feed cell of the range from `feed_health`, with a
glyph and a word per cell and a table view, and one tile from the quality report. O2 Feed
run shows `feed_run_impact`'s accounts and households, with each household's completeness
from the quality report at the run's business date. Deterministic: the stub policy of
tests/test_workbench_advisor.py; the real policy runs the same pages in
tests/test_p4_security_matrix.py.

The exit criteria: no person name, household name or account title in pages, JSON or
attributes (S11 titles included); an advisor cannot reach feed runs; a failed run never
shows zero affected accounts.
"""
# ruff: noqa: F811  (pytest fixtures imported from the workbench modules are parameters)

import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_workbench import NOW, received  # noqa: F401
from tests.test_workbench_advisor import (
    MATRIX,
    advisor_ui,  # noqa: F401
    apply,
    assert_every_flag_with_its_subject,
    envelopes,
    figures,
    preset,
    visible,
)
from tests.test_workbench_compliance import main, nav_links, neutral
from wealthcontext.storage import read_snapshot
from wealthcontext.workbench import DENIAL, PRESETS
from wealthcontext.workbench_operations import FEED_STATES, health_grid, household_completeness
from wealthcontext.workbench_sessions import MENU

PAGES = ("/workbench/data-health", "/workbench/feed-run")
ROLES = {"ADV-01": "advisor", "OPS-01": "ops", "COMP-01": "compliance", "ENG-01": "engineer"}
CELL = "source_system=PAS-B&feed=pas_snapshots&business_date=2026-10-15"
UNKNOWN = "RUN-" + "0" * 64


def run_id(database: Path, system: str = "PAS-B", feed: str = "pas_snapshots",
           day: str = "2026-10-15") -> str:
    with read_snapshot(database) as connection:
        (row,) = connection.execute(
            "SELECT id FROM feed_runs WHERE source_system=? AND feed=? AND business_date=?",
            (system, feed, day)).fetchall()
    return row[0]


def private_strings(database: Path) -> set[str]:
    private = set()
    with read_snapshot(database) as connection:
        for row in connection.execute("SELECT payload FROM facts WHERE kind IN "
                                      "('household','person','ownership','crm_ownership',"
                                      "'advisor','team')"):
            payload = json.loads(row["payload"])
            private.update(payload[key] for key in ("name", "title")
                           if isinstance(payload.get(key), str) and payload[key])
    assert len(private) > 20 and any("ignore" in value.lower() for value in private), (
        "S11 titles are in the scan")
    return private


def found(pattern: str, text: str, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    assert match is not None, pattern
    return match.group(match.lastindex or 0) if match.lastindex else match.group(0)


def governed(page, tool_key: str) -> dict:
    """The page's disclosed envelope whose data carries this key."""
    return next(envelope for envelope in envelopes(page)
                if isinstance(envelope.get("data"), dict) and tool_key in envelope["data"])


def tiles(page) -> list[str]:
    return re.findall(r'<div class="kpi"><p class="label">([^<]+)</p>', page.text)


# --- Matrix rows and menus ------------------------------------------------------------------


def test_the_operations_pages_are_in_the_operations_menu_alone():
    rows = {row["path"]: row for row in MATRIX["routes"]}
    for path, label in zip(PAGES, ("Data health", "Feed run"), strict=True):
        row = rows[path]
        assert (row["method"], row["action"], row["authentication"], row["business"]) == (
            "GET", "feed_runs", "required", True)
        assert row["menu"] == {"label": label, "group": "Operations", "roles": ["ops"]}
    for role in ("advisor", "compliance", "engineer"):
        assert not {item.path for item in MENU if role in item.roles} & set(PAGES), role
    ops = [item.path for item in MENU if "ops" in item.roles]
    assert ops[:2] == list(PAGES), "O1 is operations' home"
    assert "advisor" not in MATRIX["actions"]["feed_runs"]["roles"]


@pytest.mark.anyio
async def test_menus_and_homes(advisor_ui):
    _, client, _, _ = advisor_ui
    for persona, role in ROLES.items():
        applied = await apply(client, persona=persona)
        page = await client.get(applied.headers["location"])
        listed = nav_links(page)
        assert (set(PAGES) <= set(listed)) is (role == "ops"), role
        if role != "ops":
            assert not set(PAGES) & set(listed), role
    applied = await apply(client, persona="OPS-01")
    assert applied.headers["location"] == "/workbench/data-health"
    # Owner decision, 2026-11-12 ("Retire Feed runs"): the inspector leaves operations'
    # menu, and its route still answers operations under policy.
    home = await client.get(applied.headers["location"])
    assert "/workbench/inspector/feeds" not in nav_links(home)
    inspector = await client.get("/workbench/inspector/feeds")
    assert inspector.status_code == 200 and "NOT_AUTHORIZED" not in inspector.text


# --- Matrix-driven allow and deny, and paired denials -----------------------------------


@pytest.mark.anyio
async def test_every_role_on_every_page_and_one_denial_for_existing_and_unknown(advisor_ui):
    _, client, state, database = advisor_ui
    run = run_id(database)
    cases = {"/workbench/data-health": [("", None)],
             "/workbench/feed-run": [(f"?run_id={run}", run), (f"?run_id={UNKNOWN}", UNKNOWN),
                                     ("?" + CELL, "PAS-B pas_snapshots 2026-10-15")]}
    allowed = set(MATRIX["actions"]["feed_runs"]["roles"])
    assert allowed == {"ops", "compliance"}
    for persona, role in ROLES.items():
        await apply(client, persona=persona, as_of="2026-10-15")
        for path, queries in cases.items():
            denials = []
            for query, requested in queries:
                state["calls"].clear()
                page = await client.get(path + query)
                if role in allowed:
                    assert page.status_code == 200, (persona, path, query, page.text[-800:])
                    assert DENIAL not in page.text
                    continue
                # An advisor (and an engineer) cannot reach feed runs.
                assert page.status_code == 403, (persona, path, query, page.text[-800:])
                assert DENIAL in page.text and "Access denied" in page.text
                assert not figures(page) and "RUN-" not in visible(page).replace(
                    requested or "", "")
                denials.append(neutral(page, requested or ""))
                # Refused before any lookup: only feed_runs on the empty scope was asked.
                assert [(call[1], call[2]) for call in state["calls"]] == [("feed_runs", [])]
            assert all(item == denials[0] for item in denials), (persona, path)
    # An allowed caller asking for an unknown run gets the platform's NOT_FOUND, not a denial.
    await apply(client, persona="OPS-01")
    unknown = await client.get(f"/workbench/feed-run?run_id={UNKNOWN}")
    assert unknown.status_code == 200 and "NOT_FOUND" in unknown.text


@pytest.mark.anyio
async def test_the_advisor_cannot_reach_feed_runs_on_any_route(advisor_ui):
    _, client, _, database = advisor_ui
    await apply(client, persona="ADV-01", as_of="2026-10-15")
    for path in ("/workbench/data-health", f"/workbench/feed-run?run_id={run_id(database)}",
                 "/workbench/feed-run?" + CELL):
        page = await client.get(path)
        assert page.status_code == 403 and DENIAL in page.text, path
    inspector = await client.get("/workbench/inspector/feeds")
    assert "NOT_AUTHORIZED" in inspector.text and "RUN-" not in visible(inspector)
    assert "/workbench/inspector/feeds" not in nav_links(inspector)


# --- O1 ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_o1_default_range_draws_every_expected_cell_with_a_glyph_and_a_word(advisor_ui):
    _, client, state, database = advisor_ui
    await apply(client, persona="OPS-01", as_of="2026-10-15")
    state["calls"].clear()
    page = await client.get("/workbench/data-health")
    assert page.status_code == 200, page.text[-800:]
    health = governed(page, "business_dates")
    data = health["data"]
    # The default range: the data window's first business day to the valuation date.
    assert (data["date_from"], data["date_to"], data["business_day_count"]) == (
        "2026-09-14", "2026-10-15", 24)
    assert data["total_item_count"] == data["item_count"] == 360
    body = main(page)
    grid = found(r'<table class="data health-grid">.*?</table>', body, re.S)
    cells = re.findall(r'<td class="cell">(.*?)</td>', grid, re.S)
    assert len(cells) == 360, "every expected cell is drawn"
    for cell in cells:
        assert '<svg class="icon"' in cell and 'class="cell-word"' in cell, "glyph and word"
    words = [found(r'class="cell-word">([^<]+)<', cell) for cell in cells]
    assert sorted(set(words)) == ["Failed", "Partial", "Success"]
    # Only the non-success cells link to O2 from the grid; the table view links every run.
    links = re.findall(r'href="(/workbench/feed-run\?run_id=[^"]+)"', grid)
    assert [parse_qs(urlsplit(html.unescape(link)).query)["run_id"][0] for link in links] == [
        run_id(database, "CUSTODY-B", "custody_snapshots", "2026-10-09"), run_id(database)]
    # The only tile: the quality report's incomplete_household_count.
    quality = governed(page, "incomplete_household_count")
    assert tiles(page) == ["Households incomplete"]
    assert figures(page) == [str(quality["data"]["incomplete_household_count"])]
    assert quality["valuation_date"] == "2026-10-15"
    assert "accounts not received" not in visible(page).lower()
    # The legend shows the platform's per-state counts, each with its glyph and word.
    legend = found(r'<ul class="state-legend".*?</ul>', body, re.S)
    for item in data["state_counts"]:
        assert f'{FEED_STATES[item["state"]]["label"]}</span><b>{item["cell_count"]}</b>' in legend
    assert "Showing <b>360</b> of <b>360</b> expected cells" in body
    # Guardrail 3 (SOL-UX2-R1-WARN-001): every flag on both results, with its subject.
    assert quality["quality_flags"], "the quality report warns on this day"
    assert_every_flag_with_its_subject(page)
    assert [call[1] for call in state["calls"]] == [
        "feed_runs", "feed_runs", "quality_preflight", "view_quality"]
    assert "export" not in visible(page).lower()


@pytest.mark.anyio
async def test_o1_marks_never_received_cells_and_has_a_table_view(advisor_ui):
    _, client, _, _ = advisor_ui
    await apply(client, persona="OPS-01", as_of="2026-10-16", known_as_of="2026-10-14T23:00:00Z")
    page = await client.get("/workbench/data-health?date_from=2026-10-12")
    data = governed(page, "business_dates")["data"]
    assert data["business_day_count"] == 5
    grid = found(r'<table class="data health-grid">.*?</table>', page.text, re.S)
    words = re.findall(r'class="cell-word">([^<]+)<', grid)
    assert words.count("Not received") == 30 and len(words) == 75
    table = await client.get("/workbench/data-health?date_from=2026-10-12&view=table")
    rows = re.findall(r"<tr><td class=\"when\">", table.text)
    assert len(rows) == 75 and 'class="data health-grid"' not in table.text
    assert table.text.count("No run recorded by the cutoff") == 30
    assert table.text.count('href="/workbench/feed-run?run_id=') == 45
    assert 'aria-current="true">Table</a>' in table.text
    assert_every_flag_with_its_subject(page)
    assert_every_flag_with_its_subject(table)


@pytest.mark.anyio
async def test_o1_errors_are_the_platforms(advisor_ui):
    _, client, _, _ = advisor_ui
    await apply(client, persona="OPS-01")
    out = await client.get("/workbench/data-health?date_from=2026-09-01")
    assert out.status_code == 200 and "AS_OF_OUT_OF_RANGE" in out.text and not figures(out)
    reversed_range = await client.get(
        "/workbench/data-health?date_from=2026-10-16&date_to=2026-10-15")
    assert "INVALID_ARGUMENT" in reversed_range.text
    bad = await client.get("/workbench/data-health?view=chart")
    assert bad.status_code == 400


def test_the_grid_places_returned_cells_and_invents_none():
    data = {"business_dates": ["2026-10-14", "2026-10-15"],
            "pairs": [{"source_system": "FX", "feed": "fx"}],
            "items": [{"business_date": "2026-10-15", "source_system": "FX", "feed": "fx",
                       "state": "FAILED"}]}
    (row,) = health_grid(data)
    assert row["cells"][0] is None and row["cells"][1]["state"] == "FAILED"
    quality = {"error": None, "data": {"truncated": True, "items": [
        {"household_id": "WC-HH-0001", "complete": False}]}}
    assert [row["state"] for row in household_completeness(
        quality, ["WC-HH-0001", "WC-HH-0002"])] == ["INCOMPLETE", "NOT_RETURNED_CAPPED"]
    assert [row["state"] for row in household_completeness(None, ["WC-HH-0001"])] == [
        "NOT_RETURNED"]


# --- O2 ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_preset_names_a_cell_and_the_run_comes_from_feed_health(advisor_ui):
    _, client, state, database = advisor_ui
    (failed,) = [item for item in PRESETS if item["id"] == "failed_feed"]
    assert "run_id" not in failed["destination"] and "RUN-" not in failed["destination"]
    applied = await preset(client, "failed_feed")
    assert applied.headers["location"] == "/workbench/feed-run?" + CELL
    state["calls"].clear()
    page = await client.get(applied.headers["location"])
    assert page.status_code == 200, page.text[-800:]
    health = governed(page, "business_dates")
    (cell,) = [item for item in health["data"]["items"]
               if (item["source_system"], item["feed"]) == ("PAS-B", "pas_snapshots")]
    impact = governed(page, "account_attribution")
    assert impact["data"]["run_id"] == cell["run_id"] == run_id(database)
    # The cell's feed_health is authorized before the run is looked up.
    assert [call[1] for call in state["calls"]] == [
        "feed_runs", "feed_runs", "feed_runs", "feed_runs", "quality_preflight", "view_quality"]


@pytest.mark.anyio
async def test_o2_shows_affected_accounts_households_and_their_completeness(advisor_ui):
    _, client, _, _ = advisor_ui
    await preset(client, "failed_feed")
    page = await client.get("/workbench/feed-run?" + CELL)
    impact = governed(page, "account_attribution")["data"]
    quality = governed(page, "incomplete_household_count")
    assert quality["valuation_date"] == impact["business_date"] == "2026-10-15"
    body = main(page)
    # A failed run never shows zero affected accounts.
    assert tiles(page) == ["Accounts expected", "Accounts committed", "Accounts affected",
                           "Households affected"]
    assert figures(page) == ["15", "0", "15", "14"]
    assert re.search(r'Accounts affected</p><p class="kpi-value"><span class="figure">15<', body)
    accounts = found(r'aria-labelledby="accounts-title">.*?</table>', body, re.S)
    rows = re.findall(r"<tr><td class=\"mono\">(WC-AC-[0-9]+)</td>", accounts)
    assert rows == [item["account_id"] for item in impact["affected_accounts"]["items"]]
    body_rows = accounts.split("<tbody>", 1)[1]
    assert body_rows.count(">ACCOUNT_MANIFEST_MISMATCH<") == 15, "the stored reason codes"
    assert "Showing <b>15</b> of <b>15</b> affected accounts" in body
    households = found(r'aria-labelledby="households-title">.*?</table>', body, re.S)
    returned = {item["household_id"]: item["complete"] for item in quality["data"]["items"]}
    shown = re.findall(r'<tr><td class="mono">(WC-HH-[0-9]+)</td>\s*<td><span class="badge '
                       r'b-[a-z]+">.*?</svg>(Complete|Incomplete)</span>', households, re.S)
    assert [household for household, _ in shown] == [
        item["household_id"] for item in impact["affected_households"]["items"]]
    assert all((word == "Complete") is returned[household] for household, word in shown)
    assert "Showing <b>14</b> of <b>14</b> affected households" in body
    # Guardrail 3 (SOL-UX2-R1-WARN-001): S07's SOURCE_UNAVAILABLE warnings are listed with
    # their subjects, apart from the run's own reason codes.
    assert any(flag["code"] == "SOURCE_UNAVAILABLE" for flag in quality["quality_flags"])
    assert_every_flag_with_its_subject(page)
    warnings = found(r'Quality report warnings</h3>.*?</ul>', body, re.S)
    assert "SOURCE_UNAVAILABLE" in warnings and "ACCOUNT_MANIFEST_MISMATCH" not in warnings
    assert "export" not in visible(page).lower()


@pytest.mark.anyio
async def test_a_feed_without_per_account_availability_shows_no_zero(advisor_ui):
    _, client, _, database = advisor_ui
    await apply(client, persona="OPS-01", as_of="2026-10-15")
    for system, feed in (("CRM", "crm"), ("FX", "fx")):
        page = await client.get("/workbench/feed-run?run_id=" + run_id(database, system, feed))
        data = governed(page, "account_attribution")["data"]
        assert data["account_attribution"] == "NOT_ATTRIBUTABLE"
        assert not tiles(page) and not figures(page), "null counts are not drawn as zero"
        assert "No accounts are attributed to this run" in page.text
        assert "they are not zero" in page.text
        assert_every_flag_with_its_subject(page)


@pytest.mark.anyio
async def test_o2_a_never_received_cell_and_no_run_read_nothing(advisor_ui):
    _, client, state, _ = advisor_ui
    await apply(client, persona="OPS-01", known_as_of="2026-10-14T23:00:00Z", as_of="2026-10-15")
    page = await client.get("/workbench/feed-run?" + CELL)
    assert "No run of this feed for this business date" in page.text
    assert "Not received" in page.text and not figures(page)
    state["calls"].clear()
    empty = await client.get("/workbench/feed-run")
    assert empty.status_code == 200 and 'name="run_id"' in empty.text
    assert state["calls"] == [], "without a run or a cell the page reads nothing"
    mixed = await client.get(f"/workbench/feed-run?run_id={UNKNOWN}&feed=fx")
    assert mixed.status_code == 400


# --- No-PII projection: pages, JSON, attributes, drawers ------------------------------------


@pytest.mark.anyio
async def test_no_name_or_title_in_operations_pages_json_attributes_or_drawers(advisor_ui):
    _, client, _, database = advisor_ui
    private = private_strings(database)
    await preset(client, "failed_feed")
    pages = [await client.get("/workbench/data-health"),
             await client.get("/workbench/data-health?view=table"),
             await client.get("/workbench/feed-run?" + CELL),
             await client.get("/workbench/feed-run?run_id=" + run_id(database, "CRM", "crm"))]
    evidence = re.findall(r'href="(/workbench/evidence\?[^"]+)"', pages[2].text)
    assert evidence, "the run's source records open in the drawer"
    for url in evidence:
        for headers in ({}, {"X-Workbench-Fragment": "drawer"}):
            panel = await client.get(html.unescape(url), headers=headers)
            assert panel.status_code == 200, panel.text[-800:]
            pages.append(panel)
    assert any("[REDACTED]" in page.text or "manifest" in page.text for page in pages[4:])
    for page in pages:
        text = html.unescape(page.text)
        for value in private:
            assert value not in text and value not in page.text, (value, str(page.url))
    for envelope in [item for page in pages for item in envelopes(page)]:
        assert envelope["error"] is None
