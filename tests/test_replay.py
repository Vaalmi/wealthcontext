"""Synthetic recording fixtures prove replay safety; these are not live provider runs.

Tests named ``genuine`` read the shipped recorded provider run without changing it.
"""

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue

from wealthcontext.contracts import Answer
from wealthcontext.identity import Principal
from wealthcontext.provider_contracts import (
    HarnessRun,
    ProviderFailure,
    ProviderRequest,
    ProviderTurn,
    ToolCall,
    ToolSpec,
    TurnRecord,
)
from wealthcontext.replay import (
    ReplayProvider,
    ReplayRecording,
    load_recording,
    prose_identifier_pattern,
    recording_from_run,
    save_recording,
    validate_recording,
)
from wealthcontext.verifier import _lint, verify_answer

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
QUESTION: dict[str, JsonValue] = {
    "role": "user", "content": "Show the quarter-end household value and its records.",
}
TOOLS = [ToolSpec(name=name, description="Synthetic test specification", input_schema={})
         for name in ("get_household_snapshot", "get_data_provenance", "submit_answer")]
MARKDOWN = "Synthetic placeholder test: {{c1}} and {{c2}}."
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/replays/clean-household.openai.json"
ADVISOR = Principal("ADV-01", "advisor")


def envelope(suffix):
    return {"result_id": "r_" + suffix, "trace_id": "trace_" + suffix,
            "source_refs": ["h_" + suffix], "status": "COMPLETE", "error": None,
            "valuation_date": "2026-09-30", "calculated_at": "2026-11-01T12:00:00Z",
            "data": {"household_id": "WC-HH-0001", "assets_usd": "3000.00"}}


def request(previous=()):
    return ProviderRequest(
        system_prompt="Synthetic test prompt.", tools=TOOLS,
        messages=[QUESTION, *[{"role": "tool", "tool_call_id": call_id,
                               "content": json.dumps(result)} for call_id, result in previous]],
        authorized_result_ids=[result["result_id"] for _, result in previous],
    )


def candidate(markdown=MARKDOWN) -> dict[str, JsonValue]:
    return {"answer_markdown": markdown, "predicates": [], "caveats": [], "refusal": None,
            "claims": [{"id": claim_id, "result_id": result_id,
                        "entity": {"type": "household", "id": "WC-HH-0001"},
                        "field": "assets_usd", "household_id": "WC-HH-0001",
                        "valuation_date": "2026-09-30", "unit": "USD", "format": "value_only"}
                       for claim_id, result_id in (("c1", "r_old"), ("c2", "r_proof"))]}


def rejected_submission():
    return {"result_id": "r_rejected", "trace_id": "trace_rejected", "source_refs": [],
            "status": "ERROR", "data": None, "error": {
                "code": "INVALID_ARGUMENT", "retryable": False, "details": {},
                "message": "Answer did not satisfy the verification contract",
            }}


def captured_run(markdown=MARKDOWN, *, retry=False):
    snapshot, provenance = envelope("old"), envelope("proof")
    arguments = candidate(markdown)
    # The terminal result is the actual pure verification of the recorded candidate.
    terminal = {"result_id": "r_answer", "trace_id": "trace_answer", "source_refs": [],
                "status": "COMPLETE", "error": None, "data": verify_answer(
                    Answer.model_validate(arguments),
                    {"r_old": snapshot, "r_proof": provenance},
                )}
    calls = [
        ToolCall(id="fetch", name="get_household_snapshot", arguments={
            "household_id": "WC-HH-0001", "as_of": "2026-09-30",
        }),
        ToolCall(id="sources", name="get_data_provenance", arguments={"reference": "h_old"}),
    ]
    results = [snapshot, provenance]
    if retry:
        calls.append(ToolCall(id="draft", name="submit_answer",
                              arguments={"answer_markdown": "Draft."}))
        results.append(rejected_submission())
    calls.append(ToolCall(id="finish", name="submit_answer", arguments=arguments))
    results.append(terminal)
    previous = []
    turns = []
    for index, (call, result) in enumerate(zip(calls, results, strict=True)):
        current = request(previous)
        current.terminal_only = retry and index == len(calls) - 1
        turns.append(TurnRecord(request=current, response=ProviderTurn(
            calls=[call], model="synthetic-test-model",
        ), envelopes=[result]))
        previous.append((call.id, result))
    return HarnessRun(
        run_id="synthetic_recording", conversation_id="synthetic_conversation",
        provider="anthropic", model="synthetic-test-model", settings={"synthetic": True},
        principal_id="ADV-01", principal_role="advisor",
        prompt_sha256="a" * 64, schema_sha256="b" * 64, started_at=NOW, completed_at=NOW,
        mode="synthetic_test", turns=turns, final_valid=True, final_result=terminal,
        first_pass_valid=not retry, retries=int(retry), data_tool_calls=2,
        terminal_attempts=[{"kind": "provider", "candidate": call.arguments, "result": result}
                           for call, result in zip(calls, results, strict=True)
                           if call.name == "submit_answer"],
    )


