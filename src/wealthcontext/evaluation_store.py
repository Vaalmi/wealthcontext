"""Immutable evaluation evidence and summaries derived from its captured population."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from wealthcontext.clock import utc_timestamp
from wealthcontext.database import write_transaction
from wealthcontext.evaluation import (
    assert_scored_ready,
    freeze_manifest,
    load_cases,
    summarize_trials,
)
from wealthcontext.evaluation_contracts import AcceptanceCase, EvaluationRun, TrialRecord
from wealthcontext.storage import digest
from wealthcontext.surface_contracts import EvalReportItem

TerminalStatus = Literal["completed", "failed", "deferred_not_run"]


class EvaluationStoreError(ValueError):
    """The requested write conflicts with the run's immutable evidence or lifecycle."""


class EvaluationIntegrityError(sqlite3.DatabaseError):
    """Stored evidence cannot be trusted; report reads must fail closed."""


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _read_json(payload: str, expected_hash: str) -> object:
    if digest(payload) != expected_hash:
        raise EvaluationIntegrityError("Evaluation evidence hash mismatch")
    try:
        value = json.loads(payload)
        if _canonical(value) != payload:
            raise ValueError("noncanonical evidence")
        return value
    except (ValueError, TypeError) as exc:
        raise EvaluationIntegrityError("Evaluation evidence is malformed") from exc


