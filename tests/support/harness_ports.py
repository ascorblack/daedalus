"""The adapter's two ports, ``TerminalPort`` and ``EnvironmentPort``, over one daemon connection.

The staff runtime implements them over the terminals service (its audit, its cap, its database);
these go straight to a daemon's socket, which is all an adapter test needs: a real CLI (or a fake
one) in a real terminal behind the real protocol, with nothing of the host in between. They also
show the mapping from the ports to the daemon's methods in one place.

``Rig`` puts the pieces together for a test: a ``LivePtyd`` with the fake CLIs on its ``PATH`` in
a temporary home, a connected client, the event stream, and helpers to register a launch and start
a terminal in it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from daedalus.harness.contract import Environment, ExecResult
from daedalus.terminals.client import PtydClient
from tests.support import fake_cli
from tests.support.live_ptyd import LivePtyd


class PtydTerminalPort:
    """``TerminalPort`` for one terminal: every write names its origin, as the host's writes do."""

    def __init__(self, client: PtydClient, terminal_id: str, *, env: Environment = "container", actor: str = "adapter", launch_id: str = "") -> None:
        self.client = client
        self._id = terminal_id
        self._env = env
        self.actor = actor
        self.launch_id = launch_id

    @property
    def id(self) -> str:
        return self._id

    @property
    def env(self) -> Environment:
        return self._env

    async def write(self, *, text: str | None = None, paste: str | None = None, keys: list[str] | None = None) -> None:
        params: dict[str, Any] = {"id": self._id, "origin": {"kind": "agent", "actor": self.actor, "launch_id": self.launch_id}}
        if text is not None:
            params["text"] = text
        elif paste is not None:
            params["paste"] = paste
        elif keys is not None:
            params["keys"] = keys
        await self.client.call("terminal.write", params, timeout=60)

    async def screen(self, *, scrollback: int = 0) -> str:
        result = await self.client.call("terminal.read_screen", {"id": self._id, "format": "text", "scrollback": scrollback})
        lines = list(result.get("lines") or [])
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    async def modes(self) -> Mapping[str, bool]:
        info = await self.client.call("terminal.get", {"id": self._id})
        return {str(k): bool(v) for k, v in (info.get("modes") or {}).items()}

    async def wait_for(self, *, regex: str | None = None, idle_ms: int | None = None, timeout: float) -> bool:
        params: dict[str, Any] = {"id": self._id, "timeout_ms": int(timeout * 1000)}
        if regex is not None:
            params["regex"] = regex
        if idle_ms is not None:
            params["idle_ms"] = idle_ms
        result = await self.client.call("terminal.wait_for", params, timeout=timeout + 10)
        return result.get("matched") in ("regex", "idle")