def rehashed(run: HarnessRun, path: Path) -> Path:
    """Write a recording whose payload hash matches, bypassing recording creation checks."""
    payload = json.dumps(run.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    recording = ReplayRecording(
        origin="synthetic_test" if run.mode == "synthetic_test" else "recorded_live_provider",
        payload_sha256=hashlib.sha256(payload).hexdigest(), run=run,
    )
    path.write_text(json.dumps(recording.model_dump(mode="json")))
    return path


def assert_rejected(run: HarnessRun, path: Path, message: str, *, synthetic=False):
    with pytest.raises(ValueError, match=message):
        recording_from_run(run)
    with pytest.raises(ValueError, match=message):
        load_recording(rehashed(run, path), allow_synthetic=synthetic)


def genuine_run() -> HarnessRun:
    return load_recording(FIXTURE).run.model_copy(deep=True)


def fresh_snapshot() -> dict:
    """The genuine snapshot as a fresh governed run returns it: new IDs and run metadata."""
    current = copy.deepcopy(genuine_run().turns[0].envelopes[0])
    current.update(result_id="r_" + "f" * 32, trace_id="e" * 32, source_refs=["h_" + "d" * 32],
                   calculated_at="2026-11-04T09:00:00Z", policy_revision="sha256:" + "0" * 64,
                   entitlement_revision=3)
    return current


def genuine_requests(current: dict) -> tuple[ProviderRequest, ProviderRequest]:
    first, second = (turn.request.model_copy(deep=True) for turn in genuine_run().turns)
    assert second.messages[-1]["role"] == "tool"
    second.messages[-1]["content"] = json.dumps(current)
    second.authorized_result_ids = [current["result_id"]]
    return first, second


def provider():
    return ReplayProvider(recording_from_run(captured_run()), allow_synthetic=True)


def test_synthetic_records_require_explicit_test_opt_in_and_preserve_origin(tmp_path):
    path = tmp_path / "synthetic.json"
    saved = save_recording(path, captured_run())
    assert saved.origin == "synthetic_test"
    with pytest.raises(ValueError, match="not recorded-live"):
        load_recording(path)
    restored = load_recording(path, allow_synthetic=True)
    assert restored == saved
    with pytest.raises(ValueError, match="not recorded-live"):
        ReplayProvider(restored)
    assert ReplayProvider(restored, allow_synthetic=True).settings["origin"] == "synthetic_test"


def test_corrupt_recording_and_missing_call_result_pairs_are_rejected():
    recording = recording_from_run(captured_run())
    recording.run.turns[0].envelopes[0]["data"] = {"assets_usd": "900000.00"}
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_recording(recording, allow_synthetic=True)
    run = captured_run()
    run.turns[0].envelopes = []
    with pytest.raises(ValueError, match="corresponding governed result"):
        recording_from_run(run)
    run = captured_run()
    run.mode = "replay"
    with pytest.raises(ValueError, match="relabeled"):
        recording_from_run(run)


def test_live_label_without_provider_request_identity_is_not_replay_evidence():
    run = captured_run()
    run.mode = "live_calibration"
    with pytest.raises(ValueError, match="provider request identity"):
        recording_from_run(run)


def test_replay_rejects_changed_authenticated_persona_even_for_explicit_synthetic_fixture():
    recording = recording_from_run(captured_run())
    with pytest.raises(ValueError, match="principal differs"):
        ReplayProvider(recording, principal=Principal("ADV-02", "advisor"), allow_synthetic=True)
    adapter = ReplayProvider(recording, principal=Principal("ADV-01", "advisor"),
                             allow_synthetic=True)
    assert adapter.settings["origin"] == "synthetic_test"


@pytest.mark.anyio
async def test_replay_emits_choices_with_current_ids_and_handles_never_stored_values():
    adapter = provider()
    first = await adapter.complete(request())
    assert first.calls[0].id == "fetch"
    current = envelope("new")
    current["data"]["assets_usd"] = "3549.00"  # A fresh claimable value is permitted.
    second = await adapter.complete(request([("fetch", current)]))
    assert second.calls[0].arguments == {"reference": "h_new"}
    proof = envelope("new_proof")
    third = await adapter.complete(request([("fetch", current), ("sources", proof)]))
    claims = third.calls[0].arguments["claims"]
    assert isinstance(claims, list)
    assert [claim["result_id"] for claim in claims if isinstance(claim, dict)] == [
        "r_new", "r_new_proof",
    ]
    assert "3000.00" not in third.model_dump_json()
    with pytest.raises(ProviderFailure, match="REPLAY_EXHAUSTED"):
        await adapter.complete(request())


@pytest.mark.anyio
async def test_replay_remaps_literal_evidence_ids_as_whole_tokens_without_chaining():
    # The recorded prose must itself verify, so non-identifier cases avoid lint vocabulary.
    original = (
        "Result r_old. Result r_old: text. Trace `trace_old`; source (h_old). "
        "Proof (r_proof): {{c1}} and {{c2}}. "
        "Keep r_old_suffix, prefix_r_old, r_old:detail, r_old.detail, r_old-tail and r_unknown. "
        "Date 2026-09-30."
    )
    recording = recording_from_run(captured_run(original))
    adapter = ReplayProvider(recording, allow_synthetic=True)
    await adapter.complete(request())
    # A current ID equal to another old ID must not be substituted twice.
    current = envelope("proof")
    await adapter.complete(request([("fetch", current)]))
    proof = envelope("new_proof")
    result = await adapter.complete(request([("fetch", current), ("sources", proof)]))
    assert result.calls[0].arguments["answer_markdown"] == (
        "Result r_proof. Result r_proof: text. Trace `trace_proof`; source (h_proof). "
        "Proof (r_new_proof): {{c1}} and {{c2}}. "
        "Keep r_old_suffix, prefix_r_old, r_old:detail, r_old.detail, r_old-tail and r_unknown. "
        "Date 2026-09-30."
    )
    claims = result.calls[0].arguments["claims"]
    assert isinstance(claims, list)
    assert [claim["result_id"] for claim in claims if isinstance(claim, dict)] == [
        "r_proof", "r_new_proof",
    ]
    terminal = recording.run.turns[-1].response
    assert terminal is not None and terminal.calls[0].arguments["answer_markdown"] == original
    validate_recording(recording, allow_synthetic=True)


@pytest.mark.anyio
@pytest.mark.parametrize("content", ["Different household, please.",
                                     "Show the value known before October."])
async def test_changed_question_or_cutoff_fails_before_any_recorded_choice(content):
    current = request()
    current.messages = [{"role": "user", "content": content}]
    with pytest.raises(ProviderFailure, match="REPLAY_USER_INPUT_CHANGED"):
        await provider().complete(current)


@pytest.mark.anyio
async def test_followup_user_sequence_is_bound_at_each_turn():
    adapter = provider()
    await adapter.complete(request())
    current = request([("fetch", envelope("new"))])
    current.messages.append({"role": "user", "content": "Use a different valuation date."})
    with pytest.raises(ProviderFailure, match="REPLAY_USER_INPUT_CHANGED"):
        await adapter.complete(current)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["tombstone", "unauthorized", "missing", "source_shape"])
