"""Workspace checkpoints, revert and fork, verification receipts, learning records."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.inbox import Inbox
from daedalus.extensions.learning import Learning
from daedalus.host.checkpoints import Checkpoints
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.verify import verify


async def test_checkpoints_snapshot_and_restore(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("one")
    cp = Checkpoints(ws)
    first = await cp.snapshot("first")
    (ws / "a.txt").write_text("two")
    (ws / "b.txt").write_text("new")
    second = await cp.snapshot("second")
    assert first != second and await cp.head() == second
    await cp.restore(first)
    assert (ws / "a.txt").read_text() == "one" and not (ws / "b.txt").exists()
    assert (ws / ".checkpoints").is_dir()


async def _manager(settings: Settings, db: Database) -> SessionManager:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    return manager


async def _seed(manager: SessionManager, state: Any, turns: list[tuple[str, str]]) -> list[int]:
    """Write user/assistant pairs into the transcript and the working history; returns the user seqs."""
    history: list[Message] = []
    for ask, answer in turns:
        history.append(Message(role=MessageRole.user, content_blocks=[TextBlock(text=ask)], metadata={"daedalus.origin": "operator"}))
        history.append(Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=answer)]))
    await manager.sessions.append_transcript(state.session.id, history)
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    rows = await manager.sessions.list_transcript(state.session.id)
    return [int(m.metadata["daedalus.seq"]) for m in rows if m.role is MessageRole.user]


async def test_revert_cuts_history_and_restores_files(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    state = await manager.create_session("undo")
    (state.workspace / "notes.md").write_text("draft 1")
    seqs = await _seed(manager, state, [("first ask", "first answer")])
    await manager.checkpoint(state, kind="before", seq=None)
    # the second turn: snapshot before it, then the agent changes the workspace
    more = await _seed(manager, state, [("first ask", "first answer"), ("second ask", "second answer")])
    second_seq = more[-1]
    await db.execute("INSERT INTO checkpoints(session_id, seq, run_id, kind, sha, at) VALUES (?, ?, NULL, 'before', ?, ?)", (state.session.id, second_seq, await Checkpoints(state.workspace).snapshot("before second"), "now"))
    (state.workspace / "notes.md").write_text("draft 2 (after the second turn)")
    result = await manager.revert(state.session.id, second_seq)
    assert result["dropped"] == 2 and result["kept"] == 2 and result["workspace_restored"] is True
    assert (state.workspace / "notes.md").read_text() == "draft 1"
    working = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert [m.content_blocks[0].text for m in working] == ["first ask", "first answer"]  # type: ignore[attr-defined]
    transcript = await manager.sessions.list_transcript(state.session.id)
    assert transcript[-1].metadata.get("daedalus.origin") == "revert" and "seq" in transcript[-1].content_blocks[0].text  # type: ignore[attr-defined]
    try:
        await manager.revert(state.session.id, seqs[0] + 1)  # an assistant row
    except ValueError as exc:
        assert "operator message" in str(exc)
    await manager.close()


async def test_fork_copies_history_before_the_turn_and_the_workspace(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    source = await manager.create_session("origin")
    (source.workspace / "data.txt").write_text("v1")
    seqs = await _seed(manager, source, [("ask one", "answer one"), ("ask two", "answer two")])
    target = await manager.create_session("fork")
    result = await manager.fork_into(source.session.id, seqs[1], target)
    assert result["messages"] == 2 and result["workspace_copied"] is True
    assert (target.workspace / "data.txt").read_text() == "v1"
    working = await manager.sessions.list_messages(target.session.id, "daedalus", limit=100)
    assert [m.content_blocks[0].text for m in working] == ["ask one", "answer one"]  # type: ignore[attr-defined]
    refreshed = await manager.sessions.get(target.session.id, "daedalus")
    assert refreshed.metadata["forked_from"]["seq"] == seqs[1]
    await manager.close()


async def test_verify_records_a_receipt(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    state = await manager.create_session("verify")
    state.services.extra["manager"] = manager  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
    good = await verify().invoke(ctx, {"criterion": "echo works", "command": "echo hello"})
    bad = await verify().invoke(ctx, {"criterion": "false fails", "command": "false"})
    assert not good.is_error and good.metadata["passed"] and "✅ verified" in good.content and "hello" in good.content
    assert bad.is_error and not bad.metadata["passed"]
    rows = await db.fetchall("SELECT criterion, exit_code, passed FROM verifications WHERE session_id = ? ORDER BY id", (state.session.id,))
    assert [(r["criterion"], r["exit_code"], r["passed"]) for r in rows] == [("echo works", 0, 1), ("false fails", 1, 0)]
    await manager.close()


async def test_learning_record_and_digest(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    app.extensions["inbox"] = Inbox(app)  # type: ignore[arg-type]
    learning = Learning(app)  # type: ignore[arg-type]
    state = await manager.create_session("learn")
    await db.execute("INSERT INTO runs(id, tenant_id, session_id, status, created_at, updated_at) VALUES ('run-a', 'daedalus', ?, 'completed', '2026-09-06T10:00:00+00:00', '2026-09-06T10:00:00+00:00')", (state.session.id,))
    engine = SimpleNamespace(history=[
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="deploy the thing")], metadata={"daedalus.origin": "operator"}),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Done.\n\n⟦ deploy | status: completed; next: none | anchors: thing ⟧")]),
    ])
    state.engine = engine  # type: ignore[assignment]
    for _ in range(3):
        await learning.on_run_finished(state.session.id, "run-a", "completed")
    rows = await db.fetchall("SELECT ask, outcome, headline FROM learning_records")
    assert len(rows) == 3 and rows[0]["ask"] == "deploy the thing" and rows[0]["headline"].startswith("⟦ deploy")
    report = await learning.report(7)
    assert report["runs"] == 3 and report["outcomes"] == {"completed": 3}
    assert any("deploy the thing" in c for c in report["candidates"])
    assert await learning.maybe_digest() is True
    assert await learning.maybe_digest() is False  # once a week
    entries = await app.extensions["inbox"].list()
    assert entries and entries[0]["kind"] == "learning_digest" and "improvement candidates" in entries[0]["body"]
    await manager.close()
