"""Replay recorded provider tool choices through fresh governed retrieval, without keys."""

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import JsonValue, ValidationError

from wealthcontext.claim_resolution import _NUMERIC_UNITS
from wealthcontext.contracts import Answer, Contract
from wealthcontext.conversation import TOMBSTONE
from wealthcontext.evaluation_contracts import Sha256
from wealthcontext.identity import Principal
from wealthcontext.provider_contracts import (
    HarnessRun,
    ProviderFailure,
    ProviderRequest,
    ProviderTurn,
    ToolCall,
)
from wealthcontext.verifier import InvalidAnswer, verify_answer

# Audit metadata of one governed execution, not business evidence. Paired identifiers
# are remapped. A changed run timestamp or revision is permitted only while no
# recorded answer prose states the recorded value.
_PAIRED = frozenset({"result_id", "trace_id", "source_refs"})
_RUN_METADATA = ("calculated_at", "policy_revision", "entitlement_revision")
# A fresh value may differ only in the claimable numeric fields of claim resolution,
# which reach an answer through a placeholder rendering the fresh value. The verifier
# rejects these as bare numbers in prose. Every other recorded fact (dates, scope,
# statuses, errors, flags, labels and structure) must agree exactly.
_FRESH_VALUES = frozenset(_NUMERIC_UNITS) | {
    "price_local", "amount_local", "authoritative", "settled", "pending_delta", "difference",
}
_DECIMAL = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")


def _bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


class ReplayRecording(Contract):
    version: Literal[1] = 1
    origin: Literal["recorded_live_provider", "synthetic_test"]
    payload_sha256: Sha256
    run: HarnessRun


def recording_from_run(run: HarnessRun) -> ReplayRecording:
    if run.mode not in {"live_calibration", "live_scored", "synthetic_test"}:
        raise ValueError("a replay cannot be relabeled as an original provider recording")
    origin = "synthetic_test" if run.mode == "synthetic_test" else "recorded_live_provider"
    recording = ReplayRecording(origin=origin, payload_sha256=hashlib.sha256(
        _bytes(run.model_dump(mode="json")),
    ).hexdigest(), run=run)
    validate_recording(recording, allow_synthetic=origin == "synthetic_test")
    return recording


def _numeric(value: Any) -> bool:
    return type(value) is int or (isinstance(value, str) and bool(_DECIMAL.fullmatch(value)))


def _same_evidence(stored: Any, current: Any, replacements: dict[str, str],
                   key: str | None = None) -> bool:
    """Recorded and fresh evidence agree, up to paired IDs and claimable fresh values."""
    if isinstance(stored, dict):
        return (isinstance(current, dict) and stored.keys() == current.keys()
                and all(_same_evidence(stored[name], current[name], replacements, name)
                        for name in stored))
    if isinstance(stored, list):
        return (isinstance(current, list) and len(stored) == len(current)
                and all(_same_evidence(old, new, replacements, key)
                        for old, new in zip(stored, current, strict=True)))
    if (key in _FRESH_VALUES and type(stored) is type(current)
            and _numeric(stored) and _numeric(current)):
        return True
    if isinstance(stored, str):
        return isinstance(current, str) and replacements.get(stored, stored) == current
    return type(stored) is type(current) and stored == current


def _compatible(stored: dict[str, JsonValue], current: dict[str, JsonValue],
                replacements: dict[str, str], prose: list[str]) -> bool:
    if stored.keys() != current.keys():
        return False
    for key in _RUN_METADATA:
        old = stored.get(key)
        if isinstance(old, str) and old != current.get(key) and any(old in text for text in prose):
            return False
    return all(_same_evidence(stored[key], current[key], replacements, key) for key in stored
               if key not in _PAIRED and key not in _RUN_METADATA)


def _tool_history(request: ProviderRequest) -> list[JsonValue]:
    return [message.get("tool_call_id") for message in request.messages
            if message.get("role") == "tool"]


