from __future__ import annotations

import sys
from pathlib import Path

from protocore.contracts.tools import ToolContext
from protocore.tests_support.adapters import InMemoryToolRegistry

from daedalus.config import McpServerConfig, RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.mcp.manager import McpManager, blocked_for, mcp_tool_name
from daedalus.stores.database import Database

SERVER = Path(__file__).resolve().parents[1] / "support" / "mcp_echo_server.py"


def _config() -> dict[str, McpServerConfig]:
    return {"echo": McpServerConfig(transport="stdio", command=sys.executable, args=[str(SERVER)], description="echo tools")}


async def test_manager_connects_and_registers_proxies() -> None:
    registry = InMemoryToolRegistry()
    manager = McpManager(_config(), registry)
    assert manager.status()[0]["connected"] is False
    await manager.ensure("echo")
    names = manager.tool_names("echo")
    assert names == {mcp_tool_name("echo", "echo"), mcp_tool_name("echo", "add")}
    tool = registry.get(mcp_tool_name("echo", "add"))
    assert tool is not None and "a" in tool.definition.parameters.properties
    result = await tool.invoke(ToolContext(tenant_id="t", run_id="r", session_id="s", metadata={"tool_call_id": "c1"}), {"a": 2, "b": 3})
    assert result.content.strip() == "5" and not result.is_error
    assert blocked_for(manager, []) == names and blocked_for(manager, ["echo"]) == set()
    await manager.close()


async def test_session_toggle_changes_visibility(settings: Settings, db: Database) -> None:
    config = RuntimeConfig()
    config.mcp.servers = _config()
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("t")
    assert await manager.mcp_service("list", session_id=state.session.id) == "[off] echo — echo tools: not connected yet"
    text = await manager.mcp_service("enable", session_id=state.session.id, server="echo")
    assert "enabled echo" in text and mcp_tool_name("echo", "add") in text
    assert manager.mcp_enabled(state) == ["echo"]
    reloaded = await manager.sessions.get(state.session.id, "daedalus")
    assert reloaded.metadata["mcp_enabled"] == ["echo"]
    await manager.mcp_service("disable", session_id=state.session.id, server="echo")
    assert manager.mcp_enabled(state) == []
    await manager.close()
