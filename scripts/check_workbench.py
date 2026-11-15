"""Exercise the no-key workbench, page by page, in a real browser against a running app.

Covers build plan step 1's browser exit tests: a visitor reaches a figure's raw record
from the welcome page with no key; the keyboard path and focus return from the evidence
drawer; each persona's menu equals the permission matrix; reflow at 320 px and at 200%
text size; target sizes; self-hosted fonts under the strict Content Security Policy.

And step 2's (the advisor screens): no total anywhere on an incomplete household page or
book, every figure a returned field, every warning flag with its subject, the advisor
presets opening A6, A4 and A5, one neutral denial, and the same keyboard, drawer, reflow
and target checks on the book and household pages.

And step 3's (answers and revocation): A3's attempt badge, tool trail and answer check on
the recorded answer; A7 reached by keyboard from the "Current access" denial, its earlier
turn as paired tombstones with no figure, the recorded follow-up abstaining when its
recording is installed, an old source handle refused like an unknown one, and the same
reflow and target checks.

And step 4's (the engineer screens): ENG-01's home is E1 and its menu lists the two
contract pages alone; E1's registry is the governed catalog and each metric opens by
keyboard; E2 lists the seven MCP tools with their REST routes and opens each exported
schema unchanged, by keyboard; neither page links to a trace page, a household page or
evidence, or shows a figure; and the same reflow and target checks.

And step 5's (the compliance screens): the late-trade preset opens C2 with two cutoffs
side by side, every figure a returned field, receipts marked by run ID alone and the cited
records opening in the drawer by keyboard with focus returned; C1 filters and pages by
keyboard with the counts the platform returned and facts labelled at their stored
revision; C3 opens from a C1 decision and from the advisor's own answer, with closed span
names and no attribute; and the same reflow and target checks on all three.

And step 6's (the operations screens): the failed-feed preset names a cell and opens O2
with the run that cell's `feed_health` returned, its 15 affected accounts (never zero),
their stored reason codes and the 14 households with their completeness, and the run's
records opening in the drawer by keyboard with focus returned; O1 reached by keyboard from
the operations menu draws every expected cell with a glyph and a word, its one tile, the
failed cell opening O2 by keyboard, and its table view; no name or title on either page;
and the same reflow and target checks.

And step 7's (the close): every one of the 18 screens and the context form leaves a smoke
screenshot at the desktop width (1440 px) and one at a phone width (390 or 320 px), each
phone capture with no horizontal scroll; the screenshots use the synthetic demo data only.
"""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PERSONAS = {"ADV-01": "advisor", "OPS-01": "ops", "COMP-01": "compliance", "ENG-01": "engineer"}
MCP_TOOLS = ("get_household_snapshot", "get_portfolio_exposure", "get_concentrated_positions",
             "get_metric_definition", "get_data_provenance", "search_households",
             "get_data_quality_report")
# Links an engineer's pages never carry (brief guardrail 9; build plan decision 13).
NOT_FOR_ENGINEERS = ("/workbench/inspector/", "/workbench/household", "/workbench/book",
                     "/workbench/evidence", "/workbench/claim", "/workbench/revocation")


def submit(page: Page, selector: str) -> None:
    with page.expect_navigation(wait_until="networkidle"):
        page.locator(selector).click()


def goto(page: Page, url: str) -> None:
    page.goto(url, wait_until="networkidle")


def envelope(page: Page) -> dict:
    return json.loads(page.locator("main details pre").last.inner_text())


def envelopes(page: Page) -> list[dict]:
    """Every governed envelope a data page discloses (its JSON panels start closed)."""
    return [json.loads(text) for text in page.locator("main details pre").all_text_contents()]


def leaves(value) -> set[str]:
    if isinstance(value, dict):
        return {item for child in value.values() for item in leaves(child)}
    if isinstance(value, list):
        return {item for child in value for item in leaves(child)}
    return {str(value)}


def no_total(page: Page, name: str) -> None:
    """Nothing on the page reads as a total, and every figure is a returned field."""
    shown = page.locator("main").inner_text().lower()
    shown = shown.replace("not a total", "").replace("no total is shown", "")
    assert re.search(r"\btotal\b", shown) is None, (name, "an incomplete page shows no total")
    returned = set().union(*(leaves(item) for item in envelopes(page)))
    figures = page.locator("main .figure").all_inner_texts()
    assert figures, (name, "a page without figures proves nothing")
    for text in figures:
        value = re.sub(r"^(USD|EUR|CAD|GBP) | shares$| days$|%$", "", text.strip())
        assert value in returned, (name, text, "a figure the platform did not return")
    for item in envelopes(page):
        data = item.get("data") or {}
        if item.get("status") == "INCOMPLETE" and "fresh_subtotal_usd" in data:
            assert "assets_usd" not in data, name


def flags_with_subjects(page: Page, name: str) -> None:
    listed = page.locator("main .flag-list li").all_inner_texts()
    for item in envelopes(page):
        for flag in item.get("quality_flags") or []:
            assert any(flag["code"] in row and flag["subject"] in row for row in listed), (
                name, flag)


def focus_visible(page: Page) -> bool:
    """The focused element draws a visible indicator: an outline or a ring."""
    return page.evaluate("""() => {
      const style = getComputedStyle(document.activeElement);
      return (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0)
        || style.boxShadow !== 'none';
    }""")


