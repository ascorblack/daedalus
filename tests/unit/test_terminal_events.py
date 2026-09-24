"""A terminal daemon's events retold on the event bus: which ones, in what shape, with whose ids.

The daemon here is the in-process fake and the bus is the real one on the test database, so a
payload that does not follow the registry is refused exactly as it would be in production.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import TerminalsConfig
from daedalus.host.events import AppEvent, EventBus, EventFilter, Subscription
from daedalus.stores.database import Database
from daedalus.terminals.bus import TEXT_CHARS, BusBridge, translate
from daedalus.terminals.model import Owner, TerminalEvent, TerminalSpec
from daedalus.terminals.service import Terminals
from tests.support.fake_ptyd import FakePtyd
from tests.unit.test_terminals_service import FakeOwners


@pytest.fixture
def run_dir() -> Iterable[Path]:
    # Unix socket paths are short; pytest's temporary directories can be too long for one.
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))
    yield path / "run"
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(run_dir: Path) -> AsyncIterator[FakePtyd]:
    fake = await FakePtyd(run_dir).start()
    yield fake
    await fake.stop()


@pytest.fixture
async def bus(db: Database) -> AsyncIterator[EventBus]:
    instance = EventBus(db)
    await instance.start()
    yield instance
    await instance.close()


@pytest.fixture
def owners() -> FakeOwners:
    return FakeOwners()


@pytest.fixture
async def service(db: Database, run_dir: Path, owners: FakeOwners, bus: EventBus, daemon: FakePtyd) -> AsyncIterator[Terminals]:
    made = Terminals(db, run_dirs={"container": run_dir, "host": None}, config=lambda: TerminalsConfig(), owners=owners, bus=bus)  # type: ignore[arg-type]
    made.subscribe(BusBridge(made))
    await made.start()
    assert await made.wait_available("container")
    yield made
    await made.close()


async def _take(sub: Subscription, count: int, *, timeout: float = 10.0) -> list[AppEvent]:
    out: list[AppEvent] = []
    async with asyncio.timeout(timeout):
        while len(out) < count:
            out.append(await anext(sub))
    return out


async def _nothing_more(sub: Subscription, *, within: float = 0.3) -> None:
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(within):
            extra = await anext(sub)
            raise AssertionError(f"unexpected {extra.type} {dict(extra.payload)}")


async def test_every_kind_reaches_the_bus_with_the_owner_ids(service: Terminals, daemon: FakePtyd, bus: EventBus, owners: FakeOwners) -> None:
    session = owners.add(Owner("session", "s1"), project="p1", cwd="/tmp")
    view = await service.create(TerminalSpec(env="container", owner=session))
    tid = view["id"]
    async with bus.subscribe(EventFilter(types=("terminal.",)), name="test") as sub:
        daemon.emit("terminal.title", tid, {"title": "vim README.md", "seq": 10})
        daemon.emit("terminal.cwd", tid, {"cwd": "/tmp/app", "seq": 11})
        daemon.emit("terminal.command", tid, {"phase": "D", "exit_code": 1, "command": "npm test", "abs_row": 40, "seq": 12})
        daemon.emit("terminal.bell", tid, {"seq": 13})
        daemon.emit("terminal.notify", tid, {"title": "Build", "body": "0 errors", "seq": 14})
        daemon.emit("terminal.progress", tid, {"state": 1, "value": 40, "seq": 15})
        got = await _take(sub, 6)
    assert [(e.type, dict(e.payload)) for e in got] == [
        ("terminal.title", {"title": "vim README.md"}),
        ("terminal.cwd", {"cwd": "/tmp/app"}),
        ("terminal.command", {"exit_code": 1, "command": "npm test", "mark_seq": 12}),
        ("terminal.bell", {}),
        ("terminal.notify", {"title": "Build", "body": "0 errors"}),
        ("terminal.progress", {"state": "set", "percent": 40}),
    ]
    assert {(e.terminal_id, e.session_id, e.project_id, e.staff_id) for e in got} == {(tid, "s1", "p1", None)}
    # Persisted, except progress, which only ever matters live.
    stored = [r["type"] for r in await bus.db.fetchall("SELECT type FROM app_events WHERE terminal_id = ? ORDER BY seq", (tid,))]
    assert stored == ["terminal.created", "terminal.title", "terminal.cwd", "terminal.command", "terminal.bell", "terminal.notify"]


async def test_a_staff_terminal_carries_the_staff_id(service: Terminals, daemon: FakePtyd, bus: EventBus, owners: FakeOwners) -> None:
    staff = owners.add(Owner("staff", "st1"), project="p2", cwd="/tmp")
    tid = (await service.create(TerminalSpec(env="container", owner=staff)))["id"]
    async with bus.subscribe(EventFilter(types=("terminal.bell",), staff_id="st1"), name="test") as sub:
        daemon.emit("terminal.bell", tid, {"seq": 1})
        [event] = await _take(sub, 1)
    assert (event.staff_id, event.session_id, event.project_id) == ("st1", None, "p2")


async def test_a_title_or_a_directory_is_published_only_when_it_changes(service: Terminals, daemon: FakePtyd, bus: EventBus) -> None:
    tid = (await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp")))["id"]
    async with bus.subscribe(EventFilter(types=("terminal.title", "terminal.cwd")), name="test") as sub:
        # A shell sets the title on every prompt: three prompts in the same place are one event each.
        for _ in range(3):
            daemon.emit("terminal.title", tid, {"title": "bash", "seq": 1})
            daemon.emit("terminal.cwd", tid, {"cwd": "/tmp", "seq": 1})
        daemon.emit("terminal.title", tid, {"title": "htop", "seq": 2})
        daemon.emit("terminal.title", tid, {"title": "bash", "seq": 3})
        got = await _take(sub, 4)
        await _nothing_more(sub)
    assert [(e.type, dict(e.payload)) for e in got] == [
        ("terminal.title", {"title": "bash"}),
        ("terminal.cwd", {"cwd": "/tmp"}),
        ("terminal.title", {"title": "htop"}),
        ("terminal.title", {"title": "bash"}),
    ]


async def test_lifecycle_and_control_traffic_are_not_doubled_or_leaked(service: Terminals, daemon: FakePtyd, bus: EventBus, db: Database) -> None:
    async with bus.subscribe(EventFilter(), name="test") as sub:
        tid = (await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp")))["id"]
        daemon.emit("terminal.mode", tid, {"alt_screen": True, "bracketed_paste": True, "mouse": False, "app_cursor": False})
        daemon.emit("terminal.stats", None, {"supported": True, "terminals": [], "machine": {}})
        daemon.emit("hook", tid, {"source": "claude", "body": {"hook_event_name": "Stop"}})
        daemon.exit(tid, 0)
        got = await _take(sub, 2)
        await _nothing_more(sub)
    # The daemon's own created and exited arrive too, and are the service's to publish, once.
    assert [e.type for e in got] == ["terminal.created", "terminal.exited"]


async def test_a_replayed_bell_is_dropped_and_a_replayed_command_is_kept(service: Terminals, daemon: FakePtyd, bus: EventBus) -> None:
    tid = (await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp")))["id"]
    then = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    async with bus.subscribe(EventFilter(types=("terminal.",)), name="test") as sub:
        daemon.emit("terminal.bell", tid, {"seq": 1}, at=then)
        daemon.emit("terminal.progress", tid, {"state": 3, "value": 0, "seq": 1}, at=then)
        daemon.emit("terminal.command", tid, {"phase": "D", "exit_code": 0, "command": "make", "seq": 2}, at=then)
        [event] = await _take(sub, 1)
        await _nothing_more(sub)
    assert event.type == "terminal.command" and event.payload["command"] == "make"


async def test_a_terminal_without_a_row_publishes_nothing(service: Terminals, daemon: FakePtyd, bus: EventBus) -> None:
    async with bus.subscribe(EventFilter(types=("terminal.",)), name="test") as sub:
        # Not running, so reconcile does not adopt it; its events belong to nobody.
        daemon.emit("terminal.bell", "strangerterm", {"seq": 1})
        await _nothing_more(sub)


async def test_a_secret_on_a_command_line_does_not_reach_the_table(service: Terminals, daemon: FakePtyd, bus: EventBus, db: Database) -> None:
    tid = (await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp")))["id"]
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    async with bus.subscribe(EventFilter(types=("terminal.command",)), name="test") as sub:
        daemon.emit("terminal.command", tid, {"phase": "D", "exit_code": 0, "command": f"git push https://x:{token}@example.com/r.git", "seq": 1})
        [event] = await _take(sub, 1)
    assert token not in event.payload["command"]
    row = await db.fetchone("SELECT payload_json FROM app_events WHERE type = 'terminal.command'")
    assert row is not None and token not in row["payload_json"]


def _event(kind: str, data: dict[str, Any]) -> TerminalEvent:
    return TerminalEvent(env="container", seq=1, type=kind, terminal_id="t1", at="2026-01-01T00:00:00Z", data=data)


def test_the_payloads_follow_the_registry_not_the_daemon() -> None:
    # OSC 9 has no title; the text is the title, so a lock screen does not show a blank line.
    assert translate(_event("terminal.notify", {"title": "", "body": "Deploy done", "seq": 1})) == ("terminal.notify", {"title": "Deploy done", "body": ""})
    assert translate(_event("terminal.notify", {"title": "", "body": "  ", "seq": 1})) is None
    # The daemon numbers progress states and calls the value "value"; the bus names both.
    assert translate(_event("terminal.progress", {"state": 0, "value": 0})) == ("terminal.progress", {"state": "remove"})
    assert translate(_event("terminal.progress", {"state": 2, "value": 70})) == ("terminal.progress", {"state": "error", "percent": 70})
    assert translate(_event("terminal.progress", {"state": 3, "value": 0})) == ("terminal.progress", {"state": "indeterminate"})
    assert translate(_event("terminal.progress", {"state": 4, "value": 250})) == ("terminal.progress", {"state": "pause", "percent": 100})
    assert translate(_event("terminal.progress", {"state": 9, "value": 1})) is None
    # A command without an exit status (a mark with no number) still finished.
    assert translate(_event("terminal.command", {"phase": "D", "exit_code": None, "command": "", "seq": 5})) == ("terminal.command", {"exit_code": None, "mark_seq": 5})
    long = translate(_event("terminal.command", {"exit_code": 0, "command": "x" * 70_000}))
    assert long is not None and len(long[1]["command"]) == TEXT_CHARS
    assert translate(_event("terminal.cwd", {"cwd": ""})) is None
    for kind in ("terminal.mode", "terminal.stats", "terminal.created", "terminal.exited", "hook"):
        assert translate(_event(kind, {})) is None
