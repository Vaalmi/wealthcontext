"""What the engineer's pages (E1, E2) show besides governed results: contracts only.

The published JSON Schemas are read as files, exactly as exported under `schemas/v1`. The
MCP list is what the server's `tools/list` publishes: the registered tools with their
annotations and output schemas, and the shared argument models as input schemas, as
`mcp_server._bind_state` substitutes them. Nothing here reads business storage, a trace
or a span; the pages call the governed `get_metric_definition` for everything else.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from wealthcontext.mcp_server import ARGUMENT_MODELS, build_mcp

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "schemas/v1"
_MATRIX = _ROOT / "policies/matrix.yaml"
_GUIDELINES = _ROOT / "prompts/host_guidelines.md"


@dataclass(frozen=True)
class PublishedSchema:
    """One exported schema file: its stem is the ID a page asks for."""

    id: str
    file: str
    title: str
    uri: str
    kind: str
    text: str


def published_schemas(directory: Path = _SCHEMAS) -> dict[str, PublishedSchema]:
    """Every file under `schemas/v1`, in name order, its text unchanged."""
    schemas = {}
    for path in sorted(directory.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        document = json.loads(text)
        schemas[path.stem] = PublishedSchema(
            id=path.stem, file=f"schemas/v1/{path.name}", title=str(document.get("title", "")),
            uri=str(document.get("$id", "")),
            kind="Arguments" if path.stem.endswith("-arguments") else "Result", text=text)
    return schemas


SCHEMAS = published_schemas()


def _routes() -> list[dict]:
    return yaml.safe_load(_MATRIX.read_text())["routes"]


def rest_mirrors() -> dict[str, str]:
    """Each MCP tool's REST route, from the permission matrix."""
    return {row["tool"]: f"{row['method']} {row['path']}" for row in _routes()
            if str(row["path"]).startswith("/api/") and row.get("tool") in ARGUMENT_MODELS}


def metric_surfaces() -> list[dict]:
    """Where definitions are served outside the workbench: the matrix's rows whose action is
    `metric_definition` (the REST route and the MCP catalog resource)."""
    return [{"method": row["method"], "path": row["path"], "tool": row.get("tool")}
            for row in _routes()
            if row["action"] == "metric_definition" and not row["path"].startswith("/workbench")]


def _arguments(schema: dict) -> list[dict]:
    required = set(schema.get("required", []))
    return [{"name": name, "required": name in required}
            for name in schema.get("properties", {})]


async def published_mcp() -> dict:
    """The tools, resources and prompts the MCP server publishes, without starting it."""
    server = build_mcp(_GUIDELINES.read_text())
    mirrors = rest_mirrors()
    tools = []
    for tool in await server.list_tools():
        listed = tool.model_dump(by_alias=True, mode="json", exclude_none=True)
        input_schema = ARGUMENT_MODELS[tool.name].model_json_schema()
        tools.append({
            "name": tool.name, "description": listed.get("description", ""),
            "annotations": listed.get("annotations", {}), "input_schema": input_schema,
            "output_schema": listed.get("outputSchema"), "arguments": _arguments(input_schema),
            "rest": mirrors.get(tool.name)})
    resources = [resource.model_dump(by_alias=True, mode="json", exclude_none=True)
                 for resource in await server.list_resources()]
    prompts = [prompt.model_dump(by_alias=True, mode="json", exclude_none=True)
               for prompt in await server.list_prompts()]
    return {"tools": tools, "resources": resources, "prompts": prompts}
