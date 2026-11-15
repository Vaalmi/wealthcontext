"""Build plan step 5 exit tests: the compliance screens C1, C2 and C3.

C1 Access decisions pages `decision_log_page` with its cursor; C2 Point in time shows two
knowledge cutoffs side by side, the comparison cutoff a page parameter; C3 Trace shows the
authorized span tree, falling back to today's `trace` metadata. Deterministic: the stub
policy of tests/test_workbench_advisor.py; the real policy runs the same pages in
tests/test_p4_security_matrix.py.
"""
# ruff: noqa: F811  (pytest fixtures imported from the workbench modules are parameters)

import html
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_workbench import NOW, ORIGIN, csrf, received  # noqa: F401
from tests.test_workbench_advisor import (
    MATRIX,
    advisor_ui,  # noqa: F401
    apply,
    assert_every_flag_with_its_subject,
    envelopes,
    figures,
    leaves,
    preset,
    visible,
)
from wealthcontext.storage import read_snapshot
from wealthcontext.workbench import DENIAL, PRESETS
from wealthcontext.workbench_sessions import MENU

PAGES = ("/workbench/decisions", "/workbench/point-in-time", "/workbench/trace")
ACTIONS = {"/workbench/decisions": "view_decision_log",
           "/workbench/point-in-time": "household_receipts",
           "/workbench/trace": "trace_spans"}
ROLES = {"ADV-01": "advisor", "OPS-01": "ops", "COMP-01": "compliance", "ENG-01": "engineer"}
QUESTION = "What assets are included for the clean household at quarter-end?"


def main(page) -> str:
    found = re.search(r'<main id="main".*?</main>', page.text, re.S)
    assert found is not None, page.text[-1500:]
    return found.group(0)


def neutral(page, *requested: str) -> str:
    """The denial's body with the request's own trace and the identifier asked for masked."""
    text = re.sub(r"\b(r_[0-9a-f]{32}|[0-9a-f]{32})\b", "ID", main(page))
    for value in requested:
        if value:
            text = text.replace(html.escape(value), "REQUESTED")
    return text


def nav_links(page) -> list[str]:
    nav = re.search(r'<nav class="side".*?</nav>', page.text, re.S)
    return re.findall(r'href="([^"]+)"', nav.group(0)) if nav else []


async def answered(client) -> tuple[str, str]:
    """ADV-01's recorded answer (no key); returns the Ask page and the run's trace link."""
    await apply(client, persona="ADV-01")
    ask = await client.get("/workbench")
    sent = await client.post("/workbench/answer", headers=ORIGIN,
                             data={"csrf": csrf(ask), "question": QUESTION})
    assert sent.status_code == 303, sent.text
    page = await client.get("/workbench")
    links = re.findall(r'href="(/workbench/trace\?trace_id=[0-9a-f]{32})"', page.text)
    assert len(links) == 1, "one verified answer, one trace link"
    return page.text, html.unescape(links[0])


async def decide_often(client, count: int = 21) -> None:
    """More decisions than one page holds: each inspector read records one."""
    for _ in range(count):
        assert (await client.get("/workbench/inspector/eval")).status_code == 200


def next_link(page) -> str:
    found = re.search(r'href="(/workbench/decisions\?[^"]*cursor=[^"]+)"', page.text)
    assert found is not None, "a next page"
    return html.unescape(found.group(1))


def span_line(trace: str, span: str, name: str, start: datetime, parent: str | None = None,
              status: str = "UNSET") -> str:
    return json.dumps({
        "name": name, "context": {"trace_id": "0x" + trace, "span_id": "0x" + span},
        "parent_id": None if parent is None else "0x" + parent,
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time": (start + timedelta(milliseconds=5)).isoformat().replace("+00:00", "Z"),
        "status": {"status_code": status},
        "attributes": {"gen_ai.request.model": "PRIVATE-MODEL", "wealthcontext.tool": "x"},
        "resource": {"attributes": {"service.name": "PRIVATE-SERVICE"}},
    }) + "\n"


# --- Matrix rows and menus ------------------------------------------------------------------


