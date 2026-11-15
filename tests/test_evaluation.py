"""Hand acceptance/grader checks; no provider calls and no Git mutations."""

import hashlib
import json
import shutil
import subprocess
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from reference_oracle import evaluate, load_received_history
from wealthcontext.clock import FrozenClock
from wealthcontext.evaluation import (
    EvaluationBlocked,
    assert_scored_ready,
    freeze_manifest,
    grade_answer,
    load_cases,
    run_evaluation,
    summarize_trials,
    trial_from_harness,
    write_freeze_manifest,
)
from wealthcontext.evaluation_contracts import (
    AcceptanceCase,
    AttemptEvidence,
    EvaluationMode,
    EvaluationRun,
    GovernedResult,
    Grade,
    TrialRecord,
)
from wealthcontext.generator import write_history
from wealthcontext.provider_contracts import (
    HarnessRun,
    ProviderRequest,
    ProviderTurn,
    ToolCall,
    TurnRecord,
)

ROOT = Path(__file__).parents[1]
ACCEPTANCE = ROOT / "acceptance"
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
HASH = "a" * 64


def clean_case() -> AcceptanceCase:
    return load_cases(ACCEPTANCE)[0]


def evidence():
    case = clean_case()
    envelope = {
        "result_id": "r_current", "trace_id": "trace_current", "status": "COMPLETE",
        "as_of": "2026-09-30", "valuation_date": "2026-09-30",
        "known_as_of": "2026-10-31T16:00:00Z", "error": None,
        "data": {"household_id": "WC-HH-0001", "assets_usd": "3000.00"},
        "metric_refs": case.requests[0].metric_refs, "source_refs": ["h_current"],
    }
    result = GovernedResult(tool="get_household_snapshot", arguments=case.requests[0].arguments,
                            envelope=envelope)
    rendered = {
        "mode": "verified", "markdown": "Household assets: USD 3000.00.",
        "claims": [{"id": "c1", "result_id": "r_current", "entity": {
            "type": "household", "id": "WC-HH-0001"}, "field": "assets_usd", "value": "3000.00",
            "unit": "USD", "household_id": "WC-HH-0001", "valuation_date": "2026-09-30",
            "known_as_of": "2026-10-31T16:00:00Z"}],
        "predicates": [], "caveats": [], "refusal": None,
    }
    return case, rendered, result


def trial(case_id="F01", number=1, *, passed=True, mode: EvaluationMode = "synthetic_test"):
    grade = Grade(passed=passed, diagnostics=[] if passed else ["manual test failure"])
    return TrialRecord(
        run_id="eval_test", case_id=case_id, trial=number, provider="anthropic", model="test-only",
        actual_model_ids=["synthetic-model"], settings={}, mode=mode,
        case_set_sha256=HASH, prompt_sha256=HASH, schema_sha256=HASH,
        started_at=NOW, completed_at=NOW, attempts=[AttemptEvidence(attempt=1, valid=True)],
        first_pass_valid=True, final_valid=True, retries=0, fallback_used=False,
        first_attempt_grade=grade, final_grade=grade,
    )


def test_public_questions_and_scenarios_are_the_original_twenty_plus_ten():
    catalogue = yaml.safe_load((ACCEPTANCE / "catalogue.yaml").read_text())
    original = [{key: item[key] for key in ("id", "question", "scenario")}
                for group in ("financial", "security_failure") for item in catalogue[group]]
    digest = hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":")).encode())
    assert digest.hexdigest() == "730e8c6c25e5b110f338d5a1fb303d55e6b547243912ba34cd821d031d87cadd"
    cases = load_cases(ACCEPTANCE)
    assert len(cases) == 30
    assert len([case for case in cases if case.category == "financial"]) == 20
    assert all(case.received_records and case.manual_checks and case.authoritative_sources
               for case in cases)


@pytest.fixture(scope="module")
def raw_history(tmp_path_factory):
    directory = tmp_path_factory.mktemp("public-acceptance-received")
    write_history(directory)  # Public file producer only; no latent/internal data is read.
    return load_received_history(directory)


