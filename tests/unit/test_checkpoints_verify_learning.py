"""Workspace checkpoints, revert and fork, verification receipts, learning records."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import COMPACTION_SUMMARY_METADATA_KEY, Message, MessageRole, TextBlock

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
    with pytest.raises(ValueError, match="started a run"):
        await manager.revert(state.session.id, seqs[0] + 1)  # an assistant row
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


async def test_checkpoints_leave_nested_repositories_alone_and_stay_undoable(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("one")
    subprocess.run(["git", "init", "-q", str(ws / "clone")], check=True)
    (ws / "clone" / "n.txt").write_text("n1")
    cp = Checkpoints(ws)
    first = await cp.snapshot("first")
    (ws / "a.txt").write_text("two")
    (ws / "clone" / "n.txt").write_text("n2")
    second = await cp.snapshot("second")
    assert await cp.restore(first) == ["clone"]
    assert (ws / "a.txt").read_text() == "one" and (ws / "clone" / "n.txt").read_text() == "n2"
    # the branch moved forward: the later snapshot is still reachable, so the restore can itself be undone
    assert await cp.restore(second) == ["clone"] and (ws / "a.txt").read_text() == "two"
    assert subprocess.run(["git", "--git-dir", str(cp.git_dir), "config", "core.worktree"], capture_output=True, text=True).stdout == ""


async def test_fork_skips_reverted_turns_and_repoints_summaries(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    source = await manager.create_session("origin")
    seqs = await _seed(manager, source, [("ask one", "answer one"), ("ask two", "answer two")])
    await db.execute("INSERT INTO checkpoints(session_id, seq, run_id, kind, sha, at) VALUES (?, ?, NULL, 'before', ?, 'now')", (source.session.id, seqs[1], await Checkpoints(source.workspace).snapshot("s")))
    await manager.revert(source.session.id, seqs[1])
    await _seed(manager, source, [("ask three", "answer three")])
    # a compaction summary standing for the first turn, as the persist path would record it
    summary = Message(
        role=MessageRole.user,
        content_blocks=[TextBlock(text=f"<compacted-turn id=1>gist\n\n[archived turns seq {seqs[0]}–{seqs[0] + 1}: HistoryExpand({seqs[0]}, {seqs[0] + 1}) returns them verbatim]</compacted-turn>")],
        metadata={COMPACTION_SUMMARY_METADATA_KEY: True, "daedalus.archived": {"from_seq": seqs[0], "to_seq": seqs[0] + 1, "seqs": [seqs[0], seqs[0] + 1]}},
    )
    await manager.sessions.append_transcript(source.session.id, [summary])
    rows = await manager.sessions.list_transcript(source.session.id)
    fork_seq = int(rows[-1].metadata["daedalus.seq"]) + 1
    target = await manager.create_session("fork")
    result = await manager.fork_into(source.session.id, fork_seq, target)
    working = await manager.sessions.list_messages(target.session.id, "daedalus", limit=100)
    texts = [m.content_blocks[0].text for m in working]  # type: ignore[attr-defined]
    assert "ask two" not in texts and "ask three" in texts and result["messages"] == 3
    assert texts[-1].startswith("<compacted-turn") and "ask one" not in texts
    forked = await manager.sessions.list_transcript(target.session.id)
    archived = [m for m in forked if m.metadata.get("daedalus.archived")][0]
    own = {int(m.metadata["daedalus.seq"]) for m in forked}
    meta = archived.metadata["daedalus.archived"]
    assert set(meta["seqs"]) <= own and f"HistoryExpand({meta['from_seq']}, {meta['to_seq']})" in archived.content_blocks[0].text  # type: ignore[attr-defined]
    await manager.close()


async def test_verify_digest_hashes_raw_bytes(settings: Settings, db: Database) -> None:
    """The receipt digest must cover the bytes the process printed, not a decoded copy.

    Output containing invalid UTF-8 used to be decoded with "replace" first and
    re-encoded for the hash, so the digest described a lossy copy (U+FFFD
    sequences) instead of the real output.
    """
    import hashlib

    manager = await _manager(settings, db)
    state = await manager.create_session("verify-raw")
    state.services.extra["manager"] = manager  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
    raw = b"\xff\xfe raw"
    result = await verify().invoke(ctx, {"criterion": "raw bytes", "command": "printf '\\377\\376 raw'"})
    assert not result.is_error
    row = await db.fetchone("SELECT output_digest FROM verifications WHERE session_id = ?", (state.session.id,))
    assert row["output_digest"] == hashlib.sha256(raw).hexdigest()
    await manager.close()


async def test_verify_receipts_are_redacted(settings: Settings, db: Database) -> None:
    from daedalus.security import redact

    manager = await _manager(settings, db)
    state = await manager.create_session("verify")
    state.services.extra["manager"] = manager  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
    token = "ghp_" + "A1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9"
    await verify().invoke(ctx, {"criterion": "auth", "command": f"echo {token}"})
    row = await db.fetchone("SELECT command, output_head, sandboxed FROM verifications WHERE session_id = ?", (state.session.id,))
    assert token not in row["command"] and token not in row["output_head"] and redact.MASK in row["command"] and row["sandboxed"] == 0
    await manager.close()


async def test_learning_record_covers_only_the_run(settings: Settings, db: Database) -> None:
    from protocore.contracts.types import ToolResultBlock, ToolUseBlock

    manager = await _manager(settings, db)
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    learning = Learning(app)  # type: ignore[arg-type]
    state = await manager.create_session("learn")
    await db.execute("INSERT INTO runs(id, tenant_id, session_id, status, created_at, updated_at) VALUES ('run-b', 'daedalus', ?, 'completed', '2026-09-06T10:00:00+00:00', '2026-09-06T10:00:00+00:00')", (state.session.id,))
    earlier = [
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="old ask")], metadata={"daedalus.origin": "operator"}),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Exec", arguments_json="{}")]),
        Message(role=MessageRole.user, content_blocks=[ToolResultBlock(tool_call_id="c1", content="boom", is_error=True)]),
    ]
    this_run = [Message(role=MessageRole.user, content_blocks=[TextBlock(text="new ask")], metadata={"daedalus.origin": "schedule"}), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="ok")])]
    state.engine = SimpleNamespace(history=earlier + this_run)  # type: ignore[assignment]
    state.run_history_start = len(earlier)
    await learning.on_run_finished(state.session.id, "run-b", "completed")
    row = await db.fetchone("SELECT ask, tools, failures FROM learning_records WHERE run_id = 'run-b'")
    assert row["ask"] == "new ask" and row["tools"] == "{}" and row["failures"] == "[]"
    await manager.close()


async def test_verify_receipt_records_time_and_dependencies(settings: Settings, db: Database) -> None:
    """The receipt names when it was taken and the shared channels it rests on.

    A digest without a timestamp is 'verified for me, right now'; a receipt
    without its named dependencies hides the trust root the observation
    stands on. Both must be in the stored row and the receipt text.
    """
    manager = await _manager(settings, db)
    state = await manager.create_session("verify-deps")
    state.services.extra["manager"] = manager  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
    result = await verify().invoke(ctx, {"criterion": "echo works", "command": "echo hello", "dependencies": "container shell + provider API"})
    assert not result.is_error
    assert "· at " in result.content and "· deps: container shell + provider API" in result.content
    row = await db.fetchone("SELECT at, dependencies FROM verifications WHERE session_id = ?", (state.session.id,))
    assert row["dependencies"] == "container shell + provider API"
    assert row["at"]
    # without dependencies the field stays empty, the header stays clean
    await verify().invoke(ctx, {"criterion": "plain", "command": "true"})
    row2 = await db.fetchone("SELECT dependencies FROM verifications WHERE session_id = ? AND criterion = 'plain'", (state.session.id,))
    assert row2["dependencies"] == ""
    await manager.close()
