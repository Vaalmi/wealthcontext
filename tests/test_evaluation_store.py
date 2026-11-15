"""Evaluation evidence is immutable, population-bound and never a summary input."""

import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from wealthcontext.context_service import ContextService
from wealthcontext.database import migrate, write_transaction
from wealthcontext.evaluation import (
    EvaluationBlocked,
    freeze_manifest,
    load_cases,
    write_freeze_manifest,
)
from wealthcontext.evaluation_contracts import AttemptEvidence, EvaluationRun, Grade, TrialRecord
from wealthcontext.evaluation_store import (
    EvaluationIntegrityError,
    EvaluationStoreError,
    complete_evaluation_run,
    create_evaluation_run,
    evaluation_summaries,
    record_trial,
)
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyDecision
from wealthcontext.storage import canonical, digest, migrate_business, read_snapshot
from wealthcontext.telemetry import create_provider

ROOT = Path(__file__).parents[1]
ACCEPTANCE = ROOT / "acceptance"
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
END = NOW + timedelta(hours=1)


@pytest.fixture
def database(tmp_path: Path):
    path = tmp_path / "evaluation.sqlite"
    migrate(path)
    migrate_business(path)
    return path


def run_record(**changes):
    return EvaluationRun.model_validate({
        "id": "eval_test", "provider": "anthropic", "model": "synthetic-test-model",
        "mode": "synthetic_test", "status": "running", "recorded_at": NOW,
        "case_set_sha256": freeze_manifest(ACCEPTANCE)["case_set_sha256"],
        "prompt_sha256": "a" * 64, "schema_sha256": "b" * 64, "settings": {"temperature": 0},
        **changes,
    })


def create(database, run=None):
    run = run or run_record()
    return create_evaluation_run(database, run, repository=ROOT, acceptance_directory=ACCEPTANCE)


def trial(run, case_id="F01", number=1, **changes):
    return TrialRecord.model_validate({
        "run_id": run.id, "case_id": case_id, "trial": number, "provider": run.provider,
        "model": run.model, "actual_model_ids": ["returned-synthetic-model"],
        "settings": run.settings, "mode": run.mode, "case_set_sha256": run.case_set_sha256,
        "prompt_sha256": run.prompt_sha256, "schema_sha256": run.schema_sha256,
        "started_at": NOW, "completed_at": NOW + timedelta(minutes=1),
        "attempts": [AttemptEvidence(attempt=1, valid=True).model_dump()],
        "first_pass_valid": True, "final_valid": True, "rendered_answer_valid": True,
        "retries": 0, "fallback_used": False,
        "first_attempt_grade": Grade(passed=True).model_dump(),
        "final_grade": Grade(passed=True).model_dump(), **changes,
    })


def reports(database, known=END):
    with read_snapshot(database) as connection:
        return evaluation_summaries(connection, known)


def test_creation_captures_actual_public_population_and_is_exactly_idempotent(database):
    original = run_record()
    started = create(database, original)
    assert started.summary["expected_trial_count"] == 90
    assert create(database, original) == started
    with read_snapshot(database) as connection:
        row = connection.execute("SELECT * FROM evaluation_runs").fetchone()
        assert json.loads(row["cases_json"]) == [
            case.model_dump(mode="json") for case in load_cases(ACCEPTANCE)
        ]
        assert digest(row["cases_json"]) == row["cases_hash"]
        assert digest(row["payload"]) == row["payload_hash"]
        assert digest(row["manifest_json"]) == row["manifest_hash"]
    with pytest.raises(EvaluationStoreError, match="different evaluation evidence"):
        create(database, run_record(model="changed-model"))
    with pytest.raises(EvaluationStoreError, match="caller summary"):
        create(database, run_record(summary={"primary_financial_gate_passed": True}))


def test_scored_creation_blocks_before_any_run_is_written(database, tmp_path: Path):
    acceptance = tmp_path / "uncommitted" / "acceptance"
    shutil.copytree(ACCEPTANCE, acceptance)
    write_freeze_manifest(acceptance)
    scored = run_record(mode="live_scored")
    with pytest.raises(EvaluationBlocked, match="committed"):
        create_evaluation_run(database, scored, repository=acceptance.parent,
                              acceptance_directory=acceptance)
    assert reports(database) == []