def test_the_compliance_pages_are_in_the_compliance_menu_alone():
    rows = {row["path"]: row for row in MATRIX["routes"]}
    for path, label in zip(PAGES, ("Access decisions", "Point in time", "Trace"), strict=True):
        row = rows[path]
        assert (row["method"], row["action"], row["authentication"], row["business"]) == (
            "GET", ACTIONS[path], "required", True)
        assert row["menu"] == {"label": label, "group": "Compliance", "roles": ["compliance"]}
    for role in ("advisor", "ops", "engineer"):
        assert not {item.path for item in MENU if role in item.roles} & set(PAGES), role
    compliance = [item.path for item in MENU if "compliance" in item.roles]
    assert compliance[:3] == list(PAGES), "C1 is compliance's home"


@pytest.mark.anyio
async def test_menus_and_homes(advisor_ui):
    _, client, _, _ = advisor_ui
    for persona, role in ROLES.items():
        applied = await apply(client, persona=persona)
        page = await client.get(applied.headers["location"])
        listed = nav_links(page)
        assert (set(PAGES) <= set(listed)) is (role == "compliance"), role
        if role != "compliance":
            assert not set(PAGES) & set(listed), role
    applied = await apply(client, persona="COMP-01")
    assert applied.headers["location"] == "/workbench/decisions"


# --- Matrix-driven allow and deny, and paired denials -----------------------------------


@pytest.mark.anyio
async def test_every_role_on_every_page_and_one_denial_for_existing_and_unknown(advisor_ui):
    _, client, state, database = advisor_ui
    await apply(client, persona="COMP-01")
    await decide_often(client)
    first = await client.get("/workbench/decisions")
    cursor = parse_qs(urlsplit(next_link(first)).query)["cursor"][0]
    _, link = await answered(client)
    trace = parse_qs(urlsplit(link).query)["trace_id"][0]
    cases = {
        "/workbench/decisions": [("", ""), (f"?cursor={cursor}", cursor),
                                 ("?cursor=AAAA.BBBB", "AAAA.BBBB")],
        "/workbench/point-in-time": [("?household_id=WC-HH-0001", "WC-HH-0001"),
                                     ("?household_id=WC-HH-9999", "WC-HH-9999")],
        "/workbench/trace": [(f"?trace_id={trace}", trace), ("?trace_id=" + "0" * 32, "0" * 32)],
    }
    # Allowed (page shown) per role and case; every other case is the neutral denial.
    # Only the owner and compliance may read a trace, and an unknown trace has no owner.
    shown = {("compliance", path, requested) for path, queries in cases.items()
             for _, requested in queries if requested != "0" * 32}
    shown.add(("advisor", "/workbench/trace", trace))
    for persona, role in ROLES.items():
        await apply(client, persona=persona)
        for path, queries in cases.items():
            denials = []
            for query, requested in queries:
                state["calls"].clear()
                page = await client.get(path + query)
                if (role, path, requested) in shown:
                    assert role in MATRIX["actions"][ACTIONS[path]]["roles"]
                    assert page.status_code == 200, (persona, path, query, page.text[-800:])
                    continue
                assert page.status_code == 403, (persona, path, query, page.text[-800:])
                assert DENIAL in page.text and "Access denied" in page.text
                assert not figures(page) and "source_record_id" not in page.text
                denials.append(neutral(page, requested))
                if role not in MATRIX["actions"][ACTIONS[path]]["roles"]:
                    # Refused before any lookup: only the page's first decision was asked.
                    assert [call[1] for call in state["calls"]] == [{
                        "/workbench/decisions": "view_decision_log",
                        "/workbench/point-in-time": "household_receipts_preflight",
                        "/workbench/trace": "trace_spans_preflight"}[path]], state["calls"]
            assert all(item == denials[0] for item in denials), (persona, path)
    # An advisor who does not own a trace gets the unknown trace's page.
    await apply(client, persona="ADV-02")
    foreign = await client.get(link)
    unknown = await client.get("/workbench/trace?trace_id=" + "0" * 32)
    assert foreign.status_code == unknown.status_code == 403
    assert neutral(foreign, trace) == neutral(unknown, "0" * 32)
    with read_snapshot(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] > 0


