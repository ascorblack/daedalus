"""A terminal daemon in-process: the socket protocol of ``docs/architecture/terminals.md``, with
scripted terminals instead of processes.

It speaks the real framing, handshake, JSON-RPC and event subscription, so the host's client and
service are exercised end to end over a unix socket; what a terminal does is whatever the test says.
Restarting it is a new instance with none of the old terminals, as a restarted daemon is.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class FakeTerminal:
    id: str
    argv: list[str]
    cwd: str
    labels: dict[str, str]
    cols: int = 80
    rows: int = 24
    status: str = "running"
    exit_code: int | None = None
    exit_signal: str | None = None
    created_at: str = field(default_factory=stamp)
    exited_at: str | None = None
    output: bytearray = field(default_factory=bytearray)
    writes: list[dict[str, Any]] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    title: str = ""
    preview: list[Any] | None = None
    pid: int = field(default_factory=lambda: 1000 + secrets.randbelow(30000))

    def info(self, preview_rows: int = 0) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "pid": self.pid, "argv": self.argv, "cwd": self.cwd, "title": self.title, "status": self.status,
            "exit_code": self.exit_code, "exit_signal": self.exit_signal, "created_at": self.created_at, "exited_at": self.exited_at,
            "last_output_at": None, "last_input_at": None, "last_human_input_at": None, "cols": self.cols, "rows": self.rows,
            "size_owner": None, "clients": [], "last_detach_at": None, "keyboard": {"owner": "auto", "until": None},
            "modes": {"alt_screen": False, "bracketed_paste": False, "mouse": False, "app_cursor": False}, "busy": False,
            "last_command": None, "labels": self.labels, "launch_id": "", "sandbox": False, "output_seq": len(self.output),
        }
        if preview_rows and self.preview is not None:
            out["preview"] = self.preview[-preview_rows:]
        return out


class FakePtyd:
    def __init__(self, run_dir: Path, *, env: str = "container", protocol: int = 1) -> None:
        self.run_dir = run_dir
        self.env = env
        self.protocol = protocol
        self.instance = secrets.token_hex(8)
        self.token = secrets.token_hex(32)
        self.terminals: dict[str, FakeTerminal] = {}
        self.events: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, tuple[int, str]] = {}
        """Method → the error its next call answers with, once."""
        self.machine: dict[str, Any] = {"mem_total_bytes": 16 << 30, "mem_available_bytes": 8 << 30, "cpus": 8, "cpu_percent": 10.0, "load1": 1.0, "load5": 1.0, "load15": 1.0}
        self.rss: dict[str, int] = {}
        self.home = "/root"
        self._server: asyncio.AbstractServer | None = None
        self._subscribers: list[tuple[asyncio.StreamWriter, asyncio.Queue[dict[str, Any] | None]]] = []
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- lifecycle ------------------------------------------------------------------------

    async def start(self) -> FakePtyd:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        sock = self.run_dir / "ptyd.sock"
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(sock), limit=(1 << 20) + 64)
        (self.run_dir / "token").write_text(self.token + "\n")
        (self.run_dir / "endpoint").write_text("unix:ptyd.sock")
        return self

    async def stop(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.run_dir / "endpoint").unlink()
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
        self._server = None
        self._subscribers.clear()

    async def restart(self) -> None:
        """A new life: new instance, new token, no terminals, an empty event log."""
        await self.stop()
        self.instance = secrets.token_hex(8)
        self.token = secrets.token_hex(32)
        self.terminals.clear()
        self.events.clear()
        await self.start()

    # -- scripting --------------------------------------------------------------------------

    def emit(self, kind: str, terminal_id: str | None, data: dict[str, Any]) -> dict[str, Any]:
        event = {"seq": len(self.events) + 1, "at": stamp(), "type": kind, "data": data}
        if terminal_id:
            event["terminal_id"] = terminal_id
        self.events.append(event)
        for _, queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def exit(self, terminal_id: str, code: int = 0, signal: str | None = None) -> None:
        term = self.terminals[terminal_id]
        term.status, term.exit_code, term.exit_signal, term.exited_at = "exited", code, signal, stamp()
        self.emit("terminal.exited", terminal_id, {"exit_code": code, "signal": signal, "seq": len(term.output)})

    def spawn(self, terminal_id: str, *, labels: dict[str, str] | None = None, cwd: str = "/tmp") -> FakeTerminal:
        """A terminal the host did not ask for (or whose row it lost)."""
        term = self.terminals[terminal_id] = FakeTerminal(terminal_id, ["/bin/bash", "-l"], cwd, labels or {})
        self.emit("terminal.created", terminal_id, {"pid": term.pid, "argv": term.argv, "cwd": cwd, "labels": term.labels, "launch_id": ""})
        return term

    # -- the protocol -----------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        queue: asyncio.Queue[dict[str, Any] | None] | None = None
        pump: asyncio.Task[None] | None = None
        try:
            channel, payload = await self._read(reader)
            if channel != 0 or payload.strip() != self.token.encode():
                await asyncio.sleep(0.05)
                return
            await self._send(writer, {"jsonrpc": "2.0", "method": "hello", "params": {"version": "fake", "protocol": self.protocol, "instance": self.instance, "env": self.env}})
            while True:
                channel, payload = await self._read(reader)
                if channel != 0:
                    continue
                message = json.loads(payload)
                method, params = message.get("method"), message.get("params") or {}
                self.calls.append((method, params))
                if method == "events.subscribe":
                    after = int(params.get("after_seq") or 0)
                    resync = after > len(self.events)
                    start = 0 if resync else after
                    queue = asyncio.Queue()
                    for event in self.events[start:]:
                        queue.put_nowait(event)
                    self._subscribers.append((writer, queue))
                    await self._send(writer, {"jsonrpc": "2.0", "id": message["id"], "result": {"instance": self.instance, "from_seq": start + 1, "resync": resync}})
                    pump = asyncio.create_task(self._pump(writer, queue))
                    self._tasks.add(pump)
                    continue
                try:
                    result = self._handle(method, params)
                    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                except _RpcFail as exc:
                    reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": exc.code, "message": exc.message}}
                await self._send(writer, reply)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if pump is not None:
                pump.cancel()
            self._subscribers = [(w, q) for w, q in self._subscribers if w is not writer]
            self._writers.discard(writer)
            writer.close()
            if task is not None:
                self._tasks.discard(task)

    async def _pump(self, writer: asyncio.StreamWriter, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        with contextlib.suppress(ConnectionError, asyncio.CancelledError):
            while (event := await queue.get()) is not None:
                await self._send(writer, {"jsonrpc": "2.0", "method": "event", "params": event})

    async def _read(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        length, channel = struct.unpack(">II", await reader.readexactly(8))
        return channel, await reader.readexactly(length - 4)

    async def _send(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        payload = json.dumps(message).encode()
        writer.write(struct.pack(">II", 4 + len(payload), 0) + payload)
        await writer.drain()

    def _term(self, params: dict[str, Any]) -> FakeTerminal:
        term = self.terminals.get(str(params.get("id")))
        if term is None:
            raise _RpcFail(1001, "no such terminal")
        return term

    def _handle(self, method: str, params: dict[str, Any]) -> Any:
        if method in self.fail:
            code, text = self.fail.pop(method)
            raise _RpcFail(code, text)
        if method == "daemon.info":
            running = sum(1 for t in self.terminals.values() if t.status == "running")
            return {"version": "fake", "protocol": self.protocol, "instance": self.instance, "env": self.env, "home": self.home, "shell": "/bin/bash",
                    "capabilities": {"sandbox": "not available in this build", "emulator": "fake@1", "stats": "ok"}, "hooks": {"listen": ""},
                    "counts": {"running": running, "exited": len(self.terminals) - running}, "machine": self.machine}
        if method == "events.unsubscribe":
            return {}
        if method == "terminal.create":
            if params.get("sandbox"):
                raise _RpcFail(1007, "the sandbox is not available in this build")
            if params["id"] in self.terminals:
                raise _RpcFail(1004, "id in use")
            cwd = params.get("cwd") or self.home
            fallback = not Path(cwd).is_dir()
            term = FakeTerminal(params["id"], params.get("argv") or ["/bin/bash", "-l"], self.home if fallback else cwd, dict(params.get("labels") or {}), params.get("cols") or 80, params.get("rows") or 24, title=params.get("title") or "")
            self.terminals[term.id] = term
            self.emit("terminal.created", term.id, {"pid": term.pid, "argv": term.argv, "cwd": term.cwd, "labels": term.labels, "launch_id": ""})
            return {"id": term.id, "pid": term.pid, "cwd": term.cwd, "cwd_fallback": fallback, "shell": "/bin/bash", "created_at": term.created_at}
        if method == "terminal.list":
            ids = params.get("ids")
            return {"terminals": [t.info(int(params.get("preview_rows") or 0)) for t in self.terminals.values() if not ids or t.id in ids]}
        if method == "terminal.get":
            return self._term(params).info()
        if method == "terminal.write":
            term = self._term(params)
            if term.status != "running":
                raise _RpcFail(1002, "exited")
            data = params.get("text") or params.get("paste") or " ".join(params.get("keys") or [])
            term.writes.append(params)
            before = len(term.output)
            return {"bytes": len(str(data).encode()), "seq_before": before, "queued_ms": 0, "delivered_at": stamp()}
        if method == "terminal.resize":
            term = self._term(params)
            term.cols, term.rows = params["cols"], params["rows"]
            return {"cols": term.cols, "rows": term.rows}
        if method == "terminal.signal":
            self._term(params).signals.append(params["signal"])
            return {}
        if method == "terminal.kill":
            term = self._term(params)
            if term.status == "running":
                self.exit(term.id, -1, "SIGHUP")
            return {"exit_code": term.exit_code, "signal": term.exit_signal}
        if method == "terminal.forget":
            term = self._term(params)
            if term.status == "running":
                raise _RpcFail(1004, "still running")
            del self.terminals[term.id]
            return {}
        if method == "terminal.read_output":
            term = self._term(params)
            since = int(params.get("since_seq") or 0)
            chunk = bytes(term.output[since:])
            out = {"from_seq": since, "to_seq": len(term.output), "head_seq": len(term.output), "gap": False}
            if params.get("strip"):
                out["data"] = chunk.decode("utf-8", "replace")
            else:
                out["data_b64"] = base64.b64encode(chunk).decode()
            return out
        if method == "terminal.stats":
            running = [t for t in self.terminals.values() if t.status == "running"]
            return {"at": stamp(), "supported": True, "machine": self.machine,
                    "terminals": [{"id": t.id, "pid": t.pid, "processes": 1, "rss_bytes": self.rss.get(t.id, 50 << 20), "cpu_percent": 2.0} for t in running]}
        raise _RpcFail(-32601, f"method not found: {method}")


class _RpcFail(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = ["FakePtyd", "FakeTerminal"]
