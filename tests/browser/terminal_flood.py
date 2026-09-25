"""A real WebSocket for the terminal checks that measure throughput: the build and a flood, over a socket.

``terminal_stub.py`` plays the daemon through Playwright's socket routing, which relays every frame
through the test process and caps a flood at a few megabytes a second — the relay's speed, not the
page's. This serves the built app and ``/ws/terminals/{id}`` from one loopback server (uvicorn, in a
thread), so the page's own socket carries the frames and what is measured is the page.

The socket plays the daemon's side of the protocol the way the stub does — ``hello``, a ``resync`` and
an empty snapshot on attach, then output only while less than ``window`` bytes are unacknowledged —
and records what the page sends back: every ACK (whether one ever passed what was sent, or went
backwards), every RESIZE, and the most that was ever unacknowledged. A RESIZE is confirmed with a
``size`` event, as the daemon does, so the page resizes its terminal while output is still arriving.

The REST routes (the ticket, the listing) stay with Playwright: ``TerminalStub.route`` answers them.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect
from terminal_stub import ACK_BYTES, WINDOW, decode, enc_event, enc_output, enc_snapshot

BATCH = 64 * 1024


@dataclass
class Flood:
    """What one terminal prints once a page attaches, and what the page did with it."""

    data: bytes = b""
    window: int = WINDOW
    cols: int = 80
    rows: int = 24
    # Filled in by the socket.
    sent: int = 0
    acked: int = 0
    max_unacked: int = 0
    acks: int = 0
    ack_past_sent: int = 0
    ack_backwards: int = 0
    resizes: list[tuple[float, int, int]] = field(default_factory=list)
    attached_at: float | None = None
    finished_at: float | None = None
    started: asyncio.Event | None = None
    go: threading.Event = field(default_factory=threading.Event)
    rate: float = 0.0  # bytes per second, 0 for as fast as the window allows

    def unacked(self) -> int:
        return self.sent - self.acked


class FloodServer:
    """The build under ``/app`` and a flood per terminal id under ``/ws/terminals/{id}``."""

    def __init__(self, dist: Path) -> None:
        self.dist = dist
        self.floods: dict[str, Flood] = {}
        self.port = free_port()
        app = Starlette(routes=[
            WebSocketRoute("/ws/terminals/{id}", self.socket),
            Mount("/app/assets", StaticFiles(directory=dist / "assets")),
            Route("/app/{rest:path}", self.index),
            Route("/app", self.index),
        ])
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", ws_max_size=16 * 1024 * 1024, lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/app"

    def __enter__(self) -> FloodServer:
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("the flood server did not start")
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    async def index(self, request: Any) -> Response:
        rest = request.path_params.get("rest", "")
        candidate = (self.dist / rest).resolve()
        if rest and candidate.is_file() and self.dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(self.dist / "index.html")

    async def socket(self, ws: WebSocket) -> None:
        flood = self.floods.get(ws.path_params["id"])
        await ws.accept()
        if flood is None:
            await ws.close(code=4404)
            return
        wake = asyncio.Event()

        async def receive() -> None:
            while True:
                message = await ws.receive_bytes()
                kind, f = decode(message)
                if kind == "attach":
                    await ws.send_bytes(enc_event({
                        "type": "hello", "client_id": "c1", "read_only": False, "ack_bytes": ACK_BYTES, "window_bytes": flood.window,
                        "terminal": {"id": ws.path_params["id"], "title": "flood", "cwd": "/home/operator", "status": "running", "cols": flood.cols, "rows": flood.rows},
                        "size": {"cols": flood.cols, "rows": flood.rows, "owner": "other"},
                        "keyboard": {"owner": "auto", "until": None},
                        "modes": {"alt_screen": False, "mouse": False, "bracketed_paste": False, "app_cursor": False},
                    }))
                    await ws.send_bytes(enc_event({"type": "resync", "reason": "attach", "first_abs_row": 0}))
                    await ws.send_bytes(enc_snapshot(flood.cols, flood.rows, 0, b""))
                    flood.attached_at = time.monotonic()
                elif kind == "ack":
                    flood.acks += 1
                    if f["seq"] > flood.sent:
                        flood.ack_past_sent += 1
                    if f["seq"] < flood.acked:
                        flood.ack_backwards += 1
                    flood.acked = max(flood.acked, f["seq"])
                    if flood.finished_at is None and flood.acked >= len(flood.data) and flood.data:
                        flood.finished_at = time.monotonic()
                elif kind == "resize":
                    flood.cols, flood.rows = f["cols"], f["rows"]
                    flood.resizes.append((time.monotonic(), f["cols"], f["rows"]))
                    await ws.send_bytes(enc_event({"type": "size", "cols": f["cols"], "rows": f["rows"], "owner": "you"}))
                wake.set()

        async def send() -> None:
            while flood.attached_at is None:
                await wake.wait()
                wake.clear()
            while not flood.go.is_set():
                await asyncio.sleep(0.02)
            began = time.monotonic()
            while flood.sent < len(flood.data):
                room = flood.window - flood.unacked()
                if flood.rate:
                    allowed = int((time.monotonic() - began) * flood.rate) - flood.sent
                    room = min(room, allowed)
                if room <= 0:
                    wake.clear()
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=0.02)
                    except TimeoutError:
                        pass
                    continue
                size = min(BATCH, room, len(flood.data) - flood.sent)
                await ws.send_bytes(enc_output(flood.sent, flood.data[flood.sent:flood.sent + size]))
                flood.sent += size
                flood.max_unacked = max(flood.max_unacked, flood.unacked())

        tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.exception() and not isinstance(task.exception(), WebSocketDisconnect):
                    raise task.exception()  # type: ignore[misc]
            await tasks[0]
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()


def numbered_lines(total: int, width: int = 64) -> tuple[bytes, int]:
    """``total`` bytes of lines ``00000000 xxxx…``: short enough not to wrap at 80 columns."""
    count = total // width
    line = "x" * (width - 11)
    return "".join(f"{i:08d} {line}\r\n" for i in range(count)).encode(), count


def free_port() -> int:
    """A loopback port nobody holds, outside the range the product hands to preview servers."""
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if not 8100 <= port <= 8119:
            return port
    raise RuntimeError("no free port")


def sequence_breaks(lines: list[str]) -> tuple[int, int, int]:
    """Numbered lines held, how often the numbering did not go up by one, and the last number."""
    nums = [int(x[:8]) for x in lines if len(x) >= 9 and x[:8].isdigit() and x[8] == " "]
    breaks = sum(1 for a, b in zip(nums, nums[1:], strict=False) if b != a + 1)
    return len(nums), breaks, nums[-1] if nums else -1


# Installed before the app loads: frames, long tasks, and when a click reached the page.
PROBE = """
(() => {
  const w = window;
  w.__perf = { frames: 0, longTasks: [], clickAt: null };
  const tick = () => { w.__perf.frames++; requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  try {
    new PerformanceObserver((list) => { for (const e of list.getEntries()) w.__perf.longTasks.push(e.duration); })
      .observe({ type: 'longtask', buffered: true });
  } catch (e) {}
  addEventListener('pointerdown', () => { w.__perf.clickAt = performance.now(); }, true);
})();
"""


def web_gl_counter() -> str:
    """An init script that counts WebGL contexts the page creates and still holds."""
    return """
(() => {
  const original = HTMLCanvasElement.prototype.getContext;
  window.__webgl = { created: 0 };
  HTMLCanvasElement.prototype.getContext = function (kind, ...rest) {
    const ctx = original.call(this, kind, ...rest);
    if (ctx && (kind === 'webgl2' || kind === 'webgl')) {
      if (!this.__counted) { this.__counted = true; window.__webgl.created++; }
    }
    return ctx;
  };
})();
"""


__all__ = ["BATCH", "PROBE", "Flood", "FloodServer", "free_port", "numbered_lines", "sequence_breaks", "web_gl_counter"]
