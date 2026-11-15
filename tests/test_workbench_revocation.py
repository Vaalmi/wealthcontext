"""Build plan step 3, A7 seeded revocation: tombstones render; the follow-up abstains
without retrieval and without a key; stores are capped and cleaned up; the main demo
database is unchanged. Also the brief's revocation item: tombstoned history, old evidence
links and old provenance handles.

The stub policy here applies the matrix's roles and the received coverage, as in
tests/test_workbench_advisor.py; tests/test_p4_security_matrix.py runs A7 against the
real policy. The follow-up replays the genuine recording in fixtures/replays, as the
clean-household tests replay theirs: no provider is constructed and no key is used."""
# ruff: noqa: F811  (pytest fixtures imported from other test modules are parameters)

import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml

from tests.test_workbench import NOW, ORIGIN, csrf, received  # noqa: F401
from tests.test_workbench_advisor import advisor_ui, apply, found, main, preset  # noqa: F401
from wealthcontext.conversation import TOMBSTONE
from wealthcontext.replay import load_recording
from wealthcontext.storage import read_snapshot
from wealthcontext.workbench import DENIAL, FOLLOWUP_LIMITS, load_followup
from wealthcontext.workbench_revocation import (
    SEEDED_ARGUMENTS,
    SEEDED_PRINCIPAL,
    Seed,
    SeededStore,
    SeededStores,
    SeededStoreUnavailable,
)
from wealthcontext.workbench_sessions import COOKIE, DemoSessions

ROOT = Path(__file__).resolve().parents[1]
FOLLOWUP = ROOT / "fixtures/replays/revocation-followup.anthropic.json"
# The genuine recorded follow-up's answer (Claude, Q02 trial 1 of the 2026-11-09 scored run).
ABSTENTION = ("Household assets cannot be stated from the current authorized evidence. The "
              "earlier household result is withheld and is not reused after the household "
              "reassignment; a fresh authorized retrieval is required.")
QUESTION = "Reuse a previous result after household reassignment"
# Model tool actions a follow-up would need to retrieve anything (policies/matrix.yaml).
RETRIEVAL = {"snapshot", "portfolio_exposure", "concentrated_positions", "concentration_book",
             "search_preflight", "search_households", "provenance_preflight", "provenance",
             "metric_definition", "quality_preflight", "view_quality"}


def digest_database(path: Path) -> str:
    """Every row of every table, so any write to the main demo database shows."""
    connection = sqlite3.connect(path)
    try:
        lines = []
        for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                lines.append(json.dumps([table, *row], default=str))
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()
    finally:
        connection.close()


def issued(database: Path) -> list[str]:
    """The originating action of every result the store has issued, oldest first. A
    retrieval issues a result under its data action; reauthorizing an earlier result to
    tombstone it takes decisions but issues none."""
    with read_snapshot(database) as connection:
        return [row[0] for row in connection.execute(
            "SELECT a.action FROM results r JOIN result_access a ON a.result_id=r.id "
            "ORDER BY r.rowid")]


def test_the_seeded_principal_household_and_cutoffs_are_q02s():
    case = yaml.safe_load((ROOT / "acceptance/Q02.yaml").read_text())
    assert (SEEDED_PRINCIPAL.id, SEEDED_PRINCIPAL.role) == (case["principal"]["id"],
                                                            case["principal"]["role"])
    assert SEEDED_ARGUMENTS == {"household_id": case["household_ids"][0],
                                "as_of": case["valuation_date"],
                                "known_as_of": "2026-10-31T16:00:00+00:00"}
    assert case["known_as_of"] == "2026-10-31T16:00:00Z" and case["no_new_retrieval"] is True
    assert FOLLOWUP_LIMITS.max_data_calls == 0


