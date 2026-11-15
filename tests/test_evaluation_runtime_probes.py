"""Carried-over P5 runtime findings: bounded cleanup and executed security probes.

Every test runs the real isolated fixture, governed service, conversation manager,
harness and verifier. Only the provider and the offline policy are test doubles;
negative controls alter one governed output and show the probe then fails closed.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import anyio
import pytest
from opentelemetry import trace
from test_evaluation_runtime import EVIDENCE_TIME, ROOT, FixturePolicy, SafeProvider, run_metadata

from wealthcontext import evaluation_runtime, verifier
from wealthcontext.clock import FrozenClock
from wealthcontext.context_service import ContextService
from wealthcontext.conversation import TOMBSTONE, ConversationManager
from wealthcontext.database import migrate
from wealthcontext.evaluation import load_cases
from wealthcontext.evaluation_runtime import TrialExecutor, _terminal_evidence, public_question
from wealthcontext.evaluation_store import create_evaluation_run, record_trial
from wealthcontext.generator import generate_history
from wealthcontext.identity import Principal
from wealthcontext.ingestion import import_fixture_history, migrate_feeds
from wealthcontext.provider_contracts import HarnessLimits, ProviderRequest, ProviderTurn, ToolCall
from wealthcontext.storage import migrate_business, read_snapshot

MARKER = "\nPublic request inputs (data):\n"


@pytest.fixture(scope="module")
def history():
    return generate_history()


def case_named(name):
    return next(case for case in load_cases(ROOT / "acceptance") if case.id == name)


def executor(tmp_path, history, factory) -> TrialExecutor:
    return TrialExecutor(run_metadata(), factory, history, tmp_path, FixturePolicy(),
                         FrozenClock(EVIDENCE_TIME), HarnessLimits())


def forbidden(*_args) -> Never:
    pytest.fail("a failed pre-provider security probe invoked a provider")


# SOL-P5-R1-TRIAL-001 / SOL-P5-R1-RT-003: provider cleanup is bounded independently.

class StallingClose(SafeProvider):
    """Answers correctly, then never finishes closing."""

    def __init__(self, case_id, started: anyio.Event | None = None):
        super().__init__(case_id)
        self.started = started

    async def aclose(self):
        if self.started is not None:
            self.started.set()
        await anyio.sleep_forever()


class TracerSpy:
    def __init__(self):
        self.shutdowns = 0

    def wrap(self, path):
        provider = evaluation_runtime_create_tracer(path)
        original = provider.shutdown

        def shutdown():
            self.shutdowns += 1
            original()

        provider.shutdown = shutdown
        return provider


evaluation_runtime_create_tracer = evaluation_runtime.create_tracer


@pytest.mark.anyio
async def test_stalled_provider_close_is_bounded_recorded_persisted_and_next_trial_runs(
    tmp_path, history, monkeypatch,
):
    monkeypatch.setattr(evaluation_runtime, "PROVIDER_CLOSE_TIMEOUT_SECONDS", 0.05, raising=False)
    spy = TracerSpy()
    monkeypatch.setattr(evaluation_runtime, "create_tracer", spy.wrap)
    case = case_named("F01")
    adapters: list[StallingClose] = []

    def factory(case_id, trial):
        adapters.append(StallingClose(case_id))
        return adapters[-1]

    run = run_metadata()
    database = tmp_path / "evaluation.sqlite"
    migrate(database)
    migrate_business(database)
    create_evaluation_run(database, run, repository=ROOT, acceptance_directory=ROOT / "acceptance")
    runtime = executor(tmp_path, history, factory)
    # Bounded wall time: without an independent cleanup bound the first trial never returns.
    with anyio.fail_after(90):
        first = await runtime(case, 1)
        record_trial(database, first)
        second = await runtime(case, 2)
        record_trial(database, second)
    assert len(adapters) == 2 and spy.shutdowns == 2
    for result in (first, second):
        assert result.provider_invoked and result.final_valid and result.failure is None
        assert result.transcript["cleanup_error"] == "PROVIDER_CLOSE_TIMED_OUT"
    with read_snapshot(database) as connection:
        rows = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM evaluation_trials WHERE case_id='F01' ORDER BY trial")]
    assert [row["trial"] for row in rows] == [1, 2]
    assert all(row["transcript"]["cleanup_error"] == "PROVIDER_CLOSE_TIMED_OUT" for row in rows)


@pytest.mark.anyio
async def test_cancellation_during_provider_close_still_shuts_the_trial_tracer_down(
    tmp_path, history, monkeypatch,
):
    spy = TracerSpy()
    monkeypatch.setattr(evaluation_runtime, "create_tracer", spy.wrap)
    started = anyio.Event()
    runtime = executor(tmp_path, history, lambda case_id, _trial: StallingClose(case_id, started))
    with anyio.fail_after(90):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(runtime, case_named("F01"), 1)
            await started.wait()
            tasks.cancel_scope.cancel()
    assert spy.shutdowns == 1


# SOL-P5-R1-TRIAL-002: the original revoked exchange is paired with exactly the tombstone.

def _tampering_manager(tamper: str):
    class Tampering(ConversationManager):
        async def prepare_turn(self, principal, now, conversation_id, *args, **kwargs):
            prepared = await super().prepare_turn(principal, now, conversation_id,
                                                  *args, **kwargs)
            if prepared["status"] != "COMPLETE":
                return prepared
            for message in prepared["data"]["messages"]:
                if tamper == "snapshot_field" and message.get("tool_call_id") == "setup_snapshot":
                    # A non-aggregate field survives; the aggregate asset string does not.
                    message["content"] = json.dumps({"household_id": "WC-HH-0001",
                                                     "status": "COMPLETE"})
                if (tamper == "earlier_answer" and message["role"] == "assistant"
                        and "content" in message):
                    message["content"] = "Historical household assets were retained."
            return prepared

    return Tampering


@pytest.mark.anyio
async def test_revocation_probe_requires_the_original_exchange_to_pair_exactly_the_tombstone(
    tmp_path, history,
):
    adapters = []

    def factory(case_id, _trial):
        adapters.append(SafeProvider(case_id))
        return adapters[-1]

    result = await executor(tmp_path, history, factory)(case_named("Q02"), 1)
    evidence = result.model_dump(mode="json")["security_evidence"]
    assert result.security_check_passed is True and result.failure is None
    assert evidence["original_call_paired_tombstone"] is True
    assert evidence["original_call_arguments_redacted"] is True
    assert evidence["earlier_answer_tombstoned"] is True
    assert evidence["old_answer_redacted"] is True
    messages = adapters[0].requests[0].messages
    index = next(position for position, message in enumerate(messages)
                 if any(call["id"] == "setup_snapshot" for call in message.get("tool_calls", [])))
    assert messages[index + 1] == {"role": "tool", "tool_call_id": "setup_snapshot",
                                   "content": TOMBSTONE}


@pytest.mark.anyio
@pytest.mark.parametrize("tamper", ["snapshot_field", "earlier_answer"])
async def test_revocation_probe_fails_when_any_part_of_the_revoked_exchange_is_retained(
    tmp_path, history, monkeypatch, tamper,
):
    monkeypatch.setattr(evaluation_runtime, "ConversationManager", _tampering_manager(tamper))
    result = await executor(tmp_path, history, forbidden)(case_named("Q02"), 1)
    assert result.failure == "SETUP_FAILED:REVOCATION_NOT_ENFORCED"
    assert result.security_check_passed is False and not result.provider_invoked


# SOL-P5-R1-RT-001: public cutoffs follow the service's effective-cutoff rule.

async def _service_effective_cutoff(tmp_path: Path, history, case, arguments: dict) -> datetime:
    database = tmp_path / "cutoff.sqlite"
    clock = FrozenClock(case.request_time)  # The TrialExecutor's captured request clock.
    migrate(database)
    migrate_business(database)
    migrate_feeds(database)
    import_fixture_history(database, [event for event in history
                                      if event.delivery.manifest.business_date
                                      <= case.valuation_date], clock)
    service = ContextService(database, FixturePolicy(), trace.get_tracer("cutoff-test"))
    result = await service.execute(Principal(case.principal.id, case.principal.role),
                                   clock.now(), "get_household_snapshot", arguments)
    assert result["status"] != "ERROR", result
    return datetime.fromisoformat(result["known_as_of"])


def _requested(case) -> list[str]:
    inputs = json.loads(public_question(case, {}).split(MARKER)[1])
    return inputs.get("requested_known_as_of") or [
        datetime.fromisoformat(inputs["known_as_of"]).astimezone(UTC).isoformat()]


@pytest.mark.anyio
async def test_explicit_public_cutoff_is_the_normalized_instant_the_service_executes(
    tmp_path, history,
):
    case = case_named("F01")
    effective = await _service_effective_cutoff(tmp_path, history, case,
                                                dict(case.requests[0].arguments))
    assert _requested(case) == [effective.astimezone(UTC).isoformat()]


@pytest.mark.anyio
@pytest.mark.parametrize("cutoff", ["omitted", "null"])
async def test_omitted_or_null_public_cutoff_is_the_captured_request_time_the_service_uses(
    tmp_path, history, cutoff,
):
    case = case_named("F11")
    later = case.requests[1]
    if cutoff == "omitted":
        later.arguments.pop("known_as_of")
    else:
        later.arguments["known_as_of"] = None
    effective = await _service_effective_cutoff(tmp_path, history, case, dict(later.arguments))
    assert effective == case.request_time
    assert _requested(case) == sorted([
        datetime.fromisoformat(str(case.requests[0].arguments["known_as_of"])).isoformat(),
        effective.astimezone(UTC).isoformat(),
    ])


# SOL-P5-R1-SA-001: the Q10 omission/retry/fallback probe executes and is recorded.

@pytest.mark.anyio
async def test_q10_probe_rejects_omitted_caveat_accepts_correction_and_verifies_fallback(
    tmp_path, history, monkeypatch,
):
    order = []
    original = evaluation_runtime.run_answer

    async def recording(*args, **kwargs):
        order.append(args[5].provider)
        return await original(*args, **kwargs)

    monkeypatch.setattr(evaluation_runtime, "run_answer", recording)

    def factory(case_id, _trial):
        order.append("factory")
        return SafeProvider(case_id)

    result = await executor(tmp_path, history, factory)(case_named("Q10"), 1)
    # Both probes run, through the real harness, before the provider challenge.
    assert order == ["deterministic_probe", "deterministic_probe", "factory", "anthropic"]
    assert result.security_check_passed is True and result.first_pass_valid
    evidence = result.model_dump(mode="json")["security_evidence"]
    assert evidence["kind"] == "synthetic_deterministic_verifier_probe"
    assert evidence["genuine_provider_output_modified"] is False
    corrected, fallback = evidence["probes"]
    rejected = {"source": "provider", "status": "ERROR", "error_code": "INVALID_ARGUMENT",
                "diagnostic_codes": ["MISSING_CAVEAT"], "caveat_count": 0}
    assert corrected["kind"] == "corrected_retry"
    assert [{key: attempt[key] for key in rejected} for attempt in corrected["attempts"]] == [
        rejected,
        {"source": "provider", "status": "COMPLETE", "error_code": None, "diagnostic_codes": [],
         "caveat_count": 2},
    ]
    assert corrected["fallback"] is False and corrected["final_valid"] is True
    assert fallback["kind"] == "fallback_after_invalid_retry"
    assert [{key: attempt[key] for key in rejected} for attempt in fallback["attempts"]] == [
        rejected, rejected,
        {"source": "fallback", "status": "COMPLETE", "error_code": None, "diagnostic_codes": [],
         "caveat_count": 2},
    ]
    assert fallback["fallback"] is True and fallback["final_valid"] is True
    assert fallback["rendered_answer_redacted"] is False
    for probe in (corrected, fallback):
        assert probe["mode"] == "synthetic_test" and probe["retries"] == 1
        assert all(attempt["result_id"] for attempt in probe["attempts"])
        assert probe["conversation_id"] != result.transcript["conversation_id"]
    # The recorded probe evidence is bound to the stored verifier results.
    database = tmp_path / "trials" / "eval_test" / "Q10" / "1" / "wealthcontext.sqlite"
    with read_snapshot(database) as connection:
        for probe in (corrected, fallback):
            for attempt in probe["attempts"]:
                stored = json.loads(connection.execute("SELECT payload FROM results WHERE id=?",
                                                       (attempt["result_id"],)).fetchone()[0])
                assert stored["status"] == attempt["status"]


class _OtherDefectProbe(evaluation_runtime.OmissionProbe):
    """Rejected for a duplicated caveat, not for the omitted warning the probe claims."""

    async def complete(self, request: ProviderRequest) -> ProviderTurn:
        turn = await super().complete(request)
        call = turn.calls[0]
        if self.calls == 2:
            arguments = dict(call.arguments)
            caveats = evaluation_runtime.deterministic_fallback(
                evaluation_runtime.current_results({"data": {"messages": request.messages}}),
            ).model_dump(mode="json")["caveats"]
            arguments["caveats"] = caveats + caveats[:1]
            turn = ProviderTurn(calls=[ToolCall(id=call.id, name=call.name,
                                                arguments=arguments)], model=turn.model)
        return turn


@pytest.mark.anyio
@pytest.mark.parametrize("control", ["omitted_caveat_accepted", "rejected_for_other_reason"])
async def test_q10_probe_fails_closed_when_the_verifier_does_not_reject_the_omission(
    tmp_path, history, monkeypatch, control,
):
    if control == "omitted_caveat_accepted":
        warnings = verifier._warnings

        def lenient(answer, cited):
            found, diagnostics = warnings(answer, cited)
            return found, [item for item in diagnostics if item["code"] != "MISSING_CAVEAT"]

        monkeypatch.setattr(verifier, "_warnings", lenient)
    else:
        monkeypatch.setattr(evaluation_runtime, "OmissionProbe", _OtherDefectProbe)
    result = await executor(tmp_path, history, forbidden)(case_named("Q10"), 1)
    assert result.failure == "SETUP_FAILED:DETERMINISTIC_OMISSION_PROBE_FAILED"
    assert result.security_check_passed is False and not result.provider_invoked
    recorded = result.model_dump(mode="json")["security_evidence"]["probe"]
    assert recorded["passed"] is False and recorded["probes"]


# SOL-P5-R1-SPI-001: Q05 scans every governed ops surface for the received population.

def _received_population(database: Path) -> set[str]:
    with read_snapshot(database) as connection:
        return evaluation_runtime.received_personal_strings(connection)


@pytest.mark.anyio
async def test_q05_scan_covers_the_received_population_on_every_governed_surface(
    tmp_path, history,
):
    case = case_named("Q05")
    result = await executor(tmp_path, history, safe_factory)(case, 1)
    assert result.security_check_passed is True and result.failure is None
    evidence = result.model_dump(mode="json")["security_evidence"]
    database = tmp_path / "trials" / "eval_test" / "Q05" / "1" / "wealthcontext.sqlite"
    population = _received_population(database)
    # Every forbidden name class in the case belongs to the actual received population.
    assert all(any(mention in name for name in population)
               for mention in case.forbidden_mentions)
    assert evidence["kind"] == "actual_ops_surface_pii_scan"
    assert evidence["received_personal_string_count"] == len(population)
    counts = evidence["received_population"]
    assert counts["person_names"] > 0 and counts["household_names"] > 0
    assert counts["account_titles"] > 0
    surfaces = evidence["surfaces_inspected"]
    assert surfaces["get_data_quality_report"] == 1
    assert surfaces["conflicts"] == len(case.household_ids)
    assert surfaces["feed_runs"] >= 1 and surfaces["get_data_provenance"] >= 3
    assert evidence["surface_count"] == sum(surfaces.values())
    assert len(evidence["surface_result_ids"]) == evidence["surface_count"]
    assert evidence["feed_reject_count"] > 0
    assert evidence["leak_found"] is False and evidence["leak_count"] == 0
    assert evidence["scan"] == "PASSED"
    serialized = result.model_dump_json()
    assert not any(name in serialized for name in population)


def safe_factory(case_id, _trial):
    return SafeProvider(case_id)


def _leaking_service(tool: str, household: str | None, leaked: str):
    class Leaking(ContextService):
        async def execute(self, principal, now, name, arguments, *args, **kwargs):
            result = await super().execute(principal, now, name, arguments, *args, **kwargs)
            if (name == tool and result["data"] is not None
                    and (household is None or arguments.get("household_id") == household)):
                result = {**result, "data": {**result["data"], "note": f"owner {leaked}"}}
            return result

    return Leaking


@pytest.mark.anyio
@pytest.mark.parametrize(("tool", "household"), [
    ("get_data_quality_report", None),
    ("get_data_provenance", None),
    ("conflicts", "WC-HH-0003"),
    ("feed_runs", None),
])
async def test_q05_scan_detects_a_received_name_injected_into_any_governed_surface(
    tmp_path, history, monkeypatch, tool, household,
):
    leaked = "Synthetic Person 07"
    monkeypatch.setattr(evaluation_runtime, "ContextService",
                        _leaking_service(tool, household, leaked))
    result = await executor(tmp_path, history, forbidden)(case_named("Q05"), 1)
    assert result.failure == "SETUP_FAILED:PII_PROJECTION_FAILED"
    assert result.security_check_passed is False and not result.provider_invoked
    evidence = result.model_dump(mode="json")["security_evidence"]["probe"]
    assert evidence["leak_found"] is True and evidence["leak_count"] >= 1
    assert tool in evidence["leaking_surfaces"] and evidence["scan"] == "FAILED"
    assert leaked not in result.model_dump_json()


def test_a_cut_short_submission_is_sanitized_evidence_without_a_result():
    """SOL-C-R1-RETRY-001: the harness keeps a cut-short submission with no result."""
    evidence = _terminal_evidence({"kind": "provider", "candidate": {"caveats": []},
                                   "result": None})
    assert evidence == {"source": "provider", "result_id": None, "status": None,
                        "error_code": None, "diagnostic_codes": [], "caveat_count": 0}