def test_public_received_record_selectors_exist_in_exact_exported_events(raw_history):
    for case in load_cases(ACCEPTANCE):
        for expected in case.received_records:
            receipts = [receipt for receipt in raw_history.receipts
                        if receipt.source == expected.source_system
                        and receipt.feed == expected.feed
                        and receipt.day == expected.business_date
                        and receipt.recorded_at == expected.recorded_at]
            assert receipts, (case.id, expected)
            received_ids = {row.get("source_record_id")
                            for receipt in receipts for row in receipt.rows
                            if isinstance(row, dict)}
            assert set(expected.source_record_ids) <= received_ids, case.id


def test_manual_household_amounts_and_known_cutoffs_agree_with_independent_oracle(raw_history):
    compared = 0
    for case in load_cases(ACCEPTANCE):
        for expected in case.required_facts:
            if expected.entity_type != "household" or expected.field not in {
                "assets_usd", "cash_usd", "fresh_subtotal_usd",
            }:
                continue
            result = evaluate(raw_history, household_ids=[expected.entity_id],
                              as_of=expected.valuation_date, known_as_of=expected.known_as_of)
            assert result["households"][0][expected.field] == expected.value, case.id
            compared += 1
    assert compared >= 12
    # Explicit calculations remain separate from both implementations.
    assert Decimal("100") * Decimal("25") + Decimal("500") == Decimal("3000")
    assert Decimal("110") * Decimal("25") + Decimal("799") == Decimal("3549")
    assert Decimal("220") + Decimal("225") + Decimal("375") + Decimal("410") == Decimal("1230")
    assert Decimal("100") / Decimal("1000") * 100 == Decimal("10")


def test_grader_accepts_required_resolved_fact_without_parsing_financial_prose():
    case, answer, result = evidence()
    assert grade_answer(case, answer, [result]).passed
    answer["markdown"] = "Unverified qualitative wording is not a numeric source."
    assert grade_answer(case, answer, [result]).passed


@pytest.mark.parametrize("field,value", [
    ("value", "3001.00"), ("unit", "pct"), ("household_id", "WC-HH-0002"),
    ("valuation_date", "2026-10-01"), ("known_as_of", "2026-10-31T15:59:59Z"),
    ("result_id", "r_other"), ("entity", {"type": "account", "id": "WC-HH-0001"}),
])
def test_grader_rejects_wrong_value_unit_entity_household_and_both_clocks(field, value):
    case, answer, result = evidence()
    answer["claims"][0][field] = value
    assert not grade_answer(case, answer, [result]).passed


def test_grader_rejects_wrong_metric_scope_and_unverified_fallback():
    case, answer, result = evidence()
    missing_metric = result.model_copy(deep=True)
    missing_metric.envelope["metric_refs"] = []
    assert not grade_answer(case, answer, [missing_metric]).passed
    answer["mode"] = "fallback"
    assert not grade_answer(case, answer, [result]).passed


def test_grader_requires_caveats_predicates_and_the_actual_refusal_result():
    case, answer, result = evidence()
    changed = case.model_dump(mode="json")
    changed["required_predicates"] = [{"request": "snapshot", "entity_type": "household",
        "entity_id": "WC-HH-0001", "kind": "completeness", "field": "status", "expect": "COMPLETE"}]
    changed["required_caveats"] = [{"flag": "SOURCE_UNAVAILABLE", "subject": "WC-AC-0002"}]
    amended = AcceptanceCase.model_validate(changed)
    assert not grade_answer(amended, answer, [result]).passed
    answer["predicates"] = [{"result_id": "r_current", "entity": {"type": "household",
        "id": "WC-HH-0001"}, "kind": "completeness", "field": "status",
        "expect": "COMPLETE", "value": "COMPLETE",
        "household_id": "WC-HH-0001", "valuation_date": "2026-09-30",
        "known_as_of": "2026-10-31T16:00:00Z"}]
    answer["caveats"] = changed["required_caveats"]
    assert grade_answer(amended, answer, [result]).passed
    answer["predicates"][0]["value"] = "INCOMPLETE"
    assert not grade_answer(amended, answer, [result]).passed


