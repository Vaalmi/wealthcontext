"""Exercise the real stdio bridge against a loopback-only HTTP MCP server."""

import json
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from subprocess import DEVNULL
from typing import Annotated, Literal

import anyio
import pytest
import uvicorn
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.shared.inbound import decode_header_value
from mcp.shared.message import ClientMessageMetadata, SessionMessage
from mcp.types import (
    PROTOCOL_VERSION_META_KEY,
    JSONRPCRequest,
    JSONRPCResponse,
    TextContent,
    TextResourceContents,
)
from pydantic import Field
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from wealthcontext.bridge import BridgeError, _Headers, run_bridge

TOKEN = "synthetic-bridge-test-token"
INSTRUCTIONS = "Quote governed figures exactly and disclose every warning."
RESOURCE = "wealthcontext://metrics/catalog"


@asynccontextmanager
async def local_server(
    *, stall_delete: bool = False,
) -> AsyncIterator[tuple[str, list[dict[str, str]], anyio.Event]]:
    server = MCPServer("bridge-test", instructions=INSTRUCTIONS)

    @server.tool()
    def echo(
        text: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Context"})],
    ) -> dict[str, str]:
        return {"echo": text}

    @server.resource(RESOURCE)
    def catalog() -> str:
        return "HOUSEHOLD_ASSETS@1"

    @server.prompt(name="wealthcontext_answer")
    def answer(question: str) -> str:
        return f"Governed answer: {question}"

    app = server.streamable_http_app()
    requests: list[dict[str, str]] = []
    release_delete = anyio.Event()

    async def observed_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(Headers(scope=scope))
            requests.append({**headers, "http_method": scope["method"]})
            if headers.get("authorization") != f"Bearer {TOKEN}":
                await Response("Unauthorized", status_code=401)(scope, receive, send)
                return
            if stall_delete and scope["method"] == "DELETE":
                await release_delete.wait()
        await app(scope, receive, send)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    web_server = uvicorn.Server(uvicorn.Config(
        observed_app, log_level="critical", lifespan="on", timeout_graceful_shutdown=1,
    ))
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(web_server.serve, sockets=[listener]))
            with anyio.fail_after(5):
                while not web_server.started:
                    await anyio.sleep(0.01)
            try:
                yield f"http://127.0.0.1:{port}/mcp", requests, release_delete
            finally:
                release_delete.set()
                web_server.should_exit = True
    finally:
        listener.close()


def bridge_command(url: str, tmp_path: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "wealthcontext.bridge"],
        env={"WEALTHCONTEXT_MCP_URL": url, "WEALTHCONTEXT_TOKEN": TOKEN},
        cwd=tmp_path,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_bridge_relays_tools_resources_prompts_and_instructions(
    tmp_path: Path, mode: Literal["auto", "legacy"],
) -> None:
    async with local_server() as (url, requests, _):
        with anyio.fail_after(15):
            async with Client(bridge_command(url, tmp_path), mode=mode, cache=None) as client:
                assert client.instructions == INSTRUCTIONS
                listing = await client.list_tools()
                assert [tool.name for tool in listing.tools] == ["echo"]
                result = await client.call_tool("echo", {"text": "résumé"})
                assert result.structured_content == {"echo": "résumé"}
                resources = await client.list_resources()
                assert str(resources.resources[0].uri) == RESOURCE
                resource = await client.read_resource(RESOURCE)
                assert isinstance(resource.contents[0], TextResourceContents)
                assert resource.contents[0].text == "HOUSEHOLD_ASSETS@1"
                prompts = await client.list_prompts()
                assert prompts.prompts[0].name == "wealthcontext_answer"
                prompt = await client.get_prompt("wealthcontext_answer", {"question": "Assets?"})
                assert isinstance(prompt.messages[0].content, TextContent)
                assert prompt.messages[0].content.text == "Governed answer: Assets?"

        assert requests
        assert all(item["authorization"] == f"Bearer {TOKEN}" for item in requests)
        data_calls = [item for item in requests if item.get("mcp-method") == "tools/call"]
        assert len(data_calls) == 1
        if mode == "auto":
            assert data_calls[0]["mcp-protocol-version"] == "2026-07-28"
            assert decode_header_value(data_calls[0]["mcp-param-context"]) == "résumé"
            assert any(item.get("mcp-method") == "server/discover" for item in requests)
            assert not any(item.get("mcp-method") == "initialize" for item in requests)
        else:
            assert data_calls[0]["mcp-protocol-version"] in {
                "2025-03-26", "2025-06-18", "2025-11-25",
            }
            assert any(item["http_method"] == "DELETE" for item in requests)