def test_trial_exact_retry_does_not_replace_evidence(database):
    run = create(database)
    evidence = trial(run, transcript={"private_prompt": "SENSITIVE-PROMPT"})
    record_trial(database, evidence)
    record_trial(database, evidence)
    with pytest.raises(EvaluationStoreError, match="different evidence"):
        record_trial(database, trial(run, transcript={"private_prompt": "CHANGED"}))
    summary = reports(database)[0]
    assert summary.recorded_trial_count == 1 and summary.missing_trial_count == 89
    assert summary.first_attempt_denominator == 84 and summary.first_attempt_pass_count == 1
    assert "SENSITIVE-PROMPT" not in summary.model_dump_json()


@pytest.mark.parametrize("change", [
    {"provider": "openai"}, {"model": "other"}, {"mode": "replay"},
    {"settings": {"temperature": False}}, {"case_set_sha256": "c" * 64},
    {"prompt_sha256": "c" * 64}, {"schema_sha256": "c" * 64}, {"case_id": "F99"},
    {"started_at": NOW - timedelta(seconds=1)},
])
def test_trial_metadata_settings_and_population_must_match_exactly(database, change):
    run = create(database)
    with pytest.raises(EvaluationStoreError, match="metadata"):
        record_trial(database, trial(run, **change))
    assert reports(database)[0].recorded_trial_count == 0


def test_invalid_trial_number_is_revalidated_at_store_boundary(database):
    run = create(database)
    with pytest.raises(ValidationError):
        record_trial(database, trial(run).model_copy(update={"trial": 4}))


def test_missing_failure_fallback_and_not_invoked_counts_are_honest(database):
    run = create(database)
    invalid = Grade(passed=False, diagnostics=["synthetic failure"]).model_dump()
    record_trial(database, trial(
        run, number=1, attempts=[{"attempt": 1, "valid": False}, {"attempt": 2, "valid": False}],
        first_pass_valid=False, final_valid=False, rendered_answer_valid=True, retries=1,
        fallback_used=True, first_attempt_grade=invalid, final_grade=invalid,
    ))
    record_trial(database, trial(
        run, number=2, attempts=[], actual_model_ids=[], first_pass_valid=False, final_valid=False,
        rendered_answer_valid=False, provider_invoked=False, failure="PROVIDER_NOT_INVOKED",
        first_attempt_grade=invalid, final_grade=invalid,
    ))
    report = reports(database)[0]
    assert (report.recorded_trial_count, report.missing_trial_count) == (2, 88)
    assert (report.retry_count, report.fallback_count, report.failure_count) == (1, 1, 1)
    assert (report.provider_invoked_count, report.provider_not_invoked_count) == (1, 1)
    assert report.first_attempt_pass_count == report.final_pass_count == 0
    assert report.rendered_answer_valid_count == 1 and report.final_valid_count == 0
    assert report.financial_pass_all_three_count == 0


def test_completed_requires_every_slot_and_terminal_state_cannot_change(database):
    run = create(database)
    record_trial(database, trial(run))
    with pytest.raises(EvaluationStoreError, match="completion must follow"):
        complete_evaluation_run(database, run.id, NOW, status="failed")
    with pytest.raises(EvaluationStoreError, match="every declared trial"):
        complete_evaluation_run(database, run.id, END)
    with pytest.raises(EvaluationStoreError, match="cannot contain trial"):
        complete_evaluation_run(database, run.id, END, status="deferred_not_run")
    failed = complete_evaluation_run(database, run.id, END, status="failed")
    assert failed.status == "failed" and failed.summary["missing_trial_count"] == 89
    assert complete_evaluation_run(database, run.id, END, status="failed") == failed
    record_trial(database, trial(run))
    with pytest.raises(EvaluationStoreError, match="Terminal"):
        record_trial(database, trial(run, number=2))
    with pytest.raises(EvaluationStoreError, match="different terminal"):
        complete_evaluation_run(database, run.id, END + timedelta(seconds=1), status="failed")


