"""A finished run reports itself finished at once, and what it still owes happens behind that.

The bug these cover: everything a run did after its last token — the snapshot, the run-finished
callbacks, the queue it drained — used to happen inside the run's own task, so the session read as
running for as long as it took and the app drew a blinking cursor under an answer that was over.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import Settings
from daedalus.stores.database import Database
from tests.unit.test_session_runner import ScriptedProvider, _manager

SLOW = 2.0
"""How long the stand-in housekeeping takes. A memory extraction is a model call and takes longer."""


async def _settled_at(manager: Any, session_id: str) -> tuple[list[TurnEvent], list[float]]:
    """Collects the run_settled events and the moment each arrived."""
    events: list[TurnEvent] = []
    at: list[float] = []

    async def sink(sid: str, event: TurnEvent) -> None:
        if sid == session_id and event.type is EventType.RUN_SETTLED:
            events.append(event)
            at.append(time.perf_counter())

    manager.add_sink(sink)
    return events, at


async def test_the_run_reports_itself_over_before_the_housekeeping_runs(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "the answer"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    events, at = await _settled_at(manager, state.session.id)
    stopped: list[float] = []

    async def watch(sid: str, event: TurnEvent) -> None:
        if event.type is EventType.MESSAGE_STOP:
            stopped.append(time.perf_counter())

    manager.add_sink(watch)

    async def slow_callback(session_id: str, run_id: str, status: str) -> None:
        await asyncio.sleep(SLOW)

    manager.on_finished(slow_callback)
    snapshot_took = asyncio.Event()

    async def slow_snapshot(*args: Any, **kwargs: Any) -> str | None:
        await asyncio.sleep(SLOW)
        snapshot_took.set()
        return "sha"

    manager.checkpoint = slow_snapshot  # type: ignore[method-assign]
    await manager.submit(state.session.id, "hello")
    while not events:
        await asyncio.sleep(0.005)
    # The first announcement comes with the final token, not with the work that follows it.
    assert (at[0] - stopped[-1]) < 0.1, f"the run took {at[0] - stopped[-1]:.3f}s to say it was over"
    assert not state.running, "the session still reads as running after it announced the end of the run"
    assert events[0].payload["housekeeping"] is True and events[0].payload["status"] == "completed"
    # …and the second one when there is nothing left to save.
    await asyncio.wait_for(snapshot_took.wait(), timeout=30)
    while len(events) < 2:
        await asyncio.sleep(0.01)
    assert events[1].payload["housekeeping"] is False
    await manager.close()


async def test_the_next_run_waits_for_the_history_and_the_snapshot(settings: Settings, db: Database) -> None:
    """The one ordering guarantee the detached housekeeping may not lose.

    A revert of the turn that just ended puts the files back as that turn left them, so the snapshot
    taken after it has to be on disk before anything is allowed to change the workspace again. The
    rest of the housekeeping — the callbacks, the event log's tidying — nobody waits for.
    """
    provider = ScriptedProvider([{"text": "first"}, {"text": "second"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    order: list[str] = []
    persisted = manager._persist_history

    async def watch_persist(*args: Any, **kwargs: Any) -> None:
        await persisted(*args, **kwargs)
        order.append("persist")

    async def slow_snapshot(st: Any, *, kind: str, seq: int | None = None, run_id: str | None = None) -> str | None:
        if kind == "after":
            await asyncio.sleep(SLOW)
        order.append(f"snapshot:{kind}")
        return "sha"

    async def slow_callback(session_id: str, run_id: str, status: str) -> None:
        await asyncio.sleep(SLOW * 2)
        order.append("callback")

    manager._persist_history = watch_persist  # type: ignore[method-assign]
    manager.checkpoint = slow_snapshot  # type: ignore[method-assign]
    manager.on_finished(slow_callback)
    events, _ = await _settled_at(manager, state.session.id)
    await manager.submit(state.session.id, "one")
    while not events:
        await asyncio.sleep(0.005)
    assert not state.running
    started = time.perf_counter()
    await manager.submit(state.session.id, "two")
    order.append("submitted")
    # The second run waited for the first one's snapshot and not for its callback.
    assert order.index("persist") < order.index("snapshot:after") < order.index("submitted")
    assert "callback" not in order, "the next run waited for a run-finished callback it does not depend on"
    assert time.perf_counter() - started >= SLOW * 0.5
    await manager.close()


async def test_a_queued_message_still_starts_the_next_run(settings: Settings, db: Database) -> None:
    """The queue the settling run leaves behind is drained from the detached task, exactly once."""
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 0.4"}}, {"text": "first"}, {"text": "second"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    manager.config.compaction.auto_ratio = 0.0
    await manager.submit(state.session.id, "start")
    await asyncio.sleep(0.2)
    assert state.running
    await manager.live.save_queues(state.session.id, [{"kind": "follow_up", "text": "and then this"}], [])
    for _ in range(600):
        await asyncio.sleep(0.05)
        if not state.running and state.housekeeping is not None and state.housekeeping.done():
            break
    history = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    texts = [b.text for m in history for b in m.content_blocks if getattr(b, "text", "")]
    assert any("and then this" in t for t in texts), "the queued message never started a run"
    await manager.close()