def test_header_adapter_preserves_body_and_encodes_names() -> None:
    headers = _Headers()
    request = JSONRPCRequest(
        jsonrpc="2.0", id="read", method="resources/read",
        params={"uri": "wealthcontext://reports/résumé", "_meta": {
            PROTOCOL_VERSION_META_KEY: "2026-07-28",
        }},
    )
    outgoing = headers.outgoing(SessionMessage(request))
    assert outgoing.message is request
    assert isinstance(outgoing.metadata, ClientMessageMetadata)
    mirrored = outgoing.metadata.headers
    assert mirrored is not None
    assert mirrored["mcp-method"] == "resources/read"
    assert mirrored["mcp-protocol-version"] == "2026-07-28"
    assert decode_header_value(mirrored["mcp-name"]) == "wealthcontext://reports/résumé"
    headers.outgoing(SessionMessage(JSONRPCRequest(jsonrpc="2.0", id=1, method="initialize")))
    assert headers.version is None
    headers.incoming(SessionMessage(JSONRPCResponse(
        jsonrpc="2.0", id=1, result={"protocolVersion": "2025-11-25", "instructions": INSTRUCTIONS},
    )))
    assert headers.version == "2025-11-25"


@pytest.mark.anyio
async def test_bridge_empty_stdin_exits_without_protocol_noise(tmp_path: Path) -> None:
    command = bridge_command("http://127.0.0.1:1/mcp", tmp_path)
    with anyio.fail_after(3):
        result = await anyio.run_process(
            [command.command, *command.args], stdin=DEVNULL, env=command.env, cwd=command.cwd,
        )
    assert result.returncode == 0
    assert result.stdout == b""
    assert TOKEN.encode() not in result.stderr


@pytest.mark.anyio
async def test_bridge_eof_bounds_stalled_session_cleanup(tmp_path: Path) -> None:
    async with local_server(stall_delete=True) as (url, requests, _):
        command = bridge_command(url, tmp_path)
        async with await anyio.open_process(
            [command.command, *command.args], env=command.env, cwd=command.cwd,
        ) as process:
            assert process.stdin is not None and process.stdout is not None
            initialize = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "eof-test", "version": "1"},
                },
            }
            await process.stdin.send(json.dumps(initialize).encode() + b"\n")
            with anyio.fail_after(5):
                line = await BufferedByteReceiveStream(process.stdout).receive_until(b"\n", 65536)
            assert json.loads(line)["result"]["instructions"] == INSTRUCTIONS
            await process.stdin.aclose()
            with anyio.fail_after(2.5):
                assert await process.wait() == 0
        assert any(item["http_method"] == "DELETE" for item in requests)


@pytest.mark.anyio
@pytest.mark.parametrize("input_bytes", [b"not-json\n", b'{"wrong":true}\n'])
async def test_malformed_input_exits_without_echoing_input_or_token(
    tmp_path: Path, input_bytes: bytes,
) -> None:
    command = bridge_command("http://127.0.0.1:1/mcp", tmp_path)
    with anyio.fail_after(3):
        result = await anyio.run_process(
            [command.command, *command.args], input=input_bytes,
            env=command.env, cwd=command.cwd, check=False,
        )
    assert result.returncode != 0
    assert result.stdout == b""
    assert b"malformed stdio input" in result.stderr
    assert TOKEN.encode() not in result.stderr
    assert input_bytes.strip() not in result.stderr


@pytest.mark.anyio
async def test_missing_token_fails_before_opening_transports() -> None:
    with pytest.raises(BridgeError, match="WEALTHCONTEXT_TOKEN is required"):
        await run_bridge("http://127.0.0.1:1/mcp", "")


@pytest.mark.anyio
async def test_missing_token_cli_reports_stderr_only(tmp_path: Path) -> None:
    with anyio.fail_after(3):
        result = await anyio.run_process(
            [sys.executable, "-m", "wealthcontext.bridge"], stdin=DEVNULL, env={},
            cwd=tmp_path, check=False,
        )
    assert result.returncode != 0
    assert result.stdout == b""
    assert b"WEALTHCONTEXT_TOKEN is required" in result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.anyio
async def test_http_connection_failure_cancels_stdio_reader(tmp_path: Path) -> None:
    command = bridge_command("http://127.0.0.1:1/mcp", tmp_path)
    async with await anyio.open_process(
        [command.command, *command.args], env=command.env, cwd=command.cwd,
    ) as process:
        assert process.stdin is not None and process.stderr is not None
        await process.stdin.send(
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        )
        # Keep stdin open: the HTTP failure must cancel the blocked stdio reader.
        with anyio.fail_after(5):
            assert await process.wait() != 0
        stderr = b"".join([chunk async for chunk in process.stderr])
        assert b"MCP bridge transport failed" in stderr
        assert TOKEN.encode() not in stderr
