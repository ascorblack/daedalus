"""Long-lived MCP connections and the tool proxies that stand in for remote tools.

Every configured server is connected lazily the first time a session enables it.
Its tools are registered once, under ``Mcp_<server>_<tool>``, in the shared registry;
which sessions actually see them is decided per run by the tool visibility policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolDefinition, ToolParameterSchema, ToolResult

from daedalus.config import McpServerConfig

logger = logging.getLogger(__name__)


def mcp_tool_name(server: str, tool: str) -> str:
    safe_server = re.sub(r"[^A-Za-z0-9]+", "", server.title()) or "Server"
    safe_tool = re.sub(r"[^A-Za-z0-9_]+", "_", tool)
    return f"Mcp_{safe_server}_{safe_tool}"[:64]


class McpToolProxy(Tool):
    """A core ``Tool`` that forwards to a remote MCP tool."""

    def __init__(self, connection: McpConnection, remote_name: str, definition: ToolDefinition) -> None:
        self._connection = connection
        self._remote = remote_name
        self._definition = definition
        self.server = connection.name

    @property
    def name(self) -> str:
        return self._definition.name

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        call_id = str(context.metadata.get("tool_call_id") or "")
        try:
            result = await self._connection.call(self._remote, arguments)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call_id, content=f"MCP tool failed: {exc}", is_error=True)
        parts: list[str] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
            elif getattr(item, "type", "") == "image":
                parts.append(f"[image {getattr(item, 'mimeType', '')} returned by the tool]")
            else:
                dump = getattr(item, "model_dump", None)
                parts.append(json.dumps(dump() if dump else str(item), default=str)[:2000])
        content = "\n".join(parts) or "(empty result)"
        return ToolResult(tool_call_id=call_id, content=content[:60_000], is_error=bool(getattr(result, "is_error", None) or getattr(result, "isError", False)))


def _describe(exc: BaseException) -> str:
    """Flatten exception groups into the first real cause."""
    inner = exc
    while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
        inner = inner.exceptions[0]
    return f"{type(inner).__name__}: {inner}"


class McpConnection:
    """One server; the transport and session live in a background task."""

    def __init__(self, name: str, config: McpServerConfig) -> None:
        self.name = name
        self.config = config
        self.session: ClientSession | None = None
        self.tools: list[McpToolProxy] = []
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self.error: str | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            await self._ready.wait()
            return
        self._ready.clear()
        self._closing.clear()
        self.error = None
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.name}")
        await self._ready.wait()
        if self.error:
            raise RuntimeError(self.error)

    async def _run(self) -> None:
        try:
            if self.config.transport == "stdio":
                params = StdioServerParameters(command=self.config.command, args=list(self.config.args), env=dict(self.config.env) or None)
                async with stdio_client(params) as (read, write):
                    await self._serve(read, write)
            else:
                import httpx

                async with httpx.AsyncClient(headers=dict(self.config.headers), timeout=60.0) as client:
                    async with streamable_http_client(self.config.url, http_client=client) as streams:
                        await self._serve(streams[0], streams[1])
        except BaseException as exc:  # noqa: BLE001 — anyio wraps transport failures in groups
            self.error = _describe(exc)
            logger.warning("mcp server %s failed: %s", self.name, self.error)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.session = None
            self._ready.set()

    async def _serve(self, read: Any, write: Any) -> None:
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            self.tools = [self._proxy(t) for t in listing.tools]
            self.session = session
            self._ready.set()
            await self._closing.wait()

    def _proxy(self, remote: Any) -> McpToolProxy:
        schema = getattr(remote, "input_schema", None) or getattr(remote, "inputSchema", None) or {}
        definition = ToolDefinition(
            name=mcp_tool_name(self.name, remote.name),
            description=f"[MCP {self.name}] {remote.description or remote.name}",
            parameters=ToolParameterSchema(properties=dict(schema.get("properties") or {}), required=list(schema.get("required") or [])),
        )
        return McpToolProxy(self, remote.name, definition)

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if self.session is None:
            raise RuntimeError(f"MCP server {self.name} is not connected")
        return await asyncio.wait_for(self.session.call_tool(tool, arguments), timeout=self.config.timeout_seconds)

    async def stop(self) -> None:
        self._closing.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()


class McpManager:
    def __init__(self, servers: dict[str, McpServerConfig], registry: Any) -> None:
        self._configs = servers
        self._registry = registry
        self._connections: dict[str, McpConnection] = {}

    def reload(self, servers: dict[str, McpServerConfig]) -> None:
        self._configs = servers

    def available(self) -> list[str]:
        return sorted(self._configs)

    def describe(self, name: str) -> str:
        cfg = self._configs.get(name)
        return cfg.description if cfg else ""

    def all_tool_names(self) -> set[str]:
        return {t.name for c in self._connections.values() for t in c.tools}

    def tool_names(self, server: str) -> set[str]:
        connection = self._connections.get(server)
        return {t.name for t in connection.tools} if connection else set()

    async def ensure(self, name: str) -> McpConnection:
        """Connect a configured server (idempotent) and register its tools."""
        if name not in self._configs:
            raise KeyError(f"unknown MCP server {name!r}; configured: {self.available()}")
        connection = self._connections.get(name)
        if connection is None:
            connection = McpConnection(name, self._configs[name])
            self._connections[name] = connection
        await connection.start()
        for proxy in connection.tools:
            if self._registry.get(proxy.name) is None:
                self._registry.register(proxy)
        return connection

    async def close(self) -> None:
        for connection in self._connections.values():
            await connection.stop()

    def status(self) -> list[dict[str, Any]]:
        out = []
        for name in self.available():
            connection = self._connections.get(name)
            out.append(
                {
                    "name": name,
                    "description": self.describe(name),
                    "connected": bool(connection and connection.session is not None),
                    "error": connection.error if connection else None,
                    "tools": sorted(t.name for t in connection.tools) if connection else [],
                }
            )
        return out


def blocked_for(manager: McpManager, enabled: Sequence[str]) -> set[str]:
    """Tool names hidden from a run that has only ``enabled`` servers switched on."""
    enabled_set = set(enabled)
    return {name for name in manager.all_tool_names() if not any(name in manager.tool_names(s) for s in enabled_set)}


__all__ = ["McpConnection", "McpManager", "McpToolProxy", "blocked_for", "mcp_tool_name"]
