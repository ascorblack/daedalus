"""The command logs of a session whose folder is read-only: kept in the state volume, not in the folder.

The host writes three kinds of log for a session — the spilled output of a long ``Exec``, a
background job's output and a service's output. In a writable folder they are ``.exec/``, ``.jobs/``
and ``.services/`` inside it, as they always were. A folder the operator marked read-only must stay
as the operator left it, so there they go to ``<state>/session-scratch/<session>/`` in the same
layout, where the session's tools and the app's file pane still read them and nothing but the host
writes them.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.services import Services
from daedalus.host.services import session_scratch_dir
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec
from daedalus.tools.files import read_file, write_file
from daedalus.tools.shell import exec_command, job_kill, job_output
from tests.support.waiting import until_await

HEADERS = {"X-Daedalus-Token": "tok"}


def _snapshot(root: Path) -> dict[str, Any]:
    """Every entry under ``root`` with its bytes, and every directory's modification time.

    The times are what catch a directory created and removed again, or a file written with the same
    bytes: either changes the mtime of the directory it was made in.
    """
    out: dict[str, Any] = {".": os.stat(root).st_mtime_ns}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        out[key] = os.stat(path).st_mtime_ns if path.is_dir() else path.read_bytes()
    return out


@pytest.fixture
async def rig(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> Any:
    settings.services_port_range = "18140-18141"
    config.tools.exec.sandbox = "off"
    code, docs = tmp_path / "code", tmp_path / "docs"
    code.mkdir()
    docs.mkdir()
    (docs / "guide.md").write_text("the guide\n", encoding="utf-8")
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    project = await manager.projects.create("Atlas", [FolderSpec(str(code)), FolderSpec(str(docs), readonly=True)])
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None)
    services = Services(app)  # type: ignore[arg-type]
    app.extensions["services"] = services
    yield SimpleNamespace(settings=settings, manager=manager, project=project, code=code, docs=docs, app=app, services=services)
    for row in await services.list_all():
        await services.remove(row["session_id"], row["name"])
    await manager.close()


def _context(session_id: str, call: str) -> ToolContext:
    return ToolContext(tenant_id="t", run_id="r", session_id=session_id, metadata={"tool_call_id": call})


async def _run_everything(rig: Any, session_id: str) -> str:
    """A spilled ``Exec``, a background job and a service in ``session_id``; returns the job's id."""
    spilled = await exec_command().invoke(_context(session_id, "call-spill"), {"command": "seq 1 40000; echo THE-END"})
    assert not spilled.is_error and "THE-END" in spilled.content
    started = await exec_command().invoke(_context(session_id, "call-job"), {"command": "echo job-says-hello; sleep 30", "background": True})
    assert not started.is_error
    job_id = str(started.metadata["job_id"])

    async def said_hello() -> bool:
        return "job-says-hello" in (await job_output().invoke(_context(session_id, "c"), {"job_id": job_id})).content

    await until_await(said_hello, "the background job wrote its line")
    await job_kill().invoke(_context(session_id, "c"), {"job_id": job_id})
    await rig.services.start(session_id, name="web", command="echo service-says-hello; sleep 30", port=None)
    return job_id


async def test_a_read_only_folder_stays_byte_identical_and_the_logs_go_to_the_scratch(rig: Any) -> None:
    state = await rig.manager.create_session("Reader", project_id=rig.project.id, folder_id=rig.project.folders[1].id)
    sid = state.session.id
    scratch = session_scratch_dir(rig.settings.state_dir, sid)
    assert state.workspace == rig.docs and state.services.log_root == scratch
    # The scratch is read, never written, by the session: a readable wall and not a writable one.
    assert scratch in state.services.walls.readable and scratch not in state.services.walls.writable
    before = _snapshot(rig.docs)

    job_id = await _run_everything(rig, sid)

    assert _snapshot(rig.docs) == before
    assert (scratch / ".exec" / "call-spill.log").read_text().count("\n") >= 40000
    assert "job-says-hello" in (scratch / ".jobs" / f"{job_id}.log").read_text()
    assert "service-says-hello" in (scratch / ".services" / "web.log").read_text()