@pytest.fixture
def acceptance_copy(tmp_path):
    target = tmp_path / "acceptance"
    shutil.copytree(ACCEPTANCE, target)
    return target


def test_freeze_requires_every_exact_manifest_and_case_byte_in_head(acceptance_copy, monkeypatch):
    write_freeze_manifest(acceptance_copy)
    committed = {path.name: path.read_bytes()
                 for path in acceptance_copy.iterdir() if path.is_file()}
    calls = []

    def git_show(command, **kwargs):
        assert command[:3] == ["git", "-C", str(acceptance_copy.parent)]
        assert command[3] == "show" and command[4].startswith("HEAD:acceptance/")
        name = command[4].split("/")[-1]
        calls.append(name)
        return subprocess.CompletedProcess(command, 0, committed[name], b"")
    monkeypatch.setattr(subprocess, "run", git_show)
    expected = freeze_manifest(acceptance_copy)["case_set_sha256"]
    assert assert_scored_ready(acceptance_copy.parent, acceptance_copy) == expected
    assert len(calls) == 32  # The manifest, catalogue and thirty cases.
    case = acceptance_copy / "F01.yaml"
    case.write_text(case.read_text() + "\n# changed even without semantic change\n")
    with pytest.raises(EvaluationBlocked, match="changed after"):
        assert_scored_ready(acceptance_copy.parent, acceptance_copy)
    write_freeze_manifest(acceptance_copy)
    with pytest.raises(EvaluationBlocked, match="committed"):
        assert_scored_ready(acceptance_copy.parent, acceptance_copy)


def test_uncommitted_freeze_and_boolean_versions_cannot_score(acceptance_copy, monkeypatch):
    write_freeze_manifest(acceptance_copy)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 128, b"", b"no such committed file",
    ))
    with pytest.raises(EvaluationBlocked, match="committed"):
        assert_scored_ready(acceptance_copy.parent, acceptance_copy)
    frozen = json.loads((acceptance_copy / "freeze.json").read_text())
    frozen["version"] = True
    (acceptance_copy / "freeze.json").write_text(json.dumps(frozen))
    with pytest.raises(EvaluationBlocked):
        assert_scored_ready(acceptance_copy.parent, acceptance_copy)


def test_summary_counts_all_three_and_never_promotes_synthetic_or_fallback_runs():
    cases = load_cases(ACCEPTANCE)
    records = [trial(case.id, number) for case in cases for number in (1, 2, 3)]
    summary = summarize_trials(cases, records)
    assert summary["first_attempt_pass_count"] == summary["first_attempt_denominator"] == 84
    assert summary["predeclared_trial_denominator"] == 90
    # Fake model trials are not transport-boundary security evidence.
    assert summary["pre_provider_check_fail_count"] == 6
    assert summary["financial_pass_all_three_count"] == 20
    assert summary["primary_financial_gate_passed"] is False
    assert summary["scored"] is False
    missing = summarize_trials(cases, records[:-1])
    assert missing["missing_trial_count"] == 1 and not missing["pass_all_three"]["Q10"]
    with pytest.raises(ValueError, match="duplicate"):
        summarize_trials(cases, records + records[:1])
    bad = records[0].model_dump(mode="json")
    bad["fallback_used"] = True
    with pytest.raises(ValidationError, match="fallback"):
        TrialRecord.model_validate(bad)


