"""Retry and revert delete the durable tail instead of hiding or duplicating it."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from protocore.contracts.types import COMPACTION_SUMMARY_METADATA_KEY, Message, MessageRole, TextBlock

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config
from tests.unit.test_checkpoints_verify_learning import _seed
from tests.unit.test_session_runner import ScriptedProvider


@pytest.mark.parametrize("compacted", [False, True])
@pytest.mark.parametrize("historical", [False, True])
async def test_retry_removes_tail_without_branch_or_duplicate_input(settings: Settings, db: Database, compacted: bool, historical: bool) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    provider = ScriptedProvider([{"text": "replacement answer"}])
    manager.providers.rungs_for = lambda *args: [(provider, "scripted-model")]
    state = await manager.create_session("Conversation")
    if compacted:
        await manager.sessions.append_transcript(state.session.id, [
            Message(role=MessageRole.user, content_blocks=[TextBlock(text="cleared request")]),
            Message(role=MessageRole.user, content_blocks=[TextBlock(text="history cleared")], metadata={"daedalus.origin": "clear"}),
        ])
    turns = [("original request", "discarded answer")]
    if historical:
        turns.append(("discarded request", "discarded later answer"))
    seqs = await _seed(manager, state, turns)
    seqs = seqs[-len(turns):]
    if compacted:
        summary = Message(role=MessageRole.user, content_blocks=[TextBlock(text="discarded summary")], metadata={COMPACTION_SUMMARY_METADATA_KEY: True})
        await manager.sessions.replace_messages(state.session.id, "daedalus", [summary])
        await manager.sessions.append_transcript(state.session.id, [summary])
    events = []

    async def sink(session_id, event):  # type: ignore[no-untyped-def]
        events.append(event)

    manager.add_sink(sink)
    result = await manager.retry(state.session.id, seqs[0] + 1)
    assert result["seq"] == seqs[0] + 1 and result["through"] >= result["seq"]
    assert state.task is not None
    await state.task
    await state.settled.wait()
    for messages in (await manager.sessions.list_messages(state.session.id, "daedalus", limit=100), await manager.sessions.list_transcript(state.session.id)):
        assert [m.text for m in messages if m.text not in ("cleared request", "history cleared")] == ["original request", "replacement answer"]
    assert "discarded" not in " ".join(m.text for m in provider.requests[0].messages)
    assert "cleared" not in " ".join(m.text for m in provider.requests[0].messages)
    assert len(await manager.list_sessions()) == 1
    assert not list(state.workspace.glob(".history-*"))
    assert any(str(event.type) == "history_cut" for event in events)
    assert not await db.fetchall("SELECT text FROM transcript_fts WHERE transcript_fts MATCH 'discarded'")
    assert not await db.fetchall("PRAGMA foreign_key_check")
    await manager.close()


async def test_revert_deletes_selected_message_and_tail_without_marker(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    state = await manager.create_session("Conversation")
    other = await manager.create_session("Other")
    await _seed(manager, other, [("unrelated", "untouched")])
    seqs = await _seed(manager, state, [("first request", "first answer"), ("discarded request", "discarded answer")])
    result = await manager.revert(state.session.id, seqs[1])
    assert result["dropped"] == 2
    assert [m.text for m in await manager.sessions.list_transcript(state.session.id)] == ["first request", "first answer"]
    assert [m.text for m in await manager.sessions.list_transcript(other.session.id)] == ["unrelated", "untouched"]
    assert not list(state.workspace.glob(".history-*"))
    # An invalid or repeated request cannot delete a different row after the first cut.
    with pytest.raises(ValueError):
        await manager.revert(state.session.id, seqs[1])
    with pytest.raises(ValueError):
        await manager.retry(state.session.id, seqs[0])
    state.task = asyncio.create_task(asyncio.Event().wait())
    with pytest.raises(RuntimeError, match="busy"):
        await manager.revert(state.session.id, seqs[0])
    state.task.cancel()
    await asyncio.gather(state.task, return_exceptions=True)
    await manager.close()


async def test_retry_api_and_reasoning_off(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    provider = ScriptedProvider([{"text": "replacement"}])
    manager.providers.rungs_for = lambda *args: [(provider, "scripted-model")]
    state = await manager.create_session("Conversation")
    seqs = await _seed(manager, state, [("request", "discarded")])
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://127.0.0.1") as client:
        base = f"/api/sessions/{state.session.id}"
        denied = await client.post(f"{base}/retry", json={"seq": seqs[0] + 1})
        assert denied.status_code == 401
        client.headers["X-Daedalus-Token"] = "tok"
        off = await client.post(f"{base}/model", json={"thinking": False})
        assert off.status_code == 200 and off.json()["thinking"] is False
        bad = await client.post(f"{base}/retry", json={"seq": seqs[0]})
        assert bad.status_code == 400
        response = await client.post(f"{base}/retry", json={"seq": seqs[0] + 1})
        assert response.status_code == 200 and response.json()["run_id"]
        assert state.task is not None
        await state.task
        await state.settled.wait()
        detail = await client.get(base)
        assert detail.status_code == 200 and detail.json()["thinking"] is False
        assert [m["text"] for m in detail.json()["messages"]] == ["request", "replacement"]
        assert provider.requests[0].extra["enable_thinking"] is False
        cut = await client.post(f"{base}/revert", json={"seq": seqs[0]})
        assert cut.status_code == 200
        assert (await client.get(base)).json()["messages"] == []
    await manager.close()
