"""The terminals service against the real daemon: a shell started, written to, read, ended, and the
two restarts that matter — the host's, which leaves the terminal running, and the daemon's, which
loses it — then a program's marks on the event bus, and a session's agent reading its terminal.

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
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import TerminalsConfig
from daedalus.host.events import EventBus, EventFilter
from daedalus.host.services import SessionServices, locator
from daedalus.stores.database import Database
from daedalus.terminals.bus import BusBridge
from daedalus.terminals.model import Origin, Owner, TerminalSpec
from daedalus.terminals.service import Terminals
from daedalus.tools.terminal import terminal_read

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


async def test_a_real_programs_marks_reach_the_event_bus(db: Database, base: Path, daemon: subprocess.Popen[bytes]) -> None:
    bus = EventBus(db)
    await bus.start()
    cfg = TerminalsConfig()
    service = Terminals(db, run_dirs={"container": base / "run", "host": None}, config=lambda: cfg, owners=FreeOnly(), bus=bus)
    service.subscribe(BusBridge(service))
    await service.start()
    try:
        assert await service.wait_available("container", timeout=15)
        async with bus.subscribe(EventFilter(types=("terminal.",)), name="test") as sub:
            # A program's command marks count when they carry its terminal's nonce, as a shell's do.
            marks = r"\033]2;building\007\033]7;file://box/srv/app\007\007\033]9;Build finished\007\033]133;C;k=%s\007\033]133;D;3;k=%s\007"
            script = f"printf '{marks}' \"$DAEDALUS_SI_NONCE\" \"$DAEDALUS_SI_NONCE\"; sleep 1"
            view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd=str(base), argv=["/bin/sh", "-c", script]))
            seen: dict[str, dict[str, Any]] = {}
            async with asyncio.timeout(20):
                while "terminal.exited" not in seen:
                    event = await anext(sub)
                    assert event.terminal_id == view["id"]
                    seen.setdefault(event.type, dict(event.payload))
        assert seen["terminal.title"] == {"title": "building"}
        assert seen["terminal.cwd"] == {"cwd": "/srv/app"}
        assert seen["terminal.bell"] == {}
        assert seen["terminal.notify"] == {"title": "Build finished", "body": ""}
        assert seen["terminal.command"]["exit_code"] == 3
    finally:
        await service.close()
        await bus.close()


async def test_a_real_shell_reports_its_commands(db: Database, base: Path, daemon: subprocess.Popen[bytes]) -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    home = base / "home"
    home.mkdir()
    (home / ".bashrc").write_text("HISTFILE=\nalias ll='echo from-the-alias'\n")
    cfg = TerminalsConfig(shell="bash")
    service = Terminals(db, run_dirs={"container": base / "run", "host": None}, config=lambda: cfg, owners=FreeOnly())
    await service.start()
    try:
        assert await service.wait_available("container", timeout=15)
        view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd=str(base), env_vars={"HOME": str(home)}))
        assert view["shell_integration"] is True
        origin = Origin(actor="test")
        for line in ("ll", "false"):
            receipt = await service.write(view["id"], text=line + "\r", origin=origin, wait_keyboard=False)
            done = await service.wait_for(view["id"], command_done=True, since_seq=receipt.seq_before, timeout=15)
            assert done["matched"] == "command_done" and done["command"]["command"] == line
        commands = await service.commands(view["id"], with_output=True)
        assert [(c["command"], c["exit_code"]) for c in commands] == [("ll", 0), ("false", 1)]
        assert commands[0]["output"] in ("from-the-alias", "")  # empty only from a daemon built without its screen

        async def mirrored() -> Any:
            row = await db.fetchone("SELECT last_command_json FROM terminals WHERE id = ?", (view["id"],))
            return row["last_command_json"] and '"false"' in row["last_command_json"]

        await _until(mirrored, True)
    finally:
        await service.close()


class OneSession(FreeOnly):
    async def exists(self, owner: Owner) -> bool:
        return owner.kind == "free" or owner == Owner("session", "s-real")


async def test_a_sessions_agent_reads_a_real_terminal(db: Database, base: Path, daemon: subprocess.Popen[bytes]) -> None:
    cfg = TerminalsConfig()
    service = Terminals(db, run_dirs={"container": base / "run", "host": None}, config=lambda: cfg, owners=OneSession())
    await service.start()
    locator.register(SessionServices(session_id="s-real", workspace_dir=base, extra={"manager": SimpleNamespace(service_hooks={"terminals": service.agent_service})}))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="s-real", metadata={"tool_call_id": "c"})
    try:
        assert await service.wait_available("container", timeout=15)
        view = await service.create(TerminalSpec(env="container", owner=Owner("session", "s-real"), cwd=str(base), argv=["/bin/sh", "-c", "echo 3 failing; sleep 30"], title="tests"))

        async def output() -> str:
            return str((await terminal_read().invoke(ctx, {"what": "output", "terminal": "tests"})).content)

        async with asyncio.timeout(20):
            while "3 failing" not in await output():
                await asyncio.sleep(0.1)
        # With or without an emulator in the daemon, "screen" answers with what the terminal says.
        screen = await terminal_read().invoke(ctx, {"what": "screen"})
        assert not screen.is_error and "3 failing" in str(screen.content)
        emulator = str((service.links["container"].info.get("capabilities") or {}).get("emulator") or "")
        if not emulator.startswith("basic"):
            # A daemon that keeps the screen answers from it, not from the output.
            assert "could not be read" not in str(screen.content) and "— screen " in str(screen.content)
        listed = await terminal_read().invoke(ctx, {"what": "list"})
        assert view["id"] in str(listed.content)
    finally:
        locator.unregister("s-real")
        await service.close()