def _run(
    connection: sqlite3.Connection, run_id: str,
) -> tuple[sqlite3.Row, EvaluationRun, list[AcceptanceCase]]:
    row = connection.execute("SELECT * FROM evaluation_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise EvaluationStoreError("Evaluation run does not exist")
    try:
        run = EvaluationRun.model_validate(_read_json(row["payload"], row["payload_hash"]))
        cases = TypeAdapter(list[AcceptanceCase]).validate_python(
            _read_json(row["cases_json"], row["cases_hash"]),
        )
        manifest = _read_json(row["manifest_json"], row["manifest_hash"])
        if (run.id != row["id"] or run.provider != row["provider"] or run.model != row["model"]
                or run.mode != row["mode"] or utc_timestamp(run.recorded_at) != row["recorded_at"]
                or run.status != "running" or run.completed_at is not None or run.summary):
            raise ValueError("run identity/index mismatch")
        if (not isinstance(manifest, dict) or manifest.get("version") != 1
                or manifest.get("algorithm") != "sha256"
                or not isinstance(manifest.get("files"), dict)
                or digest(_canonical(manifest["files"])) != manifest.get("case_set_sha256")
                or manifest["case_set_sha256"] != run.case_set_sha256):
            raise ValueError("run manifest mismatch")
        case_ids = [case.id for case in cases]
        expected = [f"F{number:02}" for number in range(1, 21)] + [
            f"Q{number:02}" for number in range(1, 11)
        ]
        if case_ids != expected or set(manifest["files"]) != {
            "catalogue.yaml", *(case_id + ".yaml" for case_id in case_ids),
        }:
            raise ValueError("stored acceptance population mismatch")
    except (ValueError, TypeError, KeyError) as exc:
        raise EvaluationIntegrityError("Stored evaluation identity is invalid") from exc
    return row, run, cases


def _matches(run: EvaluationRun, trial: TrialRecord, cases: list[AcceptanceCase]) -> bool:
    return all(getattr(run, key) == getattr(trial, key) for key in (
        "provider", "model", "mode", "case_set_sha256", "prompt_sha256", "schema_sha256",
    )) and (_canonical(run.settings) == _canonical(trial.settings)
            and trial.run_id == run.id and trial.case_id in {case.id for case in cases})


def _trials(
    connection: sqlite3.Connection, run: EvaluationRun, cases: list[AcceptanceCase],
) -> list[TrialRecord]:
    trials = []
    for row in connection.execute(
        "SELECT * FROM evaluation_trials WHERE run_id=? ORDER BY case_id,trial", (run.id,),
    ):
        try:
            trial = TrialRecord.model_validate(_read_json(row["payload"], row["payload_hash"]))
            if (not _matches(run, trial, cases) or row["case_id"] != trial.case_id
                    or row["trial"] != trial.trial
                    or row["started_at"] != utc_timestamp(trial.started_at)
                    or row["completed_at"] != utc_timestamp(trial.completed_at)
                    or trial.started_at < run.recorded_at):
                raise ValueError("trial identity/index mismatch")
            trials.append(trial)
        except (ValueError, TypeError, KeyError) as exc:
            raise EvaluationIntegrityError("Stored trial identity is invalid") from exc
    return trials


def _transport_retries(trial: TrialRecord) -> int:
    """Per-trial provider transport retries, derived from the recorded turns."""
    turns = trial.transcript.get("turns", [])
    if not isinstance(turns, list):
        raise EvaluationIntegrityError("Stored trial transcript is invalid")
    counts = [turn.get("transport_retries", 0) if isinstance(turn, dict) else None
              for turn in turns]
    if any(type(count) is not int or count < 0 for count in counts):
        raise EvaluationIntegrityError("Stored trial transport retries are invalid")
    total = sum(counts)  # type: ignore[arg-type]
    if trial.transcript.get("transport_retries", 0) != total:
        raise EvaluationIntegrityError("Stored trial transport retries do not match its turns")
    return total


def _summary(cases: list[AcceptanceCase], trials: list[TrialRecord], status: str) -> dict:
    summary = summarize_trials(cases, trials)
    retried = [_transport_retries(trial) for trial in trials]
    # Transport retries (429, 5xx, connection) are distinct from the answer retry count.
    summary["transport_retry_count"] = sum(retried)
    summary["transport_retried_trial_count"] = sum(count > 0 for count in retried)
    # A running or failed run cannot publish an accepted completed gate.
    if status != "completed":
        summary["primary_financial_gate_passed"] = False
        summary["security_model_cases_passed"] = False
        summary["pre_provider_security_checks_passed"] = False
    return summary


_TRANSPORT_KEYS = ("transport_retry_count", "transport_retried_trial_count")


def _same_summary(stored: dict, derived: dict) -> bool:
    """Runs completed before transport retries existed (ADR 0008, 2026-11-05) lack both
    counts; they stay readable only while their trials record no transport retry."""
    if not any(key in stored for key in _TRANSPORT_KEYS):
        if any(derived[key] for key in _TRANSPORT_KEYS):
            return False
        derived = {key: value for key, value in derived.items() if key not in _TRANSPORT_KEYS}
    return _canonical(stored) == _canonical(derived)


def _current(
    row: sqlite3.Row, initial: EvaluationRun, cases: list[AcceptanceCase],
    trials: list[TrialRecord],
) -> EvaluationRun:
    if row["status"] == "running":
        if any(row[key] is not None for key in ("completed_at", "final_payload", "final_hash")):
            raise EvaluationIntegrityError("Running evaluation has terminal evidence")
        return initial.model_copy(update={"summary": _summary(cases, trials, "running")})
    try:
        final = EvaluationRun.model_validate(_read_json(row["final_payload"], row["final_hash"]))
        if (final.status != row["status"] or final.completed_at is None
                or utc_timestamp(final.completed_at) != row["completed_at"]
                or final.completed_at < initial.recorded_at
                or any(trial.completed_at > final.completed_at for trial in trials)
                or _canonical(final.model_dump(mode="json", exclude={
                    "status", "completed_at", "summary",
                })) != _canonical(initial.model_dump(mode="json", exclude={
                    "status", "completed_at", "summary",
                }))
                or not _same_summary(final.summary, _summary(cases, trials, final.status))
                or (final.status == "completed" and final.summary["missing_trial_count"] != 0)
                or (final.status == "deferred_not_run" and trials)):
            raise ValueError("terminal evaluation disagrees with recorded evidence")
        return final
    except (ValueError, TypeError, KeyError) as exc:
        raise EvaluationIntegrityError("Stored terminal evaluation is invalid") from exc


def create_evaluation_run(
    database: Path, run: EvaluationRun, *, repository: Path, acceptance_directory: Path,
) -> EvaluationRun:
    """Capture actual public cases and enforce the scored gate before returning a run."""
    run = EvaluationRun.model_validate(run.model_dump(mode="json"))
    if run.status != "running" or run.completed_at is not None or run.summary:
        raise EvaluationStoreError("New evaluations must be running with no caller summary")
    cases = load_cases(acceptance_directory)
    manifest = freeze_manifest(acceptance_directory)
    if manifest["case_set_sha256"] != run.case_set_sha256:
        raise EvaluationStoreError("Run case-set hash does not match public acceptance")
    if run.mode == "live_scored":
        if assert_scored_ready(repository, acceptance_directory) != run.case_set_sha256:
            raise EvaluationStoreError("Scored acceptance hash changed during readiness check")
    if (cases != load_cases(acceptance_directory)
            or manifest != freeze_manifest(acceptance_directory)):
        raise EvaluationStoreError("Acceptance changed while its snapshot was captured")
    payload = _canonical(run.model_dump(mode="json"))
    case_json = _canonical([case.model_dump(mode="json") for case in cases])
    manifest_json = _canonical(manifest)
    with write_transaction(database) as connection:
        existing = connection.execute("SELECT id FROM evaluation_runs WHERE id=?",
                                      (run.id,)).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO evaluation_runs VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,?,NULL,NULL)", (
                    run.id, run.provider, run.model, run.mode, "running",
                    utc_timestamp(run.recorded_at), payload, digest(payload), case_json,
                    digest(case_json), manifest_json, digest(manifest_json),
                ),
            )
        row, initial, stored_cases = _run(connection, run.id)
        if (row["payload"] != payload or row["cases_json"] != case_json
                or row["manifest_json"] != manifest_json):
            raise EvaluationStoreError("Run identity already has different evaluation evidence")
        return _current(row, initial, stored_cases, _trials(connection, initial, stored_cases))


