"""A scheduled turn of a standing session may stay silent; input that arrived during a failed run is not lost."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.quiet import stay_silent


async def test_stay_silent_accepts_a_scheduled_turn_and_refuses_an_operator_turn(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("standing")
        state.services.extra["manager"] = manager  # type: ignore[union-attr]
        context = ToolContext(run_id="r1", session_id=state.session.id, tenant_id="daedalus")
        state.run_origin = "operator"
        result = await stay_silent().invoke(context, {"note": "checked"})
        assert result.is_error and "operator is waiting" in result.content
        state.run_origin = "schedule"
        result = await stay_silent().invoke(context, {"note": "checked the board"})
        assert not result.is_error
        assert state.services is not None and state.services.extra["silent_run"] == "r1" and state.services.extra["silent_note"] == "checked the board"
    finally:
        await manager.close()


async def test_leftover_input_is_drained_after_a_failed_run_with_its_origin(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    started: list[Message] = []

    async def fake_start(state: Any, message: Message | None, *, continue_turn: bool = False) -> str:
        assert message is not None
        started.append(message)
        return "run-2"

    manager._start_run = fake_start  # type: ignore[method-assign]
    try:
        state = await manager.create_session("s")
        sid = state.session.id
        await manager.live.enqueue(sid, "steer", {"id": "q1", "kind": "steer", "text": "[scheduled run] check", "origin": "schedule"})
        await manager.live.enqueue(sid, "follow_up", {"id": "q2", "kind": "follow_up", "text": "and answer me", "origin": "operator"})
        await manager._drain_leftover_follow_ups(state)
        assert len(started) == 1
        message = started[0]
        assert message.metadata["daedalus.origin"] == "operator" and message.metadata["daedalus.delivery"] == "drained"
        assert "check" in message.content_blocks[0].text and "answer me" in message.content_blocks[0].text  # type: ignore[union-attr]
        queued = await manager.live.load(sid)
        assert queued["steer"] == [] and queued["follow_up"] == []
        # Only scheduled input: the drained copy is not attributed to the operator.
        await manager.live.enqueue(sid, "steer", {"id": "q3", "kind": "steer", "text": "[scheduled run] again", "origin": "schedule"})
        await manager._drain_leftover_follow_ups(state)
        assert started[-1].metadata["daedalus.origin"] == "schedule"
    finally:
        await manager.close()


async def test_verify_fails_on_a_failure_hidden_behind_a_pipe(settings: Settings, db: Database) -> None:
    from daedalus.tools.verify import verify

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("verify")
        state.services.extra["manager"] = manager  # type: ignore[union-attr]
        ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
        result = await verify().invoke(ctx, {"criterion": "tests pass", "command": "false | tail -1"})
        assert result.is_error and not result.metadata["passed"]
    finally:
        await manager.close()


def test_run_origin_is_taken_from_the_opening_message() -> None:
    message = Message(role=MessageRole.user, content_blocks=[TextBlock(text="x")], metadata={"daedalus.origin": "reminder"})
    assert str(message.metadata.get("daedalus.origin") or "operator").split(":")[0] == "reminder"
    assert SimpleNamespace(run_origin="subagent:leader").run_origin.split(":")[0] == "subagent"


async def test_wrong_tool_argument_names_answer_with_the_accepted_ones() -> None:
    from daedalus.tools import discover_tools

    tools = {t.name: t for t in discover_tools()}
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id="s")
    result = await tools["BoardAdd"].invoke(ctx, {"title": "x", "status": "doing"})
    assert result.is_error and "unknown argument 'status'" in result.content and "title" in result.content
    result = await tools["Edit"].invoke(ctx, {"old_string": "a", "new_string": "b"})
    assert result.is_error and "missing" in result.content and "path" in result.content


async def test_context_overflow_compacts_and_drives_the_turn_again(settings: Settings, db: Database) -> None:
    from protocore.runtime.events.envelope import TurnEvent
    from protocore.runtime.events.types import EventType

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    calls: list[str] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator") -> str:  # type: ignore[no-untyped-def]
        calls.append(f"submit:{origin}")
        return "run-2"

    async def fake_compact(state: Any, instructions: str, *, keep_recent: int = 0, reason: str = "manual", own_task_ok: bool = False) -> str:
        calls.append(f"compact:{reason}")
        return "ok"

    async def fake_status(state: Any) -> dict[str, Any]:
        return {"tokens": 90_000, "window": 128_000, "messages": 40, "summaries": 1, "operator_turns": 2}

    manager.submit = fake_submit  # type: ignore[method-assign]
    manager._compact_locked = fake_compact  # type: ignore[method-assign]
    manager.context_status = fake_status  # type: ignore[method-assign]
    try:
        state = await manager.create_session("s")
        await manager._dispatch_event(state, TurnEvent(type=EventType.ERROR, run_id="r1", payload={"kind": "llm_context_window_exceeded", "message": "too big"}))
        assert state.last_error_kind == "llm_context_window_exceeded"
        await manager._recover_from_overflow(state)
        assert calls == ["compact:auto", "submit:core"] and state.overflow_streak == 1
        await manager._recover_from_overflow(state)
        await manager._recover_from_overflow(state)  # the third overflow in a row is left alone
        assert calls.count("submit:core") == 2 and state.overflow_streak == 2
    finally:
        await manager.close()
