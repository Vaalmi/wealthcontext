"""HTTP workbench boundaries and genuine replay; no provider network calls."""

import asyncio
import html
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.test_chat_api import ScriptedProvider
from wealthcontext.app import create_app
from wealthcontext.bootstrap import bootstrap_history
from wealthcontext.clock import FrozenClock
from wealthcontext.database import migrate
from wealthcontext.policy import PolicyClient, PolicyDecision, PolicyUnavailable
from wealthcontext.settings import Settings
from wealthcontext.storage import migrate_business, read_snapshot
from wealthcontext.workbench import SESSION_ENDED
from wealthcontext.workbench_render import TEMPLATES, safe_markdown
from wealthcontext.workbench_sessions import AUTH_WORKBENCH_PATHS, COOKIE

NOW = datetime(2026, 11, 1, 17, tzinfo=UTC)
REVISION = "sha256:" + "a" * 64
ORIGIN = {"Origin": "http://localhost:8000"}
QUESTION = "What assets are included for the clean household at quarter-end?"


@pytest.fixture(scope="module")
def received(tmp_path_factory):
    directory = tmp_path_factory.mktemp("workbench-received")
    database = directory / "received.sqlite"
    migrate(database)
    migrate_business(database)
    bootstrap_history(database, directory / "feeds", FrozenClock(NOW))
    return database


@pytest.fixture
async def ui(tmp_path, monkeypatch, received):
    (tmp_path / "revision.txt").write_text(REVISION)
    state: dict[str, Any] = {"revoked": False, "outage": False, "factory_calls": []}

    async def revision(_self):
        return REVISION

    async def decide(_self, principal, action, resources, facts, revision, request_business_date):
        if state["outage"]:
            raise PolicyUnavailable("internal transport secret")
        covered = {fact["object"] for fact in facts
                   if fact["kind"] == "advisor_covers" and fact["subject"] == principal.id}
        roles = {
            "snapshot": {"advisor", "compliance"},
            "quality_preflight": {"ops", "compliance"}, "view_quality": {"ops", "compliance"},
            "feed_runs": {"ops", "compliance"}, "view_eval_reports": {"compliance"},
            "view_decision_log": {"compliance"}, "view_conflicts": {"advisor", "ops", "compliance"},
        }.get(action, {"advisor", "ops", "compliance", "engineer"})
        allowed = principal.role in roles and (
            not resources or principal.role in {"ops", "compliance"}
            or (principal.role == "advisor" and set(resources) <= covered
                and not state["revoked"])
        )
        return PolicyDecision(allow=allowed, allowed_ids=resources if allowed else [],
                              projection="no_pii" if principal.role == "ops" else "full",
                              reason="GRANTED" if allowed else "DENIED", policy_revision=REVISION,
                              entitlement_revision=revision)

    monkeypatch.setattr(PolicyClient, "verify_revision", revision)
    monkeypatch.setattr(PolicyClient, "decide", decide)
    monkeypatch.setattr("wealthcontext.app.bootstrap_history", lambda *_: None)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    shutil.copyfile(received, tmp_path / "wealthcontext.sqlite")

    def factory(provider):
        state["factory_calls"].append(provider)
        return state["adapter"]

    clock = FrozenClock(NOW)
    app = create_app(Settings(tmp_path, tmp_path, "http://unused.invalid"), clock,
                     provider_factory=factory)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://localhost:8000") as client:
            yield app, client, state, tmp_path / "wealthcontext.sqlite"


def csrf(response):
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match, response.text[:1000]
    return match.group(1)


async def enter(client, **changes):
    landing = await client.get("/")
    data = {"csrf": csrf(landing), "persona": "ADV-01", "as_of": "2026-09-30",
            "known_as_of": "2026-10-31T16:00:00Z", "mode": "replay", "provider": "openai",
            "household_id": "WC-HH-0001", **changes}
    selected = await client.post("/workbench/session", data=data, headers=ORIGIN)
    assert selected.status_code == 303, selected.text
    return await client.get("/workbench")


async def replay(client):
    page = await enter(client)
    response = await client.post("/workbench/answer", headers=ORIGIN,
                                 data={"csrf": csrf(page), "question": QUESTION})
    assert response.status_code == 303, response.text
    return await client.get("/workbench")


