"""Command entry points; stdout is reserved for machine-readable output."""

import argparse
import json
import os
from pathlib import Path

import uvicorn
from pydantic import JsonValue

from wealthcontext.clock import SystemClock
from wealthcontext.context_service import ConcentrationArguments, ExposureArguments
from wealthcontext.contracts import Answer, Claim, Envelope, Error
from wealthcontext.exposure import ConcentrationEnvelope, ExposureEnvelope
from wealthcontext.identity import PERSONAS, Identity
from wealthcontext.settings import Settings
from wealthcontext.snapshot import SnapshotEnvelope
from wealthcontext.surface_contracts import (
    ConflictArguments,
    ConflictEnvelope,
    DecisionLogEnvelope,
    DecisionLogPageArguments,
    DecisionLogPageEnvelope,
    EmptyArguments,
    EvalReportsEnvelope,
    FeedHealthArguments,
    FeedHealthEnvelope,
    FeedRunImpactArguments,
    FeedRunImpactEnvelope,
    FeedRunsArguments,
    FeedRunsEnvelope,
    HouseholdReceiptsArguments,
    HouseholdReceiptsEnvelope,
    ListHouseholdsEnvelope,
    QualityArguments,
    QualityEnvelope,
    SearchArguments,
    SearchEnvelope,
    TraceArguments,
    TraceEnvelope,
    TraceSpansEnvelope,
)


def export_schemas(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name, model in {
        "envelope": Envelope[dict[str, JsonValue]],
        "error": Error,
        "claim": Claim,
        "answer": Answer,
        "snapshot": SnapshotEnvelope,
        "exposure": ExposureEnvelope,
        "concentration": ConcentrationEnvelope,
        "exposure-arguments": ExposureArguments,
        "concentration-arguments": ConcentrationArguments,
        "search": SearchEnvelope,
        "search-arguments": SearchArguments,
        "list-households": ListHouseholdsEnvelope,  # reuses empty-arguments (ADR 0009)
        "quality": QualityEnvelope,
        "quality-arguments": QualityArguments,
        "feed-runs": FeedRunsEnvelope,
        "feed-runs-arguments": FeedRunsArguments,
        "conflicts": ConflictEnvelope,
        "conflicts-arguments": ConflictArguments,
        "decision-log": DecisionLogEnvelope,
        "eval-reports": EvalReportsEnvelope,
        "empty-arguments": EmptyArguments,
        "trace": TraceEnvelope,
        "trace-arguments": TraceArguments,
        # ADR 0009 sections 5 to 7; trace_spans reuses trace-arguments.
        "decision-log-page": DecisionLogPageEnvelope,
        "decision-log-page-arguments": DecisionLogPageArguments,
        "household-receipts": HouseholdReceiptsEnvelope,
        "household-receipts-arguments": HouseholdReceiptsArguments,
        "trace-spans": TraceSpansEnvelope,
        # ADR 0009 sections 3 and 4.
        "feed-health": FeedHealthEnvelope,
        "feed-health-arguments": FeedHealthArguments,
        "feed-run-impact": FeedRunImpactEnvelope,
        "feed-run-impact-arguments": FeedRunImpactArguments,
    }.items():
        schema = model.model_json_schema(
            mode="validation" if name.endswith("-arguments") else "serialization",
        )
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://wealthcontext.example/schemas/v1/{name}.json"
        (output / f"{name}.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(prog="wealthcontext")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("bridge")
    token = commands.add_parser("token")
    token.add_argument("--persona", choices=sorted(PERSONAS), required=True)
    token.add_argument("--expired", action="store_true")
    config = commands.add_parser("print-mcp-config")
    config.add_argument("--persona", choices=sorted(PERSONAS), required=True)
    config.add_argument("--project-dir", default=os.environ.get("WEALTHCONTEXT_HOST_PROJECT_DIR"))
    config.add_argument("--docker-command", default="/usr/local/bin/docker")
    schemas = commands.add_parser("export-schemas")
    schemas.add_argument("--output", type=Path, default=Path("schemas/v1"))
    generate = commands.add_parser("generate-feeds")
    generate.add_argument("--output", type=Path, default=Path("runtime/feeds"))
    generate.add_argument("--seed", type=int, default=2026)
    from wealthcontext.evaluation_cli import add_parsers, run_command
    add_parsers(commands)
    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run("wealthcontext.app:create_app", factory=True, host="0.0.0.0", port=8000)
    elif args.command == "generate-feeds":
        from wealthcontext.generator import write_history
        events = write_history(args.output, args.seed)
        print(json.dumps({"directory": str(args.output.resolve()), "events": len(events),
                          "seed": args.seed}))
    elif args.command == "bridge":
        from wealthcontext.bridge import bridge_from_env
        bridge_from_env()
    elif args.command in {"evaluate", "freeze-acceptance", "replay"}:
        code = run_command(args)
        if code:
            raise SystemExit(code)
    elif args.command in {"token", "print-mcp-config"}:
        if args.command == "print-mcp-config" and (
            not args.project_dir or not Path(args.project_dir).is_absolute()
        ):
            parser.error("print-mcp-config requires an absolute host --project-dir")
        identity = Identity.load_or_create(Settings.from_env().data_dir)
        bearer = identity.mint(args.persona, SystemClock().now(),
                               expired=getattr(args, "expired", False))
        if args.command == "token":
            print(bearer)
        else:
            print(json.dumps({"mcpServers": {"wealthcontext": {
                "command": args.docker_command,
                "args": ["compose", "-f", str(Path(args.project_dir) / "compose.yaml"),
                         "run", "--rm", "--no-deps", "-T", "-e", "WEALTHCONTEXT_TOKEN", "mcp"],
                "env": {"WEALTHCONTEXT_TOKEN": bearer},
            }}}, indent=2))
    else:
        export_schemas(args.output)
