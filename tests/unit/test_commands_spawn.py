"""Slash commands from the Mini App, and agents that create standing agents."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import commands as slash
from daedalus.extensions.inbox import Inbox
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.chat import spawn_agent


def test_parse_recognises_commands() -> None:
    assert slash.parse("/compact focus on files") == ("compact", "focus on files")
    assert slash.parse("/Model@daedalus_bot") == ("model", "")
    assert slash.parse("plain text") is None and slash.parse("/") is None and slash.parse("/not a command!") == ("not", "a command!")
    assert {c.name for c in slash.COMMANDS} >= {"compact", "cap", "brief", "usage", "doctor", "inbox"}


async def test_commands_run_without_the_chat_front(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={}, guard=None)
    app.extensions["inbox"] = Inbox(app)  # type: ignore[arg-type]
    state = await manager.create_session("s")
    sid = state.session.id
    assert "Session cap: $2.00" in await slash.run_command(app, sid, "/cap 2")  # type: ignore[arg-type]
    assert state.metadata["usd_cap"] == 2.0
    assert "removed" in await slash.run_command(app, sid, "/cap none")  # type: ignore[arg-type]
    assert "No brief" in await slash.run_command(app, sid, "/brief")  # type: ignore[arg-type]
    assert "updated" in await slash.run_command(app, sid, "/brief You keep the changelog.")  # type: ignore[arg-type]
    assert "You keep the changelog." in manager.notes_for(state) and "Brief:" in await slash.run_command(app, sid, "/brief")  # type: ignore[arg-type]
    assert "Working rules" in await slash.run_command(app, sid, "/prompt")  # type: ignore[arg-type]
    assert "nothing unread" in await slash.run_command(app, sid, "/inbox")  # type: ignore[arg-type]
    assert "No sessions with closed topics" in await slash.run_command(app, sid, "/cleanup")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        await slash.run_command(app, sid, "/nonsense")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="Telegram front"):
        await slash.run_command(app, sid, "/usage")  # type: ignore[arg-type]
    await manager.close()


async def test_spawn_agent_tool_hands_over_brief_files_and_settings(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    parent = await manager.create_session("parent")
    (parent.workspace / "howto.md").write_text("step 1")
    calls: list[dict[str, Any]] = []

    async def fake_spawn(**kw: Any) -> str:
        calls.append(kw)
        return "child123"

    parent.services.spawn_agent = fake_spawn  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r", session_id=parent.session.id)
    missing = await spawn_agent().invoke(ctx, {"title": "Changelog keeper", "brief": "Keep it.", "files": ["nope.md"]})
    assert missing.is_error and "do not exist" in missing.content
    empty = await spawn_agent().invoke(ctx, {"title": "x", "brief": "  "})
    assert empty.is_error
    result = await spawn_agent().invoke(ctx, {"title": "Changelog keeper", "brief": "Keep the changelog current.", "files": ["howto.md"], "peer_name": "changelog", "mode": "careful", "mcp": ["postingboard"]})
    assert not result.is_error and result.metadata["session_id"] == "child123" and "peer 'changelog'" in result.content
    assert calls[0]["files"] == [str(parent.workspace / "howto.md")] and calls[0]["mode"] == "careful" and calls[0]["mcp"] == ["postingboard"]
    await manager.close()


async def test_self_propose_failure_is_an_error_result(settings: Settings, db: Database) -> None:
    from daedalus.tools.selfdev import self_propose

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    state = await manager.create_session("p")

    async def refused(**kw: Any) -> str:
        raise RuntimeError("proposal failed: remote: Permission to x/y.git denied to me. 403")

    state.services.self_propose = refused  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r", session_id=state.session.id)
    result = await self_propose().invoke(ctx, {"repo": "bot", "title": "t", "summary": "s"})
    assert result.is_error and "proposal failed" in result.content and "write access" in result.content
    await manager.close()


def test_core_compaction_trigger_stays_below_the_output_reservation() -> None:
    from daedalus.host.engine_factory import runtime_constants

    config = RuntimeConfig()
    rc = runtime_constants(config, context_window=128_000, max_output_tokens=32_000, thinking=False, mode=None)
    assert rc.compaction_trigger_ratio == 0.6  # 1 − 32k/128k − 0.15: a turn of tool results below the vLLM cliff, under the 0.85 default
    rc = runtime_constants(config, context_window=128_000, max_output_tokens=4_000, thinking=False, mode=None)
    assert rc.compaction_trigger_ratio == 0.82  # 1 − 4k/128k − 0.15: still below the 0.85 default
