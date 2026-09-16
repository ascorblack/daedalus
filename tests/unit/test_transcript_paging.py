"""The read contract of a session: pages, never whole transcripts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
from protocore.contracts.types import Message, MessageRole, Session, TextBlock, ToolResultBlock, ToolUseBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteSessionStore

TENANT = "daedalus"


def _msg(i: int) -> Message:
    return Message(role=MessageRole.user if i % 2 else MessageRole.assistant, content_blocks=[TextBlock(text=f"m{i}")])


async def _seed(db: Database, sid: str, count: int) -> SqliteSessionStore:
    store = SqliteSessionStore(db)
    await store.create(Session(id=sid, tenant_id=TENANT, title="t"))
    await store.append_transcript(sid, [_msg(i) for i in range(count)])
    return store


async def test_the_tail_is_taken_in_sql_and_older_pages_walk_back(db: Database) -> None:
    store = await _seed(db, "s1", 50)
    tail = await store.list_transcript("s1", limit=10)
    assert [b.text for m in tail for b in m.content_blocks] == [f"m{i}" for i in range(40, 50)]
    first = int(tail[0].metadata["daedalus.seq"])
    older = await store.list_transcript("s1", limit=10, before_seq=first)
    assert [b.text for m in older for b in m.content_blocks] == [f"m{i}" for i in range(30, 40)]
    assert int(older[-1].metadata["daedalus.seq"]) < first
    lo, hi = await store.transcript_bounds("s1")
    assert hi == int(tail[-1].metadata["daedalus.seq"])
    assert lo == int((await store.list_transcript("s1"))[0].metadata["daedalus.seq"])
    assert await store.list_transcript("s1", limit=10, before_seq=lo) == []


async def test_a_page_reads_only_its_own_rows(db: Database, monkeypatch) -> None:
    store = await _seed(db, "s2", 200)
    seen: list[str] = []
    original = db.fetchall

    async def watching(sql: str, params=()):  # type: ignore[no-untyped-def]
        seen.append(sql)
        return await original(sql, params)

    monkeypatch.setattr(db, "fetchall", watching)
    await store.list_transcript("s2", limit=5)
    assert seen and all("LIMIT" in sql for sql in seen if "FROM transcript" in sql)


async def test_the_manager_pages_and_reports_where_the_page_sits(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    state = await manager.create_session("paged")
    sid = state.session.id
    await manager.sessions.append_transcript(sid, [_msg(i) for i in range(30)])
    newest = await manager.transcript(sid, tail=10)
    assert len(newest) == 10 and newest[-1].content_blocks[0].text == "m29"  # type: ignore[union-attr]
    first = int(newest[0].metadata["daedalus.seq"])
    older = await manager.transcript(sid, tail=10, before=first)
    assert len(older) == 10 and older[-1].content_blocks[0].text == "m19"  # type: ignore[union-attr]
    lo, _ = await manager.sessions.transcript_bounds(sid)
    assert int(older[0].metadata["daedalus.seq"]) > lo
    await manager.close()


async def test_a_tool_result_is_found_without_reading_the_session(db: Database) -> None:
    store = SqliteSessionStore(db)
    await store.create(Session(id="s3", tenant_id=TENANT, title="t"))
    call = Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c9", name="Exec", arguments_json="{}")])
    result = Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c9", content="output")])
    await store.append_transcript("s3", [_msg(i) for i in range(100)] + [call, result] + [_msg(i) for i in range(100, 200)])
    found = await store.messages_for_call("s3", "c9")
    assert {b.tool_call_id for m in found for b in m.content_blocks if hasattr(b, "tool_call_id")} == {"c9"}
    assert any(isinstance(b, ToolResultBlock) and b.content == "output" for m in found for b in m.content_blocks)
    assert await store.messages_for_call("s3", "nosuch") == []


async def test_a_call_that_is_not_the_first_block_is_still_found(db: Database) -> None:
    store = SqliteSessionStore(db)
    await store.create(Session(id="s4", tenant_id=TENANT, title="t"))
    two = Message(
        role=MessageRole.assistant,
        content_blocks=[ToolUseBlock(tool_call_id="first", name="Exec", arguments_json="{}"), ToolUseBlock(tool_call_id="second", name="Read", arguments_json="{}")],
    )
    await store.append_transcript("s4", [_msg(i) for i in range(20)] + [two])
    found = await store.messages_for_call("s4", "second")
    assert found and any(getattr(b, "tool_call_id", "") == "second" for b in found[0].content_blocks)


async def test_both_calls_of_a_parallel_batch_are_found_whole(db: Database) -> None:
    """A model that asks for two things at once writes one assistant message with both calls in it.

    The transcript key names one of them, so the second call's result row is what the key index finds
    and the message holding its arguments is keyed by the first. Returning only the result leaves the
    app with no ToolUseBlock for that call: the listing answers 404 and the file a SendFile in the
    batch handed over cannot be downloaded.
    """
    store = SqliteSessionStore(db)
    await store.create(Session(id="s5", tenant_id=TENANT, title="t"))
    batch = Message(
        role=MessageRole.assistant,
        content_blocks=[
            ToolUseBlock(tool_call_id="call-45", name="Exec", arguments_json="{}"),
            ToolUseBlock(tool_call_id="call-46", name="SendFile", arguments_json='{"path": "report.md"}'),
        ],
    )
    first = Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="call-45", content="from the first")])
    second = Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="call-46", content="from the second")])
    await store.append_transcript("s5", [_msg(i) for i in range(50)] + [batch, first, second] + [_msg(i) for i in range(50, 100)])

    for call_id, output in (("call-45", "from the first"), ("call-46", "from the second")):
        found = await store.messages_for_call("s5", call_id)
        uses = [b for m in found for b in m.content_blocks if isinstance(b, ToolUseBlock) and b.tool_call_id == call_id]
        results = [b for m in found for b in m.content_blocks if isinstance(b, ToolResultBlock) and b.tool_call_id == call_id]
        assert len(uses) == 1, f"{call_id}: the message that made the call was not returned"
        assert len(results) == 1 and results[0].content == output
    # The call that is second in the batch names the file it sent, which is what the download reads.
    sent = [b for m in await store.messages_for_call("s5", "call-46") for b in m.content_blocks if isinstance(b, ToolUseBlock) and b.tool_call_id == "call-46"]
    assert sent[0].name == "SendFile" and "report.md" in sent[0].arguments_json


async def test_a_call_id_is_matched_whole_and_not_as_a_pattern(db: Database) -> None:
    """A call id is an opaque string: an underscore in one is a character, not a LIKE wildcard."""
    store = SqliteSessionStore(db)
    await store.create(Session(id="s6", tenant_id=TENANT, title="t"))
    await store.append_transcript(
        "s6",
        [
            Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="call_1x", name="Exec", arguments_json="{}")]),
            Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="call_1x", content="right")]),
            Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="callXX", name="Exec", arguments_json="{}")]),
        ],
    )
    found = await store.messages_for_call("s6", "call_1x")
    assert {b.tool_call_id for m in found for b in m.content_blocks if hasattr(b, "tool_call_id")} == {"call_1x"}


async def test_the_api_serves_both_calls_of_a_parallel_batch(settings: Settings, db: Database, tmp_path: Path) -> None:
    """End to end: the tool result endpoint and the SendFile download, for the call that is not first."""
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    state = await manager.create_session("parallel")
    sid = state.session.id
    handed = state.workspace / "report.md"
    handed.parent.mkdir(parents=True, exist_ok=True)
    handed.write_text("the report", encoding="utf-8")
    batch = Message(
        role=MessageRole.assistant,
        content_blocks=[
            ToolUseBlock(tool_call_id="c-one", name="Exec", arguments_json="{}"),
            ToolUseBlock(tool_call_id="c-two", name="SendFile", arguments_json='{"path": "report.md"}'),
        ],
    )
    await manager.sessions.append_transcript(
        sid,
        [
            batch,
            Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c-one", content="ran")]),
            Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c-two", content="sent")]),
        ],
    )
    application = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    api = build_app(application, "tok")  # type: ignore[arg-type]
    headers = {"X-Daedalus-Token": "tok"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        for call_id, body in (("c-one", "ran"), ("c-two", "sent")):
            answer = await client.get(f"/api/sessions/{sid}/tool-results/{call_id}", headers=headers)
            assert answer.status_code == 200 and answer.json()["content"] == body
        download = await client.get(f"/api/sessions/{sid}/sent/c-two/download", headers=headers)
        assert download.status_code == 200 and download.text == "the report"
    await manager.close()
