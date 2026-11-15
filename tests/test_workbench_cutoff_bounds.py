"""Workbench cutoffs reject UTC overflow before session mutation or provider calls."""

import re
from datetime import UTC, datetime

import httpx2
import pytest

from tests.test_workbench import NOW, ORIGIN, csrf
from tests.test_workbench import received as received
from tests.test_workbench import ui as ui
from wealthcontext.app import create_app
from wealthcontext.clock import FrozenClock
from wealthcontext.settings import Settings
from wealthcontext.storage import read_snapshot
from wealthcontext.workbench_sessions import COOKIE, WorkbenchContext

INVALID_CUTOFFS = ["0001-01-01T00:00:00+00:01", "9999-12-31T23:59:59-00:01"]
VALID_CUTOFFS = [
    "0001-01-01T00:01:00+00:01",
    "9999-12-31T23:58:59-00:01",
    "2026-10-31T12:00:00.123456-04:00",
    "2026-10-31T16:00:00Z",
]


@pytest.mark.anyio
@pytest.mark.parametrize("cutoff", INVALID_CUTOFFS)
@pytest.mark.parametrize("mode", ["replay", "live"])
async def test_invalid_workbench_cutoff_is_form_error_before_session_or_provider(ui, cutoff, mode):
    app, client, state, database = ui
    landing = await client.get("/")
    original_cookie = client.cookies[COOKIE]
    session = app.state.workbench.sessions.get(original_cookie, NOW)
    original_context = session.context.model_dump()
    session_count = len(app.state.workbench.sessions._sessions)
    response = await client.post("/workbench/session", headers=ORIGIN, data={
        "csrf": csrf(landing), "known_as_of": cutoff, "mode": mode,
    })
    assert response.status_code == 400
    assert "Invalid demo context" in response.text
    assert client.cookies[COOKIE] == original_cookie
    assert app.state.workbench.sessions.get(original_cookie, NOW) is session
    assert session.context.model_dump() == original_context
    assert len(app.state.workbench.sessions._sessions) == session_count
    assert session.token is None
    assert state["factory_calls"] == []
    assert (await client.get("/")).status_code == 200
    with read_snapshot(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("cutoff", VALID_CUTOFFS)
async def test_valid_workbench_cutoff_preserves_offset_and_precision(ui, cutoff):
    app, client, state, _ = ui
    landing = await client.get("/")
    response = await client.post("/workbench/session", headers=ORIGIN, data={
        "csrf": csrf(landing), "known_as_of": cutoff,
    })
    assert response.status_code == 303
    session = app.state.workbench.sessions.get(client.cookies[COOKIE], NOW)
    assert session.context.known_as_of.isoformat() == datetime.fromisoformat(cutoff).isoformat()
    page = await client.get("/workbench")
    assert page.status_code == 200
    rendered = re.search(r'name="known_as_of" value="([^"]+)"', page.text)
    assert rendered is not None
    expected = datetime.fromisoformat(cutoff).astimezone(UTC)
    assert rendered.group(1) == expected.isoformat().replace("+00:00", "Z")
    controls = session.context.model_dump(mode="json", exclude={"known_as_of"})
    submitted = await client.post("/workbench/session", headers=ORIGIN, data={
        **controls, "csrf": csrf(page), "known_as_of": rendered.group(1),
    })
    assert submitted.status_code == 303
    refreshed = app.state.workbench.sessions.get(client.cookies[COOKIE], NOW)
    assert refreshed.context.known_as_of == expected
    assert refreshed.context.model_dump(mode="json", exclude={"known_as_of"}) == controls
    assert app.state.workbench.replay_matches(refreshed) == (cutoff == VALID_CUTOFFS[-1])
    assert (await client.get("/workbench")).status_code == 200
    assert state["factory_calls"] == []


@pytest.mark.anyio
async def test_legacy_invalid_context_is_not_loaded_across_application_restart(ui):
    app, _, state, database = ui
    # Recreate a context admitted by the earlier AwareDatetime-only release.
    legacy = WorkbenchContext.model_construct(
        known_as_of=datetime.fromisoformat(INVALID_CUTOFFS[0]),
    )
    session = app.state.workbench.sessions.create(
        NOW, legacy, app.state.identity.mint("ADV-01", NOW),
    )

    def no_provider(_):
        pytest.fail("Restart/context recovery must not construct a provider")

    replacement = create_app(
        Settings(database.parent, database.parent, "http://unused.invalid"),
        FrozenClock(NOW), provider_factory=no_provider,
    )
    async with replacement.router.lifespan_context(replacement), httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=replacement), base_url="http://localhost:8000",
    ) as client:
        client.cookies.set(COOKIE, session.id, domain="localhost.local", path="/")
        assert replacement.state.workbench.sessions.get(session.id, NOW) is None
        assert (await client.get("/workbench")).status_code == 401
        landing = await client.get("/")
        assert landing.status_code == 200
        assert client.cookies[COOKIE] != session.id
        response = await client.post("/workbench/session", headers=ORIGIN,
                                     data={"csrf": csrf(landing)})
        assert response.status_code == 303
        assert (await client.get("/workbench")).status_code == 200
    assert state["factory_calls"] == []
