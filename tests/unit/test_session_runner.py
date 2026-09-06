"""SessionManager against a scripted provider: runs, follow-ups, shutdown and resume."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from protocore.contracts.llm import ILLMProvider, LLMRequest, LLMResponse, ProviderDelta, ProviderDeltaKind
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


class ScriptedProvider(ILLMProvider):
    """Answers each request with the next script entry: a tool call or a final text."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.requests: list[LLMRequest] = []

    class _Endpoint:
        id = "scripted"

    endpoint = _Endpoint()

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        self.requests.append(request)
        step = self.script.pop(0) if self.script else {"text": "nothing left"}
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0}
        if "tool" in step:
            call_id = f"call_{len(self.requests)}"
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_start, tool_call_id=call_id, tool_name=step["tool"])
            yield ProviderDelta(
                kind=ProviderDeltaKind.tool_use_input, tool_call_id=call_id, tool_input_delta=json.dumps(step["args"])
            )
            yield ProviderDelta(
                kind=ProviderDeltaKind.tool_use_stop,
                tool_call_id=call_id,
                tool_name=step["tool"],
                tool_input_final=step["args"],
                is_block_end=True,
            )
            yield ProviderDelta(kind=ProviderDeltaKind.usage, usage=usage)
            yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="tool_use")
            return
        yield ProviderDelta(kind=ProviderDeltaKind.text, content=step["text"])
        yield ProviderDelta(kind=ProviderDeltaKind.usage, usage=usage)
        yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")

    async def complete_structured(self, request: LLMRequest, response_schema: dict[str, Any]) -> LLMResponse:
        return LLMResponse(
            message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text='{"summary": "summary"}')]),
            stop_reason=StopReason.end_turn,
        )

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="summary")]),
            stop_reason=StopReason.end_turn,
        )

    def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text) // 4

    async def aclose(self) -> None:
        return None


async def _manager(settings: Settings, db: Database, provider: ScriptedProvider) -> SessionManager:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config: [(provider, "scripted-model")]  # type: ignore[method-assign]
    return manager


async def _wait_finished(manager: SessionManager) -> list[tuple[str, str, str]]:
    finished: list[tuple[str, str, str]] = []
    done = asyncio.Event()

    async def on_finished(session_id: str, run_id: str, status: str) -> None:
        finished.append((session_id, run_id, status))
        done.set()

    manager.on_finished(on_finished)
    await asyncio.wait_for(done.wait(), timeout=30)
    return finished


async def test_run_executes_tool_and_persists_history(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo hello-from-tool"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    events: list[TurnEvent] = []

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)

    manager.add_sink(sink)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "run it")
    finished = await waiter
    assert finished[0][2] == "completed"
    results = [e for e in events if e.type is EventType.TOOL_RESULT]
    assert results and "hello-from-tool" in str(results[0].payload)
    history = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert [m.role.value for m in history] == ["user", "assistant", "tool", "assistant"]
    assert provider.requests[-1].messages[-1].role is MessageRole.tool
    usage = await db.fetchone("SELECT count(*) c FROM usage_events")
    assert usage["c"] == 0  # the scripted provider bypasses the usage sink; the loop still saw usage
    await manager.close()


