"""A fake pi: the TUI, and — since it cannot run the TypeScript bridge extension — the extension's wire
protocol emulated in its place.

With ``-e <file>`` naming the bridge extension (a file called ``pi_bridge.ts``), the fake behaves as
pi with that extension loaded:

- It listens on the bridge socket: ``$DAEDALUS_PI_SOCKET`` if set, else ``pi.sock`` in
  ``$DAEDALUS_DIAL_DIR`` (where the daemon's ``net.dial`` reaches), else beside the extension file.
  One JSON object per line: ``{"op": "send", "id", "text", "deliverAs"}`` (``steer`` injects after the
  current tool call, ``followUp`` after the turn, none starts a turn when idle and follows up when
  busy), ``{"op": "abort"}``, ``{"op": "state"}`` → ``{"idle", "pending"}``; each answered with
  ``{"ok": true, …}``.
- It posts the extension's events to ``$DAEDALUS_HOOK_URL/pi``: ``ready`` (``sessionId``,
  ``sessionFile``) once the session starts, ``agent_start``, ``tool_call``, ``agent_end``,
  ``agent_settled`` (the turn is over and nothing is pending), and ``input`` (``id``, ``source``,
  ``text``) when a message is taken in — the acknowledgement.
- The extension answers the folder-trust question itself; without it, an untrusted folder asks on
  screen unless ``--approve`` is given.
- The team tools post the same contract as the MCP bridge to ``$DAEDALUS_HOOK_URL/team``.

pi has no permission requests by design: ``perm:`` runs (the fake never runs anything) without
asking, and ``ask:`` is only said. Enter while busy steers. A ``TMUX`` variable in the environment
makes every Enter vanish, which is pi's known failure under a multiplexer and the reason the launch
environment strips it.

The session is JSON lines, version 3, under ``~/.pi/agent/sessions/--<cwd>--/<time>_<id>.jsonl``.
Commands: ``--version``, ``--list-models``, ``auth check --provider X --json --no-refresh`` (with
``--credentials`` it prints a secret and records that it did — nothing may ever ask for that),
``update self``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.support.fake_cli.agent import FakeAgent  # noqa: E402
from tests.support.fake_cli.tui import (  # noqa: E402
    Args,
    Dialog,
    Log,
    Look,
    exit_with,
    installed_version,
    latest_version,
    logged_in,
    new_id,
    now_iso,
    post_json,
    set_version,
    settle,
    state_dir,
    usage_error,
)

DEFAULT_VERSION = "0.84.2"
THINKING = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
FLAGS = {"--session-id": 1, "--extension": 1, "--name": 1, "--model": 1, "--thinking": 1, "--approve": 0, "--version": 0, "--list-models": 0}
ALIASES = {"-e": "--extension", "-v": "--version"}


def session_file(cwd: str, session_id: str) -> Path:
    base = Path(os.environ.get("HOME") or "/tmp") / ".pi" / "agent" / "sessions" / ("--" + cwd.strip("/").replace("/", "-") + "--")
    existing = sorted(base.glob(f"*_{session_id}.jsonl")) if base.exists() else []
    if existing:
        return existing[0]
    return base / f"{now_iso().replace(':', '-').replace('.', '-')}_{session_id}.jsonl"


class FakePi(FakeAgent):
    cli = "pi"
    look = Look("pi", "pi (fake)", prompt="> ", idle_hint="enter send · alt+enter follow-up", busy_hint="esc to abort", busy_word="Working...", alt_screen=False, collapse_chars=1500)

    def __init__(self, args: Args) -> None:
        super().__init__(os.getcwd())
        self.args = args
        thinking = args.get("--thinking")
        if thinking and thinking not in THINKING:
            usage_error("pi", f"invalid thinking level '{thinking}'")
        self.session_id = args.get("--session-id") or new_id()
        self.file = session_file(self.cwd, self.session_id)
        self.bridge = any(Path(e).name == "pi_bridge.ts" for e in args.all("--extension"))
        for extension in args.all("--extension"):
            if not Path(extension).exists():
                usage_error("pi", f"extension not found: {extension}")
        self.first_prompt = args.positional[-1] if args.positional else None
        self.parent: str | None = None
        self.pending_ids: list[tuple[str, str]] = []
        """(text, id) of messages sent through the bridge and not yet taken in."""
        self.follow_ups: list[tuple[str, str]] = []
        self.hook_url = os.environ.get("DAEDALUS_HOOK_URL", "").rstrip("/")
        self.token = os.environ.get("DAEDALUS_HOOK_TOKEN", "")
        self.tui.enter_broken = bool(os.environ.get("TMUX"))
        socket_path = os.environ.get("DAEDALUS_PI_SOCKET") or (str(Path(os.environ["DAEDALUS_DIAL_DIR"]) / "pi.sock") if os.environ.get("DAEDALUS_DIAL_DIR") else "")
        if not socket_path and self.bridge:
            socket_path = str(Path(next(e for e in args.all("--extension") if Path(e).name == "pi_bridge.ts")).parent / "pi.sock")
        self.socket_path = socket_path

    # -- the bridge's side ------------------------------------------------------------------------------

    async def emit(self, event: str, **fields: Any) -> None:
        if not self.bridge or not self.hook_url:
            return
        status, _ = await post_json(f"{self.hook_url}/pi", {"event": event, "at": now_iso(), **fields}, {"Authorization": f"Bearer {self.token}"}, 10)
        self.log("bridge_event", name=event, status=status)

    async def before_ready(self) -> bool:
        trusted = state_dir("pi") / "trusted.json"
        known = json.loads(trusted.read_text()) if trusted.exists() else []
        if self.cwd not in known and not self.bridge and not self.args.has("--approve"):
            future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            self.tui.open_dialog(Dialog("trust", f"Trust {self.cwd}?", ["pi will run tools in this folder."], ["Yes", "No"], on_choose=lambda i: settle(future, i), on_escape=lambda: settle(future, 1)))
            if await future != 0:
                await self.quit(1)
                return False
            trusted.write_text(json.dumps([*known, self.cwd]))
        if self.bridge and self.socket_path:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.socket_path)
            Path(self.socket_path).parent.mkdir(parents=True, exist_ok=True)
            await asyncio.start_unix_server(self.bridge_client, path=self.socket_path)
        return True

    async def on_ready(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        if not self.file.exists():
            self.write({"type": "session", "version": 3, "id": self.session_id, "timestamp": now_iso(), "cwd": self.cwd})
        await self.emit("ready", sessionId=self.session_id, sessionFile=str(self.file))

    async def bridge_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                reply = await self.bridge_op(message)
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()

    async def bridge_op(self, message: dict[str, Any]) -> dict[str, Any]:
        op = message.get("op")
        if op == "send":
            text, ident, deliver = str(message.get("text", "")), str(message.get("id", "")), message.get("deliverAs")
            self.log("submitted", text=text, busy=self.busy, via="bridge", deliverAs=deliver)
            if not self.busy and deliver != "followUp":
                self.pending_ids.append((text, ident))
                await self.submit(text)
            elif deliver == "steer":
                self.pending_ids.append((text, ident))
                self.injections.append(text)
            else:
                self.follow_ups.append((text, ident))
            return {"ok": True, "id": ident}
        if op == "abort":
            if self.busy:
                assert self.turn_task is not None
                self.log("interrupt", via="bridge")
                self.turn_task.cancel()
            return {"ok": True}
        if op == "state":
            return {"ok": True, "idle": not self.busy, "pending": bool(self.injections or self.follow_ups)}
        return {"ok": False, "error": f"unknown op {op}"}

    # -- the session file ---------------------------------------------------------------------------------

    def write(self, entry: dict[str, Any]) -> None:
        with self.file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def message(self, message: dict[str, Any]) -> None:
        ident = new_id().replace("-", "")[:8]
        self.write({"type": "message", "id": ident, "parentId": self.parent, "timestamp": now_iso(), "message": message})
        self.parent = ident

    # -- the turn -------------------------------------------------------------------------------------------

    async def on_prompt(self, text: str, *, queued: bool) -> None:
        source, ident = "interactive", ""
        for index, (pending, pending_id) in enumerate(self.pending_ids):
            if pending == text:
                source, ident = "extension", pending_id
                del self.pending_ids[index]
                break
        self.message({"role": "user", "content": [{"type": "text", "text": text}], "timestamp": now_ms()})
        await self.emit("input", id=ident, source=source, text=text)

    async def on_turn_started(self) -> None:
        await self.emit("agent_start")

    async def on_assistant(self, text: str) -> None:
        self.message({"role": "assistant", "content": [{"type": "text", "text": text}], "model": self.args.get("--model", "fake-model"), "provider": "fake", "stopReason": "stop",
                      "usage": {"input": 800, "output": 40, "cacheRead": 300, "cacheWrite": 0, "totalTokens": 1140, "cost": {"total": 0.001}}, "timestamp": now_ms()})

    async def on_tool_start(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None:
        self.message({"role": "assistant", "content": [{"type": "toolCall", "id": tool_id, "name": name.lower(), "arguments": tool_input}], "stopReason": "toolUse", "timestamp": now_ms()})
        await self.emit("tool_call", toolName=name.lower(), toolCallId=tool_id, input=tool_input)

    async def on_tool_end(self, name: str, tool_input: dict[str, Any], tool_id: str, output: str, ok: bool) -> None:
        self.message({"role": "toolResult", "toolCallId": tool_id, "toolName": name.lower(), "content": [{"type": "text", "text": output}], "isError": not ok, "timestamp": now_ms()})

    async def _turn_end(self) -> None:
        await self.emit("agent_end")
        if self.follow_ups:
            text, ident = self.follow_ups.pop(0)
            self.pending_ids.append((text, ident))
            self.injections.append(text)
            return
        if not self.injections:
            await self.emit("agent_settled")

    async def on_turn_completed(self) -> None:
        if self.faults.no_stop_hook:
            self.log("stop_hook_suppressed")
            return
        await self._turn_end()

    async def on_turn_failed(self, kind: str) -> None:
        self.message({"role": "assistant", "content": [], "stopReason": "error", "errorMessage": kind, "timestamp": now_ms()})
        await self._turn_end()

    async def on_turn_cancelled(self) -> None:
        self.message({"role": "assistant", "content": [], "stopReason": "aborted", "timestamp": now_ms()})
        await self._turn_end()

    async def permission(self, tool: str, tool_input: dict[str, Any], summary: str, tool_id: str) -> str:
        return "allow_once"

    async def question(self, question: str, options: list[str], tool_id: str) -> str:
        await self.assistant(f"{question} ({' / '.join(options)})")
        return "(asked in text; pi has no question tool)"

    async def team_tool(self, name: str, arguments: dict[str, Any], tool_id: str) -> str:
        if not self.bridge:
            return f"error: no tool {name}"
        await self.on_tool_start(name, arguments, tool_id)
        if name == "Report":
            status, _ = await post_json(f"{self.hook_url}/team", {"tool": "report", **arguments, "artifacts": arguments.get("artifacts") or []}, {"Authorization": f"Bearer {self.token}"}, 10)
            result = "recorded" if 200 <= status < 300 else f"the report was not delivered ({status})"
        else:
            hold = int(os.environ.get("DAEDALUS_ASK_HOLD_MS") or 300_000)
            status, body = await post_json(f"{self.hook_url}/team", {"tool": "ask", **arguments, "daedalus_hold_ms": hold}, {"Authorization": f"Bearer {self.token}"}, hold / 1000 + 30)
            if status == 200 and body:
                result = str(body.get("answer")) if isinstance(body, dict) and "answer" in body else json.dumps(body) if not isinstance(body, str) else body
            else:
                result = "No answer yet. Continue with what the brief allows, or call Report with kind needs_input and stop."
        await self.on_tool_end(name, arguments, tool_id, result, True)
        return result

    async def on_session_end(self) -> None:
        if self.socket_path and self.bridge:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.socket_path)


def now_ms() -> int:
    return int(time.time() * 1000)


def command(argv: list[str]) -> int | None:
    if argv[:1] in (["--version"], ["-v"]):
        print(installed_version("pi", DEFAULT_VERSION))
        return 0
    if argv[:1] == ["--list-models"]:
        print("provider   model\nanthropic  claude-sonnet-4\nopenai     gpt-5")
        return 0
    if argv[:2] == ["auth", "check"]:
        provider = argv[argv.index("--provider") + 1] if "--provider" in argv else "anthropic"
        if "--credentials" in argv:
            Log("pi")("credentials_printed", provider=provider)
            print(json.dumps({"provider": provider, "credentials": {"access": "fake-secret-that-must-never-be-read"}}))
            return 0
        ok = logged_in("pi")
        print(json.dumps({"provider": provider, "authenticated": ok}) if "--json" in argv else ("authenticated" if ok else "not authenticated"))
        return 0 if ok else 1
    if argv[:2] == ["update", "self"]:
        current, latest = installed_version("pi", DEFAULT_VERSION), latest_version("pi", DEFAULT_VERSION)
        if current != latest:
            set_version("pi", latest)
        print(f"pi {current} -> {latest}" if current != latest else f"pi is up to date ({current})")
        return 0
    return None


def main() -> None:
    argv = sys.argv[1:]
    code = command(argv)
    if code is not None:
        raise SystemExit(code)
    agent = FakePi(Args("pi", argv, flags=FLAGS, aliases=ALIASES))
    exit_with(agent.main)


if __name__ == "__main__":
    main()
