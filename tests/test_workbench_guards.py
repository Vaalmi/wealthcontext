"""Guards the workbench revamp must keep true: frozen prompts, published schemas, MCP tools.

`docs/plan/ux-plan.md` ("Tests and guardrails") and `docs/plan/ux-brief.md` freeze `prompts/`
and the published `schemas/v1` files at the accepted MVP (commit 91f4f3b). ADR 0009 ("Schemas
and matrix") allows exactly ten new schema files and no new MCP tool. Every pin below is the
sha256 of the file's bytes at 91f4f3b. Deterministic: no network, no OPA, no provider.
"""

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import anyio
import pytest

from wealthcontext import harness
from wealthcontext.mcp_server import ARGUMENT_MODELS, build_mcp

ROOT = Path(__file__).resolve().parents[1]
FROZEN = (
    "frozen at the accepted MVP (commit 91f4f3b) by docs/plan/ux-plan.md 'Tests and guardrails'"
)

# The hash every recorded replay and scored run was made under.
ACCEPTED_HARNESS_PROMPT_SHA256 = "67ec96d02ef614d80860bae4f7b4b6ce012164a86439f79ec7bf21f6064943a6"

PROMPT_SHA256 = {
    "harness_system.md": ACCEPTED_HARNESS_PROMPT_SHA256,
    "host_guidelines.md": "3c7167038d5d14ab3ce48f449fab5fd34ba7335c3003ce25273e5ff400fab7a8",
}

SCHEMA_SHA256 = {
    "answer.json": "eec9fdb90ade138230bbf19b01fc6e82b9916d9fc60cb9dfffab980baf3ad498",
    "claim.json": "38e176ab5f225b2ad06e8722b45801cd8d0bf8ab647acab105e90af4378186b3",
    "concentration-arguments.json":
        "2a4c20a240e02d683a2601ab58ba893f68dbf3a08e650e22ffbe92242f90c0bc",
    "concentration.json": "761691a417cf16382dbd4f55d1f92bd32294dee4624a422c7942ca35ec3882a7",
    "conflicts-arguments.json": "fc22ccbe614616633c5fbdab5aec65ab3adc8046e09d83d51991ee1808342c53",
    "conflicts.json": "3196229064a2119913572a0a305cf03e4a3d5286b4c91f85755fa02248acbd86",
    "decision-log.json": "2ece806d30b34805c43e1306dfa6dec11330f8264f9d88e211411418aadada32",
    "empty-arguments.json": "fe31d38c9ce280c32f040e0a4cba39939922140a2f17a91ca31e79ac628838bb",
    "envelope.json": "7683fd83b1cc6d39f77f6f7fcd8f70d21952bc51a49195f59926d8645143c5a0",
    "error.json": "011e80ef907e274b874a5e03ce044b844476be286135948a2fedf3e5f70c3cf2",
    "eval-reports.json": "1f009c6e79994610b65f3633aea3d25be9558b0f69d2507a070063526f63f374",
    "exposure-arguments.json": "7c108ba8a651f20153c1286ccc424efc8dfb3cf70bf2ce29eae42245ca2ea6e1",
    "exposure.json": "a2ca444cab007c41d15db4dc96a958343edcda7b2a9a8bacffa1b0330a8ca0d3",
    "feed-runs-arguments.json": "9367bd9538f8c3bdcd5b5bd06be1ebbb02645bc2980eb13e23334100cb9d6a09",
    "feed-runs.json": "f694f7540f0f73166f481f295e90ffac85f0e8b27ec50026fa1f0c281e77d16b",
    "quality-arguments.json": "be8a2c163b43f46fba7ea19edf1e3942666a9d6e1d0d9b2f99f1cb90425b600e",
    "quality.json": "4d8605c819483163cc0a83978bf2019a43ad0cebd43689383f5e61b077668f86",
    "search-arguments.json": "d60dc6cd4fe8a8687b388ad002a57ac9a7ab55d26e0e47a8220eca5cf6a817f8",
    "search.json": "c10dead560e5755e87b91391a1671d7dc978940f9fa1e94746520050a8239f7c",
    "snapshot.json": "cac60d70582aab45f701861004b19b6fe89afd71971150dbe6bfe561a11934bf",
    "trace-arguments.json": "9ad116d03f10b6c4b4b962673fbce099e5bb9a36e8be2c83dc63a42e5aea98af",
    "trace.json": "c2e4880c7f3ed6ef213dae98fd75cb3ecd9bff47bc6739dc2e4d9995b3532f92",
}