def test_auth_denial_is_separate_security_evidence_not_a_model_pass():
    value = trial("Q04").model_dump(mode="json")
    value.update(attempts=[], provider_invoked=False, actual_model_ids=[], first_pass_valid=False,
                 final_valid=False, failure="AUTH_DENIED_BEFORE_PROVIDER",
                 first_attempt_grade={"passed": False, "diagnostics": ["not invoked"]},
                 final_grade={"passed": False, "diagnostics": ["not invoked"]},
                 security_check_passed=True, security_evidence={"trace_id": "actual_denial_trace"})
    record = TrialRecord.model_validate(value)
    summary = summarize_trials(load_cases(ACCEPTANCE), [record])
    assert summary["provider_not_invoked_count"] == 1
    assert summary["security_check_pass_count"] == 1 and summary["final_pass_count"] == 0


@pytest.mark.anyio
async def test_runner_refuses_scored_mode_before_callback_and_records_callback_failures(
    acceptance_copy, monkeypatch,
):
    cases = load_cases(acceptance_copy)
    write_freeze_manifest(acceptance_copy)
    run = EvaluationRun(id="eval_test", provider="anthropic", model="test-only", settings={},
                        mode="live_scored", status="running", recorded_at=NOW,
                        case_set_sha256=freeze_manifest(acceptance_copy)["case_set_sha256"],
                        prompt_sha256=HASH, schema_sha256=HASH)
    calls = []

    async def fail(case, number):
        calls.append((case.id, number))
        raise TimeoutError("DO-NOT-STORE-THIS-SECRET-LIKE-EXCEPTION-BODY")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 128, b"", b"uncommitted",
    ))
    with pytest.raises(EvaluationBlocked):
        await run_evaluation(run, cases, fail, repository=acceptance_copy.parent,
                             acceptance_directory=acceptance_copy)
    assert not calls
    run.mode = "synthetic_test"
    records = await run_evaluation(run, cases, fail, repository=acceptance_copy.parent,
                                   acceptance_directory=acceptance_copy, clock=FrozenClock(NOW))
    assert len(records) == len(calls) == 90
    assert all(record.failure == "CALLBACK_FAILURE:TimeoutError" for record in records)
    assert "SECRET" not in json.dumps([record.model_dump(mode="json") for record in records])
    assert all(record.provider_invoked is None for record in records)
    summary = summarize_trials(cases, records)
    assert summary["provider_invocation_unknown_count"] == 90
    assert summary["provider_invoked_count"] == summary["provider_not_invoked_count"] == 0
    assert summary["failure_count"] == summary["raw_failure_count"] == 90
    assert summary["pre_provider_check_pass_count"] == 0


def captured_evidence():
    case, answer, governed = evidence()
    run = EvaluationRun(id="eval_test", provider="anthropic", model="requested-test-model",
                        settings={}, mode="synthetic_test", status="running", recorded_at=NOW,
                        case_set_sha256=HASH, prompt_sha256=HASH, schema_sha256=HASH)
    terminal = {"result_id": "r_terminal", "status": "COMPLETE", "data": answer, "error": None}
    captured = HarnessRun(
        run_id="harness_test", conversation_id="conversation_test", provider="anthropic",
        principal_id=case.principal.id, principal_role=case.principal.role,
        model="returned-test-model-version", settings={}, mode="synthetic_test",
        prompt_sha256=HASH, schema_sha256=HASH, started_at=NOW, completed_at=NOW,
        turns=[TurnRecord(request=ProviderRequest(system_prompt="test", messages=[], tools=[]),
                          response=ProviderTurn(model="returned-test-model-version", calls=[
                              ToolCall(id="fetch", name=governed.tool,
                                       arguments=governed.arguments),
                          ]), envelopes=[governed.envelope])],
        terminal_attempts=[{"kind": "provider", "candidate": {}, "result": terminal}],
        final_result=terminal, first_pass_valid=True, final_valid=True,
    )
    return case, run, captured


def test_harness_conversion_preserves_actual_model_and_separates_fallback_validity():
    case, run, captured = captured_evidence()
    record = trial_from_harness(run, case, 1, captured)
    assert record.first_attempt_grade.passed and record.final_grade.passed
    assert record.actual_model_ids == ["returned-test-model-version"]
    assert record.model == "requested-test-model"
    captured.fallback = True
    captured.failure = "VERIFICATION_FAILED"
    record = trial_from_harness(run, case, 1, captured)
    assert record.rendered_answer_valid and not record.final_valid
    assert not record.final_grade.passed and record.fallback_used