async def test_revoked_or_unpaired_context_is_not_replaced_by_recorded_evidence(change):
    adapter = provider()
    await adapter.complete(request())
    current = request([("fetch", envelope("new"))])
    expected = "REPLAY_CONTEXT_NOT_AUTHORIZED"
    if change == "tombstone":
        current.messages[-1]["content"] = "[REDACTED: no longer authorized]"
    elif change == "unauthorized":
        current.authorized_result_ids = []
    elif change == "missing":
        current.messages.pop()
    else:
        value = envelope("new")
        value["source_refs"] = []
        current.messages[-1]["content"] = json.dumps(value)
        expected = "REPLAY_SOURCE_SHAPE_CHANGED"
    with pytest.raises(ProviderFailure, match=expected):
        await adapter.complete(current)


@pytest.mark.anyio
async def test_changed_tool_contract_fails_closed():
    current = request()
    current.tools = current.tools[:-1]
    with pytest.raises(ProviderFailure, match="REPLAY_CONTRACT_CHANGED"):
        await provider().complete(current)


# SOL-P5-R1-REPLAY-001 and ARGON-P5-REPLAY-002: recorded choices and prose are reused
# only when the fresh business evidence still agrees with the recorded evidence.


@pytest.mark.anyio
async def test_genuine_recording_replays_with_fresh_ids_metadata_and_claim_values():
    current = fresh_snapshot()
    current["data"]["positions"][0].update(price_local="26", price_usd="26",
                                           market_value_usd="2600.00")
    current["data"]["assets_usd"] = "3100.00"
    first, second = genuine_requests(current)
    recording = load_recording(FIXTURE)
    adapter = ReplayProvider(recording, principal=ADVISOR)
    assert recording.run.turns[0].response is not None
    assert (await adapter.complete(first)).calls == recording.run.turns[0].response.calls
    answer = Answer.model_validate((await adapter.complete(second)).calls[0].arguments)
    old = recording.run.turns[0].envelopes[0]["result_id"]
    assert isinstance(old, str)
    assert {claim.result_id for claim in answer.claims} == {current["result_id"]}
    # This recording's prose names no result ID; literal-ID remapping in prose is covered
    # by the synthetic remap tests (CHAIR-P5-REPLAY-LITERAL-001).
    assert old not in answer.answer_markdown
    # Fresh values flow only through placeholders; retained literal facts still agree.
    rendered = verify_answer(answer, {current["result_id"]: current})["markdown"]
    # Wording follows the recorded prose; the fresh values come only from placeholders.
    for value in ("USD 3100.00", "USD 2600.00", "100 shares", "USD 500.00"):
        assert value in rendered
    assert "3000.00" not in rendered and "USD 2500.00" not in rendered