def test_complete_replay_never_passes_scored_gates_even_when_every_case_passes(database):
    run = create(database, run_record(mode="replay"))
    for case in load_cases(ACCEPTANCE):
        for number in (1, 2, 3):
            record_trial(database, trial(run, case.id, number))
    completed = complete_evaluation_run(database, run.id, END)
    assert completed.summary["financial_pass_all_three_count"] == 20
    assert completed.summary["security_pass_all_three_count"] == 8
    report = reports(database)[0]
    assert report.final_pass_count == report.first_attempt_pass_count == 84
    assert report.mode == "replay" and not report.scored
    assert not report.primary_financial_gate_passed and not report.security_model_cases_passed


def test_deferred_uninvoked_run_and_historical_summary_cutoff(database):
    run = create(database, run_record(provider="google", mode="live_calibration"))
    assert reports(database, NOW - timedelta(seconds=1)) == []
    complete_evaluation_run(database, run.id, END, status="deferred_not_run")
    earlier = reports(database, NOW)[0]
    assert earlier.status == "running" and earlier.completed_at is None
    deferred = reports(database)[0]
    assert deferred.status == "deferred_not_run" and deferred.recorded_trial_count == 0
    assert deferred.missing_trial_count == 90 and deferred.actual_model_ids == []
    assert not deferred.primary_financial_gate_passed


def test_scored_gate_requires_full_completed_population(database, monkeypatch):
    # Synthetic unit evidence exercises lifecycle logic; no provider is invoked.
    original = run_record(mode="live_scored")
    checks = []

    def ready(repository, directory):
        checks.append((repository, directory))
        return original.case_set_sha256

    monkeypatch.setattr("wealthcontext.evaluation_store.assert_scored_ready", ready)
    run = create(database, original)
    assert checks == [(ROOT, ACCEPTANCE)]
    for case in load_cases(ACCEPTANCE):
        for number in (1, 2, 3):
            record_trial(database, trial(run, case.id, number))
    assert reports(database)[0].primary_financial_gate_passed is False
    complete_evaluation_run(database, run.id, END)
    report = reports(database)[0]
    assert report.scored and report.primary_financial_gate_passed
    assert report.security_model_cases_passed and report.financial_pass_all_three_count == 20


def test_summary_uses_captured_cases_not_later_live_files(database, monkeypatch):
    create(database)

    def forbidden(*args):
        pytest.fail("summary consulted mutable acceptance files")

    monkeypatch.setattr("wealthcontext.evaluation_store.load_cases", forbidden)
    report = reports(database)[0]
    assert report.question_count == 30 and report.expected_trial_count == 90


@pytest.mark.parametrize("column", ["payload", "cases_json", "manifest_json"])
def test_corrupt_hashed_run_evidence_fails_closed(database, column):
    create(database)
    with write_transaction(database) as connection:
        connection.execute("DROP TRIGGER evaluation_runs_terminal_transition")
        connection.execute(f"UPDATE evaluation_runs SET {column}='{{}}'")
    with pytest.raises(EvaluationIntegrityError):
        reports(database)


@pytest.mark.parametrize("tamper", ["gate", "boolean_count", "settings"])
def test_even_rehashed_forged_terminal_evidence_is_rejected(database, tamper):
    run = create(database)
    complete_evaluation_run(database, run.id, END, status="deferred_not_run")
    with write_transaction(database) as connection:
        row = connection.execute("SELECT final_payload FROM evaluation_runs").fetchone()
        payload = json.loads(row[0])
        if tamper == "gate":
            payload["summary"]["financial_pass_all_three_count"] = 20
            payload["summary"]["primary_financial_gate_passed"] = True
        elif tamper == "boolean_count":
            payload["summary"]["provider_invoked_count"] = False
        else:
            payload["settings"]["temperature"] = False
        text = canonical(payload)
        connection.execute("DROP TRIGGER evaluation_runs_terminal_transition")
        connection.execute("UPDATE evaluation_runs SET final_payload=?,final_hash=?",
                           (text, digest(text)))
    with pytest.raises(EvaluationIntegrityError):
        reports(database)


def test_trial_hash_and_metadata_indexes_are_checked_on_read(database):
    run = create(database)
    record_trial(database, trial(run))
    with write_transaction(database) as connection:
        connection.execute("DROP TRIGGER evaluation_trials_no_update")
        connection.execute("UPDATE evaluation_trials SET case_id='Q01'")
    with pytest.raises(EvaluationIntegrityError):
        reports(database)


