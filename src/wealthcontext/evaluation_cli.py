"""Explicit evaluation/replay commands; no dotenv loading or credential output."""

import argparse
import json
import sys
from pathlib import Path

import anyio

from wealthcontext.clock import SystemClock
from wealthcontext.evaluation import EvaluationBlocked, assert_scored_ready, write_freeze_manifest
from wealthcontext.policy import PolicyClient, PolicyUnavailable
from wealthcontext.provider_contracts import HarnessLimits, ProviderFailure
from wealthcontext.providers import create_provider, provider_configuration
from wealthcontext.settings import Settings

# Closing the never-used preflight SDK client must not stall the command.
PREFLIGHT_CLOSE_SECONDS = 5.0


class CommandFailure(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="fresh isolated directory, outside the main app data directory")
    parser.add_argument("--opa-url", help="OPA endpoint; defaults to WEALTHCONTEXT_OPA_URL")
    parser.add_argument("--bundle-dir", type=Path,
                        help="policy bundle directory containing revision.txt")
    parser.add_argument("--per-call-timeout-seconds", type=float, default=30)
    parser.add_argument("--total-timeout-seconds", type=float, default=180)


def add_parsers(commands: argparse._SubParsersAction) -> None:
    evaluate = commands.add_parser("evaluate", help="run the full public 30-case, three-trial set")
    evaluate.add_argument("--provider", choices=["anthropic", "openai", "google"],
                          default="anthropic")
    evaluate.add_argument("--mode", choices=["live_calibration", "live_scored"],
                          default="live_calibration")
    evaluate.add_argument("--model", help="explicit model ID; otherwise provider configuration")
    evaluate.add_argument("--repository", type=Path, default=Path.cwd(),
                          help="actual Git checkout for scoring (default current directory)")
    evaluate.add_argument("--acceptance-dir", type=Path,
                          help="public case directory (default REPOSITORY/acceptance)")
    _runtime_arguments(evaluate)
    freeze = commands.add_parser("freeze-acceptance", help="write public hashes without committing")
    freeze.add_argument("--repository", type=Path, default=Path.cwd())
    freeze.add_argument("--acceptance-dir", type=Path)
    replay = commands.add_parser("replay", help="replay one recorded conversation without keys")
    replay.add_argument("--recording", type=Path, required=True)
    _runtime_arguments(replay)


def _isolated_directory(target: Path, main: Path) -> Path:
    target, main = target.resolve(), main.resolve()
    if target == main or target.is_relative_to(main) or main.is_relative_to(target):
        raise CommandFailure("DATA_DIRECTORY_NOT_ISOLATED",
                             "Choose a fresh directory outside the main app data directory.")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise CommandFailure("DATA_DIRECTORY_NOT_EMPTY",
                             "Choose a new or empty evaluation directory.")
    return target


def _limits(args: argparse.Namespace) -> HarnessLimits:
    try:
        return HarnessLimits(per_call_timeout_seconds=args.per_call_timeout_seconds,
                             total_timeout_seconds=args.total_timeout_seconds)
    except ValueError:
        raise CommandFailure("INVALID_TIMEOUT",
                             "Timeouts are outside the supported bounds.") from None


def _acceptance(args: argparse.Namespace) -> tuple[Path, Path]:
    repository = args.repository.resolve()
    acceptance = (args.acceptance_dir or repository / "acceptance").resolve()
    if not acceptance.is_relative_to(repository):
        raise CommandFailure("ACCEPTANCE_OUTSIDE_REPOSITORY",
                             "Acceptance files must be inside the selected repository.")
    return repository, acceptance


def _freeze(args: argparse.Namespace) -> dict:
    _repository, acceptance = _acceptance(args)
    destination = acceptance / "freeze.json"
    # write_freeze_manifest follows symlinks; never let it write through one.
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise CommandFailure("FREEZE_MANIFEST_NOT_REGULAR_FILE",
                             "The freeze manifest destination must be a regular file.")
    path = write_freeze_manifest(acceptance)
    manifest = json.loads(path.read_text())
    return {"command": args.command, "manifest": str(path),
            "case_set_sha256": manifest["case_set_sha256"], "committed": False,
            "scored_readiness": "not_established"}


async def _preflight_provider(args: argparse.Namespace, model: str, limits: HarnessLimits) -> None:
    """Fail before any data-directory write when the selected adapter cannot be built."""
    adapter = create_provider(args.provider, model=model,
                              timeout_seconds=limits.per_call_timeout_seconds)
    with anyio.move_on_after(PREFLIGHT_CLOSE_SECONDS):
        try:
            await adapter.aclose()
        except Exception:
            pass  # The client made no request; a close error is not a run failure.


async def _policy(args: argparse.Namespace, settings: Settings) -> PolicyClient:
    configured = Settings(data_dir=settings.data_dir,
                          bundle_dir=args.bundle_dir or settings.bundle_dir,
                          opa_url=args.opa_url or settings.opa_url)
    try:
        revision = configured.expected_revision
    except (OSError, ValueError):
        raise CommandFailure("POLICY_BUNDLE_UNAVAILABLE",
                             "A valid local policy bundle revision is required.") from None
    policy = PolicyClient(configured.opa_url, revision)
    await policy.verify_revision()
    return policy


