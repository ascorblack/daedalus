"""``/api/events``: the frames a client sees, where a reconnect resumes, and when it is told to resync.

Driven through ``event_stream`` with a fake ``is_disconnected`` rather than an HTTP client: a stream a
test client holds open is a connection nothing ever disconnects. The route itself is checked for the
two things only the route can get wrong — that it is mounted behind authentication, and its headers.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from daedalus.extensions.api import build_app
from daedalus.host.events import EventBus, EventFilter, event_stream, streamed_types
from daedalus.host.presence import Presence
from daedalus.stores.database import Database
from tests.unit.test_components import HEAD, FakeApp


@pytest.fixture
async def bus(db: Database) -> EventBus:
    instance = EventBus(db)
    await instance.start()
    yield instance  # type: ignore[misc]
    await instance.close()


class Frame(dict[str, Any]):
    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self["data"])


def parse(text: str) -> Frame:
    frame = Frame()
    for line in text.rstrip("\n").split("\n"):
        if line.startswith(":"):
            frame["comment"] = line[1:].strip()
            continue
        key, _, value = line.partition(": ")
        frame[key] = value
    return frame


async def never() -> bool:
    return False


def open_stream(bus: EventBus, flt: EventFilter | None = None, **kwargs: Any) -> AsyncIterator[str]:
    kwargs.setdefault("is_disconnected", never)
    kwargs.setdefault("after", None)
    return event_stream(bus, flt or EventFilter(types=streamed_types()), **kwargs)


async def frames(stream: AsyncIterator[str], count: int, *, timeout: float = 5.0) -> list[Frame]:
    out: list[Frame] = []
    async with asyncio.timeout(timeout):
        while len(out) < count:
            out.append(parse(await anext(stream)))
    return out


async def test_hello_comes_first_with_the_bounds_of_the_ring(bus: EventBus) -> None:
    for _ in range(3):
        await bus.publish("terminal.bell", {})
    stream = open_stream(bus, client="tab-1")
    (hello,) = await frames(stream, 1)
    await stream.aclose()
    assert hello["event"] == "hello" and "id" not in hello
    assert hello.data["head"] == 3 and hello.data["oldest"] == 1 and hello.data["client"] == "tab-1"
    assert hello.data["server_time"].endswith("Z")


async def test_a_live_frame_carries_its_seq_as_the_id_and_the_wire_form_as_data(bus: EventBus) -> None:
    stream = open_stream(bus)
    await frames(stream, 1)
    event = await bus.publish("session.status", {"status": "running", "run_id": "r1"}, session_id="s1", project_id="p1")
    (frame,) = await frames(stream, 1)
    await stream.aclose()
    assert frame["id"] == str(event.seq) and frame["event"] == "session.status"
    assert frame.data == event.wire()
    assert frame.data["session_id"] == "s1" and frame.data["payload"] == {"status": "running", "run_id": "r1"}


async def test_an_ephemeral_frame_has_no_id_so_a_reconnect_never_resumes_from_it(bus: EventBus) -> None:
    stream = open_stream(bus)
    await frames(stream, 1)
    await bus.publish("terminal.progress", {"state": "set", "percent": 10}, terminal_id="t1")
    (frame,) = await frames(stream, 1)
    await stream.aclose()
    assert frame["event"] == "terminal.progress" and "id" not in frame and frame.data["seq"] == 0


async def test_a_cursor_replays_then_continues_live_with_nothing_skipped_at_the_seam(bus: EventBus) -> None:
    for i in range(10):
        await bus.publish("terminal.title", {"title": str(i)})
    stream = open_stream(bus, after=5)
    hello, *replayed = await frames(stream, 6)
    assert hello["event"] == "hello"
    for i in range(10, 15):
        await bus.publish("terminal.title", {"title": str(i)})
    live = await frames(stream, 5)
    await stream.aclose()
    assert [int(f["id"]) for f in replayed + live] == list(range(6, 16))


async def test_a_cursor_older_than_retention_is_told_to_resync_and_goes_live_from_the_head(bus: EventBus) -> None:
    for _ in range(10):
        await bus.publish("terminal.bell", {})
    await bus.prune(keep_days=1, max_rows=3)
    stream = open_stream(bus, after=2)
    hello, resync = await frames(stream, 2)
    await bus.publish("terminal.bell", {})
    (live,) = await frames(stream, 1)
    await stream.aclose()
    assert hello.data["oldest"] == 8
    assert resync["event"] == "resync" and resync.data == {"reason": "expired", "head": 10}
    assert live["id"] == "11"


async def test_a_cursor_ahead_of_the_head_is_told_to_resync(bus: EventBus) -> None:
    await bus.publish("terminal.bell", {})
    stream = open_stream(bus, after=50)
    _, resync = await frames(stream, 2)
    await stream.aclose()
    assert resync.data == {"reason": "cursor_ahead", "head": 1}


async def test_a_cursor_too_far_behind_is_told_to_resync_rather_than_streamed_a_day(bus: EventBus) -> None:
    bus.replay_max = 3
    for _ in range(5):
        await bus.publish("terminal.bell", {})
    stream = open_stream(bus, after=0)
    _, resync = await frames(stream, 2)
    await stream.aclose()
    assert resync.data == {"reason": "too_far", "head": 5}

    # Counted after the filter: what this client would actually be sent is what matters.
    narrow = open_stream(bus, EventFilter(types=("terminal.title",)), after=0)
    await bus.publish("terminal.title", {"title": "x"})
    _, frame = await frames(narrow, 2)
    await narrow.aclose()
    assert frame["event"] == "terminal.title"


async def test_types_narrow_the_stream_and_an_in_process_type_is_never_sent(bus: EventBus) -> None:
    stream = open_stream(bus, EventFilter(types=("presence", "terminal.")))
    await frames(stream, 1)
    await bus.publish("presence", {"client": "c", "visible": True, "focused": True, "sessions": [], "terminals": [], "projects": [], "attended": False})
    await bus.publish("session.status", {"status": "idle"}, session_id="s1")
    await bus.publish("terminal.bell", {}, terminal_id="t1")
    (frame,) = await frames(stream, 1)
    await stream.aclose()
    assert frame["event"] == "terminal.bell"
    assert "presence" not in streamed_types()


async def test_a_keepalive_goes_out_while_nothing_happens_and_a_gone_client_ends_the_stream(bus: EventBus) -> None:
    gone = False

    async def disconnected() -> bool:
        return gone

    stream = open_stream(bus, is_disconnected=disconnected, keepalive=0.05)
    hello, keepalive = await frames(stream, 2)
    assert keepalive.get("comment") == "keepalive"
    gone = True
    async with asyncio.timeout(5):
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    assert not bus._subscriptions, "the subscription must go with the client"


async def test_closing_the_bus_ends_an_open_stream_so_a_shutdown_is_not_held_by_it(bus: EventBus) -> None:
    closed: list[str] = []

    async def on_close() -> None:
        closed.append("closed")

    stream = open_stream(bus, on_close=on_close)
    await frames(stream, 1)
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    await bus.close()
    async with asyncio.timeout(5):
        with pytest.raises(StopAsyncIteration):
            await waiting
    assert closed == ["closed"]


# -- the route -----------------------------------------------------------------------------------


def test_the_route_is_mounted_behind_authentication_and_checks_its_query(tmp_path: Path) -> None:
    app = FakeApp(tmp_path, native=True)
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        assert "/api/events" in {getattr(route, "path", "") for route in client.app.routes}  # type: ignore[attr-defined]
        assert client.get("/api/events").status_code == 401
        assert client.get("/api/events?types=Terminal.Bell", headers=HEAD).status_code == 400
        assert client.get("/api/events?types=" + ",".join(["run."] * 33), headers=HEAD).status_code == 400
        assert client.get("/api/events?client=../x", headers=HEAD).status_code == 422
        assert client.get("/api/events?kind=fridge", headers=HEAD).status_code == 422
        assert client.get("/api/events?after=-1", headers=HEAD).status_code == 422


async def test_the_route_streams_uncached_and_unbuffered(tmp_path: Path, db: Database) -> None:
    """A closed bus ends the stream after its hello, which is what lets a test read the whole response.
    No cursor is sent: the client runs the app on a loop of its own, and the database belongs to this one."""
    app = FakeApp(tmp_path, native=True)
    bus = EventBus(db)
    await bus.start()
    await bus.publish("terminal.bell", {})
    await bus.close()
    app.manager.bus = bus  # type: ignore[attr-defined]
    app.manager.presence = Presence(bus, db)  # type: ignore[attr-defined]
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        response = client.get("/api/events?client=tab-1&kind=browser", headers=HEAD)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store" and response.headers["x-accel-buffering"] == "no"
    hello = parse(response.text.split("\n\n")[0])
    assert hello["event"] == "hello" and hello.data["head"] == 1 and hello.data["client"] == "tab-1"