def test_database_prevents_evidence_rewrites(database):
    run = create(database)
    record_trial(database, trial(run))
    for query in ("UPDATE evaluation_trials SET payload='{}'", "DELETE FROM evaluation_trials",
                  "UPDATE evaluation_runs SET model='different'", "DELETE FROM evaluation_runs"):
        with pytest.raises(sqlite3.IntegrityError), write_transaction(database) as connection:
            connection.execute(query)


class CompliancePolicy(PolicyClient):
    def __init__(self):
        super().__init__("http://not-contacted.invalid", "sha256:" + "a" * 64)

    async def decide(self, principal, action, resources, facts, revision, request_business_date):
        allowed = principal.role == "compliance" and action == "view_eval_reports"
        return PolicyDecision(allow=allowed, allowed_ids=[],
                              projection="no_pii" if principal.role == "ops" else "full",
                              reason="CURRENT_GRANT" if allowed else "NO_CURRENT_GRANT",
                              policy_revision=self.expected_revision, entitlement_revision=revision)


@pytest.mark.anyio
async def test_compliance_summary_is_derived_and_other_roles_are_denied_before_read(
    database, tmp_path: Path, monkeypatch,
):
    run = create(database)
    record_trial(database, trial(run, transcript={
        "prompt": "SECRET-PROMPT", "amount": "987654.32",
    }))
    with write_transaction(database) as connection:
        connection.execute("INSERT INTO entitlement_revisions VALUES (1,?)", (NOW.isoformat(),))
    provider = create_provider(tmp_path / "trace.jsonl")
    try:
        service = ContextService(database, CompliancePolicy(), provider.get_tracer("test"))
        result = await service.execute(Principal("COMP-01", "compliance"), END, "eval_reports", {})
        assert result["status"] == "COMPLETE", result
        item = result["data"]["items"][0]
        assert item["recorded_trial_count"] == 1 and item["question_count"] == 30
        assert "SECRET-PROMPT" not in canonical(result) and "987654.32" not in canonical(result)
        with read_snapshot(database) as connection:
            assert service.result_scopes(connection, result["result_id"]) == []

        def forbidden(*args):
            pytest.fail("evaluation records read before compliance authorization")

        monkeypatch.setattr("wealthcontext.secondary.evaluation_summaries", forbidden)
        for principal in (Principal("ADV-01", "advisor"), Principal("OPS-01", "ops"),
                          Principal("ENG-01", "engineer")):
            result = await service.execute(principal, END, "eval_reports", {})
            assert result["error"]["code"] == "NOT_AUTHORIZED"
            with read_snapshot(database) as connection:
                assert service.result_scopes(connection, result["result_id"]) == []
    finally:
        provider.shutdown()


def test_positive_synthetic_pre_provider_checks_are_durable_but_require_completed_run(database):
    # Synthetic contract records test store derivation; real HTTP probes are tested separately.
    run = create(database)
    for case in load_cases(ACCEPTANCE):
        for number in (1, 2, 3):
            if case.execution == "pre_provider_security":
                evidence = trial(
                    run, case.id, number, attempts=[], actual_model_ids=[], provider_invoked=False,
                    first_pass_valid=False, final_valid=False, rendered_answer_valid=False,
                    first_attempt_grade=Grade(passed=False).model_dump(),
                    final_grade=Grade(passed=False).model_dump(),
                    failure="AUTH_DENIED_BEFORE_PROVIDER" if case.id == "Q04"
                    else "POLICY_UNAVAILABLE_BEFORE_PROVIDER",
                    security_check_passed=True,
                    security_evidence={"classification": "synthetic_store_contract_fixture"},
                )
            else:
                evidence = trial(run, case.id, number)
            record_trial(database, evidence)
    running = reports(database)[0]
    assert running.recorded_trial_count == 90 and running.missing_trial_count == 0
    assert running.pre_provider_check_pass_count == 6
    assert not running.pre_provider_security_checks_passed
    complete_evaluation_run(database, run.id, END)
    completed = reports(database)[0]
    assert completed.pre_provider_security_checks_passed
    assert completed.provider_not_invoked_count == 6
    assert completed.provider_invoked_count == 84
    assert completed.provider_invocation_unknown_count == 0
    assert completed.raw_failure_count == 6 and completed.failure_count == 0
    assert completed.mode == "synthetic_test" and not completed.scored
    assert not completed.primary_financial_gate_passed and not completed.security_model_cases_passed


