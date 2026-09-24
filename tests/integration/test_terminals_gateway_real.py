"""A browser's WebSocket through the real host to the real daemon: attach, type, read, watch, detach.

The whole chain is real except the browser: the application served over loopback, the gateway, the
terminals service and a daemon built from ``ptyd/`` running a real shell in a real PTY. It runs as a
host environment, so the audit of attaching and detaching is exercised too.

Needs a built daemon: ``DAEDALUS_INTEGRATION=1 DAEDALUS_PTYD_BIN=<path to ptyd>``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import terminals as terminals_extension
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.terminals import wire
from daedalus.terminals.gateway import TERMINAL_WS_MAX_BYTES

BINARY = os.environ.get("DAEDALUS_PTYD_BIN", "")
H = {"X-Daedalus-Token": "tok"}

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_PTYD_BIN to a built ptyd"),
]


@pytest.fixture
def base() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))  # short: a unix socket path is at most 108 bytes
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(base: Path) -> AsyncIterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(  # noqa: S603 — the binary the caller named, with fixed arguments
        [BINARY, "serve", "--env", "host", "--run-dir", str(base / "run"), "--state-dir", str(base / "state"), "--shell", "/bin/sh"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(200):
        if (base / "run" / "endpoint").exists():
            break
        await asyncio.sleep(0.05)
    yield process
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        await asyncio.to_thread(process.wait, 10)


@pytest.fixture
async def host(settings: Settings, db: Database, base: Path, daemon: subprocess.Popen[bytes]) -> AsyncIterator[tuple[str, Any]]:
    configured = settings.model_copy(update={"terminals_host_dir": base / "run"})
    manager = SessionManager(configured, RuntimeConfig(), db=db)
    await manager.start()
    application = SimpleNamespace(settings=configured, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    tasks = await terminals_extension.install(application)  # type: ignore[arg-type]
    assert await application.extensions["terminals"].wait_available("host", timeout=15)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(build_app(application, "tok"), log_level="warning", access_log=False, lifespan="off", ws_max_size=TERMINAL_WS_MAX_BYTES))  # type: ignore[arg-type]
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    try:
        yield f"127.0.0.1:{listener.getsockname()[1]}", application
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
        for task in tasks:
            task.cancel()
        await application.extensions["terminals"].close()
        await manager.close()


class Browser:
    """What the app's connection does, reduced to what the test needs: frames in, frames out."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws
        self.events: list[dict[str, Any]] = []
        self.screen = b""
        self.seq = 0

    async def attach(self, **fields: Any) -> None:
        await self.ws.send(bytes([wire.ATTACH]) + json.dumps({"lastSeq": 0, "haveState": False, "readOnly": False, **fields}).encode())

    async def type(self, text: str) -> None:
        await self.ws.send(bytes([wire.INPUT]) + text.encode())

    async def pump(self, until: Any, timeout: float = 20.0) -> None:
        async with asyncio.timeout(timeout):
            while not until(self):
                frame = wire.decode_browser(await self.ws.recv())  # type: ignore[arg-type]
                if frame.kind == "event":
                    assert frame.json is not None
                    self.events.append(frame.json)
                elif frame.kind == "snapshot":
                    self.screen, self.seq = frame.data, frame.seq
                elif frame.kind == "output":
                    self.screen += frame.data
                    self.seq = frame.seq + len(frame.data)
                    await self.ws.send(bytes([wire.ACK]) + struct.pack(">Q", self.seq))

    def event(self, kind: str) -> dict[str, Any] | None:
        return next((e for e in self.events if e.get("type") == kind), None)


async def _ticket(address: str, terminal_id: str, *, read_only: bool = False) -> str:
    async with httpx.AsyncClient(base_url=f"http://{address}") as http:
        response = await http.post(f"/api/terminals/{terminal_id}/ticket", json={"read_only": read_only}, headers=H)
    assert response.status_code == 200, response.text
    return str(response.json()["ticket"])


