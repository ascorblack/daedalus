"""Presence: which windows count as the operator looking at something, and for how long."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from daedalus.extensions.api import build_app
from daedalus.host.events import AppEvent, EventBus, EventFilter, event_stream
from daedalus.host.presence import LOCALE_KEY, Presence, PresenceReport
from daedalus.stores.database import Database
from tests.unit.test_components import HEAD, FakeApp


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def bus(db: Database) -> EventBus:
    instance = EventBus(db)
    await instance.start()
    yield instance  # type: ignore[misc]
    await instance.close()


def seen(client: str = "tab-1", *sessions: str, visible: bool = True, focused: bool = True, **extra: Any) -> PresenceReport:
    return PresenceReport(client=client, visible=visible, focused=focused, sessions=sessions, **extra)


async def collect(bus: EventBus) -> tuple[list[AppEvent], asyncio.Task[None]]:
    events: list[AppEvent] = []

    async def handle(event: AppEvent) -> None:
        events.append(event)

    task = bus.on(EventFilter(types=("presence",)), handle, name="test")
    await asyncio.sleep(0)
    return events, task


async def test_attending_needs_a_visible_focused_and_fresh_report(bus: EventBus, db: Database) -> None:
    clock = Clock()
    presence = Presence(bus, db, ttl_seconds=60, clock=clock)
    await presence.report(seen("tab-1", "s1", focused=False))
    assert not presence.attending(session_id="s1") and not presence.present()
    await presence.report(seen("tab-1", "s1", visible=False))
    assert not presence.attending(session_id="s1")
    await presence.report(seen("tab-1", "s1"))
    assert presence.attending(session_id="s1") and presence.present()
    assert not presence.attending(session_id="s2") and not presence.attending()
    clock.now += 61
    assert not presence.attending(session_id="s1") and not presence.present(), "a report nobody re-sent expires"


async def test_terminals_and_projects_are_attended_the_same_way(bus: EventBus, db: Database) -> None:
    presence = Presence(bus, db)
    await presence.report(PresenceReport(client="tab-1", visible=True, focused=True, terminals=("t1",), projects=("p1",)))
    assert presence.attending(terminal_id="t1") and presence.attending(project_id="p1")
    snapshot = presence.snapshot()
    assert snapshot.present and snapshot.attending(terminal_id="t1") and not snapshot.attending(session_id="t1")


async def test_a_closed_stream_drops_its_client_after_the_grace_and_a_reconnect_keeps_it(bus: EventBus, db: Database) -> None:
    presence = Presence(bus, db, ttl_seconds=60, grace_seconds=0.05)
    await presence.stream_opened("tab-1", "browser")
    await presence.report(seen("tab-1", "s1"))
    await presence.stream_closed("tab-1", "browser")
    await presence.stream_opened("tab-1", "browser")  # the reconnect, inside the grace
    await asyncio.sleep(0.12)
    assert presence.attending(session_id="s1")
    await presence.stream_closed("tab-1", "browser")
    assert presence.attending(session_id="s1"), "still counted during the grace"
    await asyncio.sleep(0.12)
    assert not presence.attending(session_id="s1") and not presence.present()


async def test_a_launcher_stream_is_a_desktop_and_never_presence(bus: EventBus, db: Database) -> None:
    presence = Presence(bus, db)
    assert not presence.desktop_connected()
    await presence.stream_opened("", "launcher")
    await presence.stream_opened("desk", "launcher")
    assert presence.desktop_connected() and not presence.present()
    await presence.stream_closed("", "launcher")
    assert presence.desktop_connected()
    await presence.stream_closed("desk", "launcher")
    assert not presence.desktop_connected() and presence.snapshot().desktop is False


async def test_the_event_lists_newly_attended_ids_once(bus: EventBus, db: Database) -> None:
    presence = Presence(bus, db)
    events, task = await collect(bus)
    await presence.report(seen("tab-1", "s1"))
    await presence.report(seen("tab-1", "s1"))  # the periodic re-send changes nothing and says nothing
    await presence.report(seen("tab-2", "s1", "s2"))  # s1 is already attended by the first tab
    await presence.report(seen("tab-1", "s1", focused=False))
    await asyncio.sleep(0.05)
    task.cancel()
    assert [e.payload["newly_attended"]["sessions"] for e in events] == [["s1"], ["s2"], []]
    assert events[0].seq == 0, "presence is ephemeral"
    assert events[0].payload["client"] == "tab-1" and events[0].payload["attended"] is True
    assert events[-1].payload["attended"] is False
    assert not await bus.replay(0), "and never stored"


async def test_the_locale_is_persisted_and_read_back(bus: EventBus, db: Database) -> None:
    presence = Presence(bus, db)
    await presence.report(seen("tab-1", lang="ru", tz="Europe/Moscow"))
    assert presence.locale() == ("ru", "Europe/Moscow")
    await presence.report(seen("tab-1", lang="en"))  # a report without a zone keeps the one it had
    assert presence.locale() == ("en", "Europe/Moscow")
    assert await db.kv_get(LOCALE_KEY) == {"lang": "en", "tz": "Europe/Moscow"}
    restarted = Presence(bus, db)
    await restarted.load()
    assert restarted.locale() == ("en", "Europe/Moscow") and restarted.snapshot().lang == "en"


async def never() -> bool:
    return False


async def test_the_event_stream_calls_its_hooks_once_each_and_only_after_opening(bus: EventBus, db: Database) -> None:
    calls: list[str] = []

    async def opened() -> None:
        calls.append("open")

    async def closed() -> None:
        calls.append("close")

    stream = event_stream(bus, EventFilter(), after=None, is_disconnected=never, on_open=opened, on_close=closed)
    await anext(stream)  # hello
    await stream.aclose()  # gone before the stream went live: nothing was opened, nothing is closed
    assert calls == []
    stream = event_stream(bus, EventFilter(), after=None, is_disconnected=never, on_open=opened, on_close=closed, keepalive=0.01)
    await anext(stream)
    await anext(stream)  # a keepalive: the hooks ran on the way to it
    await stream.aclose()
    assert calls == ["open", "close"]


# -- the route -----------------------------------------------------------------------------------


def test_the_presence_route_takes_a_report_and_refuses_an_oversized_one(tmp_path: Path) -> None:
    app = FakeApp(tmp_path, native=True)
    reports: list[PresenceReport] = []

    class Recording:
        async def report(self, report: PresenceReport) -> None:
            reports.append(report)

    app.manager.presence = Recording()  # type: ignore[attr-defined]
    body = {"client": "tab-1", "kind": "browser", "visible": True, "focused": True, "sessions": ["a1b2c3d4e5f6"], "lang": "ru", "tz": "Europe/Moscow"}
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        assert client.post("/api/presence", json=body).status_code == 401
        answer = client.post("/api/presence", json=body, headers=HEAD)
        assert answer.status_code == 204 and answer.content == b""
        assert client.post("/api/presence", json={**body, "sessions": [f"s{i}" for i in range(11)]}, headers=HEAD).status_code == 400
        assert client.post("/api/presence", json={**body, "terminals": ["x" * 65]}, headers=HEAD).status_code == 400
        assert client.post("/api/presence", json={**body, "kind": "launcher"}, headers=HEAD).status_code == 422
    assert len(reports) == 1 and reports[0].sessions == ("a1b2c3d4e5f6",) and reports[0].lang == "ru"


def test_the_event_route_hands_its_connection_to_presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hooks are called by the stream; the route has to pass the client and the kind through."""
    app = FakeApp(tmp_path, native=True)
    calls: list[tuple[str, str, str]] = []

    class Recording:
        async def stream_opened(self, client: str, kind: str) -> None:
            calls.append(("open", client, kind))

        async def stream_closed(self, client: str, kind: str) -> None:
            calls.append(("close", client, kind))

    captured: dict[str, Any] = {}

    async def fake_stream(bus: Any, flt: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        await kwargs["on_open"]()
        yield "event: hello\ndata: {}\n\n"
        await kwargs["on_close"]()

    app.manager.bus = object()  # type: ignore[attr-defined]
    app.manager.presence = Recording()  # type: ignore[attr-defined]
    monkeypatch.setattr("daedalus.extensions.api.event_stream", fake_stream)
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        assert client.get("/api/events?client=tab-9&kind=launcher", headers=HEAD).status_code == 200
    assert calls == [("open", "tab-9", "launcher"), ("close", "tab-9", "launcher")]
    assert captured["client"] == "tab-9" and captured["kind"] == "launcher"
