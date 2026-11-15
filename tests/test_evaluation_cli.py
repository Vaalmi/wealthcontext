"""CLI boundary tests use synthetic SDK/runtime doubles; no live provider requests."""

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from wealthcontext import cli, evaluation_cli
from wealthcontext.evaluation import write_freeze_manifest
from wealthcontext.evaluation_contracts import EvaluationRun
from wealthcontext.policy import PolicyClient, PolicyDecision, PolicyUnavailable
from wealthcontext.provider_contracts import ProviderFailure
from wealthcontext.providers import provider_configuration
from wealthcontext.replay import save_recording

ROOT = Path(__file__).parents[1]
REVISION = "sha256:" + "a" * 64
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


def arguments(*items):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    evaluation_cli.add_parsers(commands)
    return parser.parse_args(list(items))


@pytest.fixture
def environment(tmp_path, monkeypatch):
    main = tmp_path / "main-app"
    bundle = tmp_path / "policy"
    bundle.mkdir()
    (bundle / "revision.txt").write_text(REVISION + "\n")
    monkeypatch.setenv("WEALTHCONTEXT_DATA_DIR", str(main))
    monkeypatch.setenv("WEALTHCONTEXT_BUNDLE_DIR", str(bundle))
    monkeypatch.setenv("WEALTHCONTEXT_OPA_URL", "http://policy.example.test:8181")
    return tmp_path, main, bundle


def fake_policy(monkeypatch, events, *, allow=True):
    """Fakes only OPA transport: the revision check and an allow/deny decision.

    Real Rego decisions are exercised by the OPA suites in the container; these
    CLI tests check command wiring, ordering and exit status.
    """

    class Policy(PolicyClient):
        def __init__(self, url, revision):
            super().__init__(url, revision)
            events.append(("policy", url, revision))

        async def verify_revision(self):
            events.append("verify_revision")
            return self.expected_revision

        async def _decide(self, principal, action, resources, facts, revision,
                          request_business_date, *, filtered):
            events.append(("decide", principal.id, action))
            return PolicyDecision(
                allow=allow, allowed_ids=sorted(resources) if allow else [],
                projection="no_pii" if principal.role == "ops"
                and action != "metric_definition" else "full",
                reason="SYNTHETIC_CLI_TEST" if allow else "NO_CURRENT_GRANT",
                policy_revision=self.expected_revision, entitlement_revision=revision,
            )

    monkeypatch.setattr(evaluation_cli, "PolicyClient", Policy)


def completed_run(kwargs, status="completed"):
    return EvaluationRun.model_validate({
        "id": "synthetic_cli_run", "provider": kwargs["provider"], "model": kwargs["model"],
        "mode": kwargs["mode"], "status": status, "recorded_at": NOW, "completed_at": NOW,
        "settings": kwargs["settings"], "case_set_sha256": "b" * 64,
        "prompt_sha256": "c" * 64, "schema_sha256": "d" * 64,
        "summary": {"recorded_trial_count": 90, "scored": False},
    })


def stub_runtime(monkeypatch, events, *, failure=None, status="completed"):
    fake_policy(monkeypatch, events)

    async def evaluate(factory, **kwargs):
        events.append(("runtime", kwargs))
        if failure is not None:
            raise failure
        factory("F01", 1)
        return completed_run(kwargs, status)

    class Adapter:
        async def aclose(self):
            events.append("aclose")

    def factory(provider, **kwargs):
        events.append(("factory", provider, kwargs))
        return Adapter()

    monkeypatch.setattr(evaluation_cli, "create_provider", factory)
    monkeypatch.setitem(sys.modules, "wealthcontext.evaluation_runtime",
                        SimpleNamespace(evaluate_provider=evaluate))