def _move_account_out_of_scope(data):
    data["excluded_accounts"] = [data["accounts"].pop()]


GENUINE_CHANGES = {
    "price_date": lambda envelope: envelope["data"]["positions"][0].update(
        price_date="2026-09-29"),
    "status": lambda envelope: envelope.update(status="INCOMPLETE"),
    "known_as_of": lambda envelope: envelope.update(known_as_of="2026-10-30T16:00:00Z"),
    "scope": lambda envelope: _move_account_out_of_scope(envelope["data"]),
    "reconciliation": lambda envelope: envelope["data"]["reconciliation"][0]["rows"][0].update(
        reconciliation_status="BREAK"),
    "quality": lambda envelope: envelope.update(quality_flags=[{
        "code": "STALE_PRICE", "severity": "warning", "subject": "WC-AC-0001:WC-SEC-0001",
    }]),
}


@pytest.mark.anyio
@pytest.mark.parametrize("change", sorted(GENUINE_CHANGES))
async def test_genuine_candidate_fails_closed_when_retained_evidence_changed(change):
    current = fresh_snapshot()
    GENUINE_CHANGES[change](current)
    first, second = genuine_requests(current)
    adapter = ReplayProvider(load_recording(FIXTURE), principal=ADVISOR)
    await adapter.complete(first)
    with pytest.raises(ProviderFailure, match="REPLAY_EVIDENCE_CHANGED"):
        await adapter.complete(second)


