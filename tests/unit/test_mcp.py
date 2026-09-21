from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from protocore.contracts.tools import ToolContext
from protocore.tests_support.adapters import InMemoryToolRegistry

from daedalus.config import McpServerConfig, RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.mcp.manager import McpConnection, McpManager, blocked_for, mcp_tool_name
from daedalus.stores.database import Database

SERVER = Path(__file__).resolve().parents[1] / "support" / "mcp_echo_server.py"


def _config() -> dict[str, McpServerConfig]:
    return {"echo": McpServerConfig(transport="stdio", command=sys.executable, args=[str(SERVER)], description="echo tools")}


def _remote(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=name, input_schema={"type": "object", "properties": {}})


async def test_manager_connects_and_registers_proxies() -> None:
    registry = InMemoryToolRegistry()
    manager = McpManager(_config(), registry)
    assert manager.status()[0]["connected"] is False
    await manager.ensure("echo")
    assert manager.status()[0]["state"] == "ready"
    assert manager.status()[0]["catalog_revision"] == 1
    names = manager.tool_names("echo")
    assert names == {mcp_tool_name("echo", "echo"), mcp_tool_name("echo", "add")}
    tool = registry.get(mcp_tool_name("echo", "add"))
    assert tool is not None and "a" in tool.definition.parameters.properties
    result = await tool.invoke(ToolContext(tenant_id="t", run_id="r", session_id="s", metadata={"tool_call_id": "c1"}), {"a": 2, "b": 3})
    assert result.content.strip() == "5" and not result.is_error
    assert blocked_for(manager, []) == names and blocked_for(manager, ["echo"]) == set()
    await manager.close()


async def test_concurrent_enable_is_single_flight() -> None:
    registry = InMemoryToolRegistry()
    manager = McpManager(_config(), registry)
    connections = await asyncio.gather(*(manager.ensure("echo") for _ in range(10)))
    assert len({id(connection) for connection in connections}) == 1
    assert manager.status()[0]["catalog_revision"] == 1
    await manager.close()


async def test_changed_server_config_replaces_connection_and_catalog_proxy() -> None:
    registry = InMemoryToolRegistry()
    manager = McpManager(_config(), registry)
    first = await manager.ensure("echo")
    retired = first._proxy(_remote("retired"))
    manager._replace_catalog("echo", [*first.tools, retired])
    assert registry.get(retired.name) is retired
    changed = _config()
    changed["echo"] = changed["echo"].model_copy(update={"description": "changed"})
    manager.reload(changed)
    assert manager.status()[0]["state"] == "disabled"
    assert registry.get(retired.name) is None
    second = await manager.ensure("echo")
    assert second is not first and first.state == "disabled"
    proxy = registry.get(mcp_tool_name("echo", "add"))
    assert proxy is not None and proxy._connection is second
    await manager.close()


async def test_catalog_shrink_unregisters_only_that_servers_removed_tools() -> None:
    registry = InMemoryToolRegistry()
    manager = McpManager({}, registry)
    first = McpConnection("first", McpServerConfig(transport="stdio", command="first"))
    other = McpConnection("other", McpServerConfig(transport="stdio", command="other"))
    kept = first._proxy(_remote("kept"))
    removed = first._proxy(_remote("removed"))
    unrelated = other._proxy(_remote("unrelated"))
    manager._replace_catalog("first", [kept, removed])
    manager._replace_catalog("other", [unrelated])

    manager._replace_catalog("first", [kept])

    assert registry.get(kept.name) is kept
    assert registry.get(removed.name) is None
    assert registry.get(unrelated.name) is unrelated
    assert manager.all_tool_names() == {kept.name, unrelated.name}
    await manager.close()


async def test_reload_removing_server_unregisters_its_catalog() -> None:
    registry = InMemoryToolRegistry()
    manager = McpManager(_config(), registry)
    await manager.ensure("echo")
    names = manager.tool_names("echo")

    manager.reload({})

    assert names
    assert manager.all_tool_names() == set()
    assert all(registry.get(name) is None for name in names)
    await manager.close()


