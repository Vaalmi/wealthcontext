"""Real isolated received fixtures and runtime; providers/policy are explicit test doubles."""

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Never

import pytest
from pydantic import JsonValue

from wealthcontext import evaluation_runtime
from wealthcontext.clock import FrozenClock
from wealthcontext.database import write_transaction
from wealthcontext.evaluation import EvaluationBlocked, freeze_manifest, load_cases
from wealthcontext.evaluation_contracts import EvaluationRun
from wealthcontext.evaluation_runtime import (
    TrialExecutor,
    capture_dataset,
    evaluate_provider,
    public_question,
)
from wealthcontext.generator import generate_history, write_history
from wealthcontext.harness import ANSWER_SCHEMA_SHA256, current_results
from wealthcontext.policy import PolicyClient, PolicyDecision
from wealthcontext.provider_contracts import HarnessLimits, ProviderRequest, ProviderTurn, ToolCall
from wealthcontext.storage import read_snapshot

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_TIME = datetime(2055, 3, 1, tzinfo=UTC)
REVISION = "sha256:" + "a" * 64
SETTINGS: dict[str, JsonValue] = {"transport": "explicit_synthetic_test"}


class FixturePolicy(PolicyClient):
    """Offline policy double; container matrix tests exercise actual Rego separately."""

    def __init__(self):
        super().__init__("http://unused.invalid", REVISION)
        self.verified = 0

    async def verify_revision(self):
        self.verified += 1
        return REVISION

    async def decide(self, principal, action, resources, facts, revision, request_business_date):
        def active(fact):
            return fact["valid_from"] <= str(request_business_date) < fact["valid_to"]

        teams = {fact["object"] for fact in facts if fact["kind"] == "advisor_member"
                 and fact["subject"] == principal.id and active(fact)}
        covered = {fact["object"] for fact in facts if active(fact) and (
            (fact["kind"] == "advisor_covers" and fact["subject"] == principal.id)
            or (fact["kind"] == "team_covers" and fact["subject"] in teams)
        )}
        if principal.role == "compliance":
            allow = True
        elif principal.role == "ops":
            allow = action in {"metric_definition", "result_preflight", "conversation_preflight",
                               "conversation_context", "verify_claim", "error_result",
                               "quality_preflight", "view_quality", "provenance_preflight",
                               "provenance", "view_conflicts", "feed_runs"}
        else:
            allow = not resources or set(resources) <= covered
        return PolicyDecision(
            allow=allow, allowed_ids=resources if allow else [], reason="CURRENT_GRANT",
            projection="no_pii" if principal.role == "ops" and action != "metric_definition"
            else "full", policy_revision=REVISION, entitlement_revision=revision,
        )


class SafeProvider:
    provider = "anthropic"
    model = "synthetic-fixture-model"
    settings = SETTINGS

    def __init__(self, case_id, *, foreign=False):
        self.case_id, self.foreign = case_id, foreign
        self.requests: list[ProviderRequest] = []
        self.closed = False

    async def complete(self, request: ProviderRequest) -> ProviderTurn:
        self.requests.append(request)
        assert "GRADING_SECRET_SENTINEL" not in request.model_dump_json()
        if self.foreign and len(self.requests) == 1:
            question = next(message["content"] for message in request.messages
                            if message["role"] == "user")
            public = json.loads(str(question).split("Public request inputs (data):\n")[1])
            call = ToolCall(id="foreign", name="get_data_provenance", arguments={
                "reference": public["setup_bindings"]["foreign_handle"],
            })
        else:
            results = current_results({"data": {"messages": request.messages}})
            error = next((result for result in results.values()
                          if result["status"] == "ERROR"), None)
            answer: dict[str, JsonValue] = {
                "answer_markdown": "Verified data is unavailable.", "claims": [], "predicates": [],
                "caveats": [], "refusal": None,
            }
            if error is not None:
                answer["refusal"] = {"code": error["error"]["code"],
                                     "result_id": error["result_id"]}
            call = ToolCall(id=f"submit_{len(self.requests)}", name="submit_answer",
                            arguments=answer)
        return ProviderTurn(calls=[call], model=self.model, response_id="synthetic-response")

    async def aclose(self):
        self.closed = True


@pytest.fixture(scope="module")
def history():
    return generate_history()