def prior_tombstones(request: ProviderRequest) -> list[str]:
    """The tool history a recording may start from: paired authorization tombstones only.

    A follow-up after a revocation (Q02, A7) opens with the earlier turn already
    redacted: each call keeps no arguments and each result is the tombstone, so no
    argument, result or authorized result reaches the recorded choices. Anything else
    before the first recorded turn is evidence the recording did not produce itself.
    Returns the tombstoned call IDs in order; raises ValueError otherwise.
    """
    calls: list[str] = []
    paired: list[str] = []
    for message in request.messages:
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            raise ValueError("recording must start without pre-existing tool results")
        for call in tool_calls:
            if (message.get("role") != "assistant" or not isinstance(call, dict)
                    or call.get("arguments") != {}
                    or not isinstance(call.get("id"), str)):
                raise ValueError("recording must start without pre-existing tool results")
            calls.append(str(call["id"]))
        if message.get("role") == "tool":
            if message.get("content") != TOMBSTONE or len(paired) >= len(calls):
                raise ValueError("recording must start without pre-existing tool results")
            paired.append(str(message.get("tool_call_id")))
    if paired != calls or len(set(calls)) != len(calls):
        raise ValueError("recording must start without pre-existing tool results")
    return calls


def prose_identifier_pattern(tokens: Iterable[str]) -> re.Pattern[str] | None:
    """Whole identifier tokens, using exactly the boundaries verifier._lint exempts.

    "Result r_1: text" contains the identifier; "r_1.detail" and "r_1:detail" do not.
    Longest first, in one pass, so a fresh ID resembling an old ID is not replaced again.
    """
    ordered = sorted((token for token in tokens if token), key=len, reverse=True)
    if not ordered:
        return None
    return re.compile(r"(?<![A-Za-z0-9_:@-])(?:" + "|".join(map(re.escape, ordered))
                      + r")(?![A-Za-z0-9_@-]|[.:][A-Za-z0-9_])")


def validate_recording(recording: ReplayRecording, *, allow_synthetic: bool = False) -> None:
    run = recording.run
    if hashlib.sha256(_bytes(run.model_dump(mode="json"))).hexdigest() != recording.payload_sha256:
        raise ValueError("recorded provider evidence hash mismatch")
    if recording.origin == "synthetic_test":
        if not allow_synthetic or run.mode != "synthetic_test":
            raise ValueError("synthetic fixtures are not recorded-live replay evidence")
    elif run.mode not in {"live_calibration", "live_scored"}:
        raise ValueError("recorded-live origin requires an actual live run label")
    elif not run.principal_id or not run.principal_role:
        raise ValueError("live recording lacks its authenticated principal identity")
    if (run.started_at.utcoffset() is None or run.completed_at.utcoffset() is None
            or run.completed_at < run.started_at):
        raise ValueError("recording must retain aware ordered timestamps")
    if (not run.turns or not run.final_valid or run.fallback or run.failure
            or run.final_result is None):
        raise ValueError("default replay requires a completed verified provider answer")
    # Earlier conversation text may precede it, but no tool call, result or authorized
    # result the recorded choices did not themselves produce: earlier tool history is
    # allowed only as paired authorization tombstones.
    opening = run.turns[0].request
    if opening.authorized_result_ids:
        raise ValueError("recording must start without pre-existing tool results")
    prior = prior_tombstones(opening)
    if run.transport_retries != sum(turn.transport_retries for turn in run.turns):
        raise ValueError("recorded transport retries must match the recorded turns")
    calls: set[str] = set(prior)
    history: list[JsonValue] = list(prior)
    results: dict[str, dict] = {}
    submissions: list[tuple[ToolCall, dict[str, JsonValue]]] = []
    data_calls = 0
    for turn in run.turns:
        response = turn.response
        if response is None or turn.error or not response.calls or not response.model:
            raise ValueError("recording lacks a complete provider response")
        if (recording.origin == "recorded_live_provider"
                and not (response.request_id or response.response_id)):
            raise ValueError("live recording lacks provider request identity")
        if len(turn.envelopes) != len(response.calls):
            raise ValueError("every recorded call requires its corresponding governed result")
        if _tool_history(turn.request) != history:
            raise ValueError("recorded requests must carry exactly the earlier recorded results")
        for call, envelope in zip(response.calls, turn.envelopes, strict=True):
            result_id = envelope.get("result_id")
            if call.id in calls or not isinstance(result_id, str):
                raise ValueError("recorded calls must have unique IDs and result pairs")
            calls.add(call.id)
            history.append(call.id)
            if call.name != "submit_answer":
                results[result_id] = envelope
        names = [call.name for call in response.calls]
        if "submit_answer" not in names and not submissions:
            data_calls += len(names)
        elif names == ["submit_answer"]:
            submissions.append((response.calls[0], turn.envelopes[0]))
        else:
            raise ValueError("recording must finish with its verified terminal submission")
    if not 1 <= len(submissions) <= 2:
        raise ValueError("recording must finish with its verified terminal submission")
    _validate_terminal(run, submissions, results, data_calls)