def test_the_follow_up_recording_is_genuine_and_saw_only_tombstones():
    recording = load_recording(FOLLOWUP)
    run = recording.run
    assert recording.origin == "recorded_live_provider" and run.mode == "live_scored"
    assert (run.provider, run.model) == ("anthropic", "claude-opus-5-5")
    assert run.prompt_sha256 == hashlib.sha256(
        (ROOT / "prompts/harness_system.md").read_bytes()).hexdigest()
    assert run.first_pass_valid and not run.retries and not run.fallback and not run.failure
    assert run.data_tool_calls == 0 and len(run.turns) == 1
    opening: dict[str, Any] = run.turns[0].request.model_dump(mode="json")
    assert opening["authorized_result_ids"] == [] and opening["terminal_only"]
    assert [tool["name"] for tool in opening["tools"]] == ["submit_answer"]
    messages: list[dict[str, Any]] = opening["messages"]
    assert [message["content"] for message in messages
            if message["role"] == "tool"] == [TOMBSTONE, TOMBSTONE]
    assert [message["content"] for message in messages
            if message["role"] == "assistant" and "content" in message] == [TOMBSTONE]
    assert all(call["arguments"] == {} for message in messages
               for call in message.get("tool_calls", []))
    final: dict[str, Any] = json.loads(json.dumps(run.final_result))
    assert final["data"]["markdown"] == ABSTENTION
    assert final["data"]["claims"] == [] == final["data"]["predicates"]
    _, question = load_followup(FOLLOWUP)
    assert question.splitlines()[0] == QUESTION


@pytest.mark.anyio
async def test_seeded_revocation_tombstones_abstention_old_links_and_untouched_demo_database(
    advisor_ui,
):
    app, client, state, database = advisor_ui
    assert app.state.workbench.followup is not None, "the genuine recording is installed"
    # Entry: a link from the "Current access" scenario's denial page, with no preset.
    entered = await preset(client, "access")
    denial = await client.get(entered.headers["location"])
    assert denial.status_code == 403 and 'href="/workbench/revocation"' in denial.text
    before = digest_database(database)
    page = await client.get("/workbench/revocation")
    assert page.status_code == 200, page.text[-2000:]
    session_id = client.cookies[COOKIE]
    store = app.state.workbench.seeded.get(session_id)
    assert store is not None and store.database.parent != database.parent
    with read_snapshot(store.database) as connection:
        earlier = json.loads(connection.execute(
            "SELECT payload FROM results WHERE id=?", (store.seed.earlier_result_id,),
        ).fetchone()[0])
    assets = earlier["data"]["assets_usd"]

    # Tombstoned history: the earlier answer and both of its tool calls, and no figure.
    body = main(page)
    assert "Access changed" in body and TOMBSTONE in body
    assert body.count("Redacted: not authorized") == 2
    assert assets not in page.text and "Synthetic household 04" not in page.text
    assert "Earlier, before the reassignment" in body and "Verified response" not in body

    # The follow-up: replayed with no key, offered no data tool, and it abstains.
    taken = issued(store.database)
    answered = await client.post("/workbench/revocation/answer", headers=ORIGIN,
                                 data={"csrf": csrf(page)})
    assert answered.status_code == 303, answered.text
    page = await client.get("/workbench/revocation")
    body = main(page)
    assert "Verified response" in body and "Abstained · no new retrieval" in body
    assert "First pass" in body and ABSTENTION in body
    assert store.followup is not None and store.followup["data_tool_calls"] == 0
    assert store.followup["mode"] == "replay" and store.followup["final_valid"] is True
    new = issued(store.database)[len(taken):]
    assert new and not set(new) & RETRIEVAL, new  # no new retrieval
    assert assets not in page.text and body.count("Redacted: not authorized") == 2
    # It runs once per seeded store.
    again = await client.post("/workbench/revocation/answer", headers=ORIGIN,
                              data={"csrf": csrf(page)})
    assert again.status_code == 409
    rejected = await client.post("/workbench/revocation/answer", headers={},
                                 data={"csrf": csrf(page)})
    assert rejected.status_code == 403

    # Old provenance handles and the evidence links issued with the earlier result: each
    # is reauthorized in the seeded store and refused exactly as one never issued.
    links = [found(r'href="(/workbench/revocation/evidence[^"]+)"', page.text).group(1)]
    assert len(store.seed.earlier_handles) >= 1

    def provenance_calls() -> int:
        with read_snapshot(store.database) as connection:
            return connection.execute("SELECT COUNT(*) FROM decisions "
                                      "WHERE action='provenance_preflight'").fetchone()[0]

    calls = provenance_calls()
    refused = await client.get(links[0].replace("&amp;", "&"))
    # The earlier pair reached the governed provenance call, which refused it.
    assert provenance_calls() == calls + 1
    unknown = await client.get("/workbench/revocation/evidence?result=r_unknown&source=h_unknown")
    malformed = await client.get("/workbench/revocation/evidence?result=x")
    # A pair this store never issued is refused before anything is read.
    assert provenance_calls() == calls + 1
    assert refused.status_code == unknown.status_code == malformed.status_code == 403
    assert refused.text == unknown.text == malformed.text and DENIAL in refused.text
    assert "source_record_id" not in refused.text and assets not in refused.text

    # Nothing in this sequence wrote to the main demo database.
    assert digest_database(database) == before

    # A new context ends the session, and its store with it; the old links stay refused.
    path = store.database
    await apply(client, persona="ADV-03")
    assert app.state.workbench.seeded.get(session_id) is None and not path.exists()
    assert not Path(str(path) + "-wal").exists()
    assert (await client.get(links[0].replace("&amp;", "&"))).text == unknown.text
    assert state["calls"], "the stub policy decided every request"


