"""Transparent stdio-to-HTTP MCP transport; identity stays in the bearer header."""

import io
import os
import stat
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import NoReturn, TextIO

import anyio
import httpx2
from mcp.client._transport import TransportStreams
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server
from mcp.shared.inbound import (
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    NAME_BEARING_METHODS,
    encode_header_value,
    mcp_param_headers,
    x_mcp_header_map,
)
from mcp.shared.message import ClientMessageMetadata, SessionMessage
from mcp.types import (
    PROTOCOL_VERSION_META_KEY,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    RequestId,
)

# Keep normal calls bounded, and let EOF interrupt even a stalled session DELETE.
_HTTP_TIMEOUT = httpx2.Timeout(3.0, read=60.0)
_SHUTDOWN_SECONDS = 1.0


class BridgeError(RuntimeError):
    """A safe diagnostic that never includes bearer credentials or message contents."""


class _BridgeStopped(Exception):
    """Unwind transport task groups when one relay reaches EOF or fails."""


class _CancellableStdin(anyio.AsyncFile[str]):
    def __init__(self, stream: TextIO):
        super().__init__(stream)
        self._buffer = bytearray()
        self._eof = False
        mode = os.fstat(stream.fileno()).st_mode
        self._wait_for_input = (
            stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or os.isatty(stream.fileno())
        )

    async def readline(self) -> str:
        # AsyncFile's ordinary blocking reader cannot be interrupted at process
        # exit while the host still holds stdin open. Wait for the pipe instead.
        while (newline := self._buffer.find(b"\n")) == -1 and not self._eof:
            if self._wait_for_input:
                await anyio.wait_readable(self.wrapped.fileno())
            chunk = os.read(self.wrapped.fileno(), 65536)
            if chunk:
                self._buffer.extend(chunk)
            else:
                self._eof = True
        length = newline + 1 if newline != -1 else len(self._buffer)
        line = bytes(self._buffer[:length])
        del self._buffer[:length]
        return line.decode("utf-8", errors="replace")


@asynccontextmanager
async def _stdio_transport() -> AsyncIterator[TransportStreams]:
    with io.TextIOWrapper(
        os.fdopen(os.dup(sys.stdin.fileno()), "rb"), encoding="utf-8", errors="replace"
    ) as stream:
        # Keep the SDK's protocol framing and stdout diversion; only replace its
        # non-cancellable input reader. No worker is left using this descriptor.
        async with stdio_server(stdin=_CancellableStdin(stream)) as streams:
            yield streams


@dataclass
class _Headers:
    """Restore HTTP routing metadata that the stdio wire cannot carry."""

    version: str | None = None
    pending: dict[RequestId, str] = field(default_factory=dict)
    tool_headers: dict[str, dict[tuple[str, ...], str]] = field(default_factory=dict)

    def outgoing(self, item: SessionMessage) -> SessionMessage:
        message = item.message
        headers: dict[str, str] = {}
        if isinstance(message, JSONRPCRequest | JSONRPCNotification):
            params = message.params or {}
            if isinstance(message, JSONRPCRequest):
                if message.method in {"initialize", "tools/list"}:
                    self.pending[message.id] = message.method
                if message.method == "initialize":
                    self.version = None
            meta = params.get("_meta")
            if isinstance(meta, dict) and isinstance(
                version := meta.get(PROTOCOL_VERSION_META_KEY), str
            ):
                self.version = version
            headers[MCP_METHOD_HEADER] = message.method
            name_key = NAME_BEARING_METHODS.get(message.method)
            if name_key is not None and isinstance(name := params.get(name_key), str):
                headers[MCP_NAME_HEADER] = encode_header_value(name)
            if message.method == "tools/call" and isinstance(name := params.get("name"), str):
                arguments = params.get("arguments") or {}
                if isinstance(arguments, dict):
                    headers.update(mcp_param_headers(self.tool_headers.get(name, {}), arguments))
        if self.version is not None:
            headers[MCP_PROTOCOL_VERSION_HEADER] = self.version
        return SessionMessage(message, ClientMessageMetadata(headers=headers))

    def incoming(self, item: SessionMessage) -> None:
        message = item.message
        if not isinstance(message, JSONRPCResponse | JSONRPCError):
            return
        method = self.pending.pop(message.id, None) if message.id is not None else None
        if not isinstance(message, JSONRPCResponse):
            return
        if method == "initialize" and isinstance(
            version := message.result.get("protocolVersion"), str
        ):
            self.version = version
        if method == "tools/list":
            for tool in message.result.get("tools", []):
                if isinstance(tool, dict) and isinstance(name := tool.get("name"), str):
                    self.tool_headers[name] = x_mcp_header_map(tool.get("inputSchema", {}))


@dataclass
class _Lifecycle:
    scope: anyio.CancelScope
    error: str | None = None

    def stop(self, error: str | None = None) -> NoReturn:
        if self.error is None:
            self.error = error
        self.scope.deadline = min(
            self.scope.deadline, anyio.current_time() + _SHUTDOWN_SECONDS
        )
        raise _BridgeStopped


async def run_bridge(url: str, token: str) -> None:
    """Relay one stdio connection to the HTTP endpoint until EOF or transport failure.

    Messages and initialize instructions are forwarded, not reinterpreted as tools.
    An outer deadline bounds shutdown while allowing the SDK to DELETE its session.
    """
    if not token or not token.strip():
        raise BridgeError("WEALTHCONTEXT_TOKEN is required for the MCP bridge")
    if not token.isascii() or any(
        ord(character) <= 32 or ord(character) == 127 for character in token
    ):
        raise BridgeError("WEALTHCONTEXT_TOKEN is not a valid bearer token")

    scope = anyio.CancelScope()
    lifecycle = _Lifecycle(scope)
    with scope:
        routing = _Headers()
        try:
            try:
                async with (
                    httpx2.AsyncClient(
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=_HTTP_TIMEOUT,
                        trust_env=False,
                    ) as client,
                    _stdio_transport() as (stdio_read, stdio_write),
                    streamable_http_client(url, http_client=client) as (http_read, http_write),
                ):

                    async def to_http() -> None:
                        try:
                            async for item in stdio_read:
                                if isinstance(item, Exception):
                                    lifecycle.stop("The MCP bridge received malformed stdio input")
                                await http_write.send(routing.outgoing(item))
                        except _BridgeStopped:
                            raise
                        except Exception:
                            lifecycle.stop("The MCP bridge could not forward a client message")
                        lifecycle.stop()

                    async def to_stdio() -> None:
                        try:
                            async for item in http_read:
                                if isinstance(item, Exception):
                                    lifecycle.stop("The MCP bridge received an HTTP error")
                                routing.incoming(item)
                                await stdio_write.send(item)
                        except _BridgeStopped:
                            raise
                        except Exception:
                            lifecycle.stop("The MCP bridge could not forward a server message")
                        lifecycle.stop()

                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(to_http)
                        tasks.start_soon(to_stdio)
            except* _BridgeStopped:
                pass
        except Exception:
            raise BridgeError("The MCP bridge transport failed") from None
    if lifecycle.error is not None:
        raise BridgeError(lifecycle.error)


def bridge_from_env() -> None:
    """CLI entrypoint: safe errors go to stderr; stdout remains MCP-only."""
    try:
        anyio.run(
            run_bridge,
            os.environ.get("WEALTHCONTEXT_MCP_URL", "http://app:8000/mcp"),
            os.environ.get("WEALTHCONTEXT_TOKEN", ""),
        )
    except BridgeError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    bridge_from_env()