def test_harness_conversion_preserves_first_failure_and_last_success_after_retry():
    case, run, captured = captured_evidence()
    captured.terminal_attempts.insert(0, {"kind": "provider", "candidate": {}, "result": {
        "status": "ERROR", "data": None, "error": {"code": "INVALID_ANSWER"},
    }})
    captured.first_pass_valid = False
    captured.retries = 1
    record = trial_from_harness(run, case, 1, captured)
    assert record.retries == 1 and not record.first_attempt_grade.passed
    assert record.final_grade.passed
    captured.principal_id = "ADV-02"
    with pytest.raises(EvaluationBlocked, match="case principal"):
        trial_from_harness(run, case, 1, captured)


def test_capped_source_disclosure_requires_received_rows_and_both_resolved_counts():
    case, answer, snapshot = evidence()
    raw_case = case.model_dump(mode="json")
    raw_case["required_source_disclosure"] = True
    raw_case["requests"].append({
        "alias": "sources", "tool": "get_data_provenance", "arguments": {"reference": "h_current"},
        "status": "COMPLETE", "metric_refs": [],
    })
    amended = AcceptanceCase.model_validate(raw_case)
    row = {"source_record_id": "actual_received_row", "source_system": "PAS-A",
           "business_date": "2026-09-30", "payload_hash": HASH}
    provenance = GovernedResult.model_validate({
        "tool": "get_data_provenance", "arguments": {"reference": "h_current"},
        "envelope": {"result_id": "r_sources", "status": "COMPLETE", "error": None,
                     "valuation_date": "2026-09-30", "known_as_of": "2026-10-31T16:00:00Z",
                     "metric_refs": [], "data": {
            "items": [row], "item_count": 1, "total_item_count": 2, "truncated": True,
        }},
    })
    answer["sources"] = [{"result_id": "r_sources", "records": [row]}]
    assert not grade_answer(amended, answer, [snapshot, provenance]).passed
    answer["claims"].extend({"result_id": "r_sources", "field": field, "value": value,
                             "household_id": None, "valuation_date": "2026-09-30",
                             "known_as_of": "2026-10-31T16:00:00Z"}
                            for field, value in (("item_count", 1), ("total_item_count", 2)))
    assert grade_answer(amended, answer, [snapshot, provenance]).passed
    changed = deepcopy(answer)
    changed["sources"][0]["records"][0]["payload_hash"] = "b" * 64
    assert not grade_answer(amended, changed, [snapshot, provenance]).passed


def test_revocation_case_requires_no_new_retrieval_and_no_reused_financial_references():
    case = next(case for case in load_cases(ACCEPTANCE) if case.id == "Q02")
    answer = {"mode": "verified", "markdown": "Authorized evidence is unavailable.",
              "claims": [], "predicates": [], "sources": [], "refusal": None}
    assert grade_answer(case, answer, []).passed
    _, reused, observed = evidence()
    assert not grade_answer(case, reused, []).passed
    assert not grade_answer(case, answer, [observed]).passed


@pytest.mark.anyio
async def test_runner_persists_each_trial_before_next_callback_and_stops_on_sink_failure(
    acceptance_copy,
):
    cases = load_cases(acceptance_copy)
    run = EvaluationRun(id="eval_test", provider="anthropic", model="test-only", settings={},
                        mode="synthetic_test", status="running", recorded_at=NOW,
                        case_set_sha256=freeze_manifest(acceptance_copy)["case_set_sha256"],
                        prompt_sha256=HASH, schema_sha256=HASH)
    actions = []

    async def callback(case, number):
        actions.append(("invoke", case.id, number))
        result = trial(case.id, number)
        result.case_set_sha256 = run.case_set_sha256
        return result

    async def sink(record):
        actions.append(("persist", record.case_id, record.trial))
        if record.trial == 2:
            raise OSError("durable write failed")

    with pytest.raises(OSError, match="durable write"):
        await run_evaluation(run, cases, callback, repository=acceptance_copy.parent,
                             acceptance_directory=acceptance_copy, sink=sink)
    assert actions == [("invoke", "F01", 1), ("persist", "F01", 1),
                       ("invoke", "F01", 2), ("persist", "F01", 2)]


