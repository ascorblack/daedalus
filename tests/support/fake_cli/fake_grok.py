"""A fake Grok Build: the TUI, its hooks and the session files it writes, in the shapes recorded from
Grok Build 1.0.41 (``recorded/grok``).

What it imitates:

- ``grok --cwd DIR -s <uuid> --trust [--agent <name or file>] [--rules TEXT] [--allow RULE]
  [--disallowed-tools TOOLS] [-m model] [--permission-mode p] [--effort e] [-- prompt]``; ``-r <uuid>``
  resumes. ``-s`` is refused for a session that exists. Without ``--trust`` an untrusted folder asks
  first. ``--rules`` and the agent file's body are logged as ``system_prompt``.
- **The agent file** (``--agent <file>``): its front matter's ``hooks`` and ``mcpServers`` (a list of
  ``{name, command, args, env}``; a map makes Grok ignore the file, and the fake says so in its log)
  apply to the session. The fake reads the front matter as JSON values, one key per line — the form
  the adapter writes, which is also YAML. ``FAKE_GROK_AGENT_HOOKS=0`` turns the route off.
- **Hooks** from ``$GROK_HOME/hooks/*.json`` and the agent file. Payloads are camelCase with Claude's
  names beside them: ``SessionStart`` (``source``), ``UserPromptSubmit`` (``prompt``, ``promptId``,
  ``transcriptPath``), ``PreToolUse``/``PostToolUse`` (``toolName``, ``toolInput``, ``toolUseId``),
  ``Notification`` ``permission_prompt`` the moment a permission dialog waits, ``PermissionDenied``,
  ``Stop`` (``reason: end_turn``, ``promptId``, ``lastAssistantMessage``), ``StopCancelled``
  (``user_interrupt``, ``permission_rejected``, ``permission_cancelled``), ``SessionEnd`` and then a
  ``Stop`` with ``reason: shutdown`` and no ``promptId``.
- **Session files** under ``$GROK_HOME/sessions/<url-encoded cwd>/<uuid>/``: ``updates.jsonl``
  (``{"timestamp", "method": "_x.ai/session/update", "params": {"sessionId", "update"}}`` with
  ``user_message_chunk``, ``agent_message_chunk``, ``tool_call``, ``tool_call_update``,
  ``turn_completed``), ``events.jsonl`` (``phase_changed``, ``permission_requested``,
  ``permission_resolved``, ``turn_ended``), ``usage.json`` and ``summary.json``. A long cwd becomes a
  slug and a hash with a ``.cwd`` file, so a reader goes by the path the hooks name.
- **The permission dialog**: numbered rows under a ``┃`` bar — 1 "Yes, and don't ask again for
  anything (always-approve mode)", 2 "Yes, proceed", 3 "No, reject (type to add feedback)", 4 "Never
  allow: <command>"; a digit chooses and confirms; rejecting or Ctrl+C ends the turn. The row
  selected at first is ``GROK_DEFAULT_SELECTED_PERMISSION`` (``allow_once`` → 2), else 1.
- **Keys**: Ctrl+C cancels a running turn ("Turn cancelled by user"); Esc only says to press Ctrl+C.
  Enter while busy queues the message after the turn, and its ``UserPromptSubmit`` fires when it
  starts. ``/exit`` leaves with exit code 0.
- **The operator's Claude hooks** in ``~/.claude/settings.json`` run unless
  ``GROK_CLAUDE_HOOKS_ENABLED=false``; each run is logged as ``claude_hooks_ran``.
- Commands: ``--version`` (``grok X (hash)``), ``update --check --json``, ``update --version V``,
  ``models`` (its sign-in line first), ``inspect --json``, ``export <id>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.support.fake_cli.agent import FakeAgent, TurnFailed  # noqa: E402
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
    run_command_hook,
    script_of,
    set_version,
    settle,
    state_dir,
    usage_error,
)

DEFAULT_VERSION = "1.0.40"
MODES = ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan")
FLAGS = {
    "--cwd": 1, "--session-id": 1, "--resume": 1, "--trust": 0, "--agent": 1, "--model": 1, "--permission-mode": 1, "--effort": 1, "--version": 0,
    "--rules": 1, "--allow": 1, "--disallowed-tools": 1,
}
ALIASES = {"-s": "--session-id", "-r": "--resume", "-m": "--model", "-v": "--version", "--reasoning-effort": "--effort"}
ROW_MEANINGS = ["allow_always", "allow_once", "reject", "never"]
LONG_NAME = 100


def real_name(tool: str) -> str:
    """The fake model's tool names as Grok's own: its shell tool is ``run_terminal_command``."""
    return {"Bash": "run_terminal_command", "Read": "read_file"}.get(tool, tool)


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


def front_matter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    _, block, body = text.split("---", 2)
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
    return out, body.strip()


class FakeGrok(FakeAgent):
    cli = "grok"
    look = Look("fake-model", "Grok Build 1.0.40 (fake)", prompt="❯ ", idle_hint="Shift+Tab:mode  │  Ctrl+x:shortcuts", busy_hint="Shift+Tab:mode  │  Ctrl+c:cancel  │  Ctrl+x:shortcuts",
                busy_word="Responding…", alt_screen=True, collapse_chars=100_000, collapse_lines=3, marker_style="grok", boxed=True, burst_guard_ms=int(os.environ.get("FAKE_GROK_BURST_MS") or 0))

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
        self.prompt_id = ""
        self.last_text = ""
        self.queued_prompts: list[str] = []
        self.turn_usage: list[dict[str, Any]] = []
        self.tui.ctrl_c_interrupts = True
        self.tui.cancel = self.cancel_turn
        self.allowed = args.all("--allow")
        self.load_configuration()

    def load_configuration(self) -> None:
        for path in sorted((grok_home() / "hooks").glob("*.json")) if (grok_home() / "hooks").exists() else []:
            with contextlib.suppress(OSError, json.JSONDecodeError):
                self.add_hooks(json.loads(path.read_text(encoding="utf-8")).get("hooks") or {})
        agent = self.args.get("--agent")
        prompt: list[str] = []
        if agent and Path(agent).is_file():
            meta, body = front_matter(Path(agent))
            servers = meta.get("mcpServers")
            if servers is not None and not isinstance(servers, list):
                # Grok ignores an agent file whose mcpServers is a map (measured): nothing of it applies.
                self.log("agent_file_ignored", reason="mcpServers must be a list")
            elif os.environ.get("FAKE_GROK_AGENT_HOOKS", "1") != "0":
                if isinstance(meta.get("hooks"), dict):
                    self.add_hooks(meta["hooks"])
                for server in servers or []:
                    if isinstance(server, dict) and server.get("name"):
                        env = {str(e.get("name")): str(e.get("value")) for e in server.get("env") or [] if isinstance(e, dict)}
                        self.mcp_specs[str(server["name"])] = {"command": server.get("command"), "args": server.get("args") or [], "env": env}
                if body:
                    prompt.append(body)
        if self.args.get("--rules"):
            prompt.append(self.args.get("--rules"))
        if prompt:
            self.log("system_prompt", text="\n\n".join(prompt), rules=self.args.get("--rules"))

    def add_hooks(self, hooks: dict[str, Any]) -> None:
        for event, entries in hooks.items():
            self.hooks.setdefault(event, []).extend(entries if isinstance(entries, list) else [])

    # -- files ------------------------------------------------------------------------------------------

    def append(self, name: str, entry: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def update(self, update: dict[str, Any]) -> None:
        self.append("updates.jsonl", {"timestamp": int(time.time()), "method": "_x.ai/session/update", "params": {"sessionId": self.session_id, "update": update}})

    def event(self, kind: str, **fields: Any) -> None:
        self.seq += 1
        self.append("events.jsonl", {"ts": now_iso(), "type": kind, **fields})

    def summary(self) -> None:
        (self.dir / "summary.json").write_text(json.dumps({
            "info": {"id": self.session_id, "cwd": self.cwd}, "session_summary": self.title, "current_model_id": self.args.get("--model", "grok-build"),
            "created_at": now_iso(), "updated_at": now_iso(), "agent_name": "daedalus-staff" if self.args.get("--agent") else "grok-build",
        }), encoding="utf-8")
        (self.dir / "usage.json").write_text(json.dumps({"sessionId": self.session_id, "turns": self.turn_usage}), encoding="utf-8")

    @property
    def transcript(self) -> str:
        return str(self.dir / "updates.jsonl")

    # -- hooks -------------------------------------------------------------------------------------------

    async def hook(self, event: str, fields: dict[str, Any], *, tool: str = "") -> list[Any]:
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
        payload = {"hookEventName": snake, "hook_event_name": event, "sessionId": self.session_id, "session_id": self.session_id, "cwd": self.cwd, "workspaceRoot": self.cwd,
                   "permissionMode": self.mode, "timestamp": now_iso(), **fields}
        if event != "SessionStart":
            payload["transcriptPath"] = payload["transcript_path"] = self.transcript
        answers = await self._run(self.hooks.get(event) or [], payload, tool, event)
        if os.environ.get("GROK_CLAUDE_HOOKS_ENABLED", "true").lower() not in ("false", "0", "no", "off"):
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
                timeout = float(spec.get("timeout") or 5)
                status, body = await run_command_hook(expand_env(str(spec.get("command"))), payload, timeout)
                self.log("hook", name=event, status=status)
                if body:
                    answers.append(body)
        return answers

    # -- start ---------------------------------------------------------------------------------------------

    async def before_ready(self) -> bool:
        if not logged_in("grok"):
            self.tui.open_dialog(Dialog("login", "Approve in your browser to finish signing in.", ["XXXX-XXXX", "Waiting for approval..."], ["ctrl+q  quit"], on_choose=lambda i: None))
            return False
        trusted_file = state_dir("grok") / "trusted.json"
        trusted = json.loads(trusted_file.read_text()) if trusted_file.exists() else []
        if not self.args.has("--trust") and self.cwd not in trusted:
            future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            self.tui.open_dialog(Dialog("trust", f"Do you trust {self.cwd}?", ["Grok Build can read and change files here."], ["Trust this folder", "Exit"], on_choose=lambda i: settle(future, i), on_escape=lambda: settle(future, 1)))
            if await future != 0:
                await self.quit(1)
                return False
        if self.args.has("--trust") and self.cwd not in trusted:
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
        await self.hook("SessionStart", {"source": "resume" if self.args.has("--resume") else "new"})

    # -- the turn ------------------------------------------------------------------------------------------

    async def busy_enter(self, text: str) -> None:
        """Enter while busy queues the message after the turn (measured); it starts, and says so, then."""
        self.log("queued", text=text)
        self.queued_prompts.append(text)
        self.tui.queued.append(text)
        self.tui.render()

    async def escape(self) -> None:
        if self.busy:
            self.tui.notice = "Press Ctrl+c to cancel the turn"
            self.tui.render()

    async def cancel_turn(self) -> None:
        if self.busy:
            assert self.turn_task is not None
            self.log("interrupt", key="ctrl_c")
            self.cancelled_by = "user_interrupt"
            self.turn_task.cancel()

    cancelled_by = "user_interrupt"

    async def turn(self, prompt: str) -> None:
        self.tui.busy = True
        self.tui.status = f"    ⠙ {self.look.busy_word} (fake)"
        self.tui.notice = ""
        self.tui.say(f"❯ {prompt if len(prompt) < 200 else prompt[:200] + '…'}")
        self.prompt_id = new_id()
        self.cancelled_by = "user_interrupt"
        started = time.monotonic()
        outcome = "completed"
        try:
            await self.on_prompt(prompt, queued=False)
            for step in script_of(prompt):
                await self.step(step)
        except asyncio.CancelledError:
            outcome = "cancelled"
        except TurnFailed as failure:
            outcome = "failed"
            self.tui.say(f"API error: {failure.kind}")
            with contextlib.suppress(Exception):
                await self.on_turn_failed(failure.kind)
        finally:
            self.tui.busy = False
            self.tui.status = ""
            self.tui.render()
        elapsed = time.monotonic() - started
        self.turn_usage.append({"turnNumber": len(self.turn_usage) + 1, "inputTokens": 900, "outputTokens": 40, "cachedReadTokens": 100, "cacheCreationTokens": 0})
        if outcome == "cancelled":
            reason = self.cancelled_by
            words = {"user_interrupt": "by user", "permission_rejected": "because a permission was denied", "permission_cancelled": "because a permission prompt was dismissed"}[reason]
            self.tui.say(f"Turn cancelled {words} in {elapsed:.1f}s.")
            await self.on_turn_cancelled()
        elif outcome == "completed":
            await self.on_turn_completed()
        self.log("turn_ended", outcome=outcome, silent=False)
        self.turns += 1
        if self.faults.exit_after and self.turns >= self.faults.exit_after:
            self.crash()
        if self.queued_prompts and not self.done.is_set():
            text = self.queued_prompts.pop(0)
            with contextlib.suppress(ValueError):
                self.tui.queued.remove(text)
            self.turn_task = asyncio.ensure_future(self.turn(text))

    async def on_prompt(self, text: str, *, queued: bool) -> None:
        if not self.title:
            self.title = text[:60]
        self.update({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}, "_meta": {"modelId": self.args.get("--model", "grok-build")}})
        self.event("turn_started")
        self.event("phase_changed", phase="thinking")
        await self.hook("UserPromptSubmit", {"prompt": text, "promptId": self.prompt_id})

    async def on_assistant(self, text: str) -> None:
        self.last_text = text
        self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

    async def on_tool_start(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None:
        self.event("phase_changed", phase="tool")
        self.update({"sessionUpdate": "tool_call", "toolCallId": tool_id, "title": real_name(name), "rawInput": tool_input})

    async def on_tool_end(self, name: str, tool_input: dict[str, Any], tool_id: str, output: str, ok: bool) -> None:
        self.update({"sessionUpdate": "tool_call_update", "toolCallId": tool_id, "status": "completed" if ok else "failed", "rawInput": tool_input})
        await self.hook("PostToolUse", {"toolName": real_name(name), "toolInput": tool_input, "toolUseId": tool_id}, tool=real_name(name))
        self.event("phase_changed", phase="thinking")

    async def on_tool_denied(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None:
        self.update({"sessionUpdate": "tool_call_update", "toolCallId": tool_id, "status": "failed", "rawInput": tool_input})

    async def on_turn_completed(self) -> None:
        self.update({"sessionUpdate": "turn_completed", "prompt_id": self.prompt_id, "stop_reason": "end_turn"})
        self.event("turn_ended", outcome="completed")
        self.event("phase_changed", phase="idle")
        self.summary()
        if self.faults.no_stop_hook:
            self.log("stop_hook_suppressed")
        else:
            await self.hook("Stop", {"promptId": self.prompt_id, "reason": "end_turn", "stopHookActive": False, "lastAssistantMessage": self.last_text, "backgroundTasks": [], "sessionCrons": []})

    async def on_turn_failed(self, kind: str) -> None:
        self.update({"sessionUpdate": "turn_completed", "prompt_id": self.prompt_id, "stop_reason": "error"})
        self.event("turn_ended", outcome="failed")
        self.event("phase_changed", phase="idle")
        await self.hook("StopFailure", {"promptId": self.prompt_id, "error": kind})

    async def on_turn_cancelled(self) -> None:
        self.update({"sessionUpdate": "turn_completed", "prompt_id": self.prompt_id, "stop_reason": "cancelled"})
        self.event("turn_ended", outcome="cancelled", cancellation_category=self.cancelled_by)
        self.event("phase_changed", phase="idle")
        await self.hook("StopCancelled", {"promptId": self.prompt_id, "reason": self.cancelled_by, "cancelledBy": "user", "cancelTrigger": "ctrl_c" if self.cancelled_by == "user_interrupt" else "permission"})

    async def on_session_end(self) -> None:
        await self.hook("SessionEnd", {"reason": "shutdown"})
        await self.hook("Stop", {"reason": "shutdown", "stopHookActive": False})
        for client in self.mcp.values():
            await client.close()

    async def permission(self, tool: str, tool_input: dict[str, Any], summary: str, tool_id: str) -> str:
        tool = real_name(tool)
        await self.hook("PreToolUse", {"toolName": tool, "toolInput": tool_input, "toolUseId": tool_id, "toolInputTruncated": False}, tool=tool)
        if self.mode == "bypassPermissions":
            return "allow_once"
        chosen = os.environ.get("GROK_DEFAULT_SELECTED_PERMISSION", "")
        selected = 1 if chosen == "allow_once" else 0
        self.event("phase_changed", phase="permission_prompt")
        self.event("permission_requested", tool_name=tool)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        rows = ["Yes, and don't ask again for anything (always-approve mode)", "Yes, proceed", "No, reject (type to add feedback)", f"Never allow: {summary.split()[0] if summary else tool}"]
        started = time.monotonic()
        self.tui.open_dialog(Dialog(
            "permission", f"  ┃  {tool_input.get('description') or tool}", [f"  ┃  {summary}", "  ┃  ← → narrow scope"], rows, selected=selected, rows="radio",
            footer="  1/4:select  │  Tab:next option  │  ←/→:scope  │  Ctrl+o:always-approve  │  Ctrl+c:cancel  │  Esc:scrollback",
            on_choose=lambda i: settle(future, ROW_MEANINGS[i]), on_escape=lambda: None, on_ctrl_c=lambda: settle(future, "cancelled"), data={"tool": tool, "summary": summary}))
        # Only while the dialog really waits, and at once (measured: a few milliseconds after PreToolUse).
        await self.hook("Notification", {"notificationType": "permission_prompt", "message": "Tool permission requested", "level": "info"})
        try:
            answer = await future
        finally:
            if self.tui.dialog is not None and self.tui.dialog.kind == "permission":
                self.tui.close_dialog()
        decision = {"allow_once": "allow", "allow_always": "allow", "reject": "deny", "never": "deny", "cancelled": "cancelled"}[answer]
        self.event("permission_resolved", tool_name=tool, decision=decision, wait_ms=int((time.monotonic() - started) * 1000))
        if decision == "allow":
            return "allow_always" if answer == "allow_always" else "allow_once"
        if decision == "deny":
            await self.hook("PermissionDenied", {"toolName": tool, "toolInput": tool_input, "toolUseId": tool_id}, tool=tool)
            self.cancelled_by = "permission_rejected"
        else:
            self.cancelled_by = "permission_cancelled"
        # A rejected or dismissed permission ends the turn (measured).
        assert self.turn_task is not None
        self.turn_task.cancel()
        await asyncio.sleep(3600)
        return "deny"

    async def question(self, question: str, options: list[str], tool_id: str) -> str:
        await self.assistant(f"{question} ({' / '.join(options)})")
        return "(asked in text; Grok Build's question tool is removed at launch)"

    async def team_tool(self, name: str, arguments: dict[str, Any], tool_id: str) -> str:
        client = self.mcp.get("daedalus_team")
        if client is None or name not in client.tools:
            return f"error: no tool {name}"
        # Grok defers MCP tools: found by search_tool, called through use_tool, named server__tool.
        qualified = f"daedalus_team__{name}"
        await self.hook("PreToolUse", {"toolName": qualified, "toolInput": {"tool_name": qualified, "tool_input": arguments}, "toolUseId": tool_id}, tool=qualified)
        await self.on_tool_start(qualified, arguments, tool_id)
        try:
            result = await client.call(name, arguments, 600)
        except (TimeoutError, ConnectionError, RuntimeError) as exc:
            result = f"error: {exc or 'timed out'}"
        await self.on_tool_end(qualified, arguments, tool_id, result, not result.startswith("error"))
        return result


def command(argv: list[str]) -> int | None:
    version = installed_version("grok", DEFAULT_VERSION)
    if argv[:1] in (["--version"], ["-v"]):
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
        # As 1.0.41 prints it (measured): the sign-in line first.
        signed = "You are logged in with grok.com." if logged_in("grok") else "You are not authenticated."
        print(f"{signed}\n\nDefault model: grok-code-fast\n\nAvailable models:\n  * grok-code-fast (default)\n  - grok-4")
        return 0
    if argv[:2] == ["inspect", "--json"]:
        agents = [{"name": "general-purpose", "description": "General purpose agent for multi-step tasks.", "source": {"type": "builtin"}}]
        print(json.dumps({"grokVersion": version, "cwd": os.getcwd(), "projectTrusted": True, "loginPolicy": {"apiKeyAuthDisabled": False}, "agents": agents, "hooks": [], "mcpServers": []}))
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