def test_evaluate_defaults_anthropic_calibration_and_verifies_policy_before_factory(
    environment, monkeypatch, capsys,
):
    directory, _main, _bundle = environment
    events = []
    stub_runtime(monkeypatch, events)
    args = arguments("evaluate", "--repository", str(ROOT),
                     "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["provider"] == "anthropic" and output["mode"] == "live_calibration"
    model, settings = provider_configuration("anthropic")
    assert output["model"] == model and output["settings"] == settings
    assert events[0] == ("policy", "http://policy.example.test:8181", REVISION)
    # One preflight adapter is constructed and closed after policy verification and
    # before the runtime touches the data directory; the runtime then builds its own.
    factory_call = ("factory", "anthropic", {"model": model, "timeout_seconds": 30})
    assert events[1] == "verify_revision"
    assert events[2] == factory_call and events[3] == "aclose"
    assert events[4][0] == "runtime" and events[5] == factory_call
    runtime = events[4][1]
    assert runtime["repository"] == ROOT
    assert runtime["acceptance_directory"] == ROOT / "acceptance"
    assert runtime["data_dir"] == directory / "evaluation"


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
def test_explicit_provider_model_and_policy_overrides_are_recorded_without_credentials(
    environment, monkeypatch, capsys, provider,
):
    directory, _main, bundle = environment
    events = []
    stub_runtime(monkeypatch, events)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC-SECRET-DO-NOT-PRINT")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "SYNTHETIC-WORKSPACE-DO-NOT-PRINT")
    args = arguments("evaluate", "--provider", provider, "--model", "explicit-test-model",
                     "--repository", str(ROOT), "--data-dir", str(directory / "evaluation"),
                     "--opa-url", "http://alternate.example.test:8182", "--bundle-dir", str(bundle))
    assert evaluation_cli.run_command(args) == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["provider"] == provider and output["model"] == "explicit-test-model"
    assert events[0] == ("policy", "http://alternate.example.test:8182", REVISION)
    assert "SECRET" not in captured.out + captured.err
    assert "WORKSPACE" not in captured.out + captured.err
    assert "api_key" not in events[-1][2] and "workspace_id" not in events[-1][2]


def test_scored_command_without_git_blocks_before_policy_or_provider(
    environment, monkeypatch, capsys,
):
    directory, _main, _bundle = environment
    repository = directory / "container-copy-without-git"
    shutil.copytree(ROOT / "acceptance", repository / "acceptance")
    write_freeze_manifest(repository / "acceptance")
    events = []
    stub_runtime(monkeypatch, events)
    args = arguments("evaluate", "--mode", "live_scored", "--repository", str(repository),
                     "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 2
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert not captured.out and error["error"] == "EVALUATION_BLOCKED"
    assert "Git HEAD" in error["message"]
    assert not events and not (directory / "evaluation").exists()


def test_scored_gate_runs_before_policy_verification_and_runtime(
    environment, monkeypatch, capsys,
):
    directory, _main, _bundle = environment
    events = []
    stub_runtime(monkeypatch, events)
    monkeypatch.setattr(evaluation_cli, "assert_scored_ready",
                        lambda repo, cases: events.append(("freeze_gate", repo, cases)))
    args = arguments("evaluate", "--mode", "live_scored", "--repository", str(ROOT),
                     "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 0
    assert events[0] == ("freeze_gate", ROOT, ROOT / "acceptance")
    assert events[2] == "verify_revision" and events[3][0] == "factory"
    assert events[5][0] == "runtime"
    assert json.loads(capsys.readouterr().out)["mode"] == "live_scored"


def test_missing_git_executable_is_a_scored_gate_failure(environment, monkeypatch, capsys):
    directory, _main, _bundle = environment
    repository = directory / "checkout"
    shutil.copytree(ROOT / "acceptance", repository / "acceptance")
    events = []
    stub_runtime(monkeypatch, events)

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("synthetic private executable path")

    monkeypatch.setattr("wealthcontext.evaluation.subprocess.run", unavailable)
    args = arguments("evaluate", "--mode", "live_scored", "--repository", str(repository),
                     "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 2
    captured = capsys.readouterr()
    assert not captured.out and json.loads(captured.err)["error"] == "EVALUATION_BLOCKED"
    assert "private" not in captured.err and not events


@pytest.mark.parametrize("relationship", ["same", "child", "parent", "nonempty"])
def test_evaluation_requires_fresh_directory_separate_from_main_app(
    environment, monkeypatch, capsys, relationship,
):
    directory, main, _bundle = environment
    target = {"same": main, "child": main / "eval", "parent": directory,
              "nonempty": directory / "occupied"}[relationship]
    if relationship == "nonempty":
        target.mkdir()
        (target / "previous-run.json").write_text("{}")
    events = []
    stub_runtime(monkeypatch, events)
    assert evaluation_cli.run_command(arguments("evaluate", "--data-dir", str(target))) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error == ("DATA_DIRECTORY_NOT_EMPTY" if relationship == "nonempty"
                     else "DATA_DIRECTORY_NOT_ISOLATED")
    assert not events


def test_wrong_policy_revision_stops_before_runtime_without_transport_body_leak(
    environment, monkeypatch, capsys,
):
    directory, _main, _bundle = environment
    events = []
    stub_runtime(monkeypatch, events)

    async def unavailable(_args, _settings):
        raise PolicyUnavailable("SYNTHETIC-SECRET-IN-TRANSPORT-BODY")
    monkeypatch.setattr(evaluation_cli, "_policy", unavailable)
    args = arguments("evaluate", "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)["error"] == "POLICY_UNAVAILABLE"
    assert not events and "SECRET" not in output.err + output.out


@pytest.mark.parametrize("failure,error", [
    (RuntimeError("SYNTHETIC-SECRET-IN-SDK-BODY"), "COMMAND_FAILED"),
    (ProviderFailure("MISSING_PROVIDER_CREDENTIALS"), "MISSING_PROVIDER_CREDENTIALS"),
])
def test_runtime_failures_are_sanitized(environment, monkeypatch, capsys, failure, error):
    directory, _main, _bundle = environment
    stub_runtime(monkeypatch, [], failure=failure)
    args = arguments("evaluate", "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)["error"] == error
    assert "SECRET" not in output.err + output.out


def test_freeze_only_writes_manifest_without_provider_or_settings(environment, monkeypatch, capsys):
    directory, _main, _bundle = environment
    repository = directory / "checkout"
    shutil.copytree(ROOT / "acceptance", repository / "acceptance")
    monkeypatch.setattr(evaluation_cli.Settings, "from_env",
                        lambda: pytest.fail("freeze must not read runtime/provider environment"))
    args = arguments("freeze-acceptance", "--repository", str(repository))
    assert evaluation_cli.run_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["committed"] is False and output["scored_readiness"] == "not_established"
    assert json.loads((repository / "acceptance/freeze.json").read_text())["case_set_sha256"] == (
        output["case_set_sha256"]
    )
    assert not (repository / ".git").exists()


def test_replay_rejects_synthetic_evidence_without_provider_or_policy(
    environment, monkeypatch, capsys,
):
    from test_replay import captured_run

    directory, _main, _bundle = environment
    path = directory / "synthetic-recording.json"
    save_recording(path, captured_run())
    events = []
    stub_runtime(monkeypatch, events)
    args = arguments("replay", "--recording", str(path),
                     "--data-dir", str(directory / "replay"))
    assert evaluation_cli.run_command(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "INVALID_REPLAY_RECORDING"
    assert not events and not (directory / "replay").exists()


def test_main_dispatches_evaluation_subcommands(monkeypatch):
    seen = []
    monkeypatch.setattr(evaluation_cli, "run_command", lambda args: seen.append(args.command) or 0)
    monkeypatch.setattr(sys, "argv", ["wealthcontext", "freeze-acceptance"])
    cli.main()
    assert seen == ["freeze-acceptance"]


def test_evaluate_does_not_mislabel_single_record_replay_as_full_suite():
    with pytest.raises(SystemExit):
        arguments("evaluate", "--mode", "replay", "--data-dir", "/unused")


def test_argument_schema_exports_allow_runtime_defaults_and_preserve_frozen_answer(tmp_path):
    from jsonschema import Draft202012Validator

    original_answer = (ROOT / "schemas/v1/answer.json").read_bytes()
    cli.export_schemas(tmp_path)
    Draft202012Validator(json.loads((tmp_path / "exposure-arguments.json").read_text())).validate({
        "household_id": "WC-HH-0001", "as_of": "2026-09-30",
    })
    concentration = json.loads((tmp_path / "concentration-arguments.json").read_text())
    Draft202012Validator(concentration).validate({"as_of": "2026-09-30"})
    assert (tmp_path / "answer.json").read_bytes() == original_answer


# ARGON-P5-CLI-001: provider credentials are validated before any data-directory write.

def test_missing_provider_key_fails_after_policy_before_runtime_or_data_directory(
    environment, monkeypatch, capsys,
):
    directory, _main, _bundle = environment
    events = []
    fake_policy(monkeypatch, events)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def runtime(factory, **kwargs):
        # Mirrors TrialExecutor: the run directory is created, then each trial's
        # factory exception is recorded as PROVIDER_FACTORY_FAILED and the run completes.
        events.append("runtime")
        kwargs["data_dir"].mkdir(parents=True)
        (kwargs["data_dir"] / "evaluation.sqlite").write_bytes(b"")
        try:
            factory("F01", 1)
        except Exception:
            events.append("PROVIDER_FACTORY_FAILED")
        return completed_run(kwargs)

    monkeypatch.setitem(sys.modules, "wealthcontext.evaluation_runtime",
                        SimpleNamespace(evaluate_provider=runtime))
    # create_provider is the real factory; only OPA transport is faked.
    args = arguments("evaluate", "--repository", str(ROOT),
                     "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err) == {"error": "MISSING_PROVIDER_CREDENTIALS"}
    assert events == [("policy", "http://policy.example.test:8181", REVISION),
                      "verify_revision"]
    assert not (directory / "evaluation").exists()


def test_preflight_adapter_close_is_bounded(environment, monkeypatch, capsys):
    import anyio

    directory, _main, _bundle = environment
    events = []
    stub_runtime(monkeypatch, events)

    class Hanging:
        async def aclose(self):
            events.append("aclose")
            await anyio.sleep_forever()

    monkeypatch.setattr(evaluation_cli, "create_provider",
                        lambda provider, **kwargs: events.append("factory") or Hanging())
    monkeypatch.setattr(evaluation_cli, "PREFLIGHT_CLOSE_SECONDS", 0.05)
    args = arguments("evaluate", "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 0
    assert events[2:4] == ["factory", "aclose"] and events[4][0] == "runtime"
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


@pytest.mark.parametrize("status", ["failed", "running", "deferred_not_run"])
def test_evaluate_exits_nonzero_unless_run_completed(environment, monkeypatch, capsys, status):
    directory, _main, _bundle = environment
    stub_runtime(monkeypatch, [], status=status)
    args = arguments("evaluate", "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == status


# ARGON-P5-CLI-003: containment for every evaluate mode; mode-accurate block messages.

def test_calibration_rejects_acceptance_outside_repository(environment, monkeypatch, capsys):
    directory, _main, _bundle = environment
    outside = directory / "elsewhere" / "acceptance"
    shutil.copytree(ROOT / "acceptance", outside)
    events = []
    stub_runtime(monkeypatch, events)
    args = arguments("evaluate", "--repository", str(ROOT), "--acceptance-dir", str(outside),
                     "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "ACCEPTANCE_OUTSIDE_REPOSITORY"
    assert not events and not (directory / "evaluation").exists()


def test_freeze_block_message_does_not_claim_git_scoring(environment, capsys):
    directory, _main, _bundle = environment
    repository = directory / "checkout"
    shutil.copytree(ROOT / "acceptance", repository / "acceptance")
    (repository / "acceptance/undeclared.yaml").write_text("version: 1\n")
    args = arguments("freeze-acceptance", "--repository", str(repository))
    assert evaluation_cli.run_command(args) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "EVALUATION_BLOCKED"
    assert "Git HEAD" not in error["message"] and "Scoring" not in error["message"]


def test_calibration_block_message_does_not_claim_git_scoring(environment, monkeypatch, capsys):
    from wealthcontext.evaluation import EvaluationBlocked

    directory, _main, _bundle = environment
    stub_runtime(monkeypatch, [], failure=EvaluationBlocked("frozen Answer schema changed"))
    args = arguments("evaluate", "--data-dir", str(directory / "evaluation"))
    assert evaluation_cli.run_command(args) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "EVALUATION_BLOCKED"
    assert "Git HEAD" not in error["message"] and "Scoring" not in error["message"]
    assert "frozen Answer schema" not in error["message"]


# SOL-P5-R1-CLI-001: freeze never writes through a symlinked manifest destination.

@pytest.mark.parametrize("target", ["case", "outside", "dangling"])
def test_freeze_rejects_symlinked_manifest_destination(environment, capsys, target):
    directory, _main, _bundle = environment
    repository = directory / "checkout"
    acceptance = repository / "acceptance"
    shutil.copytree(ROOT / "acceptance", acceptance)
    outside = directory / "outside.txt"
    outside.write_text("synthetic outside bytes\n")
    destination = {"case": acceptance / "F01.yaml", "outside": outside,
                   "dangling": directory / "never-created.json"}[target]
    before = destination.read_bytes() if destination.exists() else None
    (acceptance / "freeze.json").unlink(missing_ok=True)
    (acceptance / "freeze.json").symlink_to(destination)
    args = arguments("freeze-acceptance", "--repository", str(repository))
    assert evaluation_cli.run_command(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "FREEZE_MANIFEST_NOT_REGULAR_FILE"
    assert (acceptance / "freeze.json").is_symlink()
    if before is None:
        assert not destination.exists()
    else:
        assert destination.read_bytes() == before


# ARGON-P5-CLI-002 coverage: complete replay runs through the CLI without provider keys.

REPLAY_FIXTURE = ROOT / "fixtures/replays/clean-household.openai.json"


def no_provider_keys(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "OPENAI_API_KEY",
                 "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("replay must not construct a provider adapter")

    monkeypatch.setattr(evaluation_cli, "create_provider", forbidden)
    monkeypatch.setattr("wealthcontext.providers.create_provider", forbidden)


def test_replay_of_shipped_recording_completes_without_keys(environment, monkeypatch, capsys):
    directory, _main, _bundle = environment
    events = []
    fake_policy(monkeypatch, events)
    no_provider_keys(monkeypatch)
    target = directory / "replay"
    args = arguments("replay", "--recording", str(REPLAY_FIXTURE), "--data-dir", str(target))
    assert evaluation_cli.run_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["provider_api_calls"] == 0 and output["evaluation_score"] is None
    assert output["origin"] == "recorded_live_provider" and output["mode"] == "replay"
    assert output["recording_sha256"] == (
        "52e50841614ca7c7722c4c676af725c3e0e86148f39a5a540674192fc2b8ba5f"
    )
    assert output["principal_id"] == "ADV-01" and output["principal_role"] == "advisor"
    assert output["first_pass_valid"] and output["final_valid"]
    assert output["failure"] is None and output["fallback"] is False
    assert events[:2] == [("policy", "http://policy.example.test:8181", REVISION),
                          "verify_revision"]
    assert any(event[0] == "decide" for event in events[2:])
    for name in ("replay.sqlite", "traces.jsonl", "replay-run.json"):
        assert (target / name).is_file()
    artifact = json.loads((target / "replay-run.json").read_text())
    assert artifact["run_id"] == output["run_id"] and artifact["mode"] == "replay"
    assert artifact["final_valid"] is True


def test_replay_fails_closed_when_current_policy_denies(environment, monkeypatch, capsys):
    directory, _main, _bundle = environment
    events = []
    fake_policy(monkeypatch, events, allow=False)
    no_provider_keys(monkeypatch)
    args = arguments("replay", "--recording", str(REPLAY_FIXTURE),
                     "--data-dir", str(directory / "replay"))
    assert evaluation_cli.run_command(args) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["provider_api_calls"] == 0 and not output["final_valid"]
    assert output["failure"] or output["fallback"]


@pytest.mark.parametrize("update", [
    {"failure": "REPLAY_USER_INPUT_CHANGED"}, {"fallback": True}, {"final_valid": False},
])
def test_replay_exits_nonzero_on_failure_fallback_or_invalid(
    environment, monkeypatch, capsys, update,
):
    from wealthcontext.replay import load_recording

    directory, _main, _bundle = environment
    fake_policy(monkeypatch, [])
    no_provider_keys(monkeypatch)
    recorded = load_recording(REPLAY_FIXTURE).run

    async def finished(*args, **kwargs):
        return recorded.model_copy(update={"mode": "replay", **update})

    monkeypatch.setattr("wealthcontext.harness.run_answer", finished)
    args = arguments("replay", "--recording", str(REPLAY_FIXTURE),
                     "--data-dir", str(directory / "replay"))
    assert evaluation_cli.run_command(args) == 2
    output = json.loads(capsys.readouterr().out)
    assert all(output[key] == value for key, value in update.items())


def test_replay_requires_a_recording_that_starts_a_fresh_conversation(
    environment, monkeypatch, capsys,
):
    from wealthcontext.replay import load_recording

    directory, _main, _bundle = environment
    events = []
    fake_policy(monkeypatch, events)
    recorded = load_recording(REPLAY_FIXTURE).run
    first = recorded.turns[0]
    prior = [{"role": "user", "content": "Earlier synthetic question."},
             {"role": "assistant", "content": "Earlier synthetic reply."}]
    request = first.request.model_copy(update={"messages": prior + first.request.messages})
    turns = [first.model_copy(update={"request": request}), *recorded.turns[1:]]
    path = directory / "continued-recording.json"
    save_recording(path, recorded.model_copy(update={"turns": turns}))
    args = arguments("replay", "--recording", str(path), "--data-dir", str(directory / "replay"))
    assert evaluation_cli.run_command(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "REPLAY_REQUIRES_PRIOR_HISTORY"
    assert not events and not (directory / "replay").exists()