def _validate_terminal(run: HarnessRun, submissions: list[tuple[ToolCall, dict[str, JsonValue]]],
                       results: dict[str, dict], data_calls: int) -> None:
    """The terminal evidence is the verified, accepted submission of its recorded candidate."""
    call, envelope = submissions[-1]
    try:
        answer = Answer.model_validate(call.arguments)
    except ValidationError:
        raise ValueError("terminal candidate is not a valid frozen Answer") from None
    data = envelope.get("data")
    if (envelope.get("status") != "COMPLETE" or envelope.get("error") is not None
            or not isinstance(data, dict) or data.get("mode") != "verified"):
        raise ValueError("terminal submission lacks a successful verified result")
    if run.final_result != envelope:
        raise ValueError("final_result is not the accepted terminal submission")
    retried = len(submissions) == 2
    if retried:
        rejected = submissions[0][1]
        error = rejected.get("error")
        if (rejected.get("status") != "ERROR" or not isinstance(error, dict)
                or error.get("code") != "INVALID_ARGUMENT"
                or not run.turns[-1].request.terminal_only):
            raise ValueError("a retry must follow a rejected first submission")
    if run.retries != int(retried) or run.first_pass_valid == retried:
        raise ValueError("retry counters do not match the recorded submissions")
    if run.terminal_attempts != [{"kind": "provider", "candidate": item.arguments,
                                  "result": result} for item, result in submissions]:
        raise ValueError("terminal attempts do not match the recorded submissions")
    if run.data_tool_calls != data_calls:
        raise ValueError("recorded data tool calls do not match the recorded turns")
    cited = {item.result_id for item in [*answer.claims, *answer.predicates]}
    if answer.refusal is not None:
        cited.add(answer.refusal.result_id)
    if not cited <= set(run.turns[-1].request.authorized_result_ids):
        raise ValueError("terminal candidate cites results outside its authorized context")
    try:
        verified = verify_answer(answer, results)
    except InvalidAnswer:
        raise ValueError(
            "terminal candidate does not verify against its recorded results",
        ) from None
    if verified != data:
        raise ValueError("terminal result does not match verification of its candidate")