def _socket(address: str, terminal_id: str, ticket: str) -> Any:
    return connect(f"ws://{address}/ws/terminals/{terminal_id}?ticket={ticket}", origin=Origin(f"http://{address}"), max_size=4 << 20, open_timeout=10)


async def test_a_browser_types_into_a_real_shell_and_a_watcher_cannot(host: tuple[str, Any], base: Path) -> None:
    address, application = host
    async with httpx.AsyncClient(base_url=f"http://{address}") as http:
        created = await http.post("/api/terminals", json={"env": "host", "owner_kind": "free", "cwd": str(base)}, headers=H)
    assert created.status_code == 201, created.text
    terminal_id = created.json()["id"]

    typed = "echo gateway-$((6*7))\r"
    async with _socket(address, terminal_id, await _ticket(address, terminal_id)) as ws:
        browser = Browser(ws)
        await browser.attach()
        await browser.pump(lambda b: b.event("hello") is not None)
        hello = browser.event("hello")
        assert hello is not None and hello["read_only"] is False and hello["terminal"]["id"] == terminal_id
        await ws.send(bytes([wire.RESIZE]) + struct.pack(">HHHH", 100, 30, 900, 540))
        await browser.type(typed)
        await browser.pump(lambda b: b"gateway-42" in b.screen)

        # A second browser on a read-only ticket sees the same terminal and cannot type into it.
        async with _socket(address, terminal_id, await _ticket(address, terminal_id, read_only=True)) as watching:
            watcher = Browser(watching)
            await watcher.attach()
            await watcher.pump(lambda b: b.event("hello") is not None)
            assert watcher.event("hello")["read_only"] is True  # type: ignore[index]
            await watcher.type("echo watcher-$((2+3))\r")
            await browser.type("echo after-$((1+1))\r")
            await browser.pump(lambda b: b"after-2" in b.screen)
            assert b"watcher-5" not in browser.screen

    service = application.extensions["terminals"]
    async with asyncio.timeout(10):
        while len([e for e in await service.audit_log(terminal_id) if e["action"] == "detach"]) < 2:
            await asyncio.sleep(0.05)
    entries = list(reversed(await service.audit_log(terminal_id)))
    assert [e["action"] for e in entries] == ["create", "attach", "attach", "detach", "detach"]
    first_detach = next(e for e in entries if e["action"] == "detach" and e["detail"]["bytes_typed"] > 0)
    assert first_detach["detail"]["bytes_typed"] == len(typed) + len("echo after-$((1+1))\r")
    watcher_detach = next(e for e in entries if e["action"] == "detach" and e is not first_detach)
    assert watcher_detach["detail"]["bytes_typed"] == 0 and watcher_detach["detail"]["input_dropped"] == len("echo watcher-$((2+3))\r")
    assert not any("gateway" in json.dumps(e["detail"]) for e in entries)
    # Detached: the daemon lists no clients for the terminal any more.
    assert (await service.get(terminal_id))["live"]["clients"] == 0


async def test_a_daemon_that_dies_closes_the_socket_with_1012(host: tuple[str, Any], base: Path, daemon: subprocess.Popen[bytes]) -> None:
    address, _ = host
    async with httpx.AsyncClient(base_url=f"http://{address}") as http:
        terminal_id = (await http.post("/api/terminals", json={"env": "host", "owner_kind": "free", "cwd": str(base)}, headers=H)).json()["id"]
    async with _socket(address, terminal_id, await _ticket(address, terminal_id)) as ws:
        browser = Browser(ws)
        await browser.attach()
        await browser.pump(lambda b: b.event("hello") is not None)
        daemon.kill()
        await asyncio.to_thread(daemon.wait, 10)
        with pytest.raises(ConnectionClosed) as closed:
            async with asyncio.timeout(10):
                while True:
                    await ws.recv()
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1012