def test_verifier_alone_accepts_the_stale_literal_account_scope():
    """Why replay must check: the verifier exempts any identifier present in the cited result,
    so the genuine prose naming the cash account survives that account moving out of scope."""
    run = genuine_run()
    stored = copy.deepcopy(run.turns[0].envelopes[0])
    data = stored["data"]
    assert isinstance(data, dict)
    accounts, excluded = data["accounts"], data["excluded_accounts"]
    assert isinstance(accounts, list) and isinstance(excluded, list)
    named = [row for row in accounts if isinstance(row, dict) and row["id"] == "WC-AC-0001"]
    data["accounts"] = [row for row in accounts if row not in named]
    data["excluded_accounts"] = [*excluded, *named]
    assert json.dumps(data["excluded_accounts"]).count('"WC-AC-0001"') == 1
    assert run.turns[1].response is not None
    answer = Answer.model_validate(run.turns[1].response.calls[0].arguments)
    result_id = stored["result_id"]
    assert isinstance(result_id, str)
    rendered = verify_answer(answer, {result_id: stored})["markdown"]
    assert "WC-AC-0001" in answer.answer_markdown and "USD 500.00" in rendered


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["incomplete", "error"])
async def test_changed_status_or_error_with_equal_source_shape_fails_closed(change):
    adapter = provider()
    await adapter.complete(request())
    current = envelope("new")
    if change == "incomplete":
        current["status"] = "INCOMPLETE"
        with pytest.raises(ProviderFailure, match="REPLAY_EVIDENCE_CHANGED"):
            await adapter.complete(request([("fetch", current)]))
        return
    await adapter.complete(request([("fetch", current)]))
    proof = envelope("new_proof")
    proof.update(status="ERROR", data=None, error={
        "code": "NOT_FOUND", "message": "Data is not available at these cutoffs",
        "retryable": False, "details": {},
    })
    with pytest.raises(ProviderFailure, match="REPLAY_EVIDENCE_CHANGED"):
        await adapter.complete(request([("fetch", current), ("sources", proof)]))


@pytest.mark.anyio
async def test_changed_run_metadata_retained_in_prose_fails_closed():
    current = envelope("new")
    current["calculated_at"] = "2026-11-04T09:00:00Z"
    # A fresh run timestamp is permitted while the recorded prose does not state it.
    adapter = provider()
    await adapter.complete(request())
    await adapter.complete(request([("fetch", current)]))
    recording = recording_from_run(captured_run(
        "Calculated 2026-11-01T12:00:00Z: {{c1}} and {{c2}}.",
    ))
    adapter = ReplayProvider(recording, allow_synthetic=True)
    await adapter.complete(request())
    with pytest.raises(ProviderFailure, match="REPLAY_EVIDENCE_CHANGED"):
        await adapter.complete(request([("fetch", current)]))


@pytest.mark.anyio
async def test_unrecorded_prior_tool_result_fails_closed():
    stray = envelope("stray")
    with pytest.raises(ProviderFailure, match="REPLAY_HISTORY_CHANGED"):
        await provider().complete(request([("stray", stray)]))
    adapter = provider()
    await adapter.complete(request())
    with pytest.raises(ProviderFailure, match="REPLAY_HISTORY_CHANGED"):
        await adapter.complete(request([("fetch", envelope("new")), ("stray", stray)]))


