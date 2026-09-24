"""The harness manager's visible runs: a program in a terminal the operator can open — installs
and sign-ins, which print progress, ask questions and take minutes. (The environment port both the
manager and the staff runtime use is ``runtime.RuntimeEnvironment``.)
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from daedalus.terminals.model import Owner, TerminalSpec

if TYPE_CHECKING:
    from daedalus.terminals.service import Terminals

EXIT_POLL_S = 1.0
OUTPUT_TAIL_BYTES = 8 << 10
"""What of a finished run's output is kept for the record: its last lines are where an installer
says why it failed."""


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


__all__ = ["TerminalRunner"]
