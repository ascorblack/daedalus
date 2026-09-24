"""A fake Grok Build: the TUI and the session files it writes.

What it imitates:

- ``grok --cwd DIR -s <uuid> --trust [--agent <name or file>] [-m model] [--permission-mode p]
  [--effort e] [prompt]``; ``-r <uuid>`` resumes. ``-s`` is refused for a session that exists.
  Without ``--trust`` an untrusted folder asks first.
- **Session files** under ``$GROK_HOME/sessions/<url-encoded cwd>/<uuid>/`` (a long cwd becomes a
  slug and a hash, with a ``.cwd`` file saying which — so a reader finds the directory by the session
  id, not by computing the name): ``updates.jsonl`` (the ACP update stream:
  ``user_message_chunk``, ``agent_message_chunk``, ``tool_call``, ``tool_call_update``),
  ``events.jsonl`` (``phase_changed``, ``permission_requested``, ``permission_resolved``,
  ``turn_cancelled`` — undocumented in the real CLI, so a reader must treat it as a hint) and
  ``summary.json``, which appears once the session is ready.
- **The permission dialog** has four rows and no digit shortcuts: arrows and Enter. The row selected
  when it opens is ``GROK_DEFAULT_SELECTED_PERMISSION`` (``allow_once``, ``allow_session``,
  ``allow_always``, ``reject``); left unset it is "Always allow on all sessions", which is why the
  launch sets it. ``permission_requested`` is written when the dialog opens.
- **Enter while busy cancels the turn and sends the new message** (``StopCancelled``, then the new
  prompt). Esc or Ctrl+C cancels a running turn.
- **Hooks**, if any are configured: from ``$GROK_HOME/hooks/*.json`` and, unless
  ``FAKE_GROK_AGENT_HOOKS=0``, from the ``hooks`` of an ``--agent`` definition file's front matter
  (flow-style values only: the fake reads JSON, not YAML). Payload keys are camelCase, with both
  ``hookEventName`` (``stop``) and ``hook_event_name`` (``Stop``): ``SessionStart``,
  ``UserPromptSubmit``, ``PreToolUse``, ``PostToolUse``, ``Stop``, ``StopCancelled``,
  ``Notification`` (``permission_prompt``, only while the dialog waits). MCP servers come from the
  agent file's ``mcpServers`` and ``$GROK_HOME/mcp.json``.
- **The operator's Claude hooks**: like the real CLI, it also runs the hooks in
  ``~/.claude/settings.json`` unless ``GROK_COMPAT_CLAUDE_HOOKS=0`` (the fake's name for the
  compatibility switch, until the real one is found); each such run is recorded.
- Commands: ``--version`` (``grok X (hash)``), ``update --check --json``, ``update --version V``,
  ``models``, ``inspect --json``, ``export <id>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.support.fake_cli.agent import FakeAgent  # noqa: E402
from tests.support.fake_cli.tui import (  # noqa: E402
    Args,
    Dialog,
    Look,
    McpClient,
    exit_with,
    expand_env,
    installed_version,
    latest_version,
    logged_in,
    new_id,
    now_iso,
    pause,
    post_json,
    run_command_hook,
    set_version,
    settle,
    state_dir,
    usage_error,
)

DEFAULT_VERSION = "1.0.40"
MODES = ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan")
FLAGS = {"--cwd": 1, "--session-id": 1, "--resume": 1, "--trust": 0, "--agent": 1, "--model": 1, "--permission-mode": 1, "--effort": 1, "--version": 0}
ALIASES = {"-s": "--session-id", "-r": "--resume", "-m": "--model", "-V": "--version"}
ROWS = ["Allow once", "Always allow this command in this session", "Always allow on all sessions", "Reject"]
ROW_MEANINGS = ["allow_once", "allow_session", "allow_always", "reject"]
LONG_NAME = 100


def grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME") or Path(os.environ.get("HOME") or "/tmp") / ".grok")


def session_dir(cwd: str, session_id: str) -> Path:
    name = urllib.parse.quote(cwd, safe="")
    base = grok_home() / "sessions"
    if len(name) > LONG_NAME:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", cwd.strip("/"))[-40:].strip("-")
        name = f"{slug}-{hashlib.sha256(cwd.encode()).hexdigest()[:8]}"
        (base / name).mkdir(parents=True, exist_ok=True)
        (base / name / ".cwd").write_text(cwd, encoding="utf-8")
    return base / name / session_id


def find_session(session_id: str) -> Path | None:
    base = grok_home() / "sessions"
    for candidate in base.glob(f"*/{session_id}") if base.exists() else []:
        return candidate
    return None


def front_matter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    block = text.split("---", 2)[1]
    out: dict[str, Any] = {}
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if not sep or line.startswith((" ", "\t")):
            continue
        value = value.strip()
        try:
            out[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            out[key.strip()] = value
    return out


class FakeGrok(FakeAgent):
    cli = "grok"
    look = Look("grok", "Grok Build (fake)", prompt="❯ ", idle_hint="enter to send", busy_hint="esc to cancel", busy_word="Thinking…", alt_screen=True, collapse_chars=10_000, burst_guard_ms=int(os.environ.get("FAKE_GROK_BURST_MS") or 0))

    def __init__(self, args: Args) -> None:
        super().__init__(str(Path(args.get("--cwd") or os.getcwd()).resolve()))
        self.args = args
        mode = args.get("--permission-mode", "default")
        if mode not in MODES:
            usage_error("grok", f"invalid permission mode '{mode}'")
        self.mode = mode
        resume = args.get("--resume")
        self.session_id = resume or args.get("--session-id") or new_id()
        existing = find_session(self.session_id)
        if args.has("--session-id") and existing is not None:
            usage_error("grok", f"session {self.session_id} already exists; resume it with -r")
        if resume and existing is None:
            usage_error("grok", f"no session {resume}")
        self.dir = existing or session_dir(self.cwd, self.session_id)
        self.first_prompt = args.positional[-1] if args.positional else None
        self.hooks: dict[str, list[Any]] = {}
        self.mcp_specs: dict[str, Any] = {}
        self.mcp: dict[str, McpClient] = {}
        self.seq = 0
        self.title = ""
        self.cancel_and_send: str | None = None
        self.tui.ctrl_c_interrupts = True
        self.load_configuration()

    def load_configuration(self) -> None:
        for path in sorted((grok_home() / "hooks").glob("*.json")) if (grok_home() / "hooks").exists() else []:
            with contextlib.suppress(OSError, json.JSONDecodeError):
                self.add_hooks(json.loads(path.read_text(encoding="utf-8")).get("hooks") or {})
        with contextlib.suppress(OSError, json.JSONDecodeError):
            self.mcp_specs.update(json.loads((grok_home() / "mcp.json").read_text(encoding="utf-8")).get("mcpServers") or {})
        agent = self.args.get("--agent")
        if agent and Path(agent).is_file():
            meta = front_matter(Path(agent))
            if os.environ.get("FAKE_GROK_AGENT_HOOKS", "1") != "0":
                if isinstance(meta.get("hooks"), dict):
                    self.add_hooks(meta["hooks"])
                if isinstance(meta.get("mcpServers"), dict):
                    self.mcp_specs.update(meta["mcpServers"])

    def add_hooks(self, hooks: dict[str, Any]) -> None:
        for event, entries in hooks.items():
            self.hooks.setdefault(event, []).extend(entries if isinstance(entries, list) else [])

    # -- files ------------------------------------------------------------------------------------------

    def append(self, name: str, entry: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def update(self, update: dict[str, Any]) -> None:
        self.append("updates.jsonl", {"timestamp": now_iso(), "sessionId": self.session_id, "update": update})

    def event(self, kind: str, **fields: Any) -> None:
        self.seq += 1
        self.append("events.jsonl", {"ts": now_iso(), "seq": self.seq, "type": kind, **fields})

    def summary(self) -> None:
        (self.dir / "summary.json").write_text(json.dumps({
            "sessionId": self.session_id, "cwd": self.cwd, "title": self.title, "model": self.args.get("--model", "grok-code-fast"),
            "createdAt": now_iso(), "updatedAt": now_iso(), "turns": self.turns,
        }), encoding="utf-8")

    # -- hooks -------------------------------------------------------------------------------------------

    async def hook(self, event: str, fields: dict[str, Any], *, tool: str = "") -> list[Any]:
        payload = {"hookEventName": event[0].lower() + event[1:], "hook_event_name": event, "sessionId": self.session_id, "cwd": self.cwd, **fields}
        answers = await self._run(self.hooks.get(event) or [], payload, tool, event)
        if os.environ.get("GROK_COMPAT_CLAUDE_HOOKS", "1") != "0":
            claude = Path(os.environ.get("HOME") or "/tmp") / ".claude" / "settings.json"
            with contextlib.suppress(OSError, json.JSONDecodeError):
                entries = (json.loads(claude.read_text(encoding="utf-8")).get("hooks") or {}).get(event) or []
                if entries:
                    self.log("claude_hooks_ran", name=event)
                    answers += await self._run(entries, payload, tool, event)
        return answers

    async def _run(self, entries: list[Any], payload: dict[str, Any], tool: str, event: str) -> list[Any]:
        answers: list[Any] = []
        for entry in entries:
            matcher = str(entry.get("matcher") or "")
            if tool and matcher and matcher != "*" and not re.fullmatch(matcher, tool):
                continue
            for spec in entry.get("hooks") or []:
                timeout = float(spec.get("timeout") or 30)
                if spec.get("type") == "http":
                    headers = {str(k): expand_env(str(v)) for k, v in (spec.get("headers") or {}).items()}
                    status, body = await post_json(expand_env(str(spec.get("url"))), payload, headers, timeout)
                else:
                    status, body = await run_command_hook(expand_env(str(spec.get("command"))), payload, timeout)
                self.log("hook", name=event, status=status)
                if body:
                    answers.append(body)
        return answers

    # -- start ---------------------------------------------------------------------------------------------

    async def before_ready(self) -> bool:
        if not logged_in("grok"):
            self.tui.open_dialog(Dialog("login", "Sign in to Grok", ["Run grok login to continue."], ["Open the browser"], on_choose=lambda i: None))
            return False
        trusted_file = state_dir("grok") / "trusted.json"
        trusted = json.loads(trusted_file.read_text()) if trusted_file.exists() else []
        if not self.args.has("--trust") and self.cwd not in trusted:
            future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            self.tui.open_dialog(Dialog("trust", f"Do you trust {self.cwd}?", ["Grok Build can read and change files here."], ["Trust this folder", "Exit"], on_choose=lambda i: settle(future, i), on_escape=lambda: settle(future, 1)))
            if await future != 0:
                await self.quit(1)
                return False
            trusted_file.write_text(json.dumps([*trusted, self.cwd]))
        for name, spec in self.mcp_specs.items():
            client = McpClient(name, expand_env(str(spec.get("command", ""))), [expand_env(str(a)) for a in spec.get("args") or []], {str(k): expand_env(str(v)) for k, v in (spec.get("env") or {}).items()})
            if await client.start():
                self.mcp[name] = client
        return True

    async def on_ready(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.summary()
        self.event("phase_changed", phase="idle")
        await self.hook("SessionStart", {"source": "resume" if self.args.has("--resume") else "startup"})

    # -- the turn ------------------------------------------------------------------------------------------

    async def busy_enter(self, text: str) -> None:
        """Enter while busy: cancel the turn, then send this message — Grok's "cancel-and-send"."""
        self.log("cancel_and_send", text=text)
        self.cancel_and_send = text
        if self.turn_task is not None:
            self.turn_task.cancel()

    async def turn(self, prompt: str) -> None:
        await super().turn(prompt)
        follow = self.cancel_and_send
        self.cancel_and_send = None
        if follow is not None and not self.done.is_set():
            self.turn_task = asyncio.ensure_future(self.turn(follow))

    async def escape(self) -> None:
        if self.busy:
            await super().escape()

    async def on_prompt(self, text: str, *, queued: bool) -> None:
        if not self.title:
            self.title = text[:60]
        self.update({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}})
        self.event("phase_changed", phase="thinking")
        await self.hook("UserPromptSubmit", {"prompt": text})

    async def on_assistant(self, text: str) -> None:
        self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

    async def on_tool_start(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None:
        self.event("phase_changed", phase="tool")
        self.update({"sessionUpdate": "tool_call", "toolCallId": tool_id, "title": name, "kind": "execute" if name == "Bash" else "read", "status": "in_progress", "rawInput": tool_input})

    async def on_tool_end(self, name: str, tool_input: dict[str, Any], tool_id: str, output: str, ok: bool) -> None:
        self.update({"sessionUpdate": "tool_call_update", "toolCallId": tool_id, "status": "completed" if ok else "failed", "content": [{"type": "content", "content": {"type": "text", "text": output}}]})
        await self.hook("PostToolUse", {"toolName": name, "toolInput": tool_input, "toolCallId": tool_id}, tool=name)
        self.event("phase_changed", phase="thinking")

    async def on_tool_denied(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None:
        self.update({"sessionUpdate": "tool_call_update", "toolCallId": tool_id, "status": "failed", "content": [{"type": "content", "content": {"type": "text", "text": "rejected"}}]})

    async def on_turn_completed(self) -> None:
        self.event("phase_changed", phase="idle")
        self.summary()
        if self.faults.no_stop_hook:
            self.log("stop_hook_suppressed")
        else:
            await self.hook("Stop", {})

    async def on_turn_failed(self, kind: str) -> None:
        self.event("turn_failed", error=kind)
        self.event("phase_changed", phase="idle")
        await self.hook("Stop", {"error": kind})

    async def on_turn_cancelled(self) -> None:
        self.event("turn_cancelled")
        self.event("phase_changed", phase="idle")
        await self.hook("StopCancelled", {})

    async def on_session_end(self) -> None:
        for client in self.mcp.values():
            await client.close()

    async def permission(self, tool: str, tool_input: dict[str, Any], summary: str, tool_id: str) -> str:
        await self.hook("PreToolUse", {"toolName": tool, "toolInput": tool_input, "toolCallId": tool_id}, tool=tool)
        if self.mode == "bypassPermissions":
            return "allow_once"
        chosen = os.environ.get("GROK_DEFAULT_SELECTED_PERMISSION", "allow_always")
        selected = ROW_MEANINGS.index(chosen) if chosen in ROW_MEANINGS else 2
        self.event("permission_requested", toolCallId=tool_id, tool=tool, command=summary, options=ROW_MEANINGS, selected=ROW_MEANINGS[selected])
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.tui.open_dialog(Dialog("permission", f"Allow {tool}?", [f"  $ {summary}"], ROWS, selected=selected, digits=False, footer="↑↓ to choose · Enter to confirm · Esc to reject",
                                    on_choose=lambda i: settle(future, ROW_MEANINGS[i]), on_escape=lambda: settle(future, "reject"), data={"tool": tool, "summary": summary}))
        notice = asyncio.ensure_future(self._notify_waiting(tool))
        try:
            answer = await future
        finally:
            notice.cancel()
            if self.tui.dialog is not None and self.tui.dialog.kind == "permission":
                self.tui.close_dialog()
        self.event("permission_resolved", toolCallId=tool_id, decision=answer)
        return {"allow_once": "allow_once", "allow_session": "allow_always", "allow_always": "allow_always"}.get(answer, "deny")

    async def _notify_waiting(self, tool: str) -> None:
        await pause(12 if self.faults.late_permission_notification else 1)
        await self.hook("Notification", {"notificationType": "permission_prompt", "message": f"Grok needs your permission to use {tool}"})

    async def question(self, question: str, options: list[str], tool_id: str) -> str:
        await self.assistant(f"{question} ({' / '.join(options)})")
        return "(asked in text; Grok Build has no question tool)"

    async def team_tool(self, name: str, arguments: dict[str, Any], tool_id: str) -> str:
        client = self.mcp.get("daedalus_team")
        if client is None or name not in client.tools:
            return f"error: no tool {name}"
        await self.on_tool_start(name, arguments, tool_id)
        try:
            result = await client.call(name, arguments, 600)
        except (TimeoutError, ConnectionError, RuntimeError) as exc:
            result = f"error: {exc or 'timed out'}"
        await self.on_tool_end(name, arguments, tool_id, result, not result.startswith("error"))
        return result


def command(argv: list[str]) -> int | None:
    version = installed_version("grok", DEFAULT_VERSION)
    if argv[:1] in (["--version"], ["-V"]):
        print(f"grok {version} (fake0a1)")
        return 0
    if argv[:1] == ["update"]:
        latest = latest_version("grok", DEFAULT_VERSION)
        if "--check" in argv:
            report = {"currentVersion": version, "latestVersion": latest, "updateAvailable": version != latest, "installer": "official", "channel": "stable", "autoUpdate": os.environ.get("GROK_DISABLE_AUTOUPDATER") != "1", "error": None}
            print(json.dumps(report) if "--json" in argv else f"{version} -> {latest}")
            return 0
        target = argv[argv.index("--version") + 1] if "--version" in argv else latest
        set_version("grok", target)
        print(f"Updated grok from {version} to {target}")
        return 0
    if argv[:1] == ["models"]:
        print("grok-code-fast\ngrok-4")
        return 0
    if argv[:2] == ["inspect", "--json"]:
        agents = [{"name": "default", "source": "builtin", "description": "The default agent"}]
        print(json.dumps({"version": version, "auth": {"signedIn": logged_in("grok")}, "agents": agents}))
        return 0
    if argv[:1] == ["export"]:
        found = find_session(argv[1]) if len(argv) > 1 else None
        if found is None:
            print("no such session", file=sys.stderr)
            return 1
        print((found / "updates.jsonl").read_text(encoding="utf-8") if (found / "updates.jsonl").exists() else "")
        return 0
    return None


def main() -> None:
    argv = sys.argv[1:]
    code = command(argv)
    if code is not None:
        raise SystemExit(code)
    agent = FakeGrok(Args("grok", argv, flags=FLAGS, aliases=ALIASES))
    exit_with(agent.main)


if __name__ == "__main__":
    main()