async def test_expired_token_refresh_is_single_flight() -> None:
    from types import SimpleNamespace

    class OAuth:
        def __init__(self) -> None:
            self.expired = True
            self.refreshes = 0

        def needs_refresh(self) -> bool:
            return self.expired

        async def refresh(self) -> None:
            self.refreshes += 1
            self.expired = False

    class Session:
        async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
            return SimpleNamespace(content=[])

    oauth = OAuth()
    connection = McpConnection(
        "remote", McpServerConfig(transport="http", url="https://example.test/mcp"), oauth=oauth
    )
    connection.session = Session()

    async def stop() -> None:
        connection.session = None

    async def start() -> None:
        connection.session = Session()

    connection.stop = stop
    connection.start = start
    await asyncio.gather(*(connection.call("read", {}) for _ in range(10)))
    assert oauth.refreshes == 1
    assert connection.in_flight == 0


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


async def test_cancelling_a_call_while_it_reconnects_stays_cancelled() -> None:
    connection = McpConnection("remote", McpServerConfig(transport="http", url="https://example.test/mcp"))
    waiting = asyncio.Event()

    async def start() -> None:
        waiting.set()
        await asyncio.Event().wait()

    connection.start = start  # type: ignore[method-assign]
    call = asyncio.create_task(connection.call("read", {}))
    await waiting.wait()
    call.cancel()
    try:
        await call
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancellation was converted into a reconnect timeout")
    assert connection.in_flight == 0


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


async def test_child_mcp_policy_cannot_outlive_parent_access(settings: Settings, db: Database) -> None:
    config = RuntimeConfig()
    config.mcp.servers = _config()
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        leader = await manager.create_session("leader")
        child = await manager.create_session("child", metadata={"subagent_of": leader.session.id})
        await manager.set_mcp(leader.session.id, "echo", True)
        await manager.set_mcp(child.session.id, "echo", True)
        tool = mcp_tool_name("echo", "add")
        assert tool in manager.tool_policy_for(child).pinned

        await manager.set_mcp(leader.session.id, "echo", False)
        await manager.set_mcp(child.session.id, "echo", True)  # a child refresh cannot widen past its parent
        policy = manager.tool_policy_for(child)
        assert tool in policy.blocked and tool not in policy.pinned
        denied = manager.policy_gate(child.session.id, "run-child").decide(tool, {"a": 2, "b": 3})
        assert denied.action == "deny" and denied.rule == "session.capabilities"

        await manager.set_mcp(leader.session.id, "echo", True)
        await manager.set_mode(leader.session.id, "plan")
        assert tool in manager.tool_policy_for(child).blocked
        assert manager.policy_gate(child.session.id, "run-child").decide(tool, {"a": 2, "b": 3}).action == "deny"
    finally:
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
    # a model that doubles the guillemets, quotes it, or drops them still gets the value
    for shape in (f"«{ref}»", f'"{ref}"', ref.strip("«»"), f" {ref} "):
        await edit.invoke(context, {"edit_token": shape})
        assert calls[-1][1]["edit_token"] == "tok9a8b7c6d5e4f3a2b1", shape


def test_schema_constraints_that_would_block_a_placeholder_are_dropped() -> None:
    from daedalus.mcp.manager import REF_HINT, loosen_for_refs

    props = {"id": {"type": "string"}, "edit_token": {"type": "string", "format": "uuid", "description": "the token"}, "sha": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "n": {"type": "integer", "format": "int32"}}
    out = loosen_for_refs(props)
    assert out["id"] == {"type": "string"} and out["n"] == props["n"]
    assert "format" not in out["edit_token"] and out["edit_token"]["description"] == f"the token (uuid). {REF_HINT}"
    assert "pattern" not in out["sha"] and REF_HINT in out["sha"]["description"]