async def _evaluate(args: argparse.Namespace) -> dict:
    repository, acceptance = _acceptance(args)
    # This external gate runs before SDK construction and is repeated by runtime
    # and persistence. A container without the actual Git checkout fails closed.
    if args.mode == "live_scored":
        assert_scored_ready(repository, acceptance)
    settings = Settings.from_env()
    directory = _isolated_directory(args.data_dir, settings.data_dir)
    limits = _limits(args)
    model, configuration = provider_configuration(args.provider, model=args.model)
    policy = await _policy(args, settings)
    # Credentials are checked once, after policy verification and before the runtime
    # creates anything; per-trial factory errors would otherwise be recorded silently.
    await _preflight_provider(args, model, limits)
    from wealthcontext.evaluation_runtime import evaluate_provider

    def factory(_case_id: str, _trial: int):
        # Only the selected adapter reads its credentials from the environment.
        return create_provider(args.provider, model=model,
                               timeout_seconds=limits.per_call_timeout_seconds)

    run = await evaluate_provider(
        factory, repository=repository, acceptance_directory=acceptance,
        data_dir=directory, policy=policy, provider=args.provider, model=model,
        settings=configuration, mode=args.mode, limits=limits,
    )
    return {"command": "evaluate", "run_id": run.id, "provider": run.provider,
            "model": run.model, "mode": run.mode, "status": run.status,
            "settings": run.settings, "case_set_sha256": run.case_set_sha256,
            "prompt_sha256": run.prompt_sha256, "schema_sha256": run.schema_sha256,
            "data_directory": str(directory), "summary": run.summary}


async def _replay(args: argparse.Namespace) -> dict:
    from wealthcontext.bootstrap import bootstrap_history
    from wealthcontext.context_service import ContextService
    from wealthcontext.conversation import ConversationManager
    from wealthcontext.database import migrate
    from wealthcontext.harness import run_answer
    from wealthcontext.identity import Principal
    from wealthcontext.replay import ReplayProvider, load_recording, validate_recording
    from wealthcontext.storage import migrate_business
    from wealthcontext.telemetry import create_provider as create_telemetry

    try:
        recording = load_recording(args.recording.resolve())
        validate_recording(recording)
    except (OSError, ValueError):
        raise CommandFailure("INVALID_REPLAY_RECORDING",
                             "Replay requires intact completed live provider evidence.") from None
    messages = recording.run.turns[0].request.messages
    if (len(messages) != 1 or messages[0].get("role") != "user"
            or not isinstance(messages[0].get("content"), str)):
        raise CommandFailure("REPLAY_REQUIRES_PRIOR_HISTORY",
                             "This command requires a recording that starts a fresh conversation.")
    question = messages[0]["content"]
    assert isinstance(question, str)
    assert recording.run.principal_id and recording.run.principal_role
    principal = Principal(recording.run.principal_id, recording.run.principal_role)
    adapter = ReplayProvider(recording, principal=principal)
    settings = Settings.from_env()
    directory = _isolated_directory(args.data_dir, settings.data_dir)
    limits = _limits(args)
    policy = await _policy(args, settings)
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "replay.sqlite"
    clock = SystemClock()
    migrate(database)
    migrate_business(database)
    bootstrap_history(database, directory / "received", clock)
    telemetry = create_telemetry(directory / "traces.jsonl")
    try:
        service = ContextService(database, policy, telemetry.get_tracer("wealthcontext.replay"))
        run = await run_answer(service, ConversationManager(service), principal, clock,
                               question, adapter, mode="replay", limits=limits)
        artifact = directory / "replay-run.json"
        artifact.write_text(
            json.dumps(run.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        )
    finally:
        telemetry.shutdown()
    return {"command": "replay", "run_id": run.run_id, "mode": run.mode,
            "origin": recording.origin, "recording_sha256": recording.payload_sha256,
            "provider": run.provider, "model": run.model, "provider_api_calls": 0,
            "principal_id": principal.id, "principal_role": principal.role,
            "first_pass_valid": run.first_pass_valid, "final_valid": run.final_valid,
            "fallback": run.fallback, "failure": run.failure,
            "artifact": str(artifact), "evaluation_score": None}


def _blocked_message(args: argparse.Namespace) -> str:
    if args.command == "evaluate" and args.mode == "live_scored":
        return "Scoring requires the unchanged public cases and manifest in Git HEAD."
    if args.command == "freeze-acceptance":
        return "Acceptance files must match the predeclared public case catalogue."
    return "Evaluation inputs failed a fixed integrity check before or during the run."


def run_command(args: argparse.Namespace) -> int:
    """Print bounded JSON metadata. SDK/transport exception bodies never escape."""
    try:
        if args.command == "freeze-acceptance":
            result = _freeze(args)
        else:
            result = anyio.run(_evaluate if args.command == "evaluate" else _replay, args)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        if args.command == "replay" and (
            result["failure"] or result["fallback"] or not result["final_valid"]
        ):
            return 2
        if args.command == "evaluate" and result["status"] != "completed":
            return 2
        return 0
    except CommandFailure as exc:
        error = {"error": exc.code, "message": str(exc)}
    except EvaluationBlocked:
        error = {"error": "EVALUATION_BLOCKED", "message": _blocked_message(args)}
    except PolicyUnavailable:
        error = {"error": "POLICY_UNAVAILABLE",
                 "message": "OPA must serve the configured policy revision before execution."}
    except ProviderFailure as exc:
        # ProviderFailure is the adapter's closed, sanitized failure boundary.
        error = {"error": exc.code}
    except Exception as exc:
        error = {"error": "COMMAND_FAILED", "exception_type": type(exc).__name__}
    print(json.dumps(error, sort_keys=True), file=sys.stderr)
    return 2