def follows_trigger(page: Page, trigger) -> None:
    """At a phone width, Enter opens the evidence right after the trigger's own article or
    section instead of beside the page; Escape closes it and focus returns."""
    drawer = page.locator("#evidence-drawer")
    page.set_viewport_size({"width": 390, "height": 844})
    trigger.focus()
    page.keyboard.press("Enter")
    expect(drawer).to_be_visible()
    expect(page.locator("#drawer-title")).to_be_focused()
    assert trigger.evaluate(
        "t => { const a = t.closest('article, section');"
        " return !!a && a.nextElementSibling === document.querySelector('#evidence-drawer'); }"
    ), "the opened evidence follows its trigger"
    assert no_horizontal_scroll(page), "evidence at 390px"
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    expect(trigger).to_be_focused()
    page.set_viewport_size({"width": 1440, "height": 1000})


def drawer_round_trip(page: Page, trigger) -> None:
    """Enter opens the evidence beside the page; Escape closes it; focus returns, visibly;
    and on a phone width the evidence opens after its trigger."""
    drawer = page.locator("#evidence-drawer")
    before = page.url
    trigger.focus()
    page.keyboard.press("Enter")
    expect(drawer).to_be_visible()
    expect(drawer.get_by_role("heading", name="Source records")).to_be_visible()
    expect(drawer).to_contain_text("source_record_id")
    expect(page.locator("#drawer-title")).to_be_focused()
    assert page.url == before, "the page underneath stays put"
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    expect(trigger).to_be_focused()
    assert focus_visible(page), "the returned focus is visible"
    follows_trigger(page, trigger)


def preset(page: Page, base: str, name: str) -> None:
    goto(page, base)
    submit(page, f'button.scenario[value="{name}"]')


def apply_context(page: Page, **fields: str) -> None:
    page.locator("[data-open-context]").first.click()
    expect(page.locator("#context-dialog")).to_be_visible()
    for field, value in fields.items():
        control = page.locator(f"#{field}")
        if control.evaluate("element => element.tagName") == "SELECT":
            control.select_option(value)
        else:
            control.fill(value)
    submit(page, '#context-form button[type="submit"]')


def no_horizontal_scroll(page: Page) -> bool:
    return page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")


def reflows(page: Page, name: str) -> None:
    """No horizontal page scroll at 320 CSS px wide or at 200% text size."""
    page.set_viewport_size({"width": 320, "height": 844})
    assert no_horizontal_scroll(page), (name, "320px")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.evaluate("document.documentElement.style.fontSize = '200%'")
    assert no_horizontal_scroll(page), (name, "200% text")
    page.evaluate("document.documentElement.style.fontSize = ''")
    page.set_viewport_size({"width": 1440, "height": 1000})