@pytest.mark.anyio
async def test_only_the_seeded_persona_gets_a_store_and_others_get_a_notice(advisor_ui):
    app, client, _, database = advisor_ui
    before = digest_database(database)
    for persona in ("ADV-01", "OPS-01", "COMP-01", "ENG-01"):
        await apply(client, persona=persona)
        page = await client.get("/workbench/revocation")
        assert page.status_code == 200
        assert "This seeded conversation is ADV-03" in page.text
        assert TOMBSTONE not in page.text and "/workbench/revocation/evidence" not in page.text
        assert len(app.state.workbench.seeded) == 0
        answered = await client.post("/workbench/revocation/answer", headers=ORIGIN,
                                     data={"csrf": csrf(page)})
        assert answered.status_code == 303
        assert answered.headers["location"] == "/workbench/revocation"
        refused = await client.get("/workbench/revocation/evidence?result=r_x&source=h_x")
        assert refused.status_code == 403 and DENIAL in refused.text
    assert digest_database(database) == before
    # The link appears on the "Current access" denial only, as ADV-03.
    await apply(client, persona="ADV-03", household_id="WC-HH-0004")
    page = await client.get("/workbench/household")
    assert page.status_code == 403 and "/workbench/revocation" not in page.text


@pytest.mark.anyio
async def test_without_the_recording_the_tombstones_show_and_no_follow_up_runs(advisor_ui):
    app, client, _, _ = advisor_ui
    app.state.workbench.followup = None
    await preset(client, "access")
    page = await client.get("/workbench/revocation")
    assert TOMBSTONE in page.text and "The recorded follow-up is not installed" in page.text
    assert "disabled" in found(r'<button type="submit" class="button primary"[^>]*>',
                               page.text).group(0)
    answered = await client.post("/workbench/revocation/answer", headers=ORIGIN,
                                 data={"csrf": csrf(page)})
    assert answered.status_code == 422


def fake_stage(directory: Path, *_args) -> Seed:
    database = directory / "template.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE marker (value TEXT)")
    connection.execute("INSERT INTO marker VALUES ('seeded')")
    connection.commit()
    connection.close()
    return Seed(database, "conv_seeded", "r_earlier", ("h_earlier",), "r_answer")


