"""The terminals service against the real daemon: a shell started, written to, read, ended, and the
two restarts that matter — the host's, which leaves the terminal running, and the daemon's, which
loses it.

Needs a built daemon: ``DAEDALUS_INTEGRATION=1 DAEDALUS_PTYD_BIN=<path to ptyd>``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import TerminalsConfig
from daedalus.stores.database import Database
from daedalus.terminals.model import Origin, Owner, TerminalSpec
from daedalus.terminals.service import Terminals

BINARY = os.environ.get("DAEDALUS_PTYD_BIN", "")

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_PTYD_BIN to a built ptyd"),
]


class FreeOnly:
    async def exists(self, owner: Owner) -> bool:
        return owner.kind == "free"

    async def labels(self, owners: Iterable[Owner]) -> dict[Owner, str]:
        return {}

    async def project_of(self, owner: Owner) -> str | None:
        return None

    async def default_cwd(self, env: str, owner: Owner, project_id: str | None) -> str | None:
        return None

    async def sandbox_writable(self, env: str, owner: Owner, project_id: str | None, cwd: str) -> list[str]:
        return [cwd]


@pytest.fixture
def base() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _daemon(base: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 — the binary the caller named, with fixed arguments
        [BINARY, "serve", "--env", "container", "--run-dir", str(base / "run"), "--state-dir", str(base / "state"), "--shell", "/bin/sh"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        await asyncio.to_thread(process.wait, 10)


@pytest.fixture
async def daemon(base: Path) -> AsyncIterator[subprocess.Popen[bytes]]:
    process = _daemon(base)
    for _ in range(200):
        if (base / "run" / "endpoint").exists():
            break
        await asyncio.sleep(0.05)
    yield process
    await _stop(process)


async def _service(db: Database, base: Path) -> Terminals:
    cfg = TerminalsConfig()
    service = Terminals(db, run_dirs={"container": base / "run", "host": None}, config=lambda: cfg, owners=FreeOnly())
    await service.start()
    assert await service.wait_available("container", timeout=15)
    return service


async def _status(db: Database, terminal_id: str) -> Any:
    row = await db.fetchone("SELECT status FROM terminals WHERE id = ?", (terminal_id,))
    return row["status"] if row else None


async def _until(check: Any, expected: Any, timeout: float = 30.0) -> None:
    async with asyncio.timeout(timeout):
        while await check() != expected:
            await asyncio.sleep(0.05)


async def test_a_real_shell_is_written_to_read_and_ended(db: Database, base: Path, daemon: subprocess.Popen[bytes]) -> None:
    service = await _service(db, base)
    try:
        view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd=str(base), argv=["/bin/sh"]))
        receipt = await service.write(view["id"], text="echo hi-from-the-host\r", origin=Origin("test"), wait_keyboard=False)
        assert receipt.bytes == len("echo hi-from-the-host\r")
        seen = ""

        async def output() -> bool:
            nonlocal seen
            seen = str((await service.read_output(view["id"], since_seq=0)).data)
            return "\nhi-from-the-host" in seen

        await _until(output, True)
        load = await service.load()
        assert load["running"] == 1 and load["used"]["mem_total_bytes"] > 0
        ended = await service.kill(view["id"])
        assert ended["status"] == "exited"
    finally:
        await service.close()


async def test_a_host_restart_keeps_the_terminal_and_a_daemon_restart_loses_it(db: Database, base: Path, daemon: subprocess.Popen[bytes]) -> None:
    first = await _service(db, base)
    view = await first.create(TerminalSpec(env="container", owner=Owner("free"), cwd=str(base), argv=["/bin/sh"]))
    await first.close()

    second = await _service(db, base)
    try:
        assert await _status(db, view["id"]) == "running"
        assert (await second.get(view["id"]))["live"] is not None
        # Killed outright, as a crash or a recreated container ends it: a daemon stopped politely
        # reports its terminals' exits first, and the row is then simply "exited".
        daemon.kill()
        await asyncio.to_thread(daemon.wait, 10)
        replacement = _daemon(base)
        try:
            await _until(lambda: _status(db, view["id"]), "lost")
        finally:
            await _stop(replacement)
    finally:
        await second.close()


async def test_an_exit_is_seen_as_an_event(db: Database, base: Path, daemon: subprocess.Popen[bytes]) -> None:
    service = await _service(db, base)
    try:
        view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd=str(base), argv=["/bin/sh", "-c", "exit 4"]))
        await _until(lambda: _status(db, view["id"]), "exited")
        row = await db.fetchone("SELECT exit_code FROM terminals WHERE id = ?", (view["id"],))
        assert row["exit_code"] == 4
    finally:
        await service.close()