def save_recording(path: Path, run: HarnessRun) -> ReplayRecording:
    recording = recording_from_run(run)
    path.write_text(json.dumps(recording.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
    return recording


def load_recording(path: Path, *, allow_synthetic: bool = False) -> ReplayRecording:
    raw = json.loads(path.read_bytes())
    if not isinstance(raw, dict) or type(raw.get("version")) is not int:
        raise ValueError("unsupported replay version")
    recording = ReplayRecording.model_validate(raw)
    validate_recording(recording, allow_synthetic=allow_synthetic)
    return recording


class ReplayProvider:
    """Only provider choices are replayed; stored financial responses are never returned."""

    def __init__(self, recording: ReplayRecording, *, principal: Principal | None = None,
                 allow_synthetic: bool = False):
        validate_recording(recording, allow_synthetic=allow_synthetic)
        expected = (recording.run.principal_id, recording.run.principal_role)
        if recording.origin == "recorded_live_provider" or principal is not None:
            if principal is None or (principal.id, principal.role) != expected:
                raise ValueError("replay principal differs from the recorded authenticated persona")
        self.recording = recording
        self.provider, self.model = recording.run.provider, recording.run.model
        self.settings: dict[str, JsonValue] = {
            "recording_sha256": recording.payload_sha256, "origin": recording.origin,
            "original_settings": recording.run.settings,
        }
        self._index = 0
        # Tombstoned calls the recording opened with; they must still be tombstones.
        self._prior = prior_tombstones(recording.run.turns[0].request)
        # Recorded answer prose, which literal recorded facts must still support.
        self._prose = [
            text for turn in recording.run.turns if turn.response is not None
            for call in turn.response.calls if call.name == "submit_answer"
            and isinstance(text := call.arguments.get("answer_markdown"), str)
        ]

    async def complete(self, request: ProviderRequest) -> ProviderTurn:
        if self._index >= len(self.recording.run.turns):
            raise ProviderFailure("REPLAY_EXHAUSTED")
        original = self.recording.run.turns[self._index]
        if (request.system_prompt != original.request.system_prompt
                or request.tools != original.request.tools
                or request.terminal_only != original.request.terminal_only):
            raise ProviderFailure("REPLAY_CONTRACT_CHANGED")
        original_users = [message for message in original.request.messages
                          if message.get("role") == "user"]
        current_users = [message for message in request.messages if message.get("role") == "user"]
        if current_users != original_users or not current_users:
            raise ProviderFailure("REPLAY_USER_INPUT_CHANGED")
        fresh = {}
        tombstoned: set[str] = set()
        for message in request.messages:
            if message.get("role") != "tool":
                continue
            call_id, content = message.get("tool_call_id"), message.get("content")
            if not isinstance(call_id, str) or not isinstance(content, str):
                continue
            if content == TOMBSTONE:
                tombstoned.add(call_id)
            try:
                envelope = json.loads(content)
            except ValueError:
                fresh[call_id] = None  # An authorization tombstone is never old data.
                continue
            fresh[call_id] = envelope if isinstance(envelope, dict) else None
        replacements: dict[str, str] = {}
        recorded_calls: list[JsonValue] = []
        pairs: list[tuple[dict[str, JsonValue], dict[str, JsonValue]]] = []
        for previous in self.recording.run.turns[:self._index]:
            assert previous.response is not None
            for call, stored in zip(previous.response.calls, previous.envelopes, strict=True):
                recorded_calls.append(call.id)
                current = fresh.get(call.id)
                if (current is None
                        or current.get("result_id") not in request.authorized_result_ids):
                    raise ProviderFailure("REPLAY_CONTEXT_NOT_AUTHORIZED")
                for key in ("result_id", "trace_id"):
                    old, new = stored.get(key), current.get(key)
                    if not isinstance(old, str) or not isinstance(new, str):
                        raise ProviderFailure("REPLAY_INVALID_FRESH_RESULT")
                    replacements[old] = new
                old_refs, new_refs = stored.get("source_refs", []), current.get("source_refs", [])
                if (not isinstance(old_refs, list) or not isinstance(new_refs, list)
                        or len(old_refs) != len(new_refs)):
                    raise ProviderFailure("REPLAY_SOURCE_SHAPE_CHANGED")
                for old, new in zip(old_refs, new_refs, strict=True):
                    if not isinstance(old, str) or not isinstance(new, str):
                        raise ProviderFailure("REPLAY_INVALID_FRESH_RESULT")
                    replacements[old] = new
                pairs.append((stored, current))
        # The recorded choices saw exactly these results; no other prior result. Calls the
        # recording opened with as tombstones must still be tombstones now.
        if (_tool_history(request) != [*self._prior, *recorded_calls]
                or not set(self._prior) <= tombstoned):
            raise ProviderFailure("REPLAY_HISTORY_CHANGED")
        # Recorded choices and prose are reused only over the same business evidence,
        # including statuses and errors, before any recorded choice is emitted.
        if not all(_compatible(stored, current, replacements, self._prose)
                   for stored, current in pairs):
            raise ProviderFailure("REPLAY_EVIDENCE_CHANGED")

        # Literal evidence IDs are permitted in answer prose as well as references.
        prose_ids = prose_identifier_pattern(replacements)

        def remap(value: JsonValue, *, field: str | None = None) -> JsonValue:
            if isinstance(value, str):
                if field == "answer_markdown" and prose_ids is not None:
                    return prose_ids.sub(lambda match: replacements[match.group()], value)
                return replacements.get(value, value)
            if isinstance(value, list):
                return [remap(item) for item in value]
            if isinstance(value, dict):
                return {key: remap(item, field=key) for key, item in value.items()}
            return value

        assert original.response is not None
        response = original.response.model_dump(mode="json")
        response["calls"] = [remap(call) for call in response["calls"]]
        # The request ID remains recording identity; run mode/settings clearly identify replay.
        self._index += 1
        return ProviderTurn.model_validate(response)
