"""Build plan step 3, A3 Ask and verify: the verifier's first-pass, retry and fallback
paths, a run with no verified answer, and the tool trail with its tombstones. Rendering
only; the harness prompt is unchanged (decision 2), which tests/test_workbench_guards.py
holds."""
# ruff: noqa: F811  (pytest fixtures imported from the workbench module are parameters)

import html
import re

import httpx2
import pytest

from tests.test_chat_api import CANDIDATE_SECRET, PROVIDER_SECRET, ScriptedProvider
from tests.test_workbench import NOW, ORIGIN, csrf, enter, received, replay, ui  # noqa: F401
from wealthcontext.conversation import TOMBSTONE
from wealthcontext.provider_contracts import ProviderFailure
from wealthcontext.workbench_sessions import COOKIE


def card_badges(page) -> list[list[str]]:
    """The badge words on each answer card's header, in order."""
    heads = re.findall(r'<article class="card answer-card"><header class="card-head">(.*?)'
                       r"</header>", page.text, re.S)
    return [[html.unescape(re.sub(r"<[^>]+>", "", item)).strip()
             for item in re.findall(r'<span class="badge[^"]*">(.*?)</span>', head, re.S)]
            for head in heads]


def trail(page) -> list[tuple[str, str]]:
    """Each tool call in the conversation with the word on its outcome badge."""
    return [(name, html.unescape(re.sub(r"<[^>]+>", "", badge)).strip())
            for name, badge in re.findall(
                r'<li><span class="mono">([a-z_]+)</span>(<span class="badge.*?</span>)</li>',
                page.text, re.S)]


async def ask(client, state, adapter) -> httpx2.Response:
    state["adapter"] = adapter
    page = await enter(client, mode="live")
    response = await client.post("/workbench/answer", headers=ORIGIN,
                                 data={"csrf": csrf(page), "question": "Household assets?"})
    assert response.status_code == 303, response.text
    return await client.get("/workbench")


@pytest.mark.anyio
async def test_a_first_pass_answer_says_so_and_shows_how_it_was_checked(ui):
    _, client, state, _ = ui
    page = await ask(client, state, ScriptedProvider())
    assert card_badges(page) == [["Verified response", "First pass"]]
    assert "How this answer was checked" in page.text
    assert "passed verification, with no retry and no fallback" in page.text
    assert trail(page) == [("get_household_snapshot", "Complete"), ("submit_answer", "Verified")]
    assert PROVIDER_SECRET not in page.text and CANDIDATE_SECRET not in page.text


@pytest.mark.anyio
async def test_a_retried_answer_shows_the_rejected_submission_and_the_retry(ui):
    app, client, state, _ = ui
    page = await ask(client, state, ScriptedProvider(invalid_attempts=1))
    session = app.state.workbench.sessions.get(client.cookies[COOKIE], NOW)
    assert session is not None
    assert session.run_metadata["first_pass_valid"] is False
    assert session.run_metadata["retries"] == 1 and not session.run_metadata["fallback"]
    assert card_badges(page) == [["Verified response", "Verified after one retry"]]
    assert trail(page) == [("get_household_snapshot", "Complete"),
                           ("submit_answer", "Rejected by the verifier"),
                           ("submit_answer", "Verified")]
    # The rejected candidate's prose is a diagnostic, never shown.
    assert CANDIDATE_SECRET not in page.text and PROVIDER_SECRET not in page.text
    assert "USD 3000.00" in page.text


@pytest.mark.anyio
async def test_a_fallback_answer_is_labelled_as_the_platforms_own_and_verified(ui):
    app, client, state, _ = ui
    page = await ask(client, state, ScriptedProvider(invalid_attempts=2))
    session = app.state.workbench.sessions.get(client.cookies[COOKIE], NOW)
    assert session is not None and session.run_metadata["fallback"] is True
    assert session.run_metadata["final_valid"] is True
    assert card_badges(page) == [["Verified response", "Deterministic fallback"]]
    assert "submitted its own conservative answer" in page.text
    assert trail(page)[-3:] == [("submit_answer", "Rejected by the verifier"),
                                ("submit_answer", "Rejected by the verifier"),
                                ("submit_answer", "Verified")]
    assert CANDIDATE_SECRET not in page.text


@pytest.mark.anyio
async def test_a_provider_failure_falls_back_and_a_run_without_an_answer_shows_none(ui):
    _, client, state, _ = ui

    class Unavailable(ScriptedProvider):
        async def complete(self, request):
            self.requests.append(request)
            raise ProviderFailure("PROVIDER_CALL_FAILED")

    # A provider failure ends the model's turns; the platform's fallback still answers,
    # from no result at all, and says so.
    page = await ask(client, state, Unavailable())
    assert card_badges(page) == [["Verified response", "Deterministic fallback"]]
    assert trail(page) == [("submit_answer", "Verified")]
    # A run that cannot start leaves no answer: the page says so and shows no model text.
    page = await ask(client, state, object())
    assert "No verified answer" in page.text and "INTERNAL" in page.text
    assert card_badges(page) == []
    assert "Verified response" not in page.text


@pytest.mark.anyio
async def test_a_revoked_conversation_shows_paired_tombstones_in_the_tool_trail(ui):
    _, client, state, _ = ui
    page = await replay(client)
    assert ("get_household_snapshot", "Complete") in trail(page)
    state["revoked"] = True
    page = await client.get("/workbench")
    assert card_badges(page) == [["Access changed"]]
    calls = trail(page)
    assert calls and all(word == "Redacted: not authorized" for _, word in calls), calls
    assert TOMBSTONE in page.text and "3000.00" not in page.text
    assert "How this answer was checked" not in page.text
