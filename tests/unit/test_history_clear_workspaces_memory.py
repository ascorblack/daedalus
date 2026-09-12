"""Starting a session over (/clear), several sessions in one workspace, and the operator's view of the agent's memory."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.memory import MemoryScope
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import TENANT, SessionManager
from daedalus.stores.database import Database


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    m = SessionManager(settings, RuntimeConfig(), db=db)
    await m.start()
    yield m
    await m.close()


def _msg(role: MessageRole, text: str) -> Message:
    return Message(role=role, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator"} if role is MessageRole.user else {})


async def test_clear_drops_the_working_history_and_keeps_everything_else(manager: SessionManager, tmp_path: Path) -> None:
    state = await manager.create_session("t", metadata={"brief": "keep me"})
    sid = state.session.id
    history = [_msg(MessageRole.user, "hello"), _msg(MessageRole.assistant, "hi"), _msg(MessageRole.user, "more"), _msg(MessageRole.assistant, "sure")]
    await manager.sessions.replace_messages(sid, TENANT, history)
    await manager.sessions.append_transcript(sid, history)
    (state.workspace / "work.txt").write_text("stays")
    result = await manager.clear_history(sid)
    assert result == {"dropped": 4}
    assert await manager.sessions.list_messages(sid, TENANT, limit=100) == []
    assert (state.workspace / "work.txt").read_text() == "stays"
    assert (await manager.get_state(sid)).metadata["brief"] == "keep me"  # type: ignore[union-attr]
    transcript = await manager.transcript(sid)
    assert transcript[-1].metadata.get("daedalus.origin") == "clear" and "4 message(s)" in transcript[-1].content_blocks[0].text  # type: ignore[union-attr]
    assert len(transcript) == 5  # the old turns stay readable
    assert list(state.workspace.glob(".history-*.jsonl"))
    assert await manager.clear_history(sid) == {"dropped": 0}


async def test_sessions_can_share_a_workspace_and_deleting_one_keeps_it(manager: SessionManager) -> None:
    first = await manager.create_session("first")
    second = await manager.create_session("second", metadata={"workspace": str(first.workspace)})
    assert second.workspace == first.workspace
    (first.workspace / "shared.txt").write_text("x")
    users = await manager.workspace_users(first.workspace)
    assert {u["id"] for u in users} == {first.session.id, second.session.id}
    # the first session owns the directory, but the second still works there: it stays
    assert await manager.delete_session(first.session.id)
    assert first.workspace.is_dir() and (first.workspace / "shared.txt").exists()
    assert [u["id"] for u in await manager.workspace_users(first.workspace)] == [second.session.id]
    # the attached session's deletion never removes a directory that is not its own
    assert await manager.delete_session(second.session.id)
    assert first.workspace.is_dir()
    # reloaded from the database, the attachment holds
    third = await manager.create_session("third", metadata={"workspace": str(first.workspace)})
    manager._states.pop(third.session.id)
    assert (await manager.get_state(third.session.id)).workspace == first.workspace  # type: ignore[union-attr]


async def test_memory_records_can_be_listed_edited_and_removed(manager: SessionManager) -> None:
    memory = manager.memory
    a = (await memory.write(TENANT, MemoryScope.global_, "", "the operator prefers short answers", kind="preference")).record
    b = (await memory.write(TENANT, MemoryScope.session, "s1", "the build takes ten minutes", kind="fact")).record
    assert {r.id for r in memory.records(TENANT)} == {a.id, b.id}
    assert [r.id for r in memory.records(TENANT, scope=MemoryScope.global_)] == [a.id]
    assert [r.id for r in memory.records(TENANT, scope=MemoryScope.session, scope_key="s1")] == [b.id]
    edited = await memory.update(TENANT, b.id, text="the build takes twelve minutes", kind="fact")
    assert edited.text == "the build takes twelve minutes" and edited.version == b.version + 1 and edited.id == b.id
    row = await manager.db.fetchone("SELECT record FROM memory_records WHERE id = ?", (b.id,))
    assert "twelve" in row["record"]
    with pytest.raises(KeyError):
        await memory.update(TENANT, "mem-nope", text="x")
    assert await memory.delete(TENANT, a.id) and not await memory.delete(TENANT, a.id)
    assert [r.id for r in memory.records(TENANT)] == [b.id]


class _App(SimpleNamespace):
    pass


@pytest.fixture
async def client(settings: Settings, db: Database, manager: SessionManager) -> Any:
    # The manager and its database live on this test's loop: the requests must run on it too, not on a
    # thread of their own (a sync TestClient would deadlock on the database lock).
    app = _App(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


H = {"X-Daedalus-Token": "tok"}


async def test_memory_api_round_trip(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/memory", json={"scope": "global", "text": "  remember this  ", "kind": "note"}, headers=H)
    assert r.status_code == 200, r.text
    rid = r.json()["record"]["id"]
    assert r.json()["record"]["text"] == "remember this"
    assert (await client.post("/api/memory", json={"scope": "session", "text": "x"}, headers=H)).status_code == 422
    assert (await client.post("/api/memory", json={"scope": "planet", "text": "x"}, headers=H)).status_code == 422
    listing = (await client.get("/api/memory", headers=H)).json()
    assert [m["id"] for m in listing["records"]] == [rid] and listing["buckets"][0] == {"scope": "global", "scope_key": "", "title": None, "count": 1}
    assert (await client.patch(f"/api/memory/{rid}", json={"text": "edited"}, headers=H)).json()["text"] == "edited"
    assert (await client.patch("/api/memory/mem-none", json={"text": "edited"}, headers=H)).status_code == 404
    assert (await client.post("/api/memory/delete", json={"ids": [rid, "mem-none"]}, headers=H)).json() == {"deleted": 1}
    assert (await client.get("/api/memory", headers=H)).json()["records"] == []


async def test_workspace_api_create_list_upload_attach_delete(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/workspaces", json={"name": "../x"}, headers=H)).status_code == 422
    r = await client.post("/api/workspaces", json={"name": "shared-lab"}, headers=H)
    assert r.status_code == 200, r.text
    assert (await client.post("/api/workspaces", json={"name": "shared-lab"}, headers=H)).status_code == 409
    r = await client.post("/api/workspaces/shared-lab/upload", files=[("files", ("a.txt", b"hello", "text/plain"))], data={"path": "docs"}, headers=H)
    assert r.json() == {"files": ["a.txt"]}
    assert (await client.get("/api/workspaces/shared-lab/files?path=docs", headers=H)).json()["entries"][0]["name"] == "a.txt"
    assert (await client.get("/api/workspaces/shared-lab/download?path=docs/a.txt", headers=H)).text == "hello"
    assert (await client.get("/api/workspaces/shared-lab/files?path=../", headers=H)).status_code == 400
    # a session attached to the workspace works there and shows up on it
    sid = (await client.post("/api/sessions", json={"title": "in the lab", "workspace": "shared-lab"}, headers=H)).json()["id"]
    detail = (await client.get(f"/api/sessions/{sid}", headers=H)).json()
    assert detail["workspace_name"] == "shared-lab" and detail["workspace_own"] is False
    assert (await client.get(f"/api/sessions/{sid}/files?path=docs", headers=H)).json()["entries"][0]["name"] == "a.txt"
    ws = {w["name"]: w for w in (await client.get("/api/workspaces", headers=H)).json()}
    assert ws["shared-lab"]["sessions"] == [{"id": sid, "title": "in the lab"}] and ws["shared-lab"]["files"] == 1
    assert (await client.delete("/api/workspaces/shared-lab", headers=H)).status_code == 409
    assert (await client.post("/api/sessions", json={"title": "nowhere", "workspace": "no-such"}, headers=H)).status_code == 404
    assert (await client.delete(f"/api/sessions/{sid}", headers=H)).json() == {"deleted": True}
    assert (await client.get("/api/workspaces/shared-lab/files", headers=H)).status_code == 200  # the attached session's deletion left it
    assert (await client.delete("/api/workspaces/shared-lab", headers=H)).json() == {"deleted": True}
    assert (await client.get("/api/workspaces/shared-lab/files", headers=H)).status_code == 404


async def test_clear_api_and_command(client: httpx.AsyncClient) -> None:
    sid = (await client.post("/api/sessions", json={"title": "c"}, headers=H)).json()["id"]
    assert (await client.post(f"/api/sessions/{sid}/clear", headers=H)).json() == {"dropped": 0}
    r = await client.post(f"/api/sessions/{sid}/command", json={"line": "/clear"}, headers=H)
    assert r.status_code == 200 and "History cleared" in r.json()["text"]
    assert any(c["name"] == "clear" and c["confirm"] for c in (await client.get("/api/commands", headers=H)).json())


async def test_sent_file_is_served_by_the_call_that_sent_it(client: httpx.AsyncClient, manager: SessionManager, tmp_path: Path) -> None:
    from protocore.contracts.types import ToolUseBlock

    sid = (await client.post("/api/sessions", json={"title": "files"}, headers=H)).json()["id"]
    state = await manager.get_state(sid)
    assert state is not None
    (state.workspace / "report.md").write_text("# report")
    outside = tmp_path / "chart.png"
    outside.write_bytes(b"\x89PNG-ish")
    calls = [
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="s1", name="SendFile", arguments_json='{"path": "report.md", "caption": "the report"}')]),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="s2", name="SendFile", arguments_json=f'{{"path": "{outside}"}}')]),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="r1", name="Read", arguments_json='{"path": "report.md"}')]),
    ]
    await manager.sessions.append_transcript(sid, calls)
    r = await client.get(f"/api/sessions/{sid}/sent/s1/download", headers=H)
    assert r.status_code == 200 and r.text == "# report" and "report.md" in r.headers["content-disposition"]
    r = await client.get(f"/api/sessions/{sid}/sent/s2/download", headers=H)
    assert r.status_code == 200 and r.content == b"\x89PNG-ish" and r.headers["content-type"].startswith("image/png")
    assert (await client.get(f"/api/sessions/{sid}/sent/r1/download", headers=H)).status_code == 404
    assert (await client.get(f"/api/sessions/{sid}/sent/nope/download", headers=H)).status_code == 404
    outside.unlink()
    assert (await client.get(f"/api/sessions/{sid}/sent/s2/download", headers=H)).status_code == 404