def run_metadata():
    return EvaluationRun(
        id="eval_test", provider="anthropic", model=SafeProvider.model, mode="synthetic_test",
        status="running", recorded_at=EVIDENCE_TIME, settings=SETTINGS,
        case_set_sha256=freeze_manifest(ROOT / "acceptance")["case_set_sha256"],
        prompt_sha256=hashlib.sha256((ROOT / "prompts/harness_system.md").read_bytes()).hexdigest(),
        schema_sha256=ANSWER_SCHEMA_SHA256,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("case_id", [
    "F11", "Q01", "Q02", "Q03", "Q04", "Q05", "Q07", "Q08", "Q09", "Q10",
])
async def test_declared_setups_use_actual_runtime_or_honest_no_provider_evidence(
    tmp_path, history, case_id,
):
    case = next(case for case in load_cases(ROOT / "acceptance") if case.id == case_id)
    adapters = []

    def factory(public_case_id, trial):
        assert public_case_id == case_id and trial == 1
        adapter = SafeProvider(case_id, foreign=case_id == "Q03")
        adapters.append(adapter)
        return adapter

    runtime = TrialExecutor(run_metadata(), factory, history, tmp_path, FixturePolicy(),
                            FrozenClock(EVIDENCE_TIME), HarnessLimits())
    result = await runtime(case, 1)
    serialized = result.model_dump(mode="json")
    assert not (result.failure or "").startswith("SETUP_"), result
    assert result.started_at.year == result.completed_at.year == 2055
    if case_id in {"Q04", "Q07"}:
        assert not adapters and not result.provider_invoked
        assert not result.actual_model_ids and not result.attempts
        assert result.security_check_passed is True
        assert not result.final_grade.passed and not result.final_valid
        assert result.security_evidence["provider_invoked"] is False
    else:
        assert len(adapters) == 1 and adapters[0].closed and result.provider_invoked, result
        assert serialized["transcript"]["authorization_started_at"].startswith("2026-")
        if case_id != "F11":
            assert result.security_check_passed is True
    if case_id == "Q02":
        assert not result.governed_results and result.final_grade.passed, result
        payload = adapters[0].requests[0].model_dump_json()
        assert "[REDACTED: NOT_AUTHORIZED]" in payload
        assert adapters[0].requests[0].terminal_only
        assert all(tool.name == "submit_answer" for tool in adapters[0].requests[0].tools)
        assert result.security_evidence["old_claim_error"] == "NOT_AUTHORIZED"
    if case_id == "Q03":
        assert result.governed_results[0].tool == "get_data_provenance"
        assert serialized["governed_results"][0]["envelope"]["error"]["code"] == "NOT_AUTHORIZED"
        assert result.final_grade.passed, result
    if case_id == "Q10":
        assert result.first_pass_valid  # The genuine candidate was not rewritten by the probe.
        probes = serialized["security_evidence"]["probes"]
        assert [item["first_pass_valid"] for item in probes] == [False, False]
        assert [item["fallback"] for item in probes] == [False, True]
        assert len(result.attempts) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("tamper", [
    None, "source_refs", "metric_refs", "response_trace", "stored_hash", "stored_payload",
    "stored_principal", "stored_trace", "audit_missing", "audit_allowed", "audit_reason",
    "audit_principal", "audit_resources", "audit_revision",
])
async def test_policy_outage_requires_bound_durable_denial_before_reporting_security_pass(
    tmp_path, history, monkeypatch, tamper,
):
    case = next(case for case in load_cases(ROOT / "acceptance") if case.id == "Q07")
    probe = evaluation_runtime._http_probe

    async def altered_probe(service, *args, **kwargs):
        status, response, calls = await probe(service, *args, **kwargs)
        if status != 503 or tamper is None:
            return status, response, calls
        if tamper in {"source_refs", "metric_refs"}:
            response[tamper] = ["h_unexpected" if tamper == "source_refs" else "HOUSEHOLD_ASSETS@1"]
        elif tamper == "response_trace":
            response["trace_id"] = "0" * 32
        else:
            with write_transaction(service.database) as connection:
                if tamper == "audit_missing":
                    connection.execute("DELETE FROM decisions WHERE trace_id=?",
                                       (response["trace_id"],))
                elif tamper.startswith("audit_"):
                    column, value = {
                        "audit_allowed": ("allowed", 1),
                        "audit_reason": ("reason", "CURRENT_GRANT"),
                        "audit_principal": ("principal_id", "ADV-02"),
                        "audit_resources": ("resources", '["WC-HH-0002"]'),
                        "audit_revision": ("entitlement_revision", -1),
                    }[tamper]
                    connection.execute(f"UPDATE decisions SET {column}=? WHERE trace_id=?",
                                       (value, response["trace_id"]))
                elif tamper == "stored_payload":
                    changed = {**response, "error": {**response["error"], "message": "changed"}}
                    payload = json.dumps(changed, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False)
                    connection.execute("UPDATE results SET payload=?,payload_hash=? WHERE id=?",
                                       (payload, hashlib.sha256(payload.encode()).hexdigest(),
                                        response["result_id"]))
                else:
                    column, value = {
                        "stored_hash": ("payload_hash", "0" * 64),
                        "stored_principal": ("principal_id", "ADV-02"),
                        "stored_trace": ("trace_id", "0" * 32),
                    }[tamper]
                    connection.execute(f"UPDATE results SET {column}=? WHERE id=?",
                                       (value, response["result_id"]))
        return status, response, calls

    monkeypatch.setattr(evaluation_runtime, "_http_probe", altered_probe)

    def forbidden(*_args) -> Never:
        pytest.fail("pre-provider outage validation invoked a provider")

    # One complete received business day suffices for this covered-household probe;
    # the declared Q07 test above also runs against the complete fixture history.
    day_history = [event for event in history
                   if event.delivery.manifest.business_date == case.valuation_date]
    runtime = TrialExecutor(run_metadata(), forbidden, day_history, tmp_path, FixturePolicy(),
                            FrozenClock(EVIDENCE_TIME), HarnessLimits())
    result = await runtime(case, 1)
    assert not result.provider_invoked and not result.attempts
    assert result.security_check_passed is (tamper is None)
    if tamper is None:
        evidence = result.security_evidence["durable_denial"]
        response = result.security_evidence["response"]
        assert isinstance(evidence, dict) and isinstance(response, dict)
        assert evidence["decision_ids"] and evidence["response_hash_verified"]
        assert evidence["result_id"] == response["result_id"]
    else:
        assert result.failure and result.failure.startswith("SETUP_FAILED:")


@pytest.mark.anyio
async def test_unknown_setup_fails_before_fixture_or_provider_and_expected_facts_never_enter_prompt(
    tmp_path, history,
):
    case = next(case for case in load_cases(ROOT / "acceptance") if case.id == "F01")
    changed = case.model_copy(update={"setup": ["An undeclared synthetic setup."]})
    calls = []

    def forbidden(*args) -> Never:
        calls.append(args)
        pytest.fail("unsupported setup invoked a provider")

    runtime = TrialExecutor(run_metadata(), forbidden, history, tmp_path,
                            FixturePolicy(), FrozenClock(EVIDENCE_TIME), HarnessLimits())
    result = await runtime(changed, 1)
    assert result.failure == "SETUP_NOT_RUN:UNSUPPORTED_DECLARATION"
    assert not calls and not result.provider_invoked
    assert not list(tmp_path.rglob("wealthcontext.sqlite"))
    case = case.model_copy(deep=True)
    case.required_facts[0].value = "GRADING_SECRET_SENTINEL"
    case.manual_checks = ["GRADING_SECRET_SENTINEL"]
    assert "GRADING_SECRET_SENTINEL" not in public_question(case, {})


@pytest.mark.anyio
async def test_scored_gate_precedes_policy_factory_and_any_runtime_fixture(tmp_path):
    # An isolated repository whose acceptance copy is never committed: the real checkout
    # may legitimately hold a committed freeze (SOL-P5-R1-RT-002).
    repository = tmp_path / "repository"
    shutil.copytree(ROOT / "acceptance", repository / "acceptance")
    git = shutil.which("git")
    if git:
        subprocess.run([git, "init", "-q", str(repository)], check=True)
    data_dir = tmp_path / "data"
    factory_calls = []
    policy = FixturePolicy()

    def forbidden(*args) -> Never:
        factory_calls.append(args)
        pytest.fail("uncommitted scored evaluation invoked a provider")

    with pytest.raises(EvaluationBlocked, match="committed"):
        await evaluate_provider(forbidden, repository=repository,
                                 acceptance_directory=repository / "acceptance",
                                 data_dir=data_dir, policy=policy, model=SafeProvider.model,
                                 settings=SETTINGS, mode="live_scored")
    assert not factory_calls and policy.verified == 0
    assert not data_dir.exists()


@pytest.mark.anyio
async def test_received_dataset_tampering_fails_closed_before_provider(tmp_path):
    receipt_directory = tmp_path / "received"
    history = write_history(receipt_directory)
    dataset = capture_dataset(receipt_directory)
    first = next(name for name in dataset["files"] if name != "index.json")
    (receipt_directory / first).write_bytes(b"changed received bytes")
    calls = []
    case = load_cases(ROOT / "acceptance")[0]

    def forbidden(*args) -> Never:
        calls.append(args)
        pytest.fail("tampered received data invoked a provider")

    runtime = TrialExecutor(run_metadata(), forbidden, history, tmp_path,
                            FixturePolicy(), FrozenClock(EVIDENCE_TIME), HarnessLimits(),
                            dataset=dataset, receipt_directory=receipt_directory)
    result = await runtime(case, 1)
    assert result.failure == "SETUP_NOT_RUN:RECEIVED_DATA_CHANGED"
    assert not calls and not result.provider_invoked
    evidence = result.model_dump(mode="json")["security_evidence"]
    assert evidence["environment"]["received_dataset_sha256"] == dataset["received_dataset_sha256"]


@pytest.mark.anyio
async def test_prompt_identity_guard_precedes_factory_and_fixture(tmp_path, history):
    def forbidden(*_args) -> Never:
        pytest.fail("changed prompt identity invoked a provider")

    changed = run_metadata().model_copy(update={"prompt_sha256": "b" * 64})
    runtime = TrialExecutor(changed, forbidden, history, tmp_path, FixturePolicy(),
                            FrozenClock(EVIDENCE_TIME), HarnessLimits())
    result = await runtime(load_cases(ROOT / "acceptance")[0], 1)
    assert result.failure == "SETUP_NOT_RUN:PROMPT_CHANGED"
    assert not result.provider_invoked
    assert not list(tmp_path.rglob("*.sqlite"))


@pytest.mark.anyio
async def test_full_declared_population_persists_ninety_isolated_honest_trials(tmp_path):
    calls = []

    def factory(case_id, trial):
        calls.append((case_id, trial))
        return SafeProvider(case_id)

    result = await evaluate_provider(
        factory, repository=ROOT, acceptance_directory=ROOT / "acceptance", data_dir=tmp_path,
        policy=FixturePolicy(), model=SafeProvider.model, settings=SETTINGS, mode="synthetic_test",
        evidence_clock=FrozenClock(EVIDENCE_TIME),
    )
    assert result.status == "completed"
    assert result.summary["recorded_trial_count"] == 90
    assert result.summary["missing_trial_count"] == 0
    assert not result.summary["scored"] and not result.summary["primary_financial_gate_passed"]
    assert len(calls) == 84  # Expired JWT and real policy outage each have no model trial.
    assert len(set(calls)) == len(calls)
    paths = list((tmp_path / "trials").rglob("wealthcontext.sqlite"))
    assert len(paths) == 90
    assert not (tmp_path / "wealthcontext.sqlite").exists()
    with read_snapshot(tmp_path / "evaluation.sqlite") as connection:
        rows = connection.execute(
            "SELECT payload FROM evaluation_trials ORDER BY case_id,trial",
        ).fetchall()
        assert len(rows) == 90
        recorded: list[dict[str, Any]] = [json.loads(row[0]) for row in rows]
        digests = {row["security_evidence"]["environment"]["received_dataset_sha256"]
                   for row in recorded}
        assert len(digests) == 1
        assert all(not (row["failure"] or "").startswith("SETUP_") for row in recorded), [
            (row["case_id"], row["failure"]) for row in recorded if row["failure"]
        ]
        assert {row["failure"] for row in recorded} <= {
            None, "AUTH_DENIED_BEFORE_PROVIDER", "POLICY_UNAVAILABLE_BEFORE_PROVIDER",
        }