async def test_follow_up_is_queued_while_running_and_placed_next_step(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 1"}}, {"text": "first done"}, {"text": "second"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "start")
    await asyncio.sleep(0.3)
    assert state.running
    await manager.submit(state.session.id, "and also this")
    queued = await manager.live.load(state.session.id)
    assert queued["steer"] and queued["steer"][0]["text"] == "and also this"
    await waiter
    texts = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
    assert any("and also this" in t for t in texts)
    await manager.close()


async def test_shutdown_keeps_snapshot_and_resume_continues(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 5"}}, {"text": "after restart"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    await manager.submit(state.session.id, "long job")
    await asyncio.sleep(0.5)
    assert state.running
    await manager.close()  # simulates the process going down mid-tool
    assert await db.fetchone("SELECT count(*) c FROM snapshots") is not None
    unfinished = await manager.events.unfinished_snapshots()
    assert unfinished and unfinished[0]["session_id"] == state.session.id

    provider2 = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo resumed"}}, {"text": "after restart"}])
    manager2 = await _manager(settings, db, provider2)
    waiter = asyncio.create_task(_wait_finished(manager2))
    resumed = await manager2.resume_unfinished()
    assert resumed
    finished = await waiter
    assert finished[0][2] == "completed"
    history = await manager2.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert history[0].role is MessageRole.user and history[-1].role is MessageRole.assistant
    assert history[-1].content_blocks[0].text == "after restart"  # type: ignore[union-attr]
    await manager2.close()


@pytest.mark.parametrize("answer", [[{"selected": ["Blue"]}]])
async def test_ask_user_pause_and_answer(settings: Settings, db: Database, answer: list[dict[str, Any]]) -> None:
    provider = ScriptedProvider(
        [
            {"tool": "AskUser", "args": {"questions": [{"question": "Color?", "options": [{"label": "Red"}, {"label": "Blue"}]}]}},
            {"text": "you chose Blue"},
        ]
    )
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "ask me")
    finished = await waiter
    assert finished[0][2] == "awaiting" and state.pending is not None
    waiter2 = asyncio.create_task(_wait_finished(manager))
    await manager.answer(state.session.id, answer)
    finished2 = await waiter2
    assert finished2[-1][2] == "completed"
    tool_msg = [m for m in provider.requests[-1].messages if m.role is MessageRole.tool][-1]
    assert "Blue" in tool_msg.content_blocks[0].content  # type: ignore[union-attr]
    await manager.close()


async def test_waiting_session_survives_restart(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider(
        [{"tool": "AskUser", "args": {"questions": [{"question": "Go?", "options": [{"label": "Yes"}]}]}}, {"text": "went"}]
    )
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "ask")
    assert (await waiter)[0][2] == "awaiting"
    await manager.close()

    manager2 = await _manager(settings, db, ScriptedProvider([{"text": "went"}]))
    restored: list[str] = []

    async def on_restored(session_id: str, pending) -> None:  # type: ignore[no-untyped-def]
        restored.append(session_id)

    manager2.on_pending_restored(on_restored)
    assert await manager2.resume_unfinished() == []
    assert restored == [state.session.id]
    state2 = await manager2.get_state(state.session.id)
    assert state2 is not None and state2.pending is not None
    waiter2 = asyncio.create_task(_wait_finished(manager2))
    await manager2.answer(state.session.id, [{"selected": ["Yes"]}])
    assert (await waiter2)[-1][2] == "completed"
    await manager2.close()


async def test_delete_session_removes_records_and_workspace(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("gone")
    (state.workspace / "inbox" / "f.txt").write_text("x")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello")
    await waiter
    assert await manager.delete_session(state.session.id)
    assert not state.workspace.exists()
    for table in ("sessions", "session_messages", "runs", "live_control"):
        row = await db.fetchone(f"SELECT count(*) c FROM {table}")
        assert row["c"] == 0, table
    assert await manager.get_state(state.session.id) is None
    await manager.close()


async def test_rename_updates_session_and_topic(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([{"text": "hi"}]))
    state = await manager.create_session("old")
    await manager.db.execute(
        "INSERT INTO topics(chat_id, thread_id, session_id, title, created_at) VALUES (?, ?, ?, ?, ?)",
        (-1, 5, state.session.id, "old", "2026-01-01T00:00:00+00:00"),
    )
    renamed = await manager.rename_session(state.session.id, "  new title  ")
    assert renamed.session.title == "new title"
    row = await manager.db.fetchone("SELECT title FROM topics WHERE session_id = ?", (state.session.id,))
    assert row["title"] == "new title"
    assert (await manager.sessions.get(state.session.id, "daedalus")).title == "new title"
    await manager.close()


def test_transcript_for_summary_clips_tool_results() -> None:
    from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock, ToolUseBlock

    from daedalus.host.session_runner import transcript_for_summary

    history = [
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="do it")]),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Exec", arguments_json='{"command": "ls"}')]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="x" * 5000)]),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="done")]),
    ]
    text = transcript_for_summary(history, result_chars=100)
    assert "[user] do it" in text and "[tool call] Exec" in text and "[assistant] done" in text
    assert " … " in text and len(text) < 1000


async def test_compact_replaces_history_with_summary_and_keeps_a_backup(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("c")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello there")
    await waiter

    async def fake_complete(request):  # type: ignore[no-untyped-def]
        from protocore.contracts.llm import LLMResponse
        from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

        assert "hello there" in request.messages[0].content_blocks[0].text
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="**Summary** of the chat")]), stop_reason=StopReason.end_turn)

    provider.complete_text = fake_complete  # type: ignore[attr-defined]
    summary = await manager.compact(state.session.id)
    assert summary.startswith("**Summary**")
    messages = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert len(messages) == 1 and messages[0].metadata.get("protocore.compaction_summary") is True
    assert "<compacted-turn" in messages[0].content_blocks[0].text
    backups = list(state.workspace.glob(".history-*.jsonl"))
    assert len(backups) == 1 and "hello there" in backups[0].read_text()
    assert state.engine is not None and len(state.engine.history) == 1 and state.engine.last_observed_prompt_tokens == 0
    await manager.close()


async def test_compact_refuses_while_running(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("busy")
    state.task = asyncio.create_task(asyncio.sleep(5))
    with pytest.raises(RuntimeError):
        await manager.compact(state.session.id)
    state.task.cancel()
    await manager.close()


async def test_steer_sent_while_running_lands_in_the_transcript(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("busy")
    state.task = asyncio.create_task(asyncio.sleep(5))
    await manager.submit(state.session.id, "change of plan")
    texts = [m.content_blocks[0].text for m in await manager.sessions.list_transcript(state.session.id)]  # type: ignore[union-attr]
    assert texts == ["change of plan"]
    state.task.cancel()
    await manager.close()