def test_recording_must_start_without_pre_existing_tool_results(tmp_path):
    run = captured_run()
    run.turns[0].request.messages.append(
        {"role": "tool", "tool_call_id": "earlier", "content": json.dumps(envelope("earlier"))},
    )
    assert_rejected(run, tmp_path / "prior.json", "without pre-existing", synthetic=True)
    run = captured_run()
    run.turns[1].request.messages.append(
        {"role": "tool", "tool_call_id": "stray", "content": json.dumps(envelope("stray"))},
    )
    assert_rejected(run, tmp_path / "stray.json", "earlier recorded results", synthetic=True)


# SOL-P5-R1-REPLAY-002: the terminal evidence of a recording must be the verified,
# accepted submission of its recorded candidate.


def test_genuine_recording_still_validates_and_rehashes_identically(tmp_path):
    recording = load_recording(FIXTURE)
    assert recording.origin == "recorded_live_provider"
    assert recording_from_run(recording.run) == recording
    assert load_recording(rehashed(genuine_run(), tmp_path / "same.json")) == recording


def _empty_candidate(run):
    run.turns[-1].response.calls[0].arguments = {}
    run.terminal_attempts[0]["candidate"] = {}


def _error_terminal(run):
    error = {**run.turns[-1].envelopes[0], "status": "ERROR", "data": None, "error": {
        "code": "INVALID_ARGUMENT", "message": "Answer did not satisfy the verification contract",
        "retryable": False, "details": {},
    }}
    run.turns[-1].envelopes[0] = error
    run.final_result = error
    run.terminal_attempts[0]["result"] = error


def _unrelated_final(run):
    run.final_result = copy.deepcopy(run.turns[0].envelopes[0])


def _changed_verified_value(run):
    terminal = copy.deepcopy(run.turns[-1].envelopes[0])
    terminal["data"]["claims"][0]["value"] = "9000.00"
    run.turns[-1].envelopes[0] = terminal
    run.final_result = terminal
    run.terminal_attempts[0]["result"] = terminal


def _changed_attempt(run):
    run.terminal_attempts[0]["candidate"] = {"answer_markdown": "Different."}


def _changed_counter(run):
    run.data_tool_calls = 0


def _uncounted_transport_retry(run):
    run.turns[0].transport_retries = 1


def _overcounted_transport_retries(run):
    run.transport_retries = 1


TERMINAL_TAMPERING = {
    "uncounted_transport_retry": (_uncounted_transport_retry, "transport retries"),
    "overcounted_transport_retries": (_overcounted_transport_retries, "transport retries"),
    "empty_candidate": (_empty_candidate, "valid frozen Answer"),
    "error_terminal": (_error_terminal, "successful verified"),
    "unrelated_final": (_unrelated_final, "final_result"),
    "changed_verified_value": (_changed_verified_value, "verification of its candidate"),
    "changed_attempt": (_changed_attempt, "terminal attempts"),
    "changed_counter": (_changed_counter, "data tool calls"),
}


@pytest.mark.parametrize("change", sorted(TERMINAL_TAMPERING))
def test_tampered_genuine_terminal_evidence_is_rejected_even_when_rehashed(change, tmp_path):
    run = genuine_run()
    tamper, message = TERMINAL_TAMPERING[change]
    tamper(run)
    assert_rejected(run, tmp_path / "tampered.json", message)


def test_retry_recording_requires_a_rejected_first_submission(tmp_path):
    recording = recording_from_run(captured_run(retry=True))
    assert recording.run.retries == 1 and not recording.run.first_pass_valid
    run = captured_run(retry=True)
    run.retries, run.first_pass_valid = 0, True
    assert_rejected(run, tmp_path / "counter.json", "retry counters", synthetic=True)
    run = captured_run(retry=True)
    accepted = {**rejected_submission(), "status": "COMPLETE", "error": None}
    run.turns[2].envelopes[0] = accepted
    run.terminal_attempts[0]["result"] = accepted
    assert_rejected(run, tmp_path / "first.json", "rejected first submission", synthetic=True)
    run = captured_run(retry=True)
    run.turns[3].request.terminal_only = False
    assert_rejected(run, tmp_path / "terminal.json", "rejected first submission", synthetic=True)


