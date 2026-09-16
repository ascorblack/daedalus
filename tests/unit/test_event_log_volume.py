"""What the event log keeps: not the streaming fragments, not the same tool surface twice, not forever."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from protocore.contracts.types import Event

from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteEventStream


def _event(name: str, run_id: str = "r1", **payload: object) -> Event:
    return Event(id=f"{name}-{id(payload)}", run_id=run_id, name=name, payload=dict(payload))


async def test_streaming_fragments_reach_the_watcher_and_not_the_disk(db: Database) -> None:
    events = SqliteEventStream(db)
    seen: list[str] = []
    stream = events.subscribe("r1", "daedalus")

    async def watch() -> None:
        async for event in stream:
            seen.append(event.name)
            if event.name == "message_stop":
                return

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    for _ in range(50):
        await events.emit(_event("content_block_delta", delta={"type": "text_delta", "text": "x"}))
    await events.emit(_event("message_stop"))
    await asyncio.wait_for(task, timeout=5)
    assert seen.count("content_block_delta") == 50 and "message_stop" in seen
    rows = await db.fetchall("SELECT name FROM events")
    assert [r["name"] for r in rows] == ["message_stop"]


async def test_the_tool_surface_is_stored_once_and_named_by_its_digest(db: Database) -> None:
    events = SqliteEventStream(db)
    tools = [{"name": f"Tool{i}", "schema": {"x" * 200: "y" * 200}} for i in range(40)]
    for run in ("r1", "r2", "r3"):
        await events.emit(_event("tool_surface_advertised", run_id=run, tools=tools))
    rows = await db.fetchall("SELECT payload FROM events")
    assert len(rows) == 3 and all("Tool39" not in r["payload"] for r in rows)
    blobs = await db.fetchall("SELECT key, value FROM kv WHERE key LIKE 'event_blob:%'")
    assert len(blobs) == 1 and "Tool39" in blobs[0]["value"]
    stored = sum(len(r["payload"]) for r in rows) + len(blobs[0]["value"])
    assert stored < 3 * len(blobs[0]["value"])  # three runs, one copy


async def test_old_events_are_swept_and_the_blob_they_named_goes_with_them(db: Database) -> None:
    events = SqliteEventStream(db)
    await events.emit(_event("tool_surface_advertised", tools=[{"name": "One"}]))
    await events.emit(_event("message_stop"))
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    await db.execute("UPDATE events SET created_at = ?", (old,))
    fresh = _event("message_stop", run_id="r2")
    await events.emit(fresh)
    dropped = await events.prune(keep_days=7, max_rows=1000)
    assert dropped == 2
    rows = await db.fetchall("SELECT id FROM events")
    assert [r["id"] for r in rows] == [fresh.id]
    assert await db.fetchall("SELECT key FROM kv WHERE key LIKE 'event_blob:%'") == []


async def test_the_sweep_keeps_the_log_under_its_ceiling(db: Database) -> None:
    events = SqliteEventStream(db)
    for i in range(30):
        await events.emit(_event("tool_result", index=i))
    await events.prune(keep_days=365, max_rows=10)
    rows = await db.fetchall("SELECT payload FROM events ORDER BY seq")
    assert len(rows) == 10 and '"index": 29' in rows[-1]["payload"]