# --- C1 ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_c1_counts_as_returned_and_the_cursor_holds_while_decisions_arrive(advisor_ui):
    _, client, _, _ = advisor_ui
    await apply(client, persona="COMP-01")
    await decide_often(client)
    first = await client.get("/workbench/decisions")
    assert first.status_code == 200
    (page,) = envelopes(first)
    data = page["data"]
    assert data["truncated"] and data["next_cursor"]
    assert (f"Showing <b>{data['item_count']}</b> of <b>{data['total_item_count']}</b> "
            "matching decisions") in first.text
    assert_every_flag_with_its_subject(first)
    assert "FACTS_AT_STORED_REVISION" in first.text
    for revision in data["revisions"]:
        assert f"Revision {revision['revision']} ·" in first.text
        assert (f"Showing <b>{revision['item_count']}</b> of "
                f"<b>{revision['total_item_count']}</b> facts at this revision") in first.text
    assert "export" not in visible(first).lower(), "no export control (ADR 0009 point 5)"
    following = next_link(first)
    # New decisions arrive between the pages.
    for _ in range(4):
        await client.get("/workbench/inspector/eval")
    second = await client.get(following)
    (next_page,) = envelopes(second)
    assert next_page["error"] is None, next_page
    assert next_page["data"]["total_item_count"] == data["total_item_count"]
    assert next_page["data"]["browse_started_at"] == data["browse_started_at"]
    seen = {item["id"] for item in data["items"]}
    assert not seen & {item["id"] for item in next_page["data"]["items"]}
    assert "First page, new browse" in second.text
    # A forged cursor gets the platform's INVALID_ARGUMENT and a way to start again.
    forged = await client.get("/workbench/decisions?cursor=AAAA.BBBB")
    assert forged.status_code == 200 and "INVALID_ARGUMENT" in forged.text
    assert "Start the browse again" in forged.text


@pytest.mark.anyio
async def test_c1_filters_pass_to_the_platform(advisor_ui):
    _, client, _, _ = advisor_ui
    await apply(client, persona="ADV-03", household_id="WC-HH-0004")
    await client.get("/workbench/household")  # a denial for WC-HH-0004
    await apply(client, persona="COMP-01")
    page = await client.get("/workbench/decisions?principal_id=ADV-03&outcome=deny"
                            "&household_id=WC-HH-0004&action=&recorded_from="
                            "2026-11-01T00:00&recorded_before=")
    assert page.status_code == 200, page.text[-1000:]
    (result,) = envelopes(page)
    items = result["data"]["items"]
    assert items and all(item["principal_id"] == "ADV-03" and not item["allowed"]
                         and "WC-HH-0004" in item["resources"] for item in items)
    assert 'value="2026-11-01T00:00:00"' in page.text and 'value="deny" selected' in page.text
    invalid = await client.get("/workbench/decisions?outcome=maybe")
    assert invalid.status_code == 400


