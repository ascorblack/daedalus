"""A browser's round trip through the real host and the real daemon: attach, drop, come back.

The app's connection keeps what it has drawn when its socket drops and asks, on the way back, for
only what it missed (``haveState`` and ``lastSeq``). The daemon answers with the raw tail when the
terminal was not resized meanwhile, and with a snapshot at the current size when it was, because raw
bytes written for another size would be drawn wrong. Both answers are checked here end to end —
ticket, WebSocket, ATTACH, output, drop, reattach — with a real shell in a real PTY.

Needs a built daemon: ``DAEDALUS_INTEGRATION=1 DAEDALUS_PTYD_BIN=<path to ptyd>``.
"""

from __future__ import annotations

import asyncio
import os
import struct
from pathlib import Path
from typing import Any

import httpx
import pytest

from daedalus.terminals import wire
from tests.integration.test_terminals_gateway_real import (  # noqa: F401 - fixtures
    Browser,
    H,
    _socket,
    _ticket,
    base,
    daemon,
    host,
)

BINARY = os.environ.get("DAEDALUS_PTYD_BIN", "")

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_PTYD_BIN to a built ptyd"),
]


class Recording(Browser):
    """The test's browser, also keeping the kind and position of every frame in arrival order."""

    def __init__(self, ws: Any) -> None:
        super().__init__(ws)
        self.frames: list[tuple[str, int, int]] = []

    async def pump(self, until: Any, timeout: float = 20.0) -> None:
        async with asyncio.timeout(timeout):
            while not until(self):
                frame = wire.decode_browser(await self.ws.recv())
                self.frames.append((frame.kind, frame.seq, len(frame.data)))
                if frame.kind == "event":
                    assert frame.json is not None
                    self.events.append(frame.json)
                elif frame.kind == "snapshot":
                    self.screen, self.seq = frame.data, frame.seq
                elif frame.kind == "output":
                    self.screen += frame.data
                    self.seq = frame.seq + len(frame.data)
                    await self.ws.send(bytes([wire.ACK]) + struct.pack(">Q", self.seq))


async def _terminal(address: str, cwd: Path) -> str:
    async with httpx.AsyncClient(base_url=f"http://{address}") as http:
        created = await http.post("/api/terminals", json={"env": "host", "owner_kind": "free", "cwd": str(cwd)}, headers=H)
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_a_dropped_browser_gets_only_the_tail_it_missed(host: tuple[str, Any], base: Path) -> None:  # noqa: F811
    address, _ = host
    terminal_id = await _terminal(address, base)
    async with _socket(address, terminal_id, await _ticket(address, terminal_id)) as ws:
        first = Recording(ws)
        await first.attach()
        await first.pump(lambda b: b.event("hello") is not None)
        await first.type("seq 1 300 | sed 's/^/before-/'\r")
        await first.pump(lambda b: b"before-300" in b.screen)
        # Output the browser will miss: printed a moment after its socket goes away.
        await first.type("sleep 1; echo away-$((40+2))\r")
        await first.pump(lambda b: b"sleep 1" in b.screen)
        held, seen = first.seq, first.screen
    await asyncio.sleep(1.5)

    async with _socket(address, terminal_id, await _ticket(address, terminal_id)) as ws:
        back = Recording(ws)
        await back.attach(lastSeq=held, haveState=True)
        await back.pump(lambda b: b"away-42" in b.screen)
    kinds = [kind for kind, _, _ in back.frames]
    assert "snapshot" not in kinds, back.frames
    outputs = [(seq, size) for kind, seq, size in back.frames if kind == "output"]
    # The tail begins exactly where the browser stopped, and nothing it already had comes again.
    assert outputs[0][0] == held
    assert b"before-300" not in back.screen
    assert b"before-300" in seen


async def test_a_terminal_resized_while_away_comes_back_as_a_snapshot(host: tuple[str, Any], base: Path) -> None:  # noqa: F811
    address, _ = host
    terminal_id = await _terminal(address, base)
    async with _socket(address, terminal_id, await _ticket(address, terminal_id)) as ws:
        first = Recording(ws)
        await first.attach()
        await first.pump(lambda b: b.event("hello") is not None)
        await ws.send(bytes([wire.RESIZE]) + struct.pack(">HHHH", 80, 24, 640, 384))
        await first.type("echo first-$((3*3))\r")
        await first.pump(lambda b: b"first-9" in b.screen)
        held = first.seq

    # Another screen prints something and then makes the terminal wider while the first one is away.
    # (A resize at the very offset the first one stopped at would leave it nothing drawn for the old
    # size to miss, and the daemon rightly sends the tail then.)
    async with _socket(address, terminal_id, await _ticket(address, terminal_id)) as ws:
        other = Recording(ws)
        await other.attach()
        await other.pump(lambda b: b.event("hello") is not None)
        await other.type("echo other-$((5*5))\r")
        await other.pump(lambda b: b"other-25" in b.screen)
        await ws.send(bytes([wire.RESIZE]) + struct.pack(">HHHH", 120, 40, 960, 640))
        await other.pump(lambda b: any(e.get("type") == "size" and e.get("cols") == 120 for e in b.events))

    async with _socket(address, terminal_id, await _ticket(address, terminal_id)) as ws:
        back = Recording(ws)
        await back.attach(lastSeq=held, haveState=True)
        await back.pump(lambda b: any(kind == "snapshot" for kind, _, _ in b.frames))
    resync = next((e for e in back.events if e.get("type") == "resync"), None)
    assert resync is not None, back.events
    snapshots = [f for f in back.frames if f[0] == "snapshot"]
    assert len(snapshots) == 1
    hello = back.event("hello")
    assert hello is not None and (hello["size"]["cols"], hello["size"]["rows"]) == (120, 40)
    assert b"first-9" in back.screen and b"other-25" in back.screen
