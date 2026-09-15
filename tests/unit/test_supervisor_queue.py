"""A rebuild asked for during another one is queued, not dropped."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(tmp_path: Path):  # type: ignore[no-untyped-def]
    for key in ("DAEDALUS_BOT_REPO", "DAEDALUS_CORE_REPO", "DAEDALUS_STATE", "DAEDALUS_WORKSPACES", "DAEDALUS_SUPERVISOR_SOCKET", "DAEDALUS_REBUILD_TRIGGER_DIR"):
        os.environ[key] = str(tmp_path / key.lower())
    spec = importlib.util.spec_from_file_location("supervisor_queue_under_test", REPO_ROOT / "launcher" / "supervisor.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["supervisor_queue_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


async def test_a_rebuild_requested_during_one_runs_right_after(tmp_path: Path) -> None:
    sup = _load(tmp_path)
    supervisor = sup.Supervisor()
    ran: list[str] = []
    release = asyncio.Event()

    async def fake_rebuild(reason: str) -> None:
        async with supervisor.lock:
            ran.append(reason)
            await release.wait()
        supervisor._run_queued_rebuild()

    supervisor._rebuild = fake_rebuild  # type: ignore[method-assign]
    first = await supervisor.rebuild("first merge")
    await asyncio.sleep(0)
    assert first.startswith("rebuild started") and ran == ["first merge"]
    second = await supervisor.rebuild("second merge")
    third = await supervisor.rebuild("third merge")  # one slot: the latest reason wins, the target is origin/main anyway
    assert "queued" in second and "queued" in third
    assert supervisor.queued_rebuild == "third merge"
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert ran == ["first merge", "third merge"]
    assert supervisor.queued_rebuild is None


async def test_a_rollback_drops_what_was_queued_behind_it(tmp_path: Path) -> None:
    sup = _load(tmp_path)
    supervisor = sup.Supervisor()
    ran: list[str] = []

    async def fake_rollback(steps_back: int) -> str:
        async with supervisor.lock:
            queued = await supervisor.rebuild("merge during rollback")
            assert "queued" in queued
            return "rolled back"

    async def fake_rebuild(reason: str) -> None:
        ran.append(reason)

    supervisor._rollback = fake_rollback  # type: ignore[method-assign]
    supervisor._rebuild = fake_rebuild  # type: ignore[method-assign]
    assert await supervisor.rollback(0) == "rolled back"
    await asyncio.sleep(0)
    assert ran == [] and supervisor.queued_rebuild is None