def test_public_component_exposure_and_blocking_facts_agree_semantically_with_raw_oracle(
    raw_history,
):
    """Independent wire labels are normalized here, never imposed on either implementation."""
    cache = {}
    checked = 0
    finance_codes = {"FX_UNAVAILABLE", "PRICE_STALE", "SOURCE_UNAVAILABLE",
                     "CLOSED_ACCOUNT_WITH_BALANCE", "HELD_AWAY_EXCLUDED"}
    for case in load_cases(ACCEPTANCE):
        if case.category != "financial" and case.id != "Q10":
            continue
        for fact in case.required_facts:
            if fact.entity_type == "result_summary" and fact.field != "applied_threshold_pct":
                # Provenance counts describe service evidence, not financial oracle output.
                continue
            household = fact.household_id or case.household_ids[0]
            key = household, fact.valuation_date, fact.known_as_of
            if key not in cache:
                cache[key] = evaluate(raw_history, household_ids=[household],
                                      as_of=fact.valuation_date, known_as_of=fact.known_as_of)
            reference = cache[key]
            result = reference["households"][0]
            actual = None
            if fact.entity_type == "household":
                actual = (len(result["included_account_ids"]) if fact.field == "item_count"
                          else result.get(fact.field))
            elif fact.entity_type == "position" and fact.field in {
                "quantity", "price_usd", "market_value_usd", "fx_rate",
            }:
                account, security = fact.entity_id.split(":", 1)
                position = next(row for row in result["positions"]
                                if (row["account_id"], row["security_id"]) == (account, security))
                actual = (position["fx"]["usd_rate"] if fact.field == "fx_rate" else
                          position["price"] if fact.field == "price_usd" else position[fact.field])
            elif fact.entity_type in {"issuer", "security"}:
                group, identifier = ("issuer_exposures", "issuer_id") if (
                    fact.entity_type == "issuer"
                ) else ("fund_vehicle_exposures", "security_id")
                row = next(row for row in result[group]["items"]
                           if row[identifier] == fact.entity_id)
                actual = row["market_value_usd" if fact.field == "exposure_usd" else fact.field]
            elif fact.field == "applied_threshold_pct":
                actual = reference["applied_threshold_pct"]
            if actual is not None:
                assert Decimal(str(actual)) == Decimal(str(fact.value)), (case.id, fact)
                checked += 1
        key = case.household_ids[0], case.valuation_date, case.known_as_of
        if key not in cache:
            cache[key] = evaluate(raw_history, household_ids=[key[0]], as_of=key[1],
                                  known_as_of=key[2])
        reference = cache[key]
        flags = {(flag["code"], flag["subject"].replace(":CASH:", ":"))
                 for flag in reference["households"][0]["quality_flags"]}
        for required in case.required_caveats:
            if required.flag in finance_codes:
                assert (required.flag, required.subject) in flags, (case.id, required)
        if case.id == "F20":
            expected = next(check.value for check in case.result_checks
                            if check.path == "data.indeterminate.*.reason")
            assert expected == "NON_POSITIVE_ASSETS"
            assert reference["indeterminate"][0]["reason"] == "NONPOSITIVE_ASSETS"
        if case.id == "F19":
            assert [row["issuer_id"] for row in reference["breaches"]] == ["WC-ISS-0001"]
            stock = reference["households"][0]["issuer_exposures"]["items"][0]
            assert (stock["market_value_usd"], stock["concentration_pct"]) == ("100.00", "10.00")
            assert "WC-ISS-0017" not in {
                row["issuer_id"] for row in reference["households"][0]["issuer_exposures"]["items"]
            }
    assert checked >= 25