@pytest.mark.anyio
async def test_stores_are_capped_least_recently_used_first_and_deleted_on_release(tmp_path):
    staged = []

    def stage(directory, *args):
        staged.append(directory)
        return fake_stage(directory, *args)

    stores = SeededStores(limit=2, root=tmp_path, stage=stage)
    first = await stores.open("one", None, None, None)  # type: ignore[arg-type]
    second = await stores.open("two", None, None, None)  # type: ignore[arg-type]
    assert await stores.open("one", None, None, None) is first  # type: ignore[arg-type]
    third = await stores.open("three", None, None, None)  # type: ignore[arg-type]
    assert len(staged) == 1, "the template is staged once and copied per session"
    assert len(stores) == 2 and stores.get("two") is None and not second.database.exists()
    assert stores.get("one") is first and stores.get("three") is third
    for store in (first, third):
        connection = sqlite3.connect(store.database)
        assert connection.execute("SELECT value FROM marker").fetchone() == ("seeded",)
        connection.close()
        assert store.database.parent == stores.directory != tmp_path
    assert first.held("r_earlier", "h_earlier") and not first.held("r_earlier", "h_other")
    assert not first.held("r_answer", "h_earlier")
    stores.release("one")
    assert stores.get("one") is None and not first.database.exists()
    directory = stores.directory
    stores.close()
    assert len(stores) == 0 and not third.database.exists() and not directory.exists()
    with pytest.raises(ValueError):
        SeededStores(limit=0)


@pytest.mark.anyio
async def test_a_failed_staging_leaves_nothing_and_the_next_request_stages_again(tmp_path):
    """SOL-UX1-R1-A7-001: a transient failure (a policy outage raises
    REVOCATION_BASELINE_UNAVAILABLE) must not disable A7 until the process restarts."""
    attempts: list[Path] = []

    def stage(directory, *args):
        attempts.append(directory)
        if len(attempts) == 1:
            (directory / "partial.sqlite").write_text("half-staged")
            raise ValueError("REVOCATION_BASELINE_UNAVAILABLE")
        return fake_stage(directory, *args)

    stores = SeededStores(limit=2, root=tmp_path, stage=stage)
    with pytest.raises(ValueError, match="REVOCATION_BASELINE_UNAVAILABLE"):
        await stores.open("one", None, None, None)  # type: ignore[arg-type]
    assert len(stores) == 0 and not attempts[0].exists(), "the failed attempt is removed"
    store = await stores.open("one", None, None, None)  # type: ignore[arg-type]
    assert len(attempts) == 2 and attempts[1] != attempts[0] and attempts[1].exists()
    assert store.database.exists() and stores.get("one") is store
    assert await stores.open("two", None, None, None) is not store  # type: ignore[arg-type]
    assert len(attempts) == 2, "once staged, the template is reused"
    stores.close()


@pytest.mark.anyio
async def test_a_store_in_use_is_deleted_when_its_last_lease_ends(tmp_path):
    """SOL-UX1-R1-A7-002: a release or an eviction while a request still uses the store
    takes it out of use at once and deletes its files when the request ends, so a write
    the request makes meanwhile cannot leave a file behind."""
    stores = SeededStores(limit=1, root=tmp_path, stage=fake_stage)
    first = await stores.open("one", None, None, None)  # type: ignore[arg-type]
    with stores.lease(first):
        stores.release("one")
        assert stores.get("one") is None and first.database.exists() and first.released
        connection = sqlite3.connect(first.database)  # the request's late write
        connection.execute("INSERT INTO marker VALUES ('late')")
        connection.commit()
        connection.close()
    assert not first.database.exists() and first.leases == 0
    second = await stores.open("two", None, None, None)  # type: ignore[arg-type]
    with stores.lease(second), stores.lease(second):
        # SOL-UX1-R3-CAP-001: a store in use is never evicted to make room.
        with pytest.raises(SeededStoreUnavailable):
            await stores.open("three", None, None, None)  # type: ignore[arg-type]
        assert stores.get("two") is second and stores.copies == 1
    third = await stores.open("three", None, None, None)  # type: ignore[arg-type]
    assert stores.get("two") is None and not second.database.exists()
    assert third.database.exists()
    with stores.lease(third):  # an unreleased store survives its leases
        pass
    assert third.database.exists()
    stores.close()
    assert not third.database.exists()