def claim_url(page):
    match = re.search(r'href="(/workbench/claim[^"]+)"', page.text)
    assert match, page.text[-5000:]
    return html.unescape(match.group(1))


@pytest.mark.anyio
async def test_landing_is_metadata_only_and_cookie_never_authenticates_api_or_mcp(ui):
    app, client, state, database = ui
    landing = await client.get("/")
    assert landing.status_code == 200
    assert "Enter workspace" in landing.text and "No API key" in landing.text
    assert "HttpOnly" in landing.headers["set-cookie"]
    assert "SameSite=strict" in landing.headers["set-cookie"]
    assert "no-store" in landing.headers["cache-control"]
    assert landing.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in landing.headers["content-security-policy"]
    icon = await client.get("/workbench/assets/favicon.svg")
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/svg+xml"
    with read_snapshot(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    await enter(client)
    for path in ("/api/get_household_snapshot", "/mcp"):
        rejected = await client.post(path, json={})
        assert rejected.status_code == 401
    assert not state["factory_calls"]
    assert app.state.workbench.sessions.get(client.cookies[COOKIE], NOW).token not in landing.text


@pytest.mark.anyio
async def test_demo_context_requires_exact_origin_csrf_loopback_and_known_persona(ui):
    _, client, _, _ = ui
    landing = await client.get("/")
    token = csrf(landing)
    for headers in ({}, {"Origin": "http://evil.example"},
                    {"Origin": "http://[broken"},
                    {**ORIGIN, "Sec-Fetch-Site": "cross-site"}):
        rejected = await client.post("/workbench/session", data={"csrf": token}, headers=headers)
        assert rejected.status_code == 403
    bad_csrf = await client.post("/workbench/session", data={"csrf": "x" * 43}, headers=ORIGIN)
    assert bad_csrf.status_code == 403
    for extra in ({"persona": "ROOT-01"}, {"principal_id": "COMP-01"},
                  {"known_as_of": "2026-10-31T16:00:00"}):
        rejected = await client.post("/workbench/session", data={"csrf": token, **extra},
                                     headers=ORIGIN)
        assert rejected.status_code == 400
    denied = await client.get("/", headers={"Host": "evil.example"})
    assert denied.status_code == 403
    assert (await client.get("/workbench")).status_code == 401


@pytest.mark.anyio
async def test_genuine_no_key_replay_has_fresh_claim_result_and_governed_provenance(ui):
    app, client, state, database = ui
    page = await replay(client)
    assert page.status_code == 200
    assert "Verified response" in page.text, page.text[-5000:]
    assert "gpt-6-luna" in page.text
    assert "USD 3000.00" in page.text
    assert not state["factory_calls"]
    session = app.state.workbench.sessions.get(client.cookies[COOKIE], NOW)
    assert session.run_metadata["first_pass_valid"] is True
    assert session.run_metadata["final_valid"] is True
    assert not session.run_metadata["fallback"] and not session.run_metadata["retries"]
    detail = await client.get(claim_url(page))
    assert detail.status_code == 200, detail.text
    assert "Source records" in detail.text and "source_record_id" in detail.text
    assert "Governed result envelope" in detail.text
    with read_snapshot(database) as connection:
        actions = {row[0] for row in connection.execute("SELECT action FROM decisions")}
        assert {"snapshot", "verify_claim", "conversation_context", "provenance"} <= actions
    recording = app.state.workbench.recording
    old_ids = {envelope["result_id"] for turn in recording.run.turns for envelope in turn.envelopes}
    assert all(result_id not in detail.text for result_id in old_ids)
    for secret in ("tool_calls", "terminal_attempts", "stop_reason", "request_id", "turns"):
        assert f'"{secret}"' not in html.unescape(page.text)
    forged = await client.get(claim_url(page) + "&reference=foreign_source")
    assert forged.status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize("change", [{"persona": "COMP-01"}, {"as_of": "2026-10-01"},
                                   {"known_as_of": "2026-10-30T16:00:00Z"},
                                   {"provider": "anthropic"}, {"household_id": "WC-HH-0002"}])
async def test_recording_never_relabels_different_context_as_a_match(ui, change):
    app, client, state, _ = ui
    page = await enter(client, **change)
    assert "This recording does not match" in page.text
    rejected = await client.post("/workbench/answer", headers=ORIGIN,
                                 data={"csrf": csrf(page), "question": QUESTION})
    assert rejected.status_code == 422
    assert not state["factory_calls"]
    assert app.state.workbench.sessions.get(client.cookies[COOKIE], NOW).conversation_id is None


@pytest.mark.anyio
async def test_replay_rejects_changed_question_and_answer_csrf(ui):
    _, client, state, _ = ui
    page = await enter(client)
    for question, headers, status in (("Another question", ORIGIN, 422), (QUESTION, {}, 403)):
        rejected = await client.post("/workbench/answer", headers=headers,
                                     data={"csrf": csrf(page), "question": question})
        assert rejected.status_code == status
    assert not state["factory_calls"]


@pytest.mark.anyio
async def test_context_rotation_and_revocation_never_redisclose_old_figures(ui):
    app, client, state, _ = ui
    page = await replay(client)
    link = claim_url(page)
    state["revoked"] = True
    denied = await client.get("/workbench")
    assert "USD 3000.00" not in denied.text
    assert "Access changed" in denied.text
    assert (await client.get(link)).status_code == 403
    old_id = client.cookies[COOKIE]
    next_page = await enter(client, persona="OPS-01")
    assert client.cookies[COOKIE] != old_id
    assert app.state.workbench.sessions.get(old_id, NOW) is None
    assert "USD 3000.00" not in next_page.text
    assert (await client.get(link)).status_code == 403


@pytest.mark.anyio
async def test_policy_outage_fails_entire_history_and_claim_view(ui):
    _, client, state, _ = ui
    page = await replay(client)
    state["outage"] = True
    for url in ("/workbench", claim_url(page)):
        view = await client.get(url)
        assert "USD 3000.00" not in view.text
        assert "internal transport secret" not in view.text
        assert "View unavailable" in view.text


@pytest.mark.anyio
async def test_inspector_reads_are_governed_and_compliance_summary_is_actual(ui):
    _, client, _, _ = ui
    await enter(client)
    for surface in ("feeds", "decisions", "eval", "quality"):
        view = await client.get(f"/workbench/inspector/{surface}")
        assert view.status_code == 200
        assert "NOT_AUTHORIZED" in view.text and "/workbench/evidence" not in view.text
    await enter(client, persona="COMP-01")
    report = await client.get("/workbench/inspector/eval")
    assert "Evaluation summary" in report.text and "COMPLETE" in report.text
    assert '"item_count": 0' in html.unescape(report.text)
    assert '"prompt"' not in html.unescape(report.text)


@pytest.mark.anyio
async def test_ops_received_feed_inspector_removes_names_and_titles(ui):
    _, client, _, _ = ui
    await enter(client, persona="OPS-01", as_of="2026-10-09")
    view = await client.get("/workbench/inspector/feeds")
    assert "Feed runs" in view.text and "TIMEOUT" in view.text
    assert "Synthetic Household" not in view.text
    assert '"records"' not in html.unescape(view.text)


@pytest.mark.anyio
async def test_live_render_escapes_question_and_never_displays_provider_candidates(ui):
    _, client, state, _ = ui
    state["adapter"] = ScriptedProvider()
    page = await enter(client, mode="live")
    payload = '<img src=x onerror=alert(1)><script>unsafe()</script>'
    await client.post("/workbench/answer", headers=ORIGIN,
                      data={"csrf": csrf(page), "question": payload})
    view = await client.get("/workbench")
    assert payload not in view.text
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in view.text
    assert "UNVERIFIED_PROVIDER_SENTINEL" not in view.text
    assert "INVALID_CANDIDATE_SENTINEL" not in view.text
    assert "SECRET_SDK_REQUEST" not in view.text
    assert state["factory_calls"] == ["openai"]


@pytest.mark.anyio
async def test_context_switch_during_live_run_cannot_restore_the_old_session(ui):
    app, client, state, _ = ui
    started, release = asyncio.Event(), asyncio.Event()

    class PausedProvider(ScriptedProvider):
        async def complete(self, request):
            if not self.requests:
                started.set()
                await release.wait()
            return await super().complete(request)

    state["adapter"] = PausedProvider()
    page = await enter(client, mode="live")
    old_id = client.cookies[COOKIE]
    pending = asyncio.create_task(client.post(
        "/workbench/answer", headers=ORIGIN,
        data={"csrf": csrf(page), "question": "Show the household assets."},
    ))
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        duplicate = await client.post("/workbench/answer", headers=ORIGIN,
                                      data={"csrf": csrf(page), "question": "Another question."})
        assert duplicate.status_code == 409
        await enter(client, persona="OPS-01")
    finally:
        release.set()
    assert (await pending).status_code == 303
    assert app.state.workbench.sessions.get(old_id, NOW) is None
    view = await client.get("/workbench")
    assert "USD 3000.00" not in view.text and "Verified response" not in view.text
    assert state["factory_calls"] == ["openai"]


@pytest.mark.anyio
async def test_named_presets_set_real_scenario_context_without_fabricating_replay(ui):
    app, client, state, _ = ui
    page = await enter(client)
    await client.post("/workbench/session", headers=ORIGIN,
                      data={"csrf": csrf(page), "apply_preset": "true", "preset": "missing"})
    session = app.state.workbench.sessions.get(client.cookies[COOKIE], NOW)
    # ADR 0009 "Presets": revamp step 2 moves this preset to ADV-03 and WC-HH-0003.
    assert session.context.persona == "ADV-03"
    assert session.context.as_of.isoformat() == "2026-10-15"
    assert session.context.household_id == "WC-HH-0003" and session.context.mode == "live"
    page = await client.get("/workbench")
    snapshot = await client.get("/workbench/inspector/snapshot")
    assert "INCOMPLETE" in snapshot.text and "SOURCE_UNAVAILABLE" in snapshot.text
    assert "PRICE_STALE" in snapshot.text
    assert not state["factory_calls"]

    await client.post("/workbench/session", headers=ORIGIN,
                      data={"csrf": csrf(page), "apply_preset": "true",
                            "preset": "reconciliation"})
    snapshot = await client.get("/workbench/inspector/snapshot")
    assert "QUANTITY_BREAK" in snapshot.text and "SOURCE_UNAVAILABLE" not in snapshot.text
    assert "CASH_BREAK" in snapshot.text


@pytest.mark.anyio
async def test_provider_availability_hides_credentials_and_recording_is_not_a_public_file(
    ui, monkeypatch,
):
    _, client, _, _ = ui
    secret = "sk-local-credential-sentinel-never-render"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    page = await enter(client)
    assert "Anthropic · configured" in page.text and secret not in page.text
    response = await client.get("/fixtures/replays/clean-household.openai.json")
    assert response.status_code == 401
    assert "turns" not in response.text


@pytest.mark.anyio
async def test_expired_demo_identity_is_audited_before_any_workbench_read(ui):
    app, client, state, database = ui
    await enter(client)
    session = app.state.workbench.sessions.get(client.cookies[COOKIE], NOW)
    session.token = app.state.identity.mint("ADV-01", NOW, expired=True)
    with read_snapshot(database) as connection:
        before = connection.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    response = await client.get("/workbench")
    # Build plan decision 4: an expired session gets the restyled error page, not JSON.
    assert response.status_code == 401 and "text/html" in response.headers["content-type"]
    assert "Session unavailable" in response.text and SESSION_ENDED in response.text
    assert '"error"' not in response.text and "UNAUTHENTICATED" not in response.text
    assert "no-store" in response.headers["cache-control"]
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["www-authenticate"] == "Bearer"
    assert not state["factory_calls"]
    with read_snapshot(database) as connection:
        latest = connection.execute("SELECT payload FROM results ORDER BY rowid DESC LIMIT 1")
        assert "UNAUTHENTICATED" in latest.fetchone()[0]
        assert connection.execute("SELECT COUNT(*) FROM results").fetchone()[0] == before + 1


@pytest.mark.anyio
async def test_every_workbench_page_without_a_session_gets_the_same_audited_error_page(ui):
    """Expired, missing and unknown sessions look the same on every authenticated
    workbench route, and each refusal is audited; the API keeps its JSON envelope."""
    app, client, _, database = ui
    await enter(client)
    session = app.state.workbench.sessions.get(client.cookies[COOKIE], NOW)
    session.token = app.state.identity.mint("ADV-01", NOW, expired=True)
    expired_cookie = client.cookies[COOKIE]
    pages = []
    for path in sorted(AUTH_WORKBENCH_PATHS):
        for cookie in (expired_cookie, None, "unknown-session"):
            client.cookies.clear()
            if cookie is not None:
                client.cookies.set(COOKIE, cookie)
            with read_snapshot(database) as connection:
                before = connection.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            response = await client.get(path)
            assert response.status_code == 401, (path, cookie, response.text[:200])
            assert "text/html" in response.headers["content-type"], path
            pages.append(response.text)
            with read_snapshot(database) as connection:
                assert connection.execute("SELECT COUNT(*) FROM results").fetchone()[0] \
                    == before + 1, path
    assert len(set(pages)) == 1 and SESSION_ENDED in pages[0]
    api = await client.post("/api/get_household_snapshot", json={})
    assert api.status_code == 401 and api.json()["error"]["code"] == "UNAUTHENTICATED"


def test_template_autoescaping_includes_untrusted_json_and_names():
    template = TEMPLATES.from_string('<pre>{{ payload|display_value }}</pre><p>{{ name }}</p>')
    attack = '</pre><script>alert("raw record")</script>'
    rendered = template.render(payload={"received": attack}, name=attack)
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
    assert "alert" in rendered


def test_markdown_subset_cannot_introduce_html_links_or_attributes():
    rendered = str(safe_markdown('**<img src=x onerror=x>**\n[x](javascript:alert(1))\n`<script>`'))
    assert "<img" not in rendered and "<script" not in rendered and "<a" not in rendered
    assert "<strong>&lt;img" in rendered
    assert "<code>&lt;script&gt;</code>" in rendered



def test_verifier_escaped_punctuation_displays_as_the_returned_identifiers():
    """SOL-C-R1-RENDER-001: citations shown (and copied) equal the returned identifiers."""
    fixture = Path(__file__).parents[1] / "fixtures/replays/clean-household.openai.json"
    recording = json.loads(fixture.read_text())
    data = recording["run"]["final_result"]["data"]
    rendered = str(safe_markdown(data["markdown"]))
    assert "\\" not in rendered
    for source in data["sources"]:
        assert f"Result {source['result_id']}; status {source['status']}; " \
               f"trace {source['trace_id']}" in rendered
    # Escaped punctuation is literal text: never markup, never raw HTML.
    literal = str(safe_markdown(r"\*\*not bold\*\* \`code\` \<b\> a\\b \- x"))
    assert literal == "<p>**not bold** `code` &lt;b&gt; a\\b - x</p>"
    assert str(safe_markdown(r"\> not a source line")) == "<p>&gt; not a source line</p>"
    assert str(safe_markdown("\x000\x00 kept")) == "<p>0 kept</p>"

@pytest.mark.anyio
async def test_non_ascii_csrf_is_controlled_on_every_mutation_route(ui):
    _, client, state, _ = ui
    await enter(client)
    for path, values in (("session", {}), ("answer", {"question": QUESTION})):
        response = await client.post(f"/workbench/{path}", headers=ORIGIN,
                                     data={"csrf": "é" * 32, **values})
        assert response.status_code in {400, 403}
        assert "no-store" in response.headers["cache-control"]
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert not state["factory_calls"]


@pytest.mark.anyio
async def test_invalid_raw_host_is_rejected_before_ui_reads_and_audited(ui):
    _, client, _, database = ui
    await enter(client)
    for host in (None, "[::1", "localhost#suffix", "localhost/path", "localhost:badport",
                 "localhost:", "localhost:65536", "localhost?query"):
        for path, expected in (("/", 403), ("/workbench", 401)):
            request = client.build_request("GET", path)
            if host is None:
                del request.headers["host"]
            else:
                request.headers["host"] = host
            response = await client.send(request)
            assert response.status_code == expected, (host, path, response.text[:300])
            assert "no-store" in response.headers["cache-control"]
        with read_snapshot(database) as connection:
            row = connection.execute("SELECT payload FROM results ORDER BY rowid DESC LIMIT 1")
            assert "UNAUTHENTICATED" in row.fetchone()[0]


@pytest.mark.anyio
async def test_absent_host_and_empty_authority_origin_cannot_select_persona(ui):
    app, client, _, _ = ui
    page = await client.get("/")
    request = client.build_request("POST", "/workbench/session",
                                   headers={"Origin": "http://"}, data={"csrf": csrf(page)})
    del request.headers["host"]
    response = await client.send(request)
    assert response.status_code == 403
    assert app.state.workbench.sessions.get(client.cookies[COOKIE], NOW).token is None


@pytest.mark.anyio
async def test_live_structured_question_limit_is_a_form_error_before_provider(ui):
    app, client, state, _ = ui
    page = await enter(client, mode="live")
    session = app.state.workbench.sessions.get(client.cookies[COOKIE], NOW)
    response = await client.post("/workbench/answer", headers=ORIGIN,
                                 data={"csrf": csrf(page), "question": '"' * 12000})
    assert response.status_code == 400
    assert "bounded question" in response.text
    assert "Replay contract" not in response.text
    assert not session.busy and not session.run_metadata and session.conversation_id is None
    assert not state["factory_calls"]


@pytest.mark.anyio
async def test_warning_answer_has_visible_callouts_predicate_and_no_null_label(ui):
    from wealthcontext.harness import current_results
    from wealthcontext.provider_contracts import ProviderTurn, ToolCall
    from wealthcontext.verifier import deterministic_fallback

    class WarningProvider(ScriptedProvider):
        async def complete(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                call = ToolCall(id="fetch", name="get_household_snapshot", arguments={
                    "household_id": "WC-HH-0013", "as_of": "2026-10-01",
                    "known_as_of": "2026-10-02T12:00:00Z",
                })
            else:
                candidate = deterministic_fallback(current_results(
                    {"data": {"messages": request.messages}},
                ))
                call = ToolCall(id="answer", name="submit_answer",
                                arguments=candidate.model_dump(mode="json"))
            return ProviderTurn(calls=[call], model=self.model)

    app, client, state, _ = ui
    state["adapter"] = WarningProvider()
    page = await enter(client, mode="live", persona="ADV-03", household_id="WC-HH-0013",
                       as_of="2026-10-01", known_as_of="2026-10-02T12:00:00Z")
    response = await client.post("/workbench/answer", headers=ORIGIN,
                                 data={"csrf": csrf(page), "question": "Explain the discrepancy."})
    assert response.status_code == 303
    page = await client.get("/workbench")
    session = app.state.workbench.sessions.get(client.cookies[COOKIE], NOW)
    assert session.run_metadata["first_pass_valid"] is True
    assert session.run_metadata["final_valid"] is True
    callouts = re.findall(r'<aside class="answer-warning"[^>]*>(.*?)</aside>', page.text, re.S)
    assert len(callouts) == 2
    # Each callout names its flag with its subject (brief guardrail 3; step 7's refresh).
    assert any("QUANTITY_BREAK" in item and "WC-AC-0030:WC-SEC-0001" in item
               for item in callouts), callouts
    assert any("CASH_BREAK" in item and "WC-AC-0030:USD" in item for item in callouts), callouts
    assert '<span>complete</span>' in page.text  # the predicate's wording
    assert '<span class="claim-meta">None' not in page.text
    assert not re.search(r'<p class="source-line">Warning:', page.text)
    assert state["factory_calls"] == ["openai"]


@pytest.mark.anyio
async def test_live_question_displays_original_text_and_separate_submitted_context(ui):
    _, client, state, _ = ui
    state["adapter"] = ScriptedProvider()
    page = await enter(client, mode="live")
    question = "Original user question <script>unsafe()</script> " + "a" * 300
    await client.post("/workbench/answer", headers=ORIGIN,
                      data={"csrf": csrf(page), "question": question})
    page = await client.get("/workbench")
    paragraph = re.search(r'<p class="question-text">(.*?)</p>', page.text, re.S)
    assert paragraph and html.unescape(paragraph.group(1)) == question
    assert "Submitted request context" in page.text
    assert '<script>unsafe()</script>' not in page.text