# ADR 0009 "Schemas and matrix": the only files the revamp may add under schemas/v1.
ADR_0009_NEW_SCHEMAS = frozenset({
    "list-households.json",
    "feed-health-arguments.json", "feed-health.json",
    "feed-run-impact-arguments.json", "feed-run-impact.json",
    "decision-log-page-arguments.json", "decision-log-page.json",
    "household-receipts-arguments.json", "household-receipts.json",
    "trace-spans.json",
})

# The ADR 0009 files built: list_households in step 2, the compliance contracts in step 5
# (trace_spans reuses trace-arguments.json), the feed contracts in step 6. That is all ten.
BUILT_NEW_SCHEMAS = frozenset({
    "list-households.json",
    "feed-health-arguments.json", "feed-health.json",
    "feed-run-impact-arguments.json", "feed-run-impact.json",
    "decision-log-page-arguments.json", "decision-log-page.json",
    "household-receipts-arguments.json", "household-receipts.json",
    "trace-spans.json",
})

MCP_TOOLS = frozenset({
    "get_household_snapshot", "get_portfolio_exposure", "get_concentrated_positions",
    "get_data_provenance", "search_households", "get_metric_definition",
    "get_data_quality_report",
})


def _tree_sha256(directory: Path) -> dict[str, str]:
    """Hash the bytes of every file below a directory, keyed by POSIX relative path."""
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*")) if path.is_file()
    }


def _pin_problems(actual: dict[str, str], pinned: dict[str, str], where: str) -> list[str]:
    problems = [f"{where}/{name} is missing" for name in sorted(pinned.keys() - actual.keys())]
    problems += [
        f"{where}/{name} changed (sha256 {actual[name]}, pinned {digest})"
        for name, digest in sorted(pinned.items()) if name in actual and actual[name] != digest
    ]
    return problems


def prompt_problems(root: Path) -> list[str]:
    actual = _tree_sha256(root / "prompts")
    problems = _pin_problems(actual, PROMPT_SHA256, "prompts")
    problems += [
        f"prompts/{name} was added" for name in sorted(actual.keys() - PROMPT_SHA256.keys())
    ]
    return problems


def schema_problems(root: Path) -> list[str]:
    actual = _tree_sha256(root / "schemas/v1")
    problems = _pin_problems(actual, SCHEMA_SHA256, "schemas/v1")
    problems += [
        f"schemas/v1/{name} was added but is not one of ADR 0009's ten new schema files"
        for name in sorted(actual.keys() - SCHEMA_SHA256.keys() - ADR_0009_NEW_SCHEMAS)
    ]
    return problems


class _ReachedAuthorization(Exception):
    """run_answer got past its prompt and Answer-schema hash checks."""


class _StopClock:
    def now(self) -> datetime:
        raise _ReachedAuthorization


async def _run_harness_hash_checks(expected_prompt_sha256: str) -> None:
    """Drive the real run_answer up to its first authorization clock read.

    run_answer hashes prompts/harness_system.md and schemas/v1/answer.json before it touches
    the service, manager, principal or provider, so those can be absent here.
    """
    nothing = cast(Any, None)
    await harness.run_answer(
        nothing, nothing, nothing, _StopClock(), "guard", nothing,
        expected_prompt_sha256=expected_prompt_sha256,
    )


def test_harness_prompt_hash_is_the_accepted_one() -> None:
    # run_answer builds its prompt inline, with no helper to import. This mirrors that one
    # expression exactly (harness._ROOT / "prompts/harness_system.md", read_text, encode,
    # sha256); the run_answer call below then proves the real code path agrees.
    prompt = (harness._ROOT / "prompts/harness_system.md").read_text()
    computed = hashlib.sha256(prompt.encode()).hexdigest()
    recorded = json.loads((ROOT / "fixtures/replays/clean-household.openai.json").read_text())

    assert computed == ACCEPTED_HARNESS_PROMPT_SHA256, (
        f"The harness prompt hash is {computed}; the accepted hash is "
        f"{ACCEPTED_HARNESS_PROMPT_SHA256}. prompts/harness_system.md is {FROZEN}: revert it."
    )
    assert recorded["run"]["prompt_sha256"] == ACCEPTED_HARNESS_PROMPT_SHA256, (
        "fixtures/replays/clean-household.openai.json no longer records the accepted harness "
        "prompt hash; the recorded replay and the prompt must stay as accepted."
    )
    assert recorded["run"]["schema_sha256"] == harness.ANSWER_SCHEMA_SHA256 == (
        SCHEMA_SHA256["answer.json"]
    ), "The recorded replay, the harness and this guard disagree on the Answer schema hash."
    with pytest.raises(_ReachedAuthorization):
        anyio.run(_run_harness_hash_checks, ACCEPTED_HARNESS_PROMPT_SHA256)