@pytest.mark.anyio
async def test_closing_waits_for_every_lease_including_one_ended_by_an_exception(tmp_path):
    """SOL-UX1-R1-A7-002 (round 2): close() must not remove a store's folder while a
    request still holds the store, whether the request ends normally or by an error."""
    stores = SeededStores(limit=2, root=tmp_path, stage=fake_stage)
    first = await stores.open("one", None, None, None)  # type: ignore[arg-type]
    second = await stores.open("two", None, None, None)  # type: ignore[arg-type]
    directory = stores.directory
    with stores.lease(first):
        with pytest.raises(RuntimeError), stores.lease(second):
            stores.close()
            assert len(stores) == 0 and first.released and second.released
            assert directory.exists() and second.database.exists()
            raise RuntimeError("the request failed while the store was held")
        assert not second.database.exists(), "the store ended by an error is deleted"
        assert directory.exists() and first.database.exists(), "still held"
        connection = sqlite3.connect(first.database)  # the request's late write
        connection.execute("INSERT INTO marker VALUES ('late')")
        connection.commit()
        connection.close()
    assert not first.database.exists() and not directory.exists()
    # A later open stages again in a new folder.
    again = await stores.open("three", None, None, None)  # type: ignore[arg-type]
    assert again.database.exists() and stores.directory != directory
    stores.close()
    assert not again.database.parent.exists()


@pytest.mark.anyio
async def test_a_failed_copy_leaves_no_file_and_the_next_request_copies_again(
        tmp_path, monkeypatch):
    """SOL-UX1-R2-COPY-001: a copy that fails after creating its target (a full disk,
    say) must not leave an unregistered database that nothing would delete."""
    import wealthcontext.workbench_revocation as module

    real = module._copy
    failures = []

    def copy(source: Path, target: Path) -> None:
        if not failures:
            failures.append(target)
            connection = sqlite3.connect(target)  # the target exists, with a journal
            connection.execute("CREATE TABLE partial (value TEXT)")
            connection.commit()
            connection.close()
            Path(str(target) + "-journal").write_text("partial")
            raise sqlite3.OperationalError("database or disk is full")
        real(source, target)

    monkeypatch.setattr(module, "_copy", copy)
    stores = SeededStores(limit=2, root=tmp_path, stage=fake_stage)
    with pytest.raises(sqlite3.OperationalError):
        await stores.open("one", None, None, None)  # type: ignore[arg-type]
    assert len(stores) == 0 and failures
    assert not any(stores.directory.glob("session-*")), "no orphaned copy or sidecar"
    store = await stores.open("one", None, None, None)  # type: ignore[arg-type]
    assert stores.get("one") is store and store.database.exists()
    assert [path.name for path in stores.directory.glob("session-*")] == [store.database.name]
    stores.close()


@pytest.mark.anyio
async def test_when_every_copy_is_in_use_the_page_says_so_and_reads_nothing(advisor_ui):
    """SOL-UX1-R3-CAP-001: at the cap with every copy held, A7 answers 503 and opens no
    store; the main demo database is untouched."""
    app, client, _, database = advisor_ui
    before = digest_database(database)
    seeded = app.state.workbench.seeded
    seeded.limit = 1
    held = SeededStore(Path("/nonexistent/held.sqlite"), None, None, None)  # type: ignore[arg-type]
    held.leases = 1
    seeded._stores["another-session"] = held
    await apply(client, persona="ADV-03")
    page = await client.get("/workbench/revocation")
    assert page.status_code == 503 and "Every seeded revocation session is in use" in page.text
    assert TOMBSTONE not in page.text and seeded.get(client.cookies[COOKIE]) is None
    assert list(seeded._stores) == ["another-session"]
    assert digest_database(database) == before


def session_files(root: Path) -> list[Path]:
    return sorted(root.glob("wealthcontext-a7-*/session-*.sqlite"))