# Owner decision 2026-11-05 (ADR 0008): per-trial transport retries are recorded and
# summarized apart from the single structured answer retry.
def _turns(*counts):
    return {"turns": [{"transport_retries": count} for count in counts],
            "transport_retries": sum(counts)}


def test_transport_retries_are_summarized_from_each_trials_recorded_turns(database):
    run = create(database)
    record_trial(database, trial(run, number=1, transcript=_turns(1, 2)))
    record_trial(database, trial(run, number=2, transcript=_turns(0, 0)))
    record_trial(database, trial(run, number=3))
    summary = create(database).summary
    assert summary["transport_retry_count"] == 3
    assert summary["transport_retried_trial_count"] == 1
    assert summary["retry_count"] == 0
    failed = complete_evaluation_run(database, run.id, END, status="failed")
    assert failed.summary["transport_retry_count"] == 3
    assert reports(database)[0].recorded_trial_count == 3


@pytest.mark.parametrize("transcript", [
    {"turns": [{"transport_retries": 1}], "transport_retries": 0},
    {"turns": [{"transport_retries": 0}], "transport_retries": 2},
    {"turns": [{"transport_retries": -1}], "transport_retries": -1},
    {"turns": [{"transport_retries": True}], "transport_retries": 1},
    {"turns": "not-a-list"},
])
def test_inconsistent_transport_retries_are_rejected_on_write_and_read(database, transcript):
    run = create(database)
    with pytest.raises(EvaluationStoreError, match="transport retries"):
        record_trial(database, trial(run, transcript=transcript))
    record_trial(database, trial(run))
    with write_transaction(database) as connection:
        row = connection.execute("SELECT payload FROM evaluation_trials").fetchone()
        payload = json.loads(row[0])
        payload["transcript"] = transcript
        text = canonical(payload)
        connection.execute("DROP TRIGGER evaluation_trials_no_update")
        connection.execute("UPDATE evaluation_trials SET payload=?,payload_hash=?",
                           (text, digest(text)))
    with pytest.raises(EvaluationIntegrityError):
        reports(database)


@pytest.mark.parametrize(("retries", "transcript_retries"), [(0, 1), (1, 0)])
def test_trial_answer_retries_must_match_the_harness_transcript(
    database, retries, transcript_retries,
):
    """SOL-C-R1-RETRY-001: a trial cannot undercount (or invent) its answer retry."""
    run = create(database)

    def retried(count, recorded):
        attempts = [{"attempt": 1, "valid": count == 0}]
        if count:
            attempts.append({"attempt": 2, "valid": True})
        first = Grade(passed=count == 0, diagnostics=[] if count == 0 else ["retried"])
        return trial(run, attempts=attempts, first_pass_valid=count == 0, retries=count,
                     first_attempt_grade=first.model_dump(), transcript={"retries": recorded})

    with pytest.raises(EvaluationStoreError, match="answer retries"):
        record_trial(database, retried(retries, transcript_retries))
    record_trial(database, retried(1, 1))
    assert reports(database)[0].retry_count == 1


def _drop_transport_counts(database):
    with write_transaction(database) as connection:
        row = connection.execute("SELECT final_payload FROM evaluation_runs").fetchone()
        payload = json.loads(row[0])
        for key in ("transport_retry_count", "transport_retried_trial_count"):
            del payload["summary"][key]
        text = canonical(payload)
        connection.execute("DROP TRIGGER evaluation_runs_terminal_transition")
        connection.execute("UPDATE evaluation_runs SET final_payload=?,final_hash=?",
                           (text, digest(text)))


def test_runs_completed_before_transport_retries_stay_readable_only_without_them(
    database, tmp_path,
):
    run = create(database)
    record_trial(database, trial(run))
    complete_evaluation_run(database, run.id, END, status="failed")
    _drop_transport_counts(database)
    assert reports(database)[0].status == "failed"
    migrated = tmp_path / "with-retries.sqlite"
    migrate(migrated)
    migrate_business(migrated)
    run = create(migrated)
    record_trial(migrated, trial(run, transcript=_turns(1)))
    complete_evaluation_run(migrated, run.id, END, status="failed")
    _drop_transport_counts(migrated)
    with pytest.raises(EvaluationIntegrityError):
        reports(migrated)
