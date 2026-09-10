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


async def test_call_reconnects_after_transport_drop() -> None:
    """A dropped transport (server restart / idle reset) must not break every call."""
    registry = InMemoryToolRegistry()
    manager = McpManager(_config(), registry)
    await manager.ensure("echo")
    tool = registry.get(mcp_tool_name("echo", "add"))
    connection = manager._connections["echo"]
    await connection.stop()  # transport dies, as on an idle reset
    assert connection.session is None
    result = await tool.invoke(ToolContext(tenant_id="t", run_id="r", session_id="s", metadata={"tool_call_id": "c2"}), {"a": 4, "b": 5})
    assert result.content.strip() == "9" and not result.is_error
    assert connection.session is not None  # the call reconnected transparently
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


async def test_proxy_round_trips_a_server_token_through_the_vault() -> None:
    """A read returns an edit token; the model sees a placeholder; the edit call gets the token back."""
    from types import SimpleNamespace
    from typing import Any

    from protocore.contracts.tools import ToolContext
    from protocore.contracts.types import ToolDefinition, ToolParameterSchema

    from daedalus.mcp.manager import VAULT_NOTE, McpToolProxy, SecretVault

    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(tool: str, arguments: dict[str, Any]) -> Any:
        calls.append((tool, arguments))
        if tool == "read":
            return SimpleNamespace(content=[SimpleNamespace(text='{"id": "p1", "edit_token": "tok9a8b7c6d5e4f3a2b1", "author": "host"}')], isError=False)
        return SimpleNamespace(content=[SimpleNamespace(text="edited")], isError=False)

    connection = SimpleNamespace(name="board", vault=SecretVault(), call=call)
    definition = ToolDefinition(name="Mcp_Board_read", description="", parameters=ToolParameterSchema(type="object", properties={}))
    read = McpToolProxy(connection, "read", definition)  # type: ignore[arg-type]
    edit = McpToolProxy(connection, "edit", ToolDefinition(name="Mcp_Board_edit", description="", parameters=ToolParameterSchema(type="object", properties={})))  # type: ignore[arg-type]
    context = ToolContext(tenant_id="t", session_id="s", run_id="r", metadata={"tool_call_id": "c1"})
    shown = await read.invoke(context, {"id": "p1"})
    assert "tok9a8b7c6d5e4f3a2b1" not in shown.content and '"edit_token": "«ref:' in shown.content and shown.content.endswith(VAULT_NOTE)
    ref = shown.content.split('"edit_token": "')[1].split('"')[0]
    result = await edit.invoke(context, {"id": "p1", "edit_token": ref, "body": f"restore with {ref}", "nested": {"tokens": [ref]}})
    assert result.content == "edited"
    sent = calls[-1][1]
    assert sent["edit_token"] == "tok9a8b7c6d5e4f3a2b1" and sent["body"] == "restore with tok9a8b7c6d5e4f3a2b1" and sent["nested"]["tokens"] == ["tok9a8b7c6d5e4f3a2b1"]
    # a placeholder the vault never issued goes through untouched
    await edit.invoke(context, {"edit_token": "«ref:0000000000»"})
    assert calls[-1][1]["edit_token"] == "«ref:0000000000»"