@pytest.mark.anyio
async def test_the_cap_counts_every_copy_still_held_and_evicts_only_unheld_stores(tmp_path):
    """SOL-UX1-R3-CAP-001: a store released while a request holds it keeps its file until
    the request ends, so it counts against the cap; an open at the cap evicts the least
    recently used store no request holds, and opens nothing when every copy is held."""
    stores = SeededStores(limit=2, root=tmp_path, stage=fake_stage)
    first = await stores.open("one", None, None, None)  # type: ignore[arg-type]
    second = await stores.open("two", None, None, None)  # type: ignore[arg-type]
    with stores.lease(first):
        third = await stores.open("three", None, None, None)  # type: ignore[arg-type]
        assert stores.get("one") is first and stores.get("two") is None, "unheld goes first"
        assert not second.database.exists() and len(session_files(tmp_path)) == 2
        with stores.lease(third):
            for attempt in range(6):  # repeated opens while both copies are held
                with pytest.raises(SeededStoreUnavailable, match="in use"):
                    await stores.open(f"new-{attempt}", None, None, None)  # type: ignore[arg-type]
                assert stores.copies == 2 and len(session_files(tmp_path)) == 2
            stores.release("one")  # the session ended; the request still holds the store
            assert stores.copies == 2 and first.database.exists()
            with pytest.raises(SeededStoreUnavailable):
                await stores.open("four", None, None, None)  # type: ignore[arg-type]
            assert len(session_files(tmp_path)) == 2
        assert len(session_files(tmp_path)) == 2
    assert not first.database.exists() and stores.copies == 1
    fourth = await stores.open("four", None, None, None)  # type: ignore[arg-type]
    assert stores.copies == 2 and session_files(tmp_path) == sorted(
        [third.database, fourth.database])
    stores.close()
    assert session_files(tmp_path) == []


async def close_while(stores: SeededStores, started, resume) -> SeededStore | BaseException:
    """Open a store, close the manager once staging or copying has started, then let it
    finish. Returns what open() returned or raised."""
    outcome: list[SeededStore | BaseException] = []

    async def opening() -> None:
        try:
            outcome.append(await stores.open("one", None, None, None))  # type: ignore[arg-type]
        except BaseException as error:  # noqa: BLE001 (the outcome under test)
            outcome.append(error)

    async with anyio.create_task_group() as group:
        group.start_soon(opening)
        await anyio.to_thread.run_sync(started.wait)
        stores.close()
        resume.set()
    return outcome[0]