def mobile_shot(page: Page, output: Path, name: str) -> None:
    """The page at a phone width (390 CSS px), with no horizontal scroll, as a smoke
    screenshot beside its desktop one, with every target at least 24 by 24 px there too;
    the desktop size is restored after."""
    page.set_viewport_size({"width": 390, "height": 844})
    assert no_horizontal_scroll(page), (name, "390px")
    assert not small_targets(page), (name, "390px", small_targets(page))
    page.screenshot(path=str(output / f"{name}-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})


def small_targets(page: Page) -> list[str]:
    """Visible controls smaller than 24 by 24 CSS px (WCAG 2.2 target size)."""
    return page.evaluate("""() => [...document.querySelectorAll(
        'a[href], button, input:not([type=hidden]), select, textarea, summary')]
      .filter(element => element.checkVisibility() && !element.closest('.answer-prose'))
      .map(element => [element, element.getBoundingClientRect()])
      .filter(([, box]) => box.width > 0 && (box.width < 24 || box.height < 24))
      .map(([element, box]) => `${element.tagName} ${element.className} ` +
           `${Math.round(box.width)}x${Math.round(box.height)}`)""")


def contracts_only(page: Page, name: str) -> None:
    """No trace page, household page or evidence is linked, and no figure is shown."""
    targets = page.evaluate("[...document.querySelectorAll('a[href], form[action]')]"
                            ".map(element => element.getAttribute('href')"
                            " || element.getAttribute('action'))")
    reached = [target for target in targets if target.startswith(NOT_FOR_ENGINEERS)]
    assert not reached, (name, reached)
    expect(page.locator("main .figure, main .figure-link")).to_have_count(0)
    assert "WC-HH-" not in page.locator("main").inner_text(), name


def active(page: Page) -> str:
    return page.evaluate(
        "document.activeElement.id || document.activeElement.className"
        " || document.activeElement.tagName")


def check(url: str, output: Path) -> dict:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("The synthetic browser check requires a loopback HTTP URL")
    output.mkdir(parents=True, exist_ok=True)
    matrix = yaml.safe_load((ROOT / "policies/matrix.yaml").read_text())
    menu_rows = [row for row in matrix["routes"] if "menu" in row]
    checks: list[dict] = []
    errors: list[str] = []

    def passed(case: str, **details) -> None:
        checks.append({"case": case, "status": "PASS", **details})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        # A blocked inline style, script or font shows up as a console error. So does
        # every 403 document load; the A5 denials below are expected, by URL, and only
        # those are exempt.
        expected_denials: set[str] = set()

        def console(message) -> None:
            if message.type != "error":
                return
            if (message.text.startswith("Failed to load resource: the server responded "
                                        "with a status of 403")
                    and message.location.get("url") in expected_denials):
                return
            errors.append(message.text)

        page.on("console", console)
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        try:
            # V1: the welcome page holds no business data and needs no key.
            goto(page, url)
            heading = page.get_by_role("heading", name="Numbers you can stand behind.")
            expect(heading).to_be_visible()
            expect(page.locator("button.scenario")).to_have_count(6)
            expect(page.locator(".claim")).to_have_count(0)
            expect(page.locator("nav.side")).to_have_count(0)
            assert page.evaluate("document.fonts.ready.then(() => ["
                                 "'600 30px Newsreader', '400 14px Inter',"
                                 " '400 12px \"JetBrains Mono\"']"
                                 ".every(font => document.fonts.check(font)))")
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "welcome-wide.png"), full_page=True)
            mobile_shot(page, output, "welcome")
            reflows(page, "welcome")
            page.locator("[data-open-context]").first.click()
            expect(page.locator("#context-dialog")).to_be_visible()
            assert not small_targets(page), ("X1", small_targets(page))
            page.screenshot(path=str(output / "context-form-wide.png"), full_page=True)
            mobile_shot(page, output, "context-form")
            reflows(page, "context form")
            page.keyboard.press("Escape")
            passed("welcome_without_business_data")

            # Keyboard: the skip link is first, and the context form opens and closes
            # from the keyboard with focus back on its opener.
            goto(page, url)
            page.keyboard.press("Tab")
            assert active(page) == "skip-link", active(page)
            assert focus_visible(page), "the skip link shows its focus"
            page.keyboard.press("Enter")
            assert active(page) == "main", active(page)
            opener = page.get_by_role("button", name="Enter workspace")
            opener.focus()
            page.keyboard.press("Enter")
            expect(page.locator("#context-dialog")).to_be_visible()
            assert page.evaluate("document.activeElement.closest('dialog') !== null")
            page.keyboard.press("Escape")
            expect(page.locator("#context-dialog")).to_be_hidden()
            expect(opener).to_be_focused()
            assert focus_visible(page), "the returned focus is visible"
            # X1 applies from the keyboard alone: a cutoff typed in, Enter submits the form.
            page.keyboard.press("Enter")
            expect(page.locator("#context-dialog")).to_be_visible()
            page.locator("#known-as-of").focus()
            # Wherever the caret starts, forward and backward deletes clear the field.
            for key in ("Delete", "Backspace"):
                for _ in range(40):
                    page.keyboard.press(key)
            assert page.locator("#known-as-of").input_value() == ""
            page.keyboard.type("2026-10-30T16:00:00Z")
            assert page.locator("#known-as-of").input_value() == "2026-10-30T16:00:00Z"
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            expect(page.locator(".ctx")).to_contain_text("30 Oct 2026, 16:00 UTC")
            assert urlparse(page.url).path.startswith("/workbench"), page.url
            passed("skip_link_and_context_form_keyboard")
            goto(page, url)

            # V2: the guided replay, started from the welcome page.
            submit(page, 'button[name="preset"][value="clean"].button')
            expect(page.get_by_text("Tour · 1 of 4")).to_be_visible()
            expect(page.locator(".ctx")).to_contain_text("ADV-01 · Advisor")
            expect(page.locator(".ctx")).to_contain_text("30 Sep 2026")
            expect(page.locator(".ctx")).to_contain_text("History · 31 Oct 2026, 16:00 UTC")
            run = page.get_by_role("button", name="Run replay")
            expect(run).to_be_enabled()
            run.focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            expect(page.get_by_text("Verified response", exact=True)).to_be_visible()
            expect(page.locator(".claim").first).to_be_visible()
            expect(page.locator(".answer-prose")).to_contain_text("3000.00")
            # A3: the attempt the answer took, its tool trail and how it was checked.
            expect(page.locator(".answer-card .card-head")).to_contain_text("First pass")
            expect(page.locator(".tool-trail").first).to_contain_text("get_household_snapshot")
            check_summary = page.locator(".answer-check > summary")
            check_summary.focus()
            page.keyboard.press("Enter")
            expect(page.locator(".answer-check .check-list")).to_be_visible()
            expect(page.locator(".answer-check")).to_contain_text("First pass")
            page.keyboard.press("Enter")
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "replay-wide.png"), full_page=True)
            passed("genuine_no_key_replay")

            # Evidence drawer by keyboard: Enter opens it beside the page, focus moves in,
            # Escape closes it and focus returns to the figure that opened it.
            claim = page.locator(".claim").first
            before = page.url
            claim.focus()
            page.keyboard.press("Enter")
            drawer = page.locator("#evidence-drawer")
            expect(drawer).to_be_visible()
            expect(drawer.get_by_role("heading", name="Source records")).to_be_visible()
            expect(drawer).to_contain_text("source_record_id")
            expect(drawer).to_contain_text("Showing")
            expect(page.locator("#drawer-title")).to_be_focused()
            assert page.url == before, "the page underneath stays put"
            expect(page.locator(".answer-prose")).to_be_visible()
            page.screenshot(path=str(output / "evidence-drawer-wide.png"), full_page=True)
            # Tab stays reachable inside the drawer; a second handle opens in place.
            # A handle inside the drawer reopens in place and keeps the page trigger.
            drawer.locator(".source-links a").first.focus()
            page.keyboard.press("Enter")
            expect(drawer.locator('.source-links a[aria-current="true"]')).to_have_count(1)
            expect(page.locator("#drawer-title")).to_be_focused()
            assert not small_targets(page), small_targets(page)
            page.keyboard.press("Escape")
            expect(drawer).to_be_hidden()
            expect(claim).to_be_focused()
            assert drawer.inner_text() == "", "closed evidence is dropped from the page"
            # The close control returns focus the same way.
            claim.click()
            expect(drawer).to_be_visible()
            drawer.get_by_role("link", name="Close evidence").click()
            expect(drawer).to_be_hidden()
            expect(claim).to_be_focused()
            passed("visitor_reaches_raw_record_keyboard_and_focus_return")

            # Reflow: 390 and 320 CSS px wide, and 200% text size.
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 844})
                assert no_horizontal_scroll(page), width
                expect(page.locator(".claim").first).to_be_visible()
            page.screenshot(path=str(output / "replay-mobile.png"), full_page=True)
            # On a narrow screen the opened evidence follows its trigger in the page.
            claim.click()
            expect(drawer).to_be_visible()
            assert page.evaluate(
                "document.querySelector('#evidence-drawer').previousElementSibling"
                ".matches('article.answer-card')"
                " && !document.querySelector('.shell > #evidence-drawer')")
            assert no_horizontal_scroll(page)
            page.screenshot(path=str(output / "evidence-drawer-mobile.png"), full_page=True)
            page.keyboard.press("Escape")
            expect(claim).to_be_focused()
            page.set_viewport_size({"width": 1280, "height": 900})
            page.evaluate("document.documentElement.style.fontSize = '200%'")
            assert no_horizontal_scroll(page), "200% text size"
            page.evaluate("document.documentElement.style.fontSize = ''")
            page.set_viewport_size({"width": 1440, "height": 1000})
            passed("reflow_320px_and_200_percent_text")

            # The trace inspector and the bearer boundary, as before the revamp.
            goto(page, url + "/workbench/inspector/trace")
            trace = envelope(page)
            assert trace["status"] == "COMPLETE"
            passed("trace", trace_id=trace["trace_id"])
            response = context.request.post(url + "/api/get_household_snapshot", data={
                "household_id": "WC-HH-0001", "as_of": "2026-09-30",
            })
            assert response.status == 401
            passed("api_requires_bearer")

            # Data-page evidence: a result on the snapshot inspector opens its records.
            goto(page, url + "/workbench/inspector/snapshot")
            handle = page.locator("main .source-links a").first
            link = handle.get_attribute("href")
            assert link is not None
            handle.focus()
            page.keyboard.press("Enter")
            expect(drawer.get_by_role("heading", name="Source records")).to_be_visible()
            expect(page.locator("#drawer-title")).to_be_focused()
            page.keyboard.press("Escape")
            expect(handle).to_be_focused()
            passed("data_page_evidence_drawer")

            # A stale selection gets the neutral denial: applying the same context again
            # replaces the session, so only the dropped pair can explain the refusal.
            preset(page, url, "clean")
            stale = context.request.get(url + link)
            assert stale.status == 403 and "not authorized" in stale.text()
            assert "source_record_id" not in stale.text()
            passed("stale_evidence_refused")
            # A6: "Settlement and reconciliation" opens the reconciliation tab at its
            # accepted cutoffs, which show two breaks.
            preset(page, url, "reconciliation")
            assert urlparse(page.url).path == "/workbench/household/reconciliation", page.url
            expect(page.get_by_text("Tour ·")).to_have_count(0)
            expect(page.locator(".ctx")).to_contain_text("1 Oct 2026")
            expect(page.locator(".ctx")).to_contain_text("2 Oct 2026, 12:00 UTC")
            (discrepancy,) = envelopes(page)
            assert '"BREAK"' in json.dumps(discrepancy["data"])
            for code in ("QUANTITY_BREAK", "CASH_BREAK"):
                expect(page.locator("main .flag-list")).to_contain_text(code)
            flags_with_subjects(page, "A6")
            expect(page.locator('.tabs a[aria-current="page"]')).to_have_text("Reconciliation")
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "reconciliation-wide.png"), full_page=True)
            mobile_shot(page, output, "reconciliation")
            reflows(page, "A6")
            # The tabs work from the keyboard.
            page.locator(".tabs a").first.focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            assert urlparse(page.url).path == "/workbench/household", page.url
            passed("discrepancy_tab", result_id=discrepancy["result_id"])

            # A4: "Missing authoritative input", moved to ADV-03 and WC-HH-0003.
            preset(page, url, "missing")
            assert urlparse(page.url).path == "/workbench/household", page.url
            expect(page.locator(".ctx")).to_contain_text("ADV-03 · Advisor")
            (incomplete,) = envelopes(page)
            assert incomplete["status"] == "INCOMPLETE"
            assert incomplete["data"]["household_id"] == "WC-HH-0003"
            expect(page.locator(".incomplete")).to_contain_text("Fresh subtotal (not a total)")
            no_total(page, "A4")
            flags_with_subjects(page, "A4")
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "incomplete-wide.png"), full_page=True)
            mobile_shot(page, output, "incomplete")
            # With its evidence open, nothing in the drawer reads as a total either.
            subtotal = page.locator(".incomplete .figure-link")
            subtotal.focus()
            page.keyboard.press("Enter")
            opened = page.locator("#evidence-drawer")
            expect(opened.get_by_role("heading", name="Source records")).to_be_visible()
            in_drawer = opened.inner_text().lower().replace("not a total", "")
            assert re.search(r"\btotal\b", in_drawer) is None, "A4 drawer shows no total"
            page.keyboard.press("Escape")
            drawer_round_trip(page, subtotal)
            reflows(page, "A4")
            # Its exposure tab returns no rows and no percentage, and shows no total.
            goto(page, url + "/workbench/household/exposure")
            (exposure,) = envelopes(page)
            assert exposure["status"] == "INCOMPLETE"
            no_total(page, "A4 exposure")
            flags_with_subjects(page, "A4 exposure")
            # And its reconciliation tab (A6 on an incomplete household).
            goto(page, url + "/workbench/household/reconciliation")
            (reconciliation,) = envelopes(page)
            assert reconciliation["status"] == "INCOMPLETE"
            no_total(page, "A6 incomplete")
            flags_with_subjects(page, "A6 incomplete")
            passed("incomplete_without_total", result_id=incomplete["result_id"])

            # A5: "Current access" gets one neutral denial with no figure and no way to
            # request access; an unknown household gets the same page.
            expected_denials.update({url + "/workbench/household",
                                     url + "/workbench/household?household_id=WC-HH-9999"})
            preset(page, url, "access")
            assert urlparse(page.url).path == "/workbench/household", page.url
            denials = []
            for household in ("WC-HH-0004", "WC-HH-9999"):
                if household != "WC-HH-0004":
                    goto(page, url + f"/workbench/household?household_id={household}")
                main = page.locator("main")
                expect(main).to_contain_text(
                    "You are not authorized for the requested resource.")
                expect(main.locator(".figure, .source-links")).to_have_count(0)
                assert "request access" not in main.inner_text().lower()
                denials.append(re.sub(r"\b[0-9a-f]{32}\b", "ID",
                                      main.inner_text().replace(household, "REQUESTED")))
            assert denials[0] == denials[1], "one neutral denial for existing and unknown"
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "denied-wide.png"), full_page=True)
            mobile_shot(page, output, "denied")
            reflows(page, "A5")
            expected_denials.clear()
            passed("current_access_denied", denied_status=403)

            # A7: the "Current access" denial links to the seeded revocation, by keyboard.
            entry = page.get_by_role("link", name="Open the seeded revocation")
            entry.focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            assert urlparse(page.url).path == "/workbench/revocation", page.url
            main = page.locator("main")
            expect(main.locator(".answer-card").first).to_contain_text("Access changed")
            expect(main).to_contain_text("[REDACTED: NOT_AUTHORIZED]")
            badges = main.locator(".tool-trail .badge").all_inner_texts()
            assert badges == ["Redacted: not authorized"] * 2, badges
            expect(main.locator(".figure")).to_have_count(0)
            expect(page.locator('nav.side a[aria-current="true"]')).to_have_attribute(
                "href", "/workbench")
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "revocation-wide.png"), full_page=True)
            mobile_shot(page, output, "revocation")
            reflows(page, "A7")
            followup = page.get_by_role("button", name="Run the recorded follow-up")
            recorded_followup = followup.is_enabled()
            if recorded_followup:
                followup.focus()
                with page.expect_navigation(wait_until="networkidle"):
                    page.keyboard.press("Enter")
                expect(main).to_contain_text("Abstained · no new retrieval")
                expect(main.locator(".figure")).to_have_count(0)
                expect(followup).to_have_count(0)
                assert not small_targets(page), small_targets(page)
                page.screenshot(path=str(output / "revocation-followup-wide.png"),
                                full_page=True)
                mobile_shot(page, output, "revocation-followup")
                reflows(page, "A7 follow-up")
            else:
                expect(main).to_contain_text("The recorded follow-up is not installed")
            # An old source handle is reauthorized and refused like one never issued.
            old = main.locator(".source-links a").first.get_attribute("href")
            assert old is not None
            unknown = "/workbench/revocation/evidence?result=r_unknown&source=h_unknown"
            expected_denials.update({url + old, url + unknown})
            refusals = []
            for target in (old, unknown):
                goto(page, url + target)
                expect(page.locator("main")).to_contain_text(
                    "You are not authorized for the requested resource.")
                refusals.append(page.content())
            assert refusals[0] == refusals[1], "one neutral denial for old and unknown"
            expected_denials.clear()
            goto(page, url + "/workbench/revocation")
            passed("seeded_revocation", followup_recorded=recorded_followup)

            # A1: my book, on a complete day. Every count is a returned field, and each
            # household's figure opens its own source records.
            apply_context(page, persona="ADV-01", household="WC-HH-0001",
                          **{"as-of": "2026-09-30"})
            assert urlparse(page.url).path == "/workbench/book", "the advisor's home"
            listed, *snapshots, concentration = envelopes(page)
            assert [item["household_id"] for item in listed["data"]["items"]] == [
                snapshot["data"]["household_id"] for snapshot in snapshots]
            expect(page.locator(".kpi").first).to_contain_text(
                str(concentration["data"]["evaluated_household_count"]))
            expect(page.locator("main")).to_contain_text("No book figure is shown")
            flags_with_subjects(page, "A1")
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "book-wide.png"), full_page=True)
            mobile_shot(page, output, "book")
            figure = page.locator("main .figure-link").first
            drawer_round_trip(page, figure)
            reflows(page, "A1")
            passed("book", households=len(snapshots))

            # A1b: the same book on an incomplete day shows no total anywhere.
            apply_context(page, **{"as-of": "2026-10-15"})
            listed, *snapshots, concentration = envelopes(page)
            assert concentration["status"] == "INCOMPLETE"
            assert {snapshot["status"] for snapshot in snapshots} == {"INCOMPLETE"}
            no_total(page, "A1b")
            flags_with_subjects(page, "A1b")
            expect(page.locator("main")).to_contain_text("Fresh subtotal (not a total)")
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "book-incomplete-wide.png"), full_page=True)
            reflows(page, "A1b")
            # On a narrow screen the opened evidence follows its trigger in the page.
            page.set_viewport_size({"width": 390, "height": 844})
            figure = page.locator("main .figure-link").first
            figure.focus()
            page.keyboard.press("Enter")
            expect(page.locator("#evidence-drawer")).to_be_visible()
            assert page.evaluate(
                "document.querySelector('#evidence-drawer').previousElementSibling"
                ".matches('section.card')"
                " && !document.querySelector('.shell > #evidence-drawer')")
            assert no_horizontal_scroll(page)
            page.screenshot(path=str(output / "book-incomplete-mobile.png"), full_page=True)
            page.keyboard.press("Escape")
            expect(figure).to_be_focused()
            page.set_viewport_size({"width": 1440, "height": 1000})
            passed("book_incomplete_without_total", households=len(snapshots))

            # A2: the household page, complete, with its figures opening evidence.
            apply_context(page, **{"as-of": "2026-09-30"})
            goto(page, url + "/workbench/household")
            (household,) = envelopes(page)
            assert household["status"] == "COMPLETE"
            expect(page.locator(".kpi .figure-link")).to_have_count(2)
            drawer_round_trip(page, page.locator(".kpi .figure-link").first)
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "household-wide.png"), full_page=True)
            mobile_shot(page, output, "household")
            reflows(page, "A2")
            goto(page, url + "/workbench/household/exposure")
            (exposure,) = envelopes(page)
            assert exposure["status"] == "COMPLETE" and exposure["data"]["issuer_exposures"]
            expect(page.locator('.tabs a[aria-current="page"]')).to_have_text("Exposure")
            expect(page.locator("main table")).to_contain_text(
                exposure["data"]["issuer_exposures"]["items"][0]["label"])
            flags_with_subjects(page, "A2 exposure")
            assert not small_targets(page), small_targets(page)
            reflows(page, "A2 exposure")
            passed("household", result_id=household["result_id"])

            # E1: the engineer's home is the metric page. Its registry is the governed
            # catalog, and each metric's governed definition opens by keyboard.
            apply_context(page, persona="ENG-01", household="WC-HH-0001")
            assert urlparse(page.url).path == "/workbench/metrics", "the engineer's home"
            shown = page.evaluate("[...document.querySelectorAll('nav.side a')]"
                                  ".map(a => a.getAttribute('href'))")
            assert shown == ["/workbench/metrics", "/workbench/contracts"], shown
            catalog, definition = envelopes(page)
            items = catalog["data"]["items"]
            assert definition["data"] == items[0]
            expect(page.locator("h1")).to_have_text(items[0]["label"])
            registry = page.locator(".registry a")
            expect(registry).to_have_count(catalog["data"]["item_count"])
            expect(page.locator(".registry .counts")).to_contain_text(
                str(catalog["data"]["item_count"]))
            contracts_only(page, "E1")
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "metric-wide.png"), full_page=True)
            reflows(page, "E1")
            registry.nth(1).focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            _, chosen = envelopes(page)
            assert chosen["data"] == items[1], chosen
            expect(page.locator("h1")).to_have_text(items[1]["label"])
            expect(page.locator('.registry a[aria-current="true"]')).to_have_count(1)
            expect(page.locator(".definition")).to_contain_text(items[1]["formula"])
            contracts_only(page, "E1 chosen")
            page.set_viewport_size({"width": 320, "height": 844})
            assert no_horizontal_scroll(page), "E1 at 320px"
            page.screenshot(path=str(output / "metric-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            passed("metric_definition", metric=items[1]["id"],
                   definitions=catalog["data"]["item_count"])

            # E2: the MCP tools with their REST routes, and every exported schema, which
            # opens unchanged by keyboard.
            page.locator("nav.side a").nth(1).focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            assert urlparse(page.url).path == "/workbench/contracts", page.url
            tools = page.locator("main table").first.locator("tbody tr")
            expect(tools).to_have_count(len(MCP_TOOLS))
            for index, name in enumerate(MCP_TOOLS):
                expect(tools.nth(index)).to_contain_text(name)
                expect(tools.nth(index)).to_contain_text(f"POST /api/{name}")
            summary = page.locator("main .plain-list summary").first
            summary.focus()
            page.keyboard.press("Enter")
            first_schema = page.locator("main .plain-list pre.schema").first
            expect(first_schema).to_be_visible()
            assert json.loads(first_schema.inner_text())["properties"], "an input schema"
            first_schema.focus()
            expect(first_schema).to_be_focused()
            files = sorted(path.name for path in (ROOT / "schemas/v1").glob("*.json"))
            expect(page.locator('main a[href*="schema_id="]')).to_have_count(len(files))
            contracts_only(page, "E2")
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "contracts-wide.png"), full_page=True)
            reflows(page, "E2")
            link = page.locator('main a[href="/workbench/contracts?schema_id=snapshot#schema"]')
            link.focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            opened = page.locator("#schema pre.schema")
            expect(opened).to_be_visible()
            assert opened.text_content() == (ROOT / "schemas/v1/snapshot.json").read_text(), (
                "the published file, unchanged")
            expect(page.locator('main a[aria-current="true"]')).to_have_count(1)
            contracts_only(page, "E2 schema")
            assert not small_targets(page), small_targets(page)
            reflows(page, "E2 schema")
            page.set_viewport_size({"width": 320, "height": 844})
            page.screenshot(path=str(output / "contracts-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            passed("tools_and_schemas", tools=len(MCP_TOOLS), schemas=len(files))

            # C2: the late-trade preset opens two cutoffs side by side.
            preset(page, url, "late_trade")
            assert urlparse(page.url).path == "/workbench/point-in-time", page.url
            expect(page.locator(".ctx")).to_contain_text("COMP-01 · Compliance")
            expect(page.locator(".ctx")).to_contain_text("31 Oct 2026, 16:00 UTC")
            columns = page.locator("main .pit-column")
            expect(columns).to_have_count(2)
            shown = envelopes(page)
            receipts = [item for item in shown if "cited_record_count" in (item["data"] or {})]
            assert [item["data"]["cited_record_count"] for item in receipts] == [29, 27]
            later, earlier = ({row["run_id"] for row in item["data"]["items"]}
                              for item in receipts)
            for index, (mine, other) in enumerate(((later, earlier), (earlier, later))):
                marked = set(columns.nth(index).locator(
                    "tr:has(.badge.b-info) .run-id").all_inner_texts())
                assert marked == mine - other, index
                expect(columns.nth(index).locator(".counts").first).to_contain_text(
                    str(receipts[index]["data"]["cited_record_count"]))
            no_total(page, "C2")
            flags_with_subjects(page, "C2")
            words = page.locator("main").inner_text().lower()
            assert "difference" not in words and "export" not in words, "C2 wording"
            drawer_round_trip(page, columns.nth(1).locator("a[data-drawer].button"))
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "point-in-time-wide.png"), full_page=True)
            reflows(page, "C2")
            page.set_viewport_size({"width": 320, "height": 844})
            page.screenshot(path=str(output / "point-in-time-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            passed("point_in_time", runs=[len(later), len(earlier)])

            # C1: the compliance home. Filter and page by keyboard; counts as returned.
            page.locator("nav.side a", has_text="Access decisions").focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            assert urlparse(page.url).path == "/workbench/decisions", page.url
            (log,) = envelopes(page)
            data = log["data"]
            expect(page.locator("main .counts").first).to_contain_text(
                f"Showing {data['item_count']} of {data['total_item_count']} matching decisions")
            expect(page.locator("main")).to_contain_text("FACTS_AT_STORED_REVISION")
            assert data["next_cursor"], "the browser's own reads fill more than one page"
            following = page.get_by_role("link", name="Next page")
            following.focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            (second,) = envelopes(page)
            assert second["data"]["total_item_count"] == data["total_item_count"], (
                "the cursor holds while this browser's own decisions arrive")
            assert not {item["id"] for item in data["items"]} & {
                item["id"] for item in second["data"]["items"]}
            page.locator("#f-outcome").select_option("allow")
            page.locator("#f-principal").focus()
            page.keyboard.type("COMP-01")
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            assert parse_qs(urlparse(page.url).query).get("outcome") == ["allow"], page.url
            expect(page.locator("#f-outcome")).to_have_value("allow")
            (filtered,) = envelopes(page)
            assert filtered["error"] is None, filtered
            assert all(item["principal_id"] == "COMP-01" and item["allowed"]
                       for item in filtered["data"]["items"]), "the platform filtered"
            expect(page.locator("main .figure, main .figure-link")).to_have_count(0)
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "decisions-wide.png"), full_page=True)
            mobile_shot(page, output, "decisions")
            reflows(page, "C1")
            passed("access_decisions", decisions=data["total_item_count"])

            # C3 from a C1 decision: the span tree, closed names, no attribute.
            trace_link = page.locator('main a[href^="/workbench/trace?trace_id="]').first
            trace_link.focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            spans = envelopes(page)[0]
            assert spans["error"] is None, spans
            closed = {"agent", "tool", "authz", "query", "calc", "provider_context",
                      "answer_render", "browser_history", "authenticate",
                      "harness_http_failure", "other"}
            assert {item["name"] for item in spans["data"]["items"]} <= closed
            if spans["data"]["span_source"] == "EXPORT":
                expect(page.locator("main .span-tree")).to_be_visible()
            else:
                expect(page.locator("main")).to_contain_text("The span export is not available")
            expect(page.locator("main")).to_contain_text("Recorded in this trace")
            assert "attributes" not in page.content() and "gen_ai" not in page.content()
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "trace-wide.png"), full_page=True)
            reflows(page, "C3")
            passed("trace_spans_compliance", source=spans["data"]["span_source"])

            # C3 for the advisor: reached from their own answer, never from the menu.
            preset(page, url, "clean")
            submit(page, '#question-form button[type="submit"]')
            expect(page.locator("nav.side a", has_text="Trace")).to_have_count(1)  # inspector
            expect(page.locator('nav.side a[href="/workbench/trace"]')).to_have_count(0)
            run_link = page.get_by_role("link", name="Trace of this run")
            run_link.focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            spans = envelopes(page)[0]
            assert spans["error"] is None and spans["data"]["span_source"] == "EXPORT", spans
            names = {item["name"] for item in spans["data"]["items"]}
            assert {"agent", "tool", "authz"} <= names <= closed, names
            expect(page.locator("main .span-tree > li").first).to_contain_text("agent")
            page.locator("main details > summary").last.focus()
            page.keyboard.press("Enter")
            expect(page.locator("main details pre").first).to_be_visible()
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "trace-advisor-wide.png"), full_page=True)
            reflows(page, "C3 advisor")
            page.set_viewport_size({"width": 320, "height": 844})
            page.screenshot(path=str(output / "trace-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            passed("trace_spans_advisor_own_run", spans=spans["data"]["item_count"])

            # O2 by the S07 preset: it names a cell, and the run is the one that cell's
            # authorized feed_health returned. A failed run never shows zero affected.
            preset(page, url, "failed_feed")
            assert urlparse(page.url).path == "/workbench/feed-run", page.url
            assert "run_id" not in parse_qs(urlparse(page.url).query), page.url
            results = envelopes(page)
            (health,) = [item for item in results if "business_dates" in (item["data"] or {})]
            (impact,) = [item for item in results
                         if "account_attribution" in (item["data"] or {})]
            (cell,) = [item for item in health["data"]["items"]
                       if (item["source_system"], item["feed"]) == ("PAS-B", "pas_snapshots")]
            data = impact["data"]
            assert data["run_id"] == cell["run_id"], (data["run_id"], cell)
            assert (data["status"], data["affected_account_count"],
                    data["affected_household_count"]) == ("FAILED", 15, 14), data
            affected = page.locator("main .kpi").nth(2)
            expect(affected).to_contain_text("Accounts affected")
            expect(affected.locator(".figure")).to_have_text("15")
            expect(page.locator('main section[aria-labelledby="accounts-title"] tbody tr')
                   ).to_have_count(data["affected_accounts"]["item_count"])
            expect(page.locator('main section[aria-labelledby="accounts-title"] tbody')
                   ).to_contain_text("ACCOUNT_MANIFEST_MISMATCH")
            expect(page.locator('main section[aria-labelledby="households-title"] tbody tr')
                   ).to_have_count(data["affected_households"]["item_count"])
            no_total(page, "O2")
            flags_with_subjects(page, "O2")
            words = page.locator("main").inner_text().lower()
            assert "export" not in words, "O2 has no export control"
            drawer_round_trip(page, page.locator("main a[data-drawer]").first)
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "feed-run-wide.png"), full_page=True)
            reflows(page, "O2")
            page.set_viewport_size({"width": 320, "height": 844})
            page.screenshot(path=str(output / "feed-run-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            passed("feed_run_failed_feed", affected=data["affected_account_count"],
                   households=data["affected_household_count"])

            # O1, the operations home, by keyboard from the menu: every expected cell with
            # a glyph and a word, one tile, the failed cell opening O2, and a table view.
            page.locator("nav.side a", has_text="Data health").focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            assert urlparse(page.url).path == "/workbench/data-health", page.url
            results = envelopes(page)
            (health,) = [item for item in results if "business_dates" in (item["data"] or {})]
            (quality,) = [item for item in results
                          if "incomplete_household_count" in (item["data"] or {})]
            grid = health["data"]
            assert (grid["date_from"], grid["date_to"]) == ("2026-09-14", "2026-10-15"), grid
            cells = page.locator("main table.health-grid td.cell")
            expect(cells).to_have_count(grid["total_item_count"])
            expect(page.locator("main table.health-grid td.cell svg.icon")).to_have_count(
                grid["total_item_count"])
            expect(page.locator("main table.health-grid .cell-word")).to_have_count(
                grid["total_item_count"])
            expect(page.locator("main .kpi")).to_have_count(1)
            expect(page.locator("main .kpi")).to_contain_text("Households incomplete")
            expect(page.locator("main .kpi .figure")).to_have_text(
                str(quality["data"]["incomplete_household_count"]))
            no_total(page, "O1")
            flags_with_subjects(page, "O1")
            assert "accounts not received" not in page.locator("main").inner_text().lower()
            failed = page.locator("main table.health-grid a.cell-mark", has_text="Failed")
            failed.focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            assert parse_qs(urlparse(page.url).query)["run_id"] == [cell["run_id"]], page.url
            page.go_back(wait_until="networkidle")
            assert urlparse(page.url).path == "/workbench/data-health", page.url
            assert not small_targets(page), small_targets(page)
            page.screenshot(path=str(output / "data-health-wide.png"), full_page=True)
            reflows(page, "O1 grid")
            page.set_viewport_size({"width": 320, "height": 844})
            page.screenshot(path=str(output / "data-health-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            table = page.get_by_role("link", name="Table", exact=True)
            table.focus()
            with page.expect_navigation(wait_until="networkidle"):
                page.keyboard.press("Enter")
            expect(page.locator("main table.health-grid")).to_have_count(0)
            expect(page.locator("main table.data tbody tr")).to_have_count(
                grid["total_item_count"])
            expect(page.get_by_role("link", name="Table", exact=True)).to_have_attribute(
                "aria-current", "true")
            assert not small_targets(page), small_targets(page)
            reflows(page, "O1 table")
            flags_with_subjects(page, "O1 table")
            passed("data_health", cells=grid["total_item_count"],
                   incomplete=quality["data"]["incomplete_household_count"])

            # Each persona's menu is exactly what the permission matrix names for its role.
            for persona, role in PERSONAS.items():
                apply_context(page, persona=persona, household="WC-HH-0001")
                expected = [[row["path"], row["menu"]["label"]] for row in menu_rows
                            if role in row["menu"]["roles"]]
                shown = page.evaluate(
                    "[...document.querySelectorAll('nav.side a')]"
                    ".map(a => [a.getAttribute('href'), a.textContent.trim()])")
                assert shown == expected, (persona, shown, expected)
                for path, _ in expected:
                    goto(page, url + path)
                    expect(page.locator('nav.side a[aria-current="page"]')).to_have_attribute(
                        "href", path)
                    assert not small_targets(page), (path, small_targets(page))
                    reflows(page, path)
                passed(f"menu_{role}", pages=[path for path, _ in expected])
            apply_context(page, persona="COMP-01", household="WC-HH-0001")
            for surface in ("decisions", "eval"):
                goto(page, url + f"/workbench/inspector/{surface}")
                assert envelope(page)["status"] == "COMPLETE"
                passed(surface)
            assert all(urlparse(item).netloc == parsed.netloc for item in requests), (
                "every request stays on the local origin")
            assert not errors, errors
        finally:
            context.close()
            browser.close()
    result = {"base_url": url, "checks": checks, "javascript_errors": errors,
              "provider_calls_expected": 0, "mode": "recorded_replay_and_governed_pages"}
    (output / "checks.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("output/playwright/smoke"))
    args = parser.parse_args()
    print(json.dumps(check(args.url.rstrip("/"), args.output), indent=2))