def test_prompts_are_frozen() -> None:
    problems = prompt_problems(ROOT)
    assert not problems, (
        f"prompts/ is {FROZEN}; the revamp never edits, adds or removes a prompt:\n  "
        + "\n  ".join(problems)
    )


def test_published_schemas_are_frozen_and_only_adr_0009_files_are_added() -> None:
    assert len(SCHEMA_SHA256) == 22 and len(ADR_0009_NEW_SCHEMAS) == 10
    problems = schema_problems(ROOT)
    assert not problems, (
        f"The 22 published schemas/v1 files are {FROZEN}, and ADR 0009 'Schemas and matrix' "
        "names the only ten files the revamp may add. A new contract needs an ADR first:\n  "
        + "\n  ".join(problems)
    )


def test_the_added_schema_files_are_exactly_those_built_so_far() -> None:
    added = set(_tree_sha256(ROOT / "schemas/v1")) - set(SCHEMA_SHA256)
    # With step 6 every contract is built: the additions are exactly ADR 0009's ten.
    assert BUILT_NEW_SCHEMAS == ADR_0009_NEW_SCHEMAS
    assert added == BUILT_NEW_SCHEMAS, (
        f"schemas/v1 adds {sorted(added)}; the steps built so far add "
        f"{sorted(BUILT_NEW_SCHEMAS)}"
    )


def test_mcp_registers_exactly_the_seven_tools() -> None:
    server = build_mcp((ROOT / "prompts/host_guidelines.md").read_text())
    registered = [tool.name for tool in anyio.run(server.list_tools)]
    assert len(registered) == len(set(registered)) and set(registered) == MCP_TOOLS, (
        "The MCP server must register exactly the seven accepted tools; the revamp's new "
        "surfaces are workbench and REST only (ADR 0009). "
        f"Unexpected: {sorted(set(registered) - MCP_TOOLS)}; "
        f"missing: {sorted(MCP_TOOLS - set(registered))}."
    )
    assert set(ARGUMENT_MODELS) == MCP_TOOLS, (
        "mcp_server.ARGUMENT_MODELS feeds the harness tool list and must name the same seven "
        f"tools; it names {sorted(ARGUMENT_MODELS)}."
    )


# The tests below prove the guards bite, on scratch copies; they never edit the repository.


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    shutil.copytree(ROOT / "schemas", tmp_path / "schemas")
    assert prompt_problems(tmp_path) == [] and schema_problems(tmp_path) == []
    return tmp_path


def _flip_last_byte(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))


@pytest.mark.parametrize("name", sorted(PROMPT_SHA256))
def test_prompt_guard_catches_one_changed_byte(scratch: Path, name: str) -> None:
    _flip_last_byte(scratch / "prompts" / name)
    assert [problem.split(" (")[0] for problem in prompt_problems(scratch)] == [
        f"prompts/{name} changed"
    ]


def test_prompt_guard_catches_added_and_removed_files(scratch: Path) -> None:
    (scratch / "prompts/extra.md").write_text("new")
    (scratch / "prompts/host_guidelines.md").unlink()
    assert prompt_problems(scratch) == [
        "prompts/host_guidelines.md is missing", "prompts/extra.md was added",
    ]


def test_harness_refuses_a_changed_prompt(
    scratch: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flip_last_byte(scratch / "prompts/harness_system.md")
    monkeypatch.setattr(harness, "_ROOT", scratch)
    with pytest.raises(ValueError, match="Harness prompt changed before inference"):
        anyio.run(_run_harness_hash_checks, ACCEPTED_HARNESS_PROMPT_SHA256)


@pytest.mark.parametrize("name", sorted(SCHEMA_SHA256))
def test_schema_guard_catches_one_changed_byte(scratch: Path, name: str) -> None:
    _flip_last_byte(scratch / "schemas/v1" / name)
    assert [problem.split(" (")[0] for problem in schema_problems(scratch)] == [
        f"schemas/v1/{name} changed"
    ]


def test_schema_guard_allows_only_the_ten_named_additions(scratch: Path) -> None:
    for name in ADR_0009_NEW_SCHEMAS:
        (scratch / "schemas/v1" / name).write_text("{}")
    assert schema_problems(scratch) == []
    (scratch / "schemas/v1/unplanned.json").write_text("{}")
    (scratch / "schemas/v1/trace.json").unlink()
    assert schema_problems(scratch) == [
        "schemas/v1/trace.json is missing",
        "schemas/v1/unplanned.json was added but is not one of ADR 0009's ten new schema files",
    ]