# --- C2 ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_c2_preset_shows_two_cutoffs_compared_by_run_id_only(advisor_ui):
    _, client, state, _ = advisor_ui
    (late,) = [item for item in PRESETS if item["id"] == "late_trade"]
    assert late["context"].persona == "COMP-01"
    assert late["context"].household_id == "WC-HH-0009"
    applied = await preset(client, "late_trade")
    assert applied.headers["location"] == (
        "/workbench/point-in-time?compare_known_as_of=2026-10-06T13%3A00%3A00Z")
    state["calls"].clear()
    page = await client.get(applied.headers["location"])
    assert page.status_code == 200, page.text[-1500:]
    assert [call[1] for call in state["calls"] if not call[1].endswith("_preflight")] == [
        "household_receipts", "snapshot", "household_receipts", "snapshot"]
    shown = envelopes(page)
    assert [item["error"] for item in shown] == [None] * 4
    receipts = [item for item in shown if "cited_record_count" in (item["data"] or {})]
    assert [item["known_as_of"] for item in receipts] == ["2026-10-31T16:00:00Z",
                                                          "2026-10-06T13:00:00Z"]
    assert [item["data"]["cited_record_count"] for item in receipts] == [29, 27]
    later, earlier = ({item["run_id"] for item in result["data"]["items"]}
                      for result in receipts)
    columns = re.findall(r'<section class="card pit-column".*?</section>', page.text, re.S)
    assert len(columns) == 2
    for column, mine, other in ((columns[0], later, earlier), (columns[1], earlier, later)):
        marked = set(re.findall(r'<span class="mono small muted cell-note run-id">'
                                r'(RUN-[0-9a-f]+)</span><span class="cell-note">'
                                r'<span class="badge b-info">', column))
        assert marked == mine - other and marked, "marked by run ID, nothing else"
    # Every figure is a field a governed result returned; nothing is differenced.
    returned = set().union(*(leaves(item) for item in shown))
    assert figures(page) and set(figures(page)) <= {f"USD {value}" for value in returned} | (
        returned)
    words = visible(page).lower()
    assert "difference" not in words and "changed by" not in words and "delta" not in words
    assert "export" not in words
    # The comparison cutoff is a page parameter: the session keeps its own cutoff.
    assert "31 Oct 2026, 16:00 UTC" in page.text
    assert 'name="known_as_of" value="2026-10-31T16:00:00Z"' in page.text or (
        "2026-10-31T16:00" in page.text)
    again = await client.get("/workbench/point-in-time")
    assert len(envelopes(again)) == 2, "without the parameter, one cutoff"
    assert "Choose a comparison cutoff" in again.text


@pytest.mark.anyio
async def test_c2_flags_every_warning_and_an_incomplete_snapshot_shows_no_total(advisor_ui):
    _, client, _, _ = advisor_ui
    await apply(client, persona="COMP-01", household_id="WC-HH-0003", as_of="2026-10-15",
                mode="live")
    page = await client.get("/workbench/point-in-time?compare_known_as_of=2026-10-20T12:00:00Z")
    assert page.status_code == 200
    shown = envelopes(page)
    listed = re.findall(r'<ul class="flag-list".*?</ul>', page.text, re.S)
    for item in shown:
        for flag in item["quality_flags"]:
            assert any(flag["code"] in block and html.escape(flag["subject"]) in block
                       for block in listed), flag
    snapshots = [item for item in shown if item["data"] and "fresh_subtotal_usd" in item["data"]]
    assert snapshots and all(item["status"] == "INCOMPLETE" for item in snapshots)
    assert "Fresh subtotal (not a total)" in page.text
    text = visible(page).lower().replace("not a total", "").replace("no total is shown", "")
    assert not re.search(r"\btotal\b", text)


@pytest.mark.anyio
async def test_c2_labels_each_column_with_the_returned_valuation_date(advisor_ui):
    """SOL-UX2-R1-DATE-001: a weekend request is valued on the business day the platform
    resolves; the page names that date, never the requested one, as the valuation date."""
    _, client, _, _ = advisor_ui
    await apply(client, persona="COMP-01", household_id="WC-HH-0009", as_of="2026-10-03")
    page = await client.get("/workbench/point-in-time?compare_known_as_of=2026-10-06T13:00:00Z")
    assert page.status_code == 200, page.text[-1500:]
    snapshots = [item for item in envelopes(page) if "cited_record_count" not in
                 (item["data"] or {})]
    assert [item["valuation_date"] for item in snapshots] == ["2026-10-02"] * 2
    match = re.search(r'<p class="sub"><span class="mono">WC-HH-0009</span>.*?</p>', page.text,
                      re.S)
    assert match is not None
    head = match.group(0)
    assert "requested for 3 Oct 2026" in head and "valuation date" not in head
    columns = re.findall(r'<section class="card pit-column".*?</section>', page.text, re.S)
    assert len(columns) == 2
    for column in columns:
        assert "valued on 2 Oct 2026" in column and "valued on 3 Oct 2026" not in column
    assert "valuation date 3 Oct 2026" not in page.text
    assert_every_flag_with_its_subject(page)