# ARGON-P5-REPLAY-001: prose identifiers use exactly the verifier's identifier boundaries.


@pytest.mark.parametrize("text", [
    "Result r_0ld9: text", "r_0ld9.detail", "Result r_0ld9.", "r_0ld9:", "r_0ld9:detail",
    "(r_0ld9)", "`r_0ld9`", "r_0ld9-tail", "r_0ld9_tail", "r_0ld9@x", "prefix_r_0ld9",
    "x:r_0ld9", "x.r_0ld9", "r_0ld9,", "r_0ld9;",
])
def test_prose_remapping_matches_verifier_identifier_boundaries(text):
    token = "r_0ld9"
    pattern = prose_identifier_pattern([token])
    assert pattern is not None
    exempt = not any(item["code"] == "UNVERIFIED_NUMBER"
                     for item in _lint(text, set(), {token: {"result_id": token}}))
    assert bool(pattern.search(text)) is exempt


# A7's follow-up (decision 12): a recording may open with earlier tool history only when
# all of it is paired authorization tombstones, as Q02's follow-up does after the
# reassignment. Nothing the recorded choices did not produce reaches them.
FOLLOWUP = FIXTURE.with_name("revocation-followup.anthropic.json")
TOMBSTONE = "[REDACTED: NOT_AUTHORIZED]"


def followup_run() -> HarnessRun:
    return load_recording(FOLLOWUP).run.model_copy(deep=True)


def test_a_follow_up_recording_may_open_with_paired_tombstones_only(tmp_path):
    run = followup_run()
    assert recording_from_run(run).origin == "recorded_live_provider"
    opening = run.turns[0].request.messages
    tool = next(index for index, message in enumerate(opening) if message["role"] == "tool")
    call = next(index for index, message in enumerate(opening) if message.get("tool_calls"))
    path = tmp_path / "followup.json"
    for change in (
        lambda messages: messages[tool].update(content='{"result_id": "r_old"}'),
        lambda messages: messages[call]["tool_calls"][0].update(  # type: ignore[index,union-attr]
            arguments={"household_id": "WC-HH-0004"}),
        lambda messages: messages.pop(tool),  # a call without its paired result
        lambda messages: messages.insert(tool, {"role": "tool", "tool_call_id": "extra",
                                                "content": TOMBSTONE}),
    ):
        changed = followup_run()
        change(changed.turns[0].request.messages)
        assert_rejected(changed, path, "without pre-existing tool results")
    changed = followup_run()
    changed.turns[0].request.authorized_result_ids = ["r_old"]
    assert_rejected(changed, path, "without pre-existing tool results")


@pytest.mark.anyio
async def test_a_follow_up_replays_only_while_its_earlier_calls_are_still_tombstones():
    recording = load_recording(FOLLOWUP)
    opening = recording.run.turns[0].request
    adapter = ReplayProvider(recording, principal=Principal("ADV-03", "advisor"))
    restored = opening.model_copy(deep=True)
    restored.messages[2]["content"] = '{"result_id": "r_restored"}'  # access came back
    with pytest.raises(ProviderFailure, match="REPLAY_HISTORY_CHANGED"):
        await adapter.complete(restored)
    missing = opening.model_copy(deep=True)
    missing.messages = [message for message in missing.messages
                        if message["role"] == "user"]  # the earlier turn left out
    with pytest.raises(ProviderFailure, match="REPLAY_HISTORY_CHANGED"):
        await ReplayProvider(recording, principal=Principal("ADV-03", "advisor")).complete(
            missing)
    turn = await ReplayProvider(recording, principal=Principal("ADV-03", "advisor")).complete(
        opening.model_copy(deep=True))
    assert [call.name for call in turn.calls] == ["submit_answer"]
    assert turn.calls[0].arguments["claims"] == []