class PtydEnvironmentPort:
    """``EnvironmentPort`` for the daemon's environment."""

    def __init__(self, client: PtydClient, *, env: Environment = "container") -> None:
        self.client = client
        self._env = env

    @property
    def name(self) -> Environment:
        return self._env

    async def run(self, argv: list[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None, timeout: float = 30.0) -> ExecResult:
        params: dict[str, Any] = {"argv": list(argv), "timeout_ms": int(timeout * 1000)}
        if cwd:
            params["cwd"] = cwd
        if env:
            params["env"] = dict(env)
        result = await self.client.call("exec.run", params, timeout=timeout + 15)
        return ExecResult(exit_code=int(result["exit_code"]), stdout=str(result["stdout"]), stderr=str(result["stderr"]), timed_out=bool(result["timed_out"]))

    async def read(self, path: str, *, offset: int = 0, limit: int = 1 << 20) -> bytes:
        out = bytearray()
        while len(out) < limit:
            result = await self.client.call("fs.read", {"path": path, "offset": offset + len(out), "max": min(limit - len(out), 640 << 10)})
            out += base64.b64decode(result["data_b64"])
            if result.get("eof") or not result["data_b64"]:
                break
        return bytes(out)

    async def stat(self, path: str) -> Mapping[str, Any] | None:
        result = await self.client.call("fs.stat", {"path": path})
        return result if result.get("exists") else None

    async def list(self, path: str) -> list[str]:
        result = await self.client.call("fs.list", {"path": path})
        return [str(entry["name"]) for entry in result.get("entries") or []]


class Rig:
    """A daemon with fake CLIs, a client and its events, in a temporary home. Use as
    ``async with Rig() as rig:``; everything it started is stopped and removed at the end.

    ``extra_env`` reaches every program the daemon starts (``FAKE_CLI_TIME_SCALE``,
    ``FAKE_CLI_FAULTS``…); ``log`` is the fakes' ``FAKE_CLI_LOG``.
    """

    def __init__(self, *, extra_env: Mapping[str, str] | None = None, input_idle_ms: int = 10_000, launch_grace_s: float = 30.0, ptyd_bin: Path | None = None) -> None:
        # Short, because a unix socket path is limited to 108 bytes and dial sockets live below it.
        self.root = Path(tempfile.mkdtemp(prefix="ptyd-"))
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.work = self.root / "work"
        self.log = self.root / "fake-cli.jsonl"
        for directory in (self.home, self.work):
            directory.mkdir()
        fake_cli.install(self.bin, ptyd=ptyd_bin)
        env = {"FAKE_CLI_LOG": str(self.log), "FAKE_CLI_TIME_SCALE": "0.05", **(extra_env or {})}
        self.ptyd = LivePtyd(self.root / "run", home=self.home, bin_dir=self.bin, base_env=env, input_idle_ms=input_idle_ms, launch_grace_s=launch_grace_s)
        self.events: list[dict[str, Any]] = []
        self._event = asyncio.Event()
        self.client = PtydClient("container", self.root / "run", on_notification=self._on_event)
        self.env_port = PtydEnvironmentPort(self.client)

    async def __aenter__(self) -> Rig:
        await self.ptyd.start()
        self.ptyd.allow_root(self.root)
        await self.client.connect()
        await self.client.call("events.subscribe", {"after_seq": 0})
        return self

    async def __aexit__(self, *exc: object) -> None:
        with contextlib.suppress(Exception):
            await self.client.close()
        await self.ptyd.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    async def _on_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "event":
            self.events.append(params)
            self._event.set()
            self._event = asyncio.Event()

    async def event(self, kind: str, *, where: Mapping[str, Any] | None = None, timeout: float = 30.0, after: int = 0) -> dict[str, Any]:
        """The first event of ``kind`` (past index ``after``) whose ``data`` has the ``where`` values."""
        async with asyncio.timeout(timeout):
            while True:
                for event in self.events[after:]:
                    data = event.get("data") or {}
                    if event["type"] == kind and all(data.get(k) == v for k, v in (where or {}).items()):
                        return event
                await self._event.wait()

    def hooks(self, name: str | None = None) -> list[dict[str, Any]]:
        """Hook events so far, oldest first, optionally of one hook name."""
        return [e["data"] for e in self.events if e["type"] == "hook" and (name is None or e["data"].get("name") == name)]

    async def register(self, *, files: Mapping[str, bytes] | None = None, ports: list[int] | None = None, hold_max_ms: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"launch_id": "l" + secrets.token_hex(6), "files": {k: base64.b64encode(v).decode() for k, v in (files or {}).items()}}
        if ports:
            params["ports"] = ports
        if hold_max_ms:
            params["hold_max_ms"] = hold_max_ms
        result: dict[str, Any] = await self.client.call("hooks.register_launch", params)
        return result

    async def spawn(self, argv: list[str], *, cwd: Path | str | None = None, env: Mapping[str, str] | None = None, launch_id: str = "", cols: int = 100, rows: int = 30) -> PtydTerminalPort:
        terminal_id = "t" + secrets.token_hex(6)
        params: dict[str, Any] = {"id": terminal_id, "argv": argv, "cwd": str(cwd or self.work), "cols": cols, "rows": rows, "env": dict(env or {})}
        if launch_id:
            params["launch_id"] = launch_id
        await self.client.call("terminal.create", params)
        return PtydTerminalPort(self.client, terminal_id, launch_id=launch_id)

    async def screen_until(self, term: PtydTerminalPort, regex: str, *, timeout: float = 30.0) -> str:
        """The screen once ``regex`` is on it; fails with the screen as it was."""
        if not await term.wait_for(regex=regex, timeout=timeout):
            raise AssertionError(f"{regex!r} never appeared; the screen:\n{await term.screen()}")
        return await term.screen()


__all__ = ["PtydEnvironmentPort", "PtydTerminalPort", "Rig"]