async def test_the_logs_are_read_through_the_tools_and_the_api(rig: Any) -> None:
    state = await rig.manager.create_session("Reader", project_id=rig.project.id, folder_id=rig.project.folders[1].id)
    sid = state.session.id
    scratch = session_scratch_dir(rig.settings.state_dir, sid)
    job_id = await _run_everything(rig, sid)
    ctx = _context(sid, "c-read")

    # Named as the app and the agent name them, relative to the session, and by the path the result gave.
    for path in (f".jobs/{job_id}.log", str(scratch / ".jobs" / f"{job_id}.log")):
        result = await read_file().invoke(ctx, {"path": path})
        assert not result.is_error and "job-says-hello" in result.content, result.content
    assert "service-says-hello" in await rig.services.logs(sid, "web")

    # The scratch is the host's: the agent writes neither there nor, by the relative name, into the folder.
    for path in (f".jobs/{job_id}.log", str(scratch / ".jobs" / "planted.log")):
        refused = await write_file().invoke(ctx, {"path": path, "content": "x"})
        assert refused.is_error
    assert not (scratch / ".jobs" / "planted.log").exists() and not (rig.docs / ".jobs").exists()
    # The rest of the state volume stays sealed, another session's scratch included.
    other = session_scratch_dir(rig.settings.state_dir, "someone-else") / ".jobs"
    other.mkdir(parents=True)
    (other / "x.log").write_text("not yours\n")
    for path in (str(other / "x.log"), str(rig.settings.state_dir / "config.toml")):
        assert (await read_file().invoke(ctx, {"path": path})).is_error

    api = build_app(rig.app, "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        base = f"/api/sessions/{sid}"
        download = await client.get(f"{base}/download", headers=HEADERS, params={"path": f".jobs/{job_id}.log"})
        assert download.status_code == 200 and "job-says-hello" in download.text
        listing = (await client.get(f"{base}/files", headers=HEADERS, params={"path": ".services"})).json()
        assert [e["name"] for e in listing["entries"]] == ["web.log"]
        text = (await client.get(f"{base}/files", headers=HEADERS, params={"path": ".exec/call-spill.log"})).json()
        assert text["kind"] == "file" and "40000" in text["content"]
        # The way out of the scratch is refused as the way out of any folder is.
        assert (await client.get(f"{base}/download", headers=HEADERS, params={"path": ".jobs/../../someone-else/.jobs/x.log"})).status_code == 400
        # The folder itself is read where it is.
        assert (await client.get(f"{base}/download", headers=HEADERS, params={"path": "guide.md"})).text == "the guide\n"


async def test_deleting_the_session_removes_its_scratch(rig: Any) -> None:
    state = await rig.manager.create_session("Reader", project_id=rig.project.id, folder_id=rig.project.folders[1].id)
    sid = state.session.id
    await _run_everything(rig, sid)
    scratch = session_scratch_dir(rig.settings.state_dir, sid)
    assert scratch.is_dir()
    await rig.services.stop_all(sid)
    assert await rig.manager.delete_session(sid)
    assert not scratch.exists() and (rig.docs / "guide.md").read_text() == "the guide\n"


async def test_a_writable_folder_keeps_its_logs_where_they_always_were(rig: Any) -> None:
    state = await rig.manager.create_session("Writer", project_id=rig.project.id)
    sid = state.session.id
    assert state.workspace == rig.code and state.services.log_root is None
    assert state.services.walls.readable == (rig.code, rig.docs)
    job_id = await _run_everything(rig, sid)
    assert (rig.code / ".exec" / "call-spill.log").is_file()
    assert "job-says-hello" in (rig.code / ".jobs" / f"{job_id}.log").read_text()
    assert "service-says-hello" in (rig.code / ".services" / "web.log").read_text()
    assert not (rig.settings.state_dir / "session-scratch").exists()
    # The relative name is the folder's own path, readable and writable as any file in it.
    assert state.services.resolve(f".jobs/{job_id}.log") == rig.code / ".jobs" / f"{job_id}.log"
    assert state.services.resolve(".jobs/new.log", write=True) == rig.code / ".jobs" / "new.log"
