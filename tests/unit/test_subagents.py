"""Subagents: shared workspace, model choice, async report to the leader."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.subagents import NO_ANSWER, Subagents
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
    yield app
    await manager.close()


def _capture(manager: SessionManager) -> list[tuple[str, str, str]]:
    submitted: list[tuple[str, str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator") -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, origin))
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    return submitted


async def test_subagent_shares_workspace_and_reports_after_the_leader_turn(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    submitted = _capture(manager)
    result = await subs.spawn(leader_id=leader.session.id, task="count the files", name="counter")
    child = await manager.get_state(result["session_id"])
    assert child is not None and child.workspace == leader.workspace
    assert child.metadata["subagent_of"] == leader.session.id and child.metadata["unattended"] is True
    assert submitted[0][0] == child.session.id and "count the files" in submitted[0][1] and submitted[0][2] == f"subagent-task:{leader.session.id}"
    # The child's workspace survives its deletion: it is the leader's directory.
    (leader.workspace / "made-by-child.txt").write_text("x")
    # The report goes to the leader once the child's run ends, whether or not the leader is running.
    await manager.sessions.append_transcript(child.session.id, [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="there are 3 files\n\n⟦ h | status: completed; next: none | anchors: files ⟧")])])
    await subs.on_run_finished(child.session.id, "run-x", "completed")
    assert submitted[-1][0] == leader.session.id and "there are 3 files" in submitted[-1][1] and submitted[-1][2] == "subagent:counter"
    assert "⟦" not in submitted[-1][1]
    assert await manager.delete_session(child.session.id)
    assert (leader.workspace / "made-by-child.txt").exists()
    # A leader that finished gets a plain restart of its turn: the same submit path handles it.
    await subs.on_run_finished(leader.session.id, "run-y", "completed")  # not a subagent: nothing happens
    assert len(submitted) == 2


async def test_subagent_failed_run_reports_no_answer(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    submitted = _capture(manager)
    result = await subs.spawn(leader_id=leader.session.id, task="explode")
    await subs.on_run_finished(result["session_id"], "run-x", "failed")
    assert submitted[-1][0] == leader.session.id and NO_ANSWER in submitted[-1][1] and "failed" in submitted[-1][1]


async def test_subagent_model_choice_depth_and_limits(app: Any) -> None:
    manager: SessionManager = app.manager
    subs = Subagents(app)
    leader = await manager.create_session("lead")
    _capture(manager)
    with pytest.raises(ValueError, match="unknown model"):
        await subs.spawn(leader_id=leader.session.id, task="t", model="nope.model")
    models = await subs.models()
    if models:
        chosen = models[-1]
        result = await subs.spawn(leader_id=leader.session.id, task="t", model=chosen)
        assert (await manager.live.load(result["session_id"])).get("preset") == chosen
    # Without a model the child inherits the leader's override.
    if models:
        await manager.live.set_model(leader.session.id, preset=models[0])
        result = await subs.spawn(leader_id=leader.session.id, task="t2")
        assert (await manager.live.load(result["session_id"])).get("preset") == models[0]
    app.config.subagents.max_depth = 1
    child = await manager.get_state(result["session_id"]) if models else await manager.create_session("c", metadata={"subagent_depth": 1})
    assert child is not None
    with pytest.raises(ValueError, match="too deep"):
        await subs.spawn(leader_id=child.session.id, task="deeper")
    with pytest.raises(ValueError, match="empty"):
        await subs.spawn(leader_id=leader.session.id, task="   ")
