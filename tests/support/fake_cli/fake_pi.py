"""A fake pi: the TUI, and — since it cannot run the TypeScript bridge extension — the extension's wire
protocol emulated in its place, in the shapes recorded from pi 0.84.2 running the real bridge
(``recorded/pi``).

With ``-e <file>`` naming the bridge extension (a file called ``pi_bridge.ts``), the fake behaves as
pi with that extension loaded:

- It listens on ``$DAEDALUS_DIAL_DIR/pi.sock`` — where the terminal daemon's ``net.dial`` reaches —
  for one JSON object per line: ``{"op": "send", "id", "text", "deliverAs"?}`` (idle: a new run;
  busy: ``steer`` goes in after the current tool call, anything else follows the run inside the same
  run), ``{"op": "abort"}``, ``{"op": "state"}`` and ``{"op": "shutdown"}``, each answered with one line.
- It posts the extension's events to ``$DAEDALUS_HOOK_URL/pi``, in order: ``ready`` (``reason``,
  ``sessionId``, ``sessionFile``, ``cwd``, ``model`` — empty when no provider is signed in),
  ``input`` (``id``, ``source``, ``text``, ``streamingBehavior``) the moment a prompt is taken in or
  queued, ``agent_start``, ``tool_start``/``tool_end``, then once per run ``agent_end`` and
  ``agent_settled`` (``lastMessage``, ``stopReason`` ``stop``/``aborted``/``error``,
  ``errorMessage``), and ``session_end`` (``reason: quit``). A team ``hello`` precedes ``ready``.
- An abort puts a steer that had not gone in yet back into the composer, unsent (measured).
- The extension answers the folder-trust question itself; without it, a folder with ``.pi`` settings
  asks on screen ("Trust project folder?") unless ``--approve`` is given.
- The team tools post the contract of the daemon's ``team-mcp`` to ``$DAEDALUS_HOOK_URL/team``:
  held (``wait_ms``), with a ``call_id``, the reply's ``text`` as the result.
- Signed out (``FAKE_PI_LOGGED_IN=0``) it starts without a model, says so, and answers every prompt
  with pi's error instead of a run.

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

from tests.support.fake_cli.agent import FakeAgent, TurnFailed  # noqa: E402
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
    script_of,
    set_version,
    settle,
    state_dir,
    usage_error,
)

DEFAULT_VERSION = "0.84.2"
THINKING = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
FLAGS = {
    "--session-id": 1, "--extension": 1, "--name": 1, "--model": 1, "--thinking": 1, "--approve": 0, "--version": 0, "--list-models": 0,
    "--append-system-prompt": 1, "--skill": 1,
}
ALIASES = {"-e": "--extension", "-v": "--version", "-n": "--name", "-a": "--approve"}
EXPIRED = "Pending: nobody has answered yet. The answer will arrive as a message; carry on with what the brief allows meanwhile, or end your turn and wait for it. Do not ask the same question again."
GONE = "this session is no longer connected to its team; nobody received the call"
NO_KEY = "Error: No API key found for the selected model."


def session_file(cwd: str, session_id: str) -> Path:
    base = Path(os.environ.get("HOME") or "/tmp") / ".pi" / "agent" / "sessions" / ("--" + cwd.strip("/").replace("/", "-") + "--")
    existing = sorted(base.glob(f"*_{session_id}.jsonl")) if base.exists() else []
    if existing:
        return existing[0]
    return base / f"{now_iso().replace(':', '-').replace('.', '-')}_{session_id}.jsonl"


class FakePi(FakeAgent):
    cli = "pi"
    look = Look("pi", "pi v0.84.2 (fake)", prompt="", idle_hint="0.0%/32k (auto)  fake/fake-model", busy_hint="0.0%/32k (auto)  fake/fake-model", busy_word="Working...", alt_screen=False, collapse_chars=1500)

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
        self.model = "" if not logged_in("pi") else (args.get("--model") or "fake/fake-model")
        self.parent: str | None = None
        self.pending_ids: list[tuple[str, str]] = []
        """(text, id) of messages sent through the bridge, to name the id when pi takes one in."""
        self.follow_ups: list[str] = []
        self.last_text = ""
        self.shutdown_requested = False
        self.hook_url = os.environ.get("DAEDALUS_HOOK_URL", "").rstrip("/")
        self.token = os.environ.get("DAEDALUS_HOOK_TOKEN", "")
        self.tui.enter_broken = bool(os.environ.get("TMUX"))
        dial = os.environ.get("DAEDALUS_DIAL_DIR", "")
        self.socket_path = str(Path(dial) / "pi.sock") if dial and self.bridge else ""
        self.calls = 0
        self.call_prefix = f"{os.environ.get('DAEDALUS_LAUNCH_ID') or 'nolaunch'}:{os.getpid():08x}"
        appended = args.get("--append-system-prompt")
        if appended:
            self.log("system_prompt", text=Path(appended).read_text(encoding="utf-8") if Path(appended).is_file() else appended)
        if args.all("--skill"):
            self.log("skills", paths=args.all("--skill"))

    # -- the bridge's side ------------------------------------------------------------------------------

    async def emit(self, event: str, **fields: Any) -> None:
        if not self.bridge or not self.hook_url:
            return
        status, _ = await post_json(f"{self.hook_url}/pi", {"event": event, "at": now_iso(), **fields}, {"Authorization": f"Bearer {self.token}"}, 10)
        self.log("bridge_event", name=event, status=status)

    async def team_post(self, body: dict[str, Any], hold: int) -> tuple[int, Any]:
        url = f"{self.hook_url}/team" + (f"?wait_ms={hold}" if hold else "")
        return await post_json(url, body, {"Authorization": f"Bearer {self.token}"}, hold / 1000 + 10)

    async def before_ready(self) -> bool:
        trusted = state_dir("pi") / "trusted.json"
        known = json.loads(trusted.read_text()) if trusted.exists() else []
        needs_trust = (Path(self.cwd) / ".pi" / "settings.json").exists() or (Path(self.cwd) / ".pi" / "extensions").exists()
        if needs_trust and self.cwd not in known and not self.bridge and not self.args.has("--approve"):
            future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            self.tui.open_dialog(Dialog(
                "trust", "Trust project folder?", [self.cwd, "", "This allows pi to load .pi settings and resources, install missing project packages, and execute project extensions."],
                ["Trust", "Trust parent folder", "Trust (this session only)", "Do not trust", "Do not trust (this session only)"], numbered=False, digits=False,
                footer="↑↓ navigate  enter select  escape/ctrl+c cancel", on_choose=lambda i: settle(future, i), on_escape=lambda: settle(future, 3)))
            choice = await future
            if choice >= 3:
                await self.quit(1)
                return False
            if choice < 2:
                trusted.write_text(json.dumps([*known, self.cwd]))
        if self.socket_path:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.socket_path)
            await asyncio.start_unix_server(self.bridge_client, path=self.socket_path)
        return True

    async def on_ready(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        if not self.file.exists():
            self.write({"type": "session", "version": 3, "id": self.session_id, "timestamp": now_iso(), "cwd": self.cwd})
            if self.args.get("--name"):
                self.entry("session_info", name=self.args.get("--name"))
            self.entry("model_change", provider=self.model.split("/")[0] if self.model else "", modelId=self.model.split("/")[-1] if self.model else "")
        if not self.model:
            self.tui.say(" Warning: No models available. Use /login to log into a provider via OAuth or API key.")
        if self.bridge and self.hook_url:
            await self.team_post({"tool": "hello", "stage": "extension", "client": {"name": "pi"}}, 0)
        await self.emit("ready", reason="startup", sessionId=self.session_id, sessionFile=str(self.file), cwd=self.cwd, model=self.model or "unknown/unknown")

    async def bridge_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    writer.write(b'{"ok": false, "error": "not JSON"}\n')
                    continue
                reply = await self.bridge_op(message)
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()

    async def bridge_op(self, message: dict[str, Any]) -> dict[str, Any]:
        op = message.get("op")
        if op == "send":
            text, ident = str(message.get("text", "")), str(message.get("id", ""))
            if not text.strip():
                return {"ok": False, "id": ident, "error": "empty message"}
            deliver = None if not self.busy else ("steer" if message.get("deliverAs") == "steer" else "followUp")
            self.log("submitted", text=text, busy=self.busy, via="bridge", deliverAs=deliver)
            if deliver is None:
                self.pending_ids.append((text, ident))
                await self.submit(text)
            else:
                await self.queue(text, ident, "extension", deliver)
            return {"ok": True, "id": ident, "delivered": deliver or "prompt"}
        if op == "abort":
            idle = not self.busy
            if not idle:
                assert self.turn_task is not None
                self.log("interrupt", via="bridge")
                self.turn_task.cancel()
            return {"ok": True, "wasIdle": idle}
        if op == "state":
            return {"ok": True, "idle": not self.busy, "pending": bool(self.injections or self.follow_ups)}
        if op == "shutdown":
            # pi defers its exit until it is idle.
            if self.busy:
                self.shutdown_requested = True
            else:
                self.tui.spawn(self.quit(0))
            return {"ok": True}
        return {"ok": False, "error": f"unknown op {op}"}

    async def queue(self, text: str, ident: str, source: str, deliver: str) -> None:
        """A message for a busy pi: taken in now (the ``input`` event fires at once), run later."""
        await self.emit("input", id=ident, source=source, text=text, streamingBehavior=deliver)
        if deliver == "steer":
            self.injections.append(text)
            self.tui.queued.append(text)
            self.tui.render()
        else:
            self.follow_ups.append(text)

    async def busy_enter(self, text: str) -> None:
        """Enter while a run goes on steers, as pi's own TUI does."""
        await self.queue(text, "", "interactive", "steer")

    # -- the session file ---------------------------------------------------------------------------------

    def write(self, entry: dict[str, Any]) -> None:
        with self.file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entry(self, kind: str, **fields: Any) -> None:
        ident = new_id().replace("-", "")[:8]
        self.write({"type": kind, "id": ident, "parentId": self.parent, "timestamp": now_iso(), **fields})
        self.parent = ident

    def message(self, message: dict[str, Any]) -> None:
        self.entry("message", message=message)

    # -- the run ------------------------------------------------------------------------------------------

    async def submit(self, text: str) -> None:
        if not self.model:
            # Signed out: pi takes the prompt, says it has no key, and runs nothing.
            await self.emit("input", id=self.take_id(text), source="extension" if self.bridge_sent(text) else "interactive", text=text, streamingBehavior="")
            self.tui.say(f" {text}")
            self.tui.say(f" {NO_KEY}")
            self.log("turn_ended", outcome="no_model", silent=False)
            return
        await super().submit(text)

    def bridge_sent(self, text: str) -> bool:
        return any(sent == text for sent, _ in self.pending_ids)

    def take_id(self, text: str) -> str:
        for index, (sent, ident) in enumerate(self.pending_ids):
            if sent == text:
                del self.pending_ids[index]
                return ident
        return ""

    async def turn(self, prompt: str) -> None:
        """One agent run as pi has it: the prompt, the steers taken in after tool calls, then each
        follow-up queued meanwhile — one ``agent_start``, one ``agent_end``, one ``agent_settled``."""
        self.tui.busy = True
        self.tui.status = "⠋ Working..."
        self.tui.say(f" {prompt if len(prompt) < 200 else prompt[:200] + '…'}")
        stop, error = "stop", ""
        silent = False
        try:
            await self.on_prompt(prompt, queued=False)
            await self.emit("agent_start")
            texts = [prompt]
            while texts:
                text = texts.pop(0)
                if text is not prompt:
                    self.tui.say(f" {text}")
                    self.message({"role": "user", "content": [{"type": "text", "text": text}], "timestamp": now_ms()})
                steps = script_of(text)
                silent = silent or any(step.kind == "silent" for step in steps)
                for step in steps:
                    await self.step(step)
                    await self.inject_pending()
                if self.follow_ups:
                    texts.append(self.follow_ups.pop(0))
        except asyncio.CancelledError:
            stop, error = "aborted", "Operation aborted"
        except TurnFailed as failure:
            stop, error = "error", failure.kind
        finally:
            self.tui.busy = False
            self.tui.status = ""
        if stop == "aborted":
            self.message({"role": "assistant", "content": [], "stopReason": "aborted", "errorMessage": error, "timestamp": now_ms()})
            self.tui.say(" Operation aborted")
            # A steer not yet taken in goes back to the editor, unsent (measured).
            if self.injections:
                self.tui.parts = ["\n".join(self.injections)]
                self.injections.clear()
                self.tui.queued.clear()
            self.follow_ups.clear()
        elif stop == "error":
            self.message({"role": "assistant", "content": [], "stopReason": "error", "errorMessage": error, "timestamp": now_ms()})
            self.tui.say(f" Error: {error}")
        self.tui.render()
        if self.faults.no_stop_hook:
            self.log("stop_hook_suppressed")
        elif not silent:
            words = self.last_text if stop == "stop" else ""
            await self.emit("agent_end", lastMessage=words, stopReason=stop, errorMessage=error)
            await self.emit("agent_settled", lastMessage=words, stopReason=stop, errorMessage=error)
        self.log("turn_ended", outcome={"stop": "completed", "aborted": "cancelled", "error": "failed"}[stop], silent=silent)
        self.last_text = ""
        self.turns += 1
        if self.faults.exit_after and self.turns >= self.faults.exit_after:
            self.crash()
        if self.shutdown_requested:
            await self.quit(0)

    async def on_prompt(self, text: str, *, queued: bool) -> None:
        self.message({"role": "user", "content": [{"type": "text", "text": text}], "timestamp": now_ms()})
        if not queued:
            ident = self.take_id(text)
            await self.emit("input", id=ident, source="extension" if ident or self.bridge_sent(text) else "interactive", text=text, streamingBehavior="")

    async def on_assistant(self, text: str) -> None:
        self.last_text = text
        self.message({"role": "assistant", "content": [{"type": "text", "text": text}], "api": "openai-completions", "provider": "fake", "model": self.model or "fake-model", "stopReason": "stop",
                      "usage": {"input": 800, "output": 40, "cacheRead": 300, "cacheWrite": 0, "totalTokens": 1140, "cost": {"input": 0.0008, "output": 0.0002, "cacheRead": 0, "cacheWrite": 0, "total": 0.001}}, "timestamp": now_ms()})

    async def on_tool_start(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None:
        self.message({"role": "assistant", "content": [{"type": "toolCall", "id": tool_id, "name": name.lower() if name not in ("Report", "AskOrchestrator") else name, "arguments": tool_input}], "stopReason": "toolUse", "timestamp": now_ms()})
        await self.emit("tool_start", toolName=name.lower() if name not in ("Report", "AskOrchestrator") else name, toolCallId=tool_id)

    async def on_tool_end(self, name: str, tool_input: dict[str, Any], tool_id: str, output: str, ok: bool) -> None:
        tool = name.lower() if name not in ("Report", "AskOrchestrator") else name
        self.message({"role": "toolResult", "toolCallId": tool_id, "toolName": tool, "content": [{"type": "text", "text": output}], "isError": not ok, "timestamp": now_ms()})
        await self.emit("tool_end", toolName=tool, toolCallId=tool_id, isError=not ok)

    async def permission(self, tool: str, tool_input: dict[str, Any], summary: str, tool_id: str) -> str:
        return "allow_once"

    async def question(self, question: str, options: list[str], tool_id: str) -> str:
        await self.assistant(f"{question} ({' / '.join(options)})")
        return "(asked in text; pi has no question tool)"

    async def team_tool(self, name: str, arguments: dict[str, Any], tool_id: str) -> str:
        if not self.bridge:
            return f"error: no tool {name}"
        await self.on_tool_start(name, arguments, tool_id)
        self.calls += 1
        call_id = f"{self.call_prefix}:{self.calls}"
        if name == "Report":
            hold = int(os.environ.get("DAEDALUS_REPORT_HOLD_MS") or 15_000)
            body: dict[str, Any] = {"tool": "report", "kind": arguments.get("kind", "checkpoint"), "note": arguments.get("note", ""), "artifacts": arguments.get("artifacts") or [], "call_id": call_id}
            silence = "recorded"
        else:
            hold = int(os.environ.get("DAEDALUS_ASK_HOLD_MS") or 300_000)
            body = {"tool": "ask", "question": arguments.get("question", ""), "options": arguments.get("options") or [], "call_id": call_id}
            silence = EXPIRED
        status, reply = await self.team_post(body, hold)
        ok = True
        if 200 <= status < 300:
            if isinstance(reply, dict) and isinstance(reply.get("text"), str):
                result, ok = reply["text"], reply.get("error") is not True
            elif isinstance(reply, str) and reply:
                result = reply
            else:
                result = silence
        elif status in (401, 410):
            result, ok = GONE, False
        else:
            result, ok = f"the team refused the call ({status})", False
        await self.on_tool_end(name, arguments, tool_id, result, ok)
        return result

    async def on_session_end(self) -> None:
        await self.emit("session_end", reason="quit")
        if self.socket_path:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.socket_path)


def now_ms() -> int:
    return int(time.time() * 1000)


def command(argv: list[str]) -> int | None:
    if argv[:1] in (["--version"], ["-v"]):
        print(installed_version("pi", DEFAULT_VERSION))
        return 0
    if argv[:1] == ["--list-models"]:
        print("provider   model            context  max-out  thinking  images\nanthropic  claude-haiku-4   200K     64K      yes       yes\nopenai     gpt-5-mini       400K     128K     yes       yes")
        return 0
    if argv[:2] == ["auth", "check"]:
        provider = argv[argv.index("--provider") + 1] if "--provider" in argv else "anthropic"
        if "--credentials" in argv:
            Log("pi")("credentials_printed", provider=provider)
            print(json.dumps({"provider": provider, "credentials": {"access": "fake-secret-that-must-never-be-read"}}))
            return 0
        ok = logged_in("pi")
        # As pi 0.84.2 prints it (measured).
        report = {"status": "ready", "provider": provider, "authType": "oauth"} if ok else {"status": "not_ready", "provider": provider, "reason": "credentials_not_configured"}
        print(json.dumps(report) if "--json" in argv else ("ready" if ok else "not ready"))
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