def record_trial(database: Path, trial: TrialRecord) -> None:
    """Append one exact trial; an identical retry never changes immutable evidence."""
    trial = TrialRecord.model_validate(trial.model_dump(mode="json"))
    try:
        _transport_retries(trial)
    except EvaluationIntegrityError as exc:
        raise EvaluationStoreError("Trial transport retries do not match its turns") from exc
    if "retries" in trial.transcript and trial.transcript["retries"] != trial.retries:
        # The harness counts the second native submission (SOL-C-R1-RETRY-001).
        raise EvaluationStoreError("Trial answer retries do not match its transcript")
    payload = _canonical(trial.model_dump(mode="json"))
    with write_transaction(database) as connection:
        row, run, cases = _run(connection, trial.run_id)
        if not _matches(run, trial, cases) or trial.started_at < run.recorded_at:
            raise EvaluationStoreError("Trial metadata does not match the captured evaluation")
        existing = connection.execute(
            "SELECT payload,payload_hash FROM evaluation_trials "
            "WHERE run_id=? AND case_id=? AND trial=?",
            (trial.run_id, trial.case_id, trial.trial),
        ).fetchone()
        if existing is not None:
            _read_json(existing["payload"], existing["payload_hash"])
            if existing["payload"] != payload:
                raise EvaluationStoreError("Trial identity already has different evidence")
            return
        if row["status"] != "running":
            raise EvaluationStoreError("Terminal evaluations cannot accept another trial")
        connection.execute("INSERT INTO evaluation_trials VALUES (?,?,?,?,?,?,?)", (
            trial.run_id, trial.case_id, trial.trial, utc_timestamp(trial.started_at),
            utc_timestamp(trial.completed_at), payload, digest(payload),
        ))


def complete_evaluation_run(
    database: Path, run_id: str, completed_at: datetime, *, status: TerminalStatus = "completed",
) -> EvaluationRun:
    """Commit one terminal disposition, deriving its summary solely from stored trials."""
    utc_timestamp(completed_at)
    if status not in {"completed", "failed", "deferred_not_run"}:
        raise EvaluationStoreError("Invalid terminal evaluation status")
    with write_transaction(database) as connection:
        row, run, cases = _run(connection, run_id)
        trials = _trials(connection, run, cases)
        summary = _summary(cases, trials, status)
        if (completed_at < run.recorded_at
                or any(item.completed_at > completed_at for item in trials)):
            raise EvaluationStoreError("Run completion must follow every captured trial")
        if status == "completed" and summary["missing_trial_count"]:
            raise EvaluationStoreError("Completed evaluations require every declared trial slot")
        if status == "deferred_not_run" and trials:
            raise EvaluationStoreError("Deferred evaluations cannot contain trial evidence")
        final = EvaluationRun.model_validate({**run.model_dump(), "status": status,
                                              "completed_at": completed_at, "summary": summary})
        if row["status"] != "running":
            current = _current(row, run, cases, trials)
            if current != final:
                raise EvaluationStoreError(
                    "Evaluation already has a different terminal disposition",
                )
            return current
        payload = _canonical(final.model_dump(mode="json"))
        connection.execute(
            "UPDATE evaluation_runs SET status=?,completed_at=?,final_payload=?,final_hash=? "
            "WHERE id=?",
            (status, utc_timestamp(completed_at), payload, digest(payload), run_id),
        )
        return final


def evaluation_summaries(
    connection: sqlite3.Connection, known_as_of: datetime,
) -> list[EvalReportItem]:
    """Read metadata/counts only after the caller authorizes the compliance surface."""
    items = []
    for identity in connection.execute(
        "SELECT id FROM evaluation_runs WHERE recorded_at<=? ORDER BY recorded_at DESC,id ASC",
        (utc_timestamp(known_as_of),),
    ).fetchall():
        row, initial, cases = _run(connection, identity["id"])
        trials = _trials(connection, initial, cases)
        current = _current(row, initial, cases, trials)
        visible = [trial for trial in trials if trial.completed_at <= known_as_of]
        if current.completed_at is not None and current.completed_at > known_as_of:
            current = initial
        summary = _summary(cases, visible, current.status)
        values = {key: summary[key] for key in EvalReportItem.model_fields if key in summary}
        items.append(EvalReportItem(
            **values, id=current.id, provider=current.provider, model=current.model,
            actual_model_ids=sorted({model for trial in visible
                                     for model in trial.actual_model_ids}),
            mode=current.mode, status=current.status, recorded_at=current.recorded_at,
            completed_at=current.completed_at, case_set_sha256=current.case_set_sha256,
            case_snapshot_sha256=row["cases_hash"], prompt_sha256=current.prompt_sha256,
            schema_sha256=current.schema_sha256, question_count=len(cases),
            financial_question_count=sum(case.category == "financial" for case in cases),
            security_question_count=sum(case.category == "security_failure" for case in cases),
        ))
    return items
