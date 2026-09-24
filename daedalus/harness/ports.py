"""The adapter's environment port, and the manager's visible runs, over the terminals service.

``ServiceEnvironmentPort`` is the production ``EnvironmentPort``: every program run and file read
goes through the terminals service, so it is audited and fenced by the daemon's lists like any other
side-channel call. ``TerminalRunner`` runs a program in a terminal the operator can open — installs
and sign-ins, which print progress, ask questions and take minutes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from daedalus.harness.contract import Environment, EnvironmentUnavailable, ExecResult, ProgramNotFound
from daedalus.terminals.model import EnvUnavailable, NotFound, Owner, TerminalSpec

if TYPE_CHECKING:
    from daedalus.terminals.service import Terminals

READ_CHUNK = 640 << 10
"""The most one ``fs.read`` reply carries; a longer read continues by offset."""
EXIT_POLL_S = 1.0
OUTPUT_TAIL_BYTES = 8 << 10
"""What of a finished run's output is kept for the record: its last lines are where an installer
says why it failed."""


class ServiceEnvironmentPort:
    """``EnvironmentPort`` for one environment of the terminals service."""

    def __init__(self, terminals: Terminals, env: Environment, *, home: str = "", actor: str = "harness") -> None:
        self.terminals = terminals
        self._env = env
        self._home = home
        self.actor = actor

    @property
    def name(self) -> Environment:
        return self._env

    @property
    def home(self) -> str:
        return self._home

    async def run(self, argv: list[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None, timeout: float = 30.0) -> ExecResult:
        try:
            result = await self.terminals.exec_run(self._env, list(argv), cwd=cwd, env_vars=dict(env) if env else None, timeout=timeout, actor=self.actor)
        except NotFound as exc:
            raise ProgramNotFound(exc.message) from None
        except EnvUnavailable as exc:
            raise EnvironmentUnavailable(exc.message) from None
        return ExecResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr, timed_out=result.timed_out, path=result.path)

    async def read(self, path: str, *, offset: int = 0, limit: int = 1 << 20) -> bytes:
        out = bytearray()
        while len(out) < limit:
            chunk = await self.terminals.fs_read(self._env, path, offset=offset + len(out), max_bytes=min(limit - len(out), READ_CHUNK))
            out += chunk.data
            if chunk.eof or not chunk.data:
                break
        return bytes(out)

    async def stat(self, path: str) -> Mapping[str, Any] | None:
        result = await self.terminals.fs_stat(self._env, path)
        return result if result.get("exists") else None

    async def list(self, path: str) -> list[str]:
        result = await self.terminals.fs_list(self._env, path)
        return [str(entry.get("name") or "") for entry in result.get("entries") or [] if entry.get("name")]


class TerminalRunner:
    """Runs a program in a free terminal the operator can open, and waits for it to end.

    Started as the operator's own terminal: pressing Install or Sign in is a person's choice, so it is
    not queued behind the machine's cap the way an agent's launch is.
    """

    def __init__(self, terminals: Terminals) -> None:
        self.terminals = terminals

    async def start(self, env: str, argv: list[str], *, title: str) -> str:
        view = await self.terminals.create(
            TerminalSpec(env=env, owner=Owner("free"), argv=list(argv), title=title, cols=120, rows=32, profile="shell", created_by="operator"),
            confirm_over_cap=True,
        )
        return str(view["id"])

    async def wait(self, terminal_id: str, *, timeout: float) -> tuple[int | None, str]:
        """``(exit code, the end of its output)``; the code is None when it did not end in time or the
        terminal was lost, and then the terminal is left running for the operator to look at."""
        deadline = time.monotonic() + timeout
        view = await self.terminals.get(terminal_id)
        while view["status"] == "running" and time.monotonic() < deadline:
            await asyncio.sleep(EXIT_POLL_S)
            view = await self.terminals.get(terminal_id)
        tail = ""
        try:
            head = (await self.terminals.read_output(terminal_id, since_seq=0, max_bytes=1)).head_seq
            chunk = await self.terminals.read_output(terminal_id, since_seq=max(0, head - OUTPUT_TAIL_BYTES), max_bytes=OUTPUT_TAIL_BYTES)
            tail = chunk.data if isinstance(chunk.data, str) else chunk.data.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the output is a courtesy; the exit code is the answer
            tail = ""
        if view["status"] != "exited":
            return None, tail
        code = view.get("exit_code")
        return (int(code) if code is not None else None), tail


__all__ = ["ServiceEnvironmentPort", "TerminalRunner"]