@pytest.mark.anyio
async def test_closing_while_the_template_is_staged_registers_nothing(tmp_path):
    """SOL-UX1-R1-A7-002 (round 3): close() during staging must not remove the folder the
    staging writes into, and the staging must not register a store or keep its template
    afterwards; a later open stages again in a new folder."""
    import threading

    started, resume = threading.Event(), threading.Event()
    staged: list[Path] = []

    def stage(directory, *args):
        staged.append(directory)
        if len(staged) == 1:
            started.set()
            resume.wait(5)
            assert directory.exists(), "close() removed the folder under the staging"
        return fake_stage(directory, *args)

    stores = SeededStores(limit=2, root=tmp_path, stage=stage)
    outcome = await close_while(stores, started, resume)
    assert isinstance(outcome, SeededStoreUnavailable)
    assert len(stores) == 0 and stores.copies == 0
    assert not staged[0].exists() and list(tmp_path.iterdir()) == [], "the folder is removed"
    store = await stores.open("one", None, None, None)  # type: ignore[arg-type]
    assert len(staged) == 2 and store.seed is not None and store.database.exists()
    assert store.seed.database.parent == staged[1] and staged[1].exists()
    stores.close()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_closing_while_a_copy_is_made_registers_nothing(tmp_path, monkeypatch):
    """SOL-UX1-R1-A7-002 (round 3): close() during a copy must not let the copy register
    a store whose file and template were removed with the folder."""
    import threading

    import wealthcontext.workbench_revocation as module

    real, started, resume = module._copy, threading.Event(), threading.Event()
    targets: list[Path] = []

    def copy(source: Path, target: Path) -> None:
        targets.append(target)
        if len(targets) == 1:
            started.set()
            resume.wait(5)
            assert source.exists(), "close() removed the template under the copy"
        real(source, target)

    monkeypatch.setattr(module, "_copy", copy)
    stores = SeededStores(limit=2, root=tmp_path, stage=fake_stage)
    outcome = await close_while(stores, started, resume)
    assert isinstance(outcome, SeededStoreUnavailable)
    assert len(stores) == 0 and stores.get("one") is None and stores.copies == 0
    assert not targets[0].exists() and list(tmp_path.iterdir()) == []
    store = await stores.open("one", None, None, None)  # type: ignore[arg-type]
    assert store.seed is not None and store.seed.database.exists() and store.database.exists()
    assert store.database.parent != targets[0].parent
    stores.close()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_a_closed_folder_is_removed_when_its_own_stores_end(tmp_path):
    """A store still held in a newer folder must not keep a closed folder alive."""
    stores = SeededStores(limit=2, root=tmp_path, stage=fake_stage)
    old = await stores.open("one", None, None, None)  # type: ignore[arg-type]
    holding_old = stores.lease(old)
    holding_old.__enter__()
    stores.close()
    new = await stores.open("two", None, None, None)  # type: ignore[arg-type]
    assert new.database.parent != old.database.parent
    with stores.lease(new):
        stores.release("two")  # released and held, in the current folder
        assert old.database.parent.exists()
        holding_old.__exit__(None, None, None)  # the old folder's last request ends
        assert not old.database.parent.exists(), "kept alive by another folder's store"
        assert new.database.exists()
    assert new.database.parent.exists() and not new.database.exists()
    stores.close()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_a_new_context_during_a_paused_a7_read_leaves_no_store_behind(advisor_ui):
    """SOL-UX1-R1-A7-002 through the routes: the A7 provenance read is paused inside its
    authorization while the visitor applies a new context. The store leaves the table at
    once, the paused read finishes into it, and then its files are gone."""
    app, client, state, _ = advisor_ui
    await preset(client, "access")
    page = await client.get("/workbench/revocation")
    assert page.status_code == 200, page.text[-2000:]
    session_id = client.cookies[COOKIE]
    store = app.state.workbench.seeded.get(session_id)
    assert store is not None
    link = found(r'href="(/workbench/revocation/evidence[^"]+)"', page.text).group(1)
    started, resume = anyio.Event(), anyio.Event()
    state["pause"] = ("provenance_preflight", started, resume)
    outcome: dict[str, int] = {}

    async def read() -> None:
        outcome["status"] = (await client.get(link.replace("&amp;", "&"))).status_code

    async with anyio.create_task_group() as group:
        group.start_soon(read)
        await started.wait()
        await apply(client, persona="ADV-03")
        assert app.state.workbench.seeded.get(session_id) is None
        assert store.released and store.leases == 1 and store.database.exists()
        resume.set()
    assert outcome["status"] in (303, 403)
    assert store.leases == 0 and not store.database.exists()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(store.database) + suffix).exists()
    assert len(app.state.workbench.seeded) == 0


def test_every_way_a_session_leaves_releases_its_store():
    dropped: list[str] = []
    sessions = DemoSessions(on_drop=lambda session: dropped.append(session.id))
    replaced = sessions.create(NOW)
    sessions.remove(replaced)
    expired = sessions.create(NOW)
    assert sessions.get(None, NOW + timedelta(hours=2)) is None
    kept = [sessions.create(NOW) for _ in range(1024)]
    evicted = kept[0]
    sessions.create(NOW)
    assert dropped == [replaced.id, expired.id, evicted.id]
    # Removing a session object that is no longer the table's entry drops nothing.
    sessions.remove(replaced)
    assert dropped == [replaced.id, expired.id, evicted.id]