# --- C3 ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_c3_from_the_advisors_answer_with_no_attributes(advisor_ui, tmp_path):
    app, client, _, _ = advisor_ui
    _, link = await answered(client)
    trace = parse_qs(urlsplit(link).query)["trace_id"][0]
    spans = app.state.service.span_export
    start = NOW - timedelta(minutes=5)
    with spans.open("a") as stream:
        stream.write(span_line(trace, "a" * 16, "agent", start))
        stream.write(span_line(trace, "b" * 16, "tool", start + timedelta(seconds=1), "a" * 16))
        stream.write(span_line(trace, "c" * 16, "invented_name", start + timedelta(seconds=2),
                               "b" * 16, "ERROR"))
    page = await client.get(link)
    assert page.status_code == 200, page.text[-1500:]
    spans_result, metadata = envelopes(page)
    assert spans_result["data"]["span_source"] == "EXPORT"
    names = [item["name"] for item in spans_result["data"]["items"]]
    assert names == ["agent", "tool", "other"]
    tree = re.search(r'<ul class="span-tree".*?</ul>\s*</li>\s*</ul>', page.text, re.S)
    assert tree is not None and tree.group(0).count("<ul>") == 2, "nested by parent"
    for item in spans_result["data"]["items"]:
        assert f"{item['duration_ms']}</span> ms" in page.text, "the platform's duration"
    for private in ("PRIVATE-MODEL", "PRIVATE-SERVICE", "invented_name", "wealthcontext.tool",
                    "gen_ai"):
        assert private not in page.text
    # The action names come from today's trace metadata.
    for decision in metadata["data"]["decisions"]["items"]:
        assert f'<td class="mono">{decision["action"]}</td>' in page.text
    assert "Ask</a> / Trace" in page.text
    assert_every_flag_with_its_subject(page)
    # The menu never lists it for an advisor.
    assert "/workbench/trace" not in nav_links(page)


@pytest.mark.anyio
async def test_c3_refused_to_engineers_operations_others_and_revoked_advisors(advisor_ui):
    _, client, state, _ = advisor_ui
    _, link = await answered(client)
    trace = parse_qs(urlsplit(link).query)["trace_id"][0]
    unknown = "f" * 32
    for persona in ("ENG-01", "OPS-01", "ADV-02"):
        await apply(client, persona=persona)
        page = await client.get(link)
        assert page.status_code == 403 and DENIAL in page.text, persona
        assert "span_id" not in page.text and "Span tree" not in page.text
        other = await client.get("/workbench/trace?trace_id=" + unknown)
        assert neutral(page, trace) == neutral(other, unknown), persona
    # Revoked: the owner no longer covers the run's household.
    await apply(client, persona="ADV-01")
    state["revoked"] = {"WC-HH-0001"}
    revoked = await client.get(link)
    assert revoked.status_code == 403 and "Span tree" not in revoked.text
    other = await client.get("/workbench/trace?trace_id=" + unknown)
    assert neutral(revoked, trace) == neutral(other, unknown)
    assert ("ADV-01", "trace_spans", ["WC-HH-0001"], False) in state["calls"]


@pytest.mark.anyio
async def test_c3_falls_back_to_trace_metadata_when_the_export_is_unavailable(
    advisor_ui, monkeypatch,
):
    app, client, _, _ = advisor_ui
    _, link = await answered(client)
    monkeypatch.setattr(app.state.service, "span_export",
                        app.state.service.span_export.parent / "absent.jsonl")
    await apply(client, persona="COMP-01")
    page = await client.get(link)
    assert page.status_code == 200
    spans_result, metadata = envelopes(page)
    assert spans_result["data"]["span_source"] == "UNAVAILABLE"
    assert "The span export is not available." in page.text
    assert '<ul class="span-tree"' not in page.text
    assert metadata["error"] is None and metadata["data"]["decisions"]["items"]
    assert "Recorded in this trace" in page.text
    # Without a trace ID the page reads nothing.
    empty = await client.get("/workbench/trace")
    assert empty.status_code == 200 and not envelopes(empty)
