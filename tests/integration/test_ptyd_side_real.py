"""The side channels against the real daemon: a launch whose shell posts a hook through the command
its environment names and gets the host's reply to a held one, a program run for its output, files
read under a root and refused off it, and a byte stream to a socket in the launch's dial directory.

Needs a built daemon: ``DAEDALUS_INTEGRATION=1 DAEDALUS_PTYD_BIN=<path to ptyd>``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from daedalus.config import TerminalsConfig
from daedalus.stores.database import Database
from daedalus.terminals.model import Forbidden, LaunchSpec, Origin, Owner, TerminalSpec
from daedalus.terminals.service import Terminals
from tests.integration.test_ptyd_real import FreeOnly

BINARY = os.environ.get("DAEDALUS_PTYD_BIN", "")

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_PTYD_BIN to a built ptyd"),
]


@pytest.fixture
def base() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def service(db: Database, base: Path) -> AsyncIterator[Terminals]:
    home = base / "home"
    home.mkdir()
    process = subprocess.Popen(  # noqa: S603 — the binary the caller named, with fixed arguments
        [BINARY, "serve", "--env", "container", "--run-dir", str(base / "run"), "--state-dir", str(base / "state"), "--shell", "/bin/sh", "--home", str(home)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(200):
        if (base / "run" / "endpoint").exists():
            break
        await asyncio.sleep(0.05)
    made = Terminals(db, run_dirs={"container": base / "run", "host": None}, config=lambda: TerminalsConfig(input_idle_ms=0), owners=FreeOnly())
    await made.start()
    assert await made.wait_available("container", timeout=15)
    try:
        yield made
    finally:
        await made.close()
        process.send_signal(signal.SIGTERM)
        await asyncio.to_thread(process.wait, 10)


async def _output(service: Terminals, terminal_id: str, want: str, timeout: float = 30.0) -> str:
    async with asyncio.timeout(timeout):
        while True:
            seen = str((await service.read_output(terminal_id, since_seq=0)).data)
            if want in seen:
                return seen
            await asyncio.sleep(0.05)


async def test_a_launch_posts_hooks_and_gets_its_reply(service: Terminals, base: Path) -> None:
    launch = await service.register_launch("container", LaunchSpec(files={"settings.json": b'{"hooks":{}}'}, hold_max_ms=30_000))
    assert Path(launch.dir, "settings.json").read_bytes() == b'{"hooks":{}}'
    view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd=str(base), argv=["/bin/sh"], launch_id=launch.launch_id))
    events = service.hook_events(launch.launch_id)
    await service.write(view["id"], text="""echo '{"x":1}' | "$DAEDALUS_HOOK_CMD" Stop; echo "posted=$?"\r""", origin=Origin("test"), wait_keyboard=False)
    first = await asyncio.wait_for(anext(events), 30)
    assert first.name == "Stop" and first.body == {"x": 1} and first.terminal_id == view["id"] and first.launch_id == launch.launch_id
    await _output(service, view["id"], "posted=0")

    await service.write(view["id"], text="""echo '{"q":"go?"}' | "$DAEDALUS_HOOK_CMD" ask --wait-ms 20000; echo " asked=$?"\r""", origin=Origin("test"), wait_keyboard=False)
    held = await asyncio.wait_for(anext(events), 30)
    assert held.name == "ask" and held.reply_id
    await service.reply_hook("container", held.reply_id, 200, "answer-42", launch_id=launch.launch_id)
    await _output(service, view["id"], "answer-42 asked=0")

    await service.kill(view["id"])
    # The launch ends thirty seconds after its terminal; ended now, its hooks stream ends too.
    assert await service.unregister_launch("container", launch.launch_id) is True
    assert await asyncio.wait_for(anext(events, None), 10) is None
    assert not Path(launch.dir).exists()


async def test_programs_files_and_streams(service: Terminals, base: Path) -> None:
    if shutil.which("git"):
        result = await service.exec_run("container", ["git", "--version"], cwd=str(base))
        assert result.exit_code == 0 and result.stdout.startswith("git version")
    with pytest.raises(Forbidden):
        await service.exec_run("container", ["sh", "-c", "id"])

    project = base / "project"
    (project / ".claude").mkdir(parents=True)
    (project / "notes.md").write_text("hello")
    (project / ".claude" / ".credentials.json").write_text("{}")
    service.set_extra_roots("container", "test", [str(project)])
    async with asyncio.timeout(15):
        while True:
            try:
                chunk = await service.fs_read("container", str(project / "notes.md"))
                break
            except Forbidden:
                await asyncio.sleep(0.2)  # the roots reach the daemon within a housekeeping tick
    assert chunk.data == b"hello" and chunk.eof
    with pytest.raises(Forbidden):
        await service.fs_read("container", str(project / ".claude" / ".credentials.json"))
    with pytest.raises(Forbidden):
        await service.fs_read("container", str(base / "state" / "agent-writes.jsonl"))

    launch = await service.register_launch("container", LaunchSpec())

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.readline())
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(echo, path=str(Path(launch.dial_dir) / "bridge.sock"))
    try:
        stream = await service.net_dial("container", "unix:bridge.sock", launch.launch_id)
        await stream.write(b"ping\n")
        received = b""
        async with asyncio.timeout(10):
            async for chunk_bytes in stream:
                received += chunk_bytes
        assert received == b"ping\n" and stream.closed
    finally:
        server.close()
    await service.unregister_launch("container", launch.launch_id)
