"""Services: a detached process with a published port, its log, stopping, and what a restart does to it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.inbox import Inbox
from daedalus.extensions.services import Services, parse_range, pid_alive
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    settings.services_port_range = "18100-18103"
    settings.services_public_host = "10.0.0.5"
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
    app.extensions["inbox"] = Inbox(app)  # type: ignore[arg-type]
    yield app
    await manager.close()


def test_port_range_parsing() -> None:
    assert parse_range("8100-8119") == (8100, 8119) and parse_range("9000") == (9000, 9000)
    with pytest.raises(ValueError):
        parse_range("9-1")


async def test_service_runs_detached_with_a_port_and_a_log(app: Any) -> None:
    services = Services(app)
    state = await app.manager.create_session("host")
    sid = state.session.id
    s = await services.start(sid, name="Web Demo", command='echo "up on $PORT"; python3 -c "import time; print(\'serving\', flush=True); time.sleep(30)"')
    assert s["name"] == "web-demo" and s["status"] == "running" and 18100 <= s["port"] <= 18103
    assert s["url"] == f"http://10.0.0.5:{s['port']}" and pid_alive(s["pid"])
    assert "serving" in await services.logs(sid, "web-demo") and f"up on {s['port']}" in await services.logs(sid, "web-demo")
    with pytest.raises(ValueError, match="already running"):
        await services.start(sid, name="web-demo", command="sleep 5")
    second = await services.start(sid, name="other", command="sleep 30")
    assert second["port"] != s["port"]
    listed = await services.list(sid)
    assert {x["name"] for x in listed} == {"web-demo", "other"} and all(x["status"] == "running" for x in listed)
    stopped = await services.stop(sid, "web-demo")
    assert stopped["status"] == "stopped" and not pid_alive(s["pid"])
    assert (await services.get(sid, "web-demo"))["restart"] == 0
    assert await services.stop_all(sid) == 1 and await services.list(sid) == []


async def test_a_command_that_dies_at_once_is_reported_with_its_log(app: Any) -> None:
    services = Services(app)
    state = await app.manager.create_session("host")
    with pytest.raises(RuntimeError, match="exited with code 3"):
        await services.start(state.session.id, name="bad", command="echo boom; exit 3")
    assert (await services.get(state.session.id, "bad"))["status"] == "dead"
    with pytest.raises(ValueError, match="outside the published range"):
        await services.start(state.session.id, name="p", command="sleep 5", port=1)


async def test_reconcile_restarts_or_reports_after_a_rebuild(app: Any) -> None:
    services = Services(app)
    state = await app.manager.create_session("host")
    sid = state.session.id
    kept = await services.start(sid, name="keep", command="sleep 30", restart=True)
    gone = await services.start(sid, name="gone", command="sleep 30", restart=False)
    # the container was rebuilt: both processes are dead, the table still says running
    await services._terminate(int(kept["pid"]))
    await services._terminate(int(gone["pid"]))
    await services.reconcile()
    rows = {r["name"]: r for r in await services.list(sid)}
    assert rows["keep"]["status"] == "running" and rows["keep"]["pid"] != kept["pid"]
    assert rows["gone"]["status"] == "dead" and "not running after the restart" in rows["gone"]["note"]
    entries = await app.db.fetchall("SELECT title FROM inbox WHERE session_id = ?", (sid,))
    assert any("gone" in e["title"] for e in entries)
    await services.stop_all(sid)
    await asyncio.sleep(0)
