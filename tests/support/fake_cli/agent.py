"""The turn engine every fake runs: a prompt becomes a turn, a turn runs its scripted steps, and each
step reports through the hooks a CLI module overrides — which is where the fakes differ from one
another (Claude posts hooks, Codex notifies app-server clients, OpenCode publishes server events, pi
posts bridge events, Grok appends to its session files).

Only one turn runs at a time. What Enter does while one runs is the CLI's quirk: Claude and
OpenCode hold the message and inject it at the next tool boundary, Codex steers it into the turn,
pi steers it after the current tool, Grok cancels the turn and sends the new message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from typing import Any

from tests.support.fake_cli.tui import (
    Dialog,
    Faults,
    Log,
    Look,
    Step,
    Tui,
    ask_arguments,
    new_id,
    pause,
    report_arguments,
    scaled,
    script_of,
)


class TurnFailed(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


FAILURE_TEXT = {
    "rate_limit": "API Error: rate limit reached. Try again later.",
    "authentication_failed": "API Error: authentication failed. Please sign in again.",
    "overloaded": "API Error: the service is overloaded.",
}


class FakeAgent:
    """Subclasses set ``cli`` and ``look`` and override the ``on_*`` hooks they have channels for."""

    cli = "fake"
    look = Look("fake", "fake agent")
    interrupted_line = "⎿ Interrupted by user"

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.faults = Faults.from_env()
        self.log = Log(self.cli)
        self.tui = Tui(self.look, faults=self.faults, log=self.log)
        self.tui.submit = self.submit
        self.tui.escape = self.escape
        self.tui.quit = self.quit
        self.tui.busy_enter = self.busy_enter
        self.turn_task: asyncio.Task[None] | None = None
        self.turns = 0
        self.injections: list[str] = []
        self.done = asyncio.Event()
        self.exit_code = 0
        self.first_prompt: str | None = None
        self.interrupted_in_tool = False
        self.in_tool = False
        """A tool is running: an interrupt now is an interrupt of the tool, which some CLIs record apart."""

    # -- life ----------------------------------------------------------------------------------

    async def main(self) -> int:
        self.tui.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGHUP, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, lambda: self.tui.spawn(self.quit(129)))
        try:
            self.tui.say(f"cwd: {self.cwd}")
            if not await self.before_ready():
                await self.done.wait()
                return self.exit_code
            if self.faults.slow_ready_ms:
                self.tui.status = "Starting…"
                self.tui.render()
                await asyncio.sleep(self.faults.slow_ready_ms / 1000)
                self.tui.status = ""
            self.tui.ready = True
            self.tui.render()
            self.log("ready")
            await self.on_ready()
            if self.first_prompt:
                self.log("submitted", text=self.first_prompt, busy=False, via="argv")
                await self.submit(self.first_prompt)
            await self.done.wait()
            return self.exit_code
        finally:
            self.tui.restore()

    async def before_ready(self) -> bool:
        """Dialogs before the composer (trust, sign-in). False: the CLI will not get ready."""
        return True

    async def quit(self, code: int = 0) -> None:
        if self.done.is_set():
            return
        if self.turn_task is not None and not self.turn_task.done():
            self.turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.turn_task
        with contextlib.suppress(Exception):
            await self.on_session_end()
        self.log("exit", code=code)
        self.exit_code = code
        self.tui.exiting = True
        self.done.set()

    def crash(self) -> None:
        """Die the way a crashed CLI does: no goodbye, no hook, a non-zero code."""
        self.log("exit", code=3, crash=True)
        self.tui.restore()
        os._exit(3)

    # -- turns ---------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.turn_task is not None and not self.turn_task.done()

    async def submit(self, text: str) -> None:
        if self.busy:
            await self.busy_enter(text)
            return
        self.turn_task = asyncio.ensure_future(self.turn(text))

    async def busy_enter(self, text: str) -> None:
        """Default: hold the message and inject it at the next tool boundary (Claude's queue)."""
        self.injections.append(text)
        self.tui.queued.append(text)
        self.tui.render()

    async def inject_pending(self) -> None:
        while self.injections:
            text = self.injections.pop(0)
            with contextlib.suppress(ValueError):
                self.tui.queued.remove(text)
            self.tui.say(f"> {text}")
            await self.on_prompt(text, queued=True)
            # The model answers what was injected inside the same turn, as it does in the real CLIs.
            for step in script_of(text):
                await self.step(step)

    async def escape(self) -> None:
        if self.busy:
            assert self.turn_task is not None
            self.log("interrupt", key="esc")
            self.interrupted_in_tool = self.in_tool
            self.turn_task.cancel()

    async def turn(self, prompt: str) -> None:
        steps = script_of(prompt)
        silent = any(step.kind == "silent" for step in steps)
        self.tui.busy = True
        self.tui.status = f"✻ {self.look.busy_word} ({self.look.busy_hint})"
        self.tui.say(f"> {prompt if len(prompt) < 200 else prompt[:200] + '…'}")
        outcome = "completed"
        try:
            await self.on_prompt(prompt, queued=False)
            await self.on_turn_started()
            for step in steps:
                await self.step(step)
                await self.inject_pending()
            # A message queued during the last step becomes a turn of its own.
        except asyncio.CancelledError:
            outcome = "cancelled"
        except TurnFailed as failure:
            outcome = "failed"
            self.tui.say(f"⎿ {FAILURE_TEXT.get(failure.kind, 'API Error: ' + failure.kind)}")
            with contextlib.suppress(Exception):
                await self.on_turn_failed(failure.kind)
        finally:
            self.tui.busy = False
            self.tui.status = ""
            self.tui.render()
        if outcome == "cancelled":
            self.tui.say(self.interrupted_line)
            await self.on_turn_cancelled()
        elif outcome == "completed" and not silent:
            await self.on_turn_completed()
        # After every hook and event of the turn: the one moment a test can say "nothing more about
        # this turn is coming" without guessing a delay.
        self.log("turn_ended", outcome=outcome, silent=silent)
        self.turns += 1
        if self.faults.exit_after and self.turns >= self.faults.exit_after:
            self.crash()
        if self.injections and not self.done.is_set():
            text = self.injections.pop(0)
            with contextlib.suppress(ValueError):
                self.tui.queued.remove(text)
            self.turn_task = asyncio.ensure_future(self.turn(text))

    async def step(self, step: Step) -> None:
        kind, arg = step.kind, step.arg
        if kind == "echo":
            await pause(0.3)
            await self.assistant(arg)
        elif kind == "perm":
            tool_id = "toolu_" + new_id().replace("-", "")[:24]
            decision = await self.permission("Bash", {"command": arg, "description": f"Run {arg}"}, arg, tool_id)
            if decision.startswith("allow"):
                await self.tool("Bash", {"command": arg}, tool_id, output=f"(the fake did not run {arg})")
                await self.assistant(f"Ran {arg}.")
            else:
                await self.on_tool_denied("Bash", {"command": arg}, tool_id)
                await self.assistant(f"Understood, I did not run {arg}.")
        elif kind == "ask":
            spec = ask_arguments(arg)
            tool_id = "toolu_" + new_id().replace("-", "")[:24]
            answer = await self.question(spec["question"], spec.get("options", []), tool_id)
            await self.assistant(f"You chose: {answer}")
        elif kind == "cat":
            # A file the brief names, opened where this CLI runs: what proves a handed-over file arrived.
            tool_id = "toolu_" + new_id().replace("-", "")[:24]
            target = arg if arg.startswith("/") else os.path.join(self.cwd, arg)
            try:
                with open(target, encoding="utf-8") as handle:
                    first = handle.readline().strip()
            except OSError as exc:
                first = f"cannot open: {exc.strerror}"
            self.log("opened", path=target, first=first)
            await self.tool("Read", {"file_path": target}, tool_id, output=first)
            await self.assistant(f"read {arg}: {first}")
        elif kind == "fail":
            await pause(0.2)
            raise TurnFailed(arg or "unknown")
        elif kind == "slow":
            seconds = float(arg or 1)
            loop = asyncio.get_running_loop()
            end = loop.time() + scaled(seconds)
            n = 0
            while loop.time() < end:
                n += 1
                tool_id = "toolu_" + new_id().replace("-", "")[:24]
                await self.tool("Read", {"file_path": f"{self.cwd}/file{n}.txt"}, tool_id, output=f"line {n}", duration=0.5)
                await self.inject_pending()
            await self.assistant(f"Worked through {n} files.")
        elif kind == "silent":
            await pause(3)
            self.tui.say("(worked quietly)")
        elif kind in ("report", "askorch"):
            name = "Report" if kind == "report" else "AskOrchestrator"
            arguments = report_arguments(arg) if kind == "report" else ask_arguments(arg)
            tool_id = "toolu_" + new_id().replace("-", "")[:24]
            result = await self.team_tool(name, arguments, tool_id)
            await self.assistant(f"{name}: {result}")
        elif kind == "mcp":
            server, _, rest = arg.partition(":")
            name, _, raw = rest.partition(":")
            tool_id = "toolu_" + new_id().replace("-", "")[:24]
            result = await self.mcp_tool(server, name, json.loads(raw or "{}"), tool_id)
            self.log("mcp_result", server=server, tool=name, result=result)
            await self.assistant(f"{name}: {result}")

    async def tool(self, name: str, tool_input: dict[str, Any], tool_id: str, *, output: str, duration: float = 0.2) -> None:
        self.tui.say(f"● {name}({summary_of(tool_input)})")
        await self.on_tool_start(name, tool_input, tool_id)
        self.in_tool = True
        try:
            await pause(duration)
        finally:
            self.in_tool = False
        self.tui.say(f"  ⎿ {output}")
        await self.on_tool_end(name, tool_input, tool_id, output, True)

    async def assistant(self, text: str) -> None:
        self.tui.say(f"● {text}")
        await self.on_assistant(text)

    # -- what a CLI overrides ---------------------------------------------------------------------

    async def on_ready(self) -> None: ...

    async def on_prompt(self, text: str, *, queued: bool) -> None: ...

    async def on_turn_started(self) -> None: ...

    async def on_assistant(self, text: str) -> None: ...

    async def on_tool_start(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None: ...

    async def on_tool_end(self, name: str, tool_input: dict[str, Any], tool_id: str, output: str, ok: bool) -> None: ...

    async def on_tool_denied(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None: ...

    async def on_turn_completed(self) -> None: ...

    async def on_turn_failed(self, kind: str) -> None: ...

    async def on_turn_cancelled(self) -> None: ...

    async def on_session_end(self) -> None: ...

    async def mcp_tool(self, server: str, name: str, arguments: dict[str, Any], tool_id: str) -> str:
        """Any MCP server's tool; a CLI that starts none has none."""
        return f"error: no MCP server {server}"

    async def team_tool(self, name: str, arguments: dict[str, Any], tool_id: str) -> str:
        return "error: this CLI has no team tools"

    async def permission(self, tool: str, tool_input: dict[str, Any], summary: str, tool_id: str) -> str:
        """Ask on screen; ``allow_once``, ``allow_always`` or ``deny``."""
        return await self.permission_dialog(tool, summary, ["Yes", f"Yes, and don't ask again for {summary.split()[0] if summary else tool} commands", "No, and tell me what to do differently (esc)"], ["allow_once", "allow_always", "deny"])

    async def permission_dialog(self, tool: str, summary: str, options: list[str], meanings: list[str], *, selected: int = 0, title: str = "", body: list[str] | None = None, footer: str = "") -> str:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def choose(index: int) -> None:
            if not future.done():
                future.set_result(meanings[index])

        def escape() -> None:
            if not future.done():
                future.set_result("deny")

        dialog = Dialog("permission", title or f"{tool} command", body if body is not None else [f"  {summary}", "Do you want to proceed?"], options, selected=selected, on_choose=choose, on_escape=escape, data={"tool": tool, "summary": summary})
        if footer:
            dialog.footer = footer
        self.tui.open_dialog(dialog)
        try:
            return await future
        finally:
            if self.tui.dialog is not None and self.tui.dialog.kind == "permission":
                self.tui.close_dialog()

    async def question(self, question: str, options: list[str], tool_id: str) -> str:
        return await self.question_dialog(question, options)

    async def question_dialog(self, question: str, options: list[str]) -> str:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        choices = [*options, "Type something."] if options else ["Type something."]

        def choose(index: int) -> None:
            if not future.done():
                future.set_result(choices[index])

        def escape() -> None:
            if not future.done():
                future.set_result("(no answer)")

        self.tui.open_dialog(Dialog("question", question, [], choices, on_choose=choose, on_escape=escape, data={"question": question}))
        try:
            return await future
        finally:
            if self.tui.dialog is not None and self.tui.dialog.kind == "question":
                self.tui.close_dialog()


def summary_of(tool_input: dict[str, Any]) -> str:
    for key in ("command", "file_path", "question", "pattern"):
        if key in tool_input:
            return str(tool_input[key])[:80]
    return ""


__all__ = ["FAILURE_TEXT", "FakeAgent", "TurnFailed", "summary_of"]
