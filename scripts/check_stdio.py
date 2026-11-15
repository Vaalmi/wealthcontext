"""Launch the documented Compose stdio client from an unrelated directory, without a TTY."""

import argparse
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import anyio
from mcp import Client, StdioServerParameters
from mcp.types import TextContent, TextResourceContents


async def check(compose: Path, persona: str) -> list[dict]:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Docker CLI required")
    # Never print tokens or include them in the recorded check output.
    token = subprocess.check_output(
        [docker, "compose", "-f", str(compose), "exec", "-T", "app",
         "wealthcontext", "token", "--persona", persona], text=True,
    ).strip()
    evidence = []
    modes: tuple[Literal["auto", "legacy"], ...] = ("auto", "legacy")
    for mode in modes:
        with tempfile.TemporaryDirectory(prefix="wealthcontext-stdio-check-") as unrelated:
            parameters = StdioServerParameters(
                command=docker,
                args=["compose", "-f", str(compose), "run", "--rm", "--no-deps", "-T",
                      "-e", "WEALTHCONTEXT_TOKEN", "mcp"],
                env={"WEALTHCONTEXT_TOKEN": token}, cwd=unrelated,
            )
            with anyio.fail_after(90):
                async with Client(parameters, mode=mode, cache=None) as client:
                    assert client.instructions and "unverified" in client.instructions
                    listing = await client.list_tools()
                    assert {tool.name for tool in listing.tools} == {
                        "get_household_snapshot", "get_metric_definition", "get_data_provenance",
                        "get_portfolio_exposure", "get_concentrated_positions",
                        "search_households", "get_data_quality_report",
                    }
                    result = await client.call_tool("get_household_snapshot", {
                        "household_id": "WC-HH-0001", "as_of": "2026-09-30",
                    })
                    envelope = result.structured_content
                    assert envelope and envelope["status"] == "COMPLETE", result
                    assert envelope["data"]["assets_usd"] == "3000.00"
                    resource = await client.read_resource("wealthcontext://metrics/catalog")
                    assert isinstance(resource.contents[0], TextResourceContents)
                    assert json.loads(resource.contents[0].text)["data"]["item_count"] == 5
                    prompt = await client.get_prompt("wealthcontext_answer", {
                        "question": "What assets are included?", "as_of": "2026-09-30",
                    })
                    assert isinstance(prompt.messages[0].content, TextContent)
                    assert client.instructions in prompt.messages[0].content.text
                    evidence.append({
                        "mode": mode, "trace_id": envelope["trace_id"],
                        "result_id": envelope["result_id"], "assets_usd": "3000.00",
                        "tool": "PASS", "resource": "PASS", "prompt": "PASS",
                        "instructions": "PASS", "outside_repository": True, "tty": False,
                    })
            evidence[-1]["clean_shutdown"] = True
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--persona", default="ADV-01")
    options = parser.parse_args()
    print(json.dumps(asyncio.run(check(options.compose.resolve(), options.persona)), indent=2))
