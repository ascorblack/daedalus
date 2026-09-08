"""Loop agents: the wake-up cadence, the dynamic schedule-or-end contract, pause/resume, busy deferral."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.inbox import Inbox
from daedalus.extensions.loops import Loops, fmt_interval
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
    app.extensions["inbox"] = Inbox(app)  # type: ignore[arg-type]
    submitted: list[tuple[str, str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator") -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, origin))
        return "run-l"

    manager.submit = fake_submit  # type: ignore[method-assign]
    app.submitted = submitted
    yield app
    await manager.close()


def test_interval_formatting() -> None:
    assert fmt_interval(600) == "10m" and fmt_interval(7200) == "2h" and fmt_interval(90) == "90s" and fmt_interval(None) == "dynamic"


async def test_interval_loop_fires_at_once_then_on_its_cadence(app: Any) -> None:
    loops = Loops(app)
    state = await app.manager.create_session("watcher")
    sid = state.session.id
    loop = await loops.create(sid, instruction="check the board", interval_seconds=600)
    assert loop["status"] == "active" and loop["run_count"] == 1  # the first iteration ran at once
    assert app.submitted[-1][0] == sid and app.submitted[-1][2] == "loop"
    assert "<loop_instruction>\ncheck the board\n</loop_instruction>" in app.submitted[-1][1]
    assert "every 10m" in app.submitted[-1][1] and "LoopNext is not needed" in app.submitted[-1][1]
    assert "Your loop (every 10m" in app.manager.notes_for(state) and state.metadata["loop"]["mode"] == "interval"
    nxt = datetime.fromisoformat(loop["next_run_at"])
    assert timedelta(minutes=9) < nxt - datetime.now(UTC) <= timedelta(minutes=10)
    await loops.tick()
    assert len(app.submitted) == 1  # not due yet
    await app.db.execute("UPDATE loops SET next_run_at = ? WHERE session_id = ?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), sid))
    await loops.tick()
    assert len(app.submitted) == 2 and "Loop iteration 2" in app.submitted[-1][1]


async def test_dynamic_loop_ends_without_loop_next_and_continues_with_it(app: Any) -> None:
    loops = Loops(app)
    state = await app.manager.create_session("pacer")
    sid = state.session.id
    await loops.create(sid, instruction="watch the deploy", mode="dynamic")
    assert "LoopNext(delay_seconds, reason)" in app.submitted[-1][1]
    state.run_origin = "loop"
    # The iteration scheduled its next wake-up: the loop lives on.
    result = await loops.schedule_next(sid, 5, "deploy still running")
    assert result["delay_seconds"] == app.config.loops.min_interval_seconds and result["clamped"]
    await loops.on_run_finished(sid, "r1", "completed")
    assert (await loops.get(sid))["status"] == "active"
    # The next iteration ends without LoopNext: the loop is done and the operator is told.
    await app.db.execute("UPDATE loops SET next_run_at = NULL WHERE session_id = ?", (sid,))
    await loops.on_run_finished(sid, "r2", "completed")
    loop = await loops.get(sid)
    assert loop["status"] == "done" and "without LoopNext" in loop["stop_reason"]
    entries = await app.db.fetchall("SELECT kind, body FROM inbox WHERE session_id = ?", (sid,))
    assert any(e["kind"] == "loop" for e in entries)
    # Resume runs an iteration at once.
    await loops.resume(sid)
    assert (await loops.get(sid))["status"] == "active" and app.submitted[-1][2] == "loop"


async def test_busy_session_defers_the_wake_up_until_idle(app: Any) -> None:
    loops = Loops(app)
    state = await app.manager.create_session("busy")
    sid = state.session.id
    import asyncio

    state.task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]  # a run in flight
    try:
        loop = await loops.create(sid, instruction="ping", interval_seconds=600)
        assert loop["run_count"] == 0 and app.submitted == []  # not fired into a busy session
        await loops.tick()
        assert app.submitted == []
    finally:
        state.task.cancel()  # type: ignore[union-attr]
        state.task = None
    state.run_origin = "operator"
    await loops.on_run_finished(sid, "r", "completed")  # the session went idle: the due wake-up fires now
    assert len(app.submitted) == 1 and app.submitted[0][2] == "loop"


async def test_pause_stop_max_runs_and_validation(app: Any) -> None:
    loops = Loops(app)
    state = await app.manager.create_session("bounded")
    sid = state.session.id
    with pytest.raises(ValueError, match="at least"):
        await loops.create(sid, instruction="x", interval_seconds=5)
    with pytest.raises(ValueError, match="instruction"):
        await loops.create(sid, instruction="  ", interval_seconds=600)
    await loops.create(sid, instruction="count", interval_seconds=600, max_runs=1)
    state.run_origin = "loop"
    await loops.on_run_finished(sid, "r1", "completed")
    assert (await loops.get(sid))["status"] == "done"
    await loops.resume(sid)
    await loops.pause(sid, "need the API key")
    loop = await loops.get(sid)
    assert loop["status"] == "paused" and loop["pause_note"] == "need the API key" and "status paused (need the API key)" in state.metadata["loop_note"]
    with pytest.raises(ValueError, match="paused"):
        await loops.schedule_next(sid, 60, "x")
    await loops.stop(sid, "enough")
    assert (await loops.get(sid))["stop_reason"] == "enough"
    assert await loops.remove(sid) and "loop" not in state.metadata and await loops.get(sid) is None


async def test_persisted_loop_for_deleted_session_is_gated_not_fired(app: Any) -> None:
    """A loop whose session was deleted must be stopped by the existence gate, not fired into a dead session.

    Regression for the "cross-session execution without an ownership gate" defect class:
    ``delete_session`` removes the session row but not the ``loops`` row, so a persisted
    loop can outlive its session. The gate in ``_fire`` (``get_state`` -> None -> ``stop``)
    must stop it and submit nothing.
    """
    loops = Loops(app)
    state = await app.manager.create_session("doomed")
    sid = state.session.id
    loop = await loops.create(sid, instruction="watch", interval_seconds=600)
    assert loop["status"] == "active" and len(app.submitted) == 1  # the first iteration fired once
    assert app.submitted[-1][0] == sid and app.submitted[-1][2] == "loop"
    # The session is deleted; the loop row outlives it (delete_session does not touch `loops`).
    assert await app.manager.delete_session(sid, delete_workspace=False)
    assert await app.manager.get_state(sid) is None  # the gate's precondition
    # Make the loop due, then tick: the gate must stop it and fire nothing.
    await app.db.execute("UPDATE loops SET next_run_at = ? WHERE session_id = ?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), sid))
    await loops.tick()
    assert len(app.submitted) == 1  # no new prompt went anywhere
    loop = await loops.get(sid)
    assert loop is not None and loop["status"] == "stopped" and "no longer exists" in loop["stop_reason"]


async def test_loop_cannot_be_created_for_a_nonexistent_session(app: Any) -> None:
    """A loop must not be created for a session that does not exist in the store.

    Pins the class-1 invariant from the shared defect catalogue ("foreign prompt
    succeeds"): the gate is at CREATION time, not only at fire time. ``loops.create``
    requires ``get_state(session_id) is not None``, else ``KeyError`` — so a loop can
    never be attached to a dead or foreign session id. In the single-agent architecture a
    loop is structurally bound to its own ``session_id`` (no retargeting surface), and
    creation is gated on the session existing locally; this test locks that in.
    Complements ``test_persisted_loop_for_deleted_session_is_gated_not_fired`` (class 2).
    """
    loops = Loops(app)
    with pytest.raises(KeyError) as excinfo:
        await loops.create("no-such-session", instruction="watch", interval_seconds=600)
    # The gate fires before any write and names the offending session id.
    assert excinfo.value.args == ("no-such-session",)
    assert await app.db.fetchone("SELECT * FROM loops WHERE session_id = ?", ("no-such-session",)) is None
    assert app.submitted == []
