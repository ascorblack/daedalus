"""A fake Codex: ``codex app-server`` on a unix socket, and the TUI that attaches to it with ``--remote``.

The adapter runs the app server as a companion terminal and the TUI as the staff member's terminal;
both are imitated, with the protocol subset the adapter uses kept in ``codex_protocol.json``.

The app server (``codex app-server --listen unix://<path> [-c key=value]…``):

- Messages are one JSON object per line, JSON-RPC without the ``"jsonrpc"`` member, as Codex's app
  server writes them (a client should accept either). Requests: ``initialize`` (then the client's
  ``initialized`` notification), ``thread/start``, ``thread/resume``, ``thread/loaded/list``,
  ``turn/start`` (``input``, ``clientUserMessageId``), ``turn/steer`` (``expectedTurnId`` required;
  refused when it does not name the running turn — the race when a turn has just ended),
  ``turn/interrupt``, ``thread/turns/list``, ``thread/items/list``, ``config/read``.
- Notifications to every client subscribed to the thread: ``thread/started``,
  ``thread/status/changed`` (``idle``; ``active`` with ``waitingOnApproval`` / ``waitingOnUserInput``;
  ``systemError``), ``turn/started``, ``item/started`` / ``item/completed`` (``userMessage`` with the
  ``clientId`` the client chose, ``agentMessage``, ``commandExecution``, ``mcpToolCall``),
  ``turn/completed`` (``completed``, ``interrupted``, ``failed``), ``thread/tokenUsage/updated``,
  ``turn/diff/updated``, ``serverRequest/resolved``, ``error``.
- Server requests for decisions: ``item/commandExecution/requestApproval`` (``accept``,
  ``acceptForSession``, ``decline``, ``cancel``) and ``item/tool/requestUserInput``. Which clients
  receive them is ``FAKE_CODEX_APPROVALS``: ``all`` (the default; every subscribed client, the first
  answer wins and the others are told ``serverRequest/resolved``) or ``owner`` (only the client that
  started the thread) — the two designs the adapter must support until the real one is measured.
- Configuration overrides with ``-c`` are TOML values under dotted keys, as Codex parses them:
  ``projects."<cwd>".trust_level="trusted"`` trusts a folder for the TUI;
  ``check_for_update_on_startup=false`` stops the TUI's update prompt (the key name is this fake's
  stand-in until the real one is found); ``mcp_servers.<name>={command=…,args=[…],env={…},
  tool_timeout_sec=N}`` starts an MCP server whose tools the script's ``report:`` / ``askorch:`` call.
- A ``silent`` turn sends its end only to the TUI (the client that says it is ``codex-tui``): the other
  clients see a turn that never ends, the case only a screen reconcile finds.

The TUI (``codex --remote unix://<path> resume <threadId>``, or ``codex --remote unix://<path>
[prompt]`` to start the thread itself): an untrusted folder shows the trust prompt, an available
update shows the update prompt, then the thread is drawn and followed. Enter while idle is
``turn/start``, while busy ``turn/steer``; Esc is ``turn/interrupt``; approval requests that reach
it are dialogs.

Commands: ``--version`` (``codex-cli X``), ``login status``, ``debug models``, ``update``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.support.fake_cli.agent import FAILURE_TEXT  # noqa: E402
from tests.support.fake_cli.tui import (  # noqa: E402
    Args,
    Dialog,
    Faults,
    Log,
    Look,
    McpClient,
    Tui,
    ask_arguments,
    exit_with,
    installed_version,
    latest_version,
    logged_in,
    new_id,
    now_iso,
    pause,
    report_arguments,
    scaled,
    script_of,
    set_version,
    settle,
    usage_error,
)

DEFAULT_VERSION = "0.155.1"
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
APPROVALS = ("on-request", "never")
GLOBAL_FLAGS = {"--remote": 1, "-c": 1, "--model": 1, "--sandbox": 1, "--ask-for-approval": 1, "--profile": 1, "--cd": 1, "--version": 0, "--listen": 1}
ALIASES = {"-m": "--model", "-s": "--sandbox", "-a": "--ask-for-approval", "-p": "--profile", "-C": "--cd", "-V": "--version", "--config": "-c"}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path(os.environ.get("HOME") or "/tmp") / ".codex")


def parse_overrides(values: list[str]) -> dict[str, Any]:
    """``-c key=value``: the value is TOML, or a plain string when it does not parse."""
    merged: dict[str, Any] = {}
    for item in values:
        key, eq, value = item.partition("=")
        if not eq:
            usage_error("codex", f"invalid override '{item}': expected key=value")
        try:
            parsed = tomllib.loads(f"{key.strip()} = {value.strip()}")
        except tomllib.TOMLDecodeError:
            parsed = tomllib.loads(f"{key.strip()} = {json.dumps(value.strip())}")
        deep_merge(merged, parsed)
    return merged


def deep_merge(into: dict[str, Any], other: dict[str, Any]) -> None:
    for key, value in other.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            deep_merge(into[key], value)
        else:
            into[key] = value


def file_config() -> dict[str, Any]:
    with contextlib.suppress(OSError, tomllib.TOMLDecodeError):
        return tomllib.loads((codex_home() / "config.toml").read_text(encoding="utf-8"))
    return {}


def trust_folder(cwd: str) -> None:
    path = codex_home() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[projects."{cwd}"]\ntrust_level = "trusted"\n')


def rpc_error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"id": ident, "error": {"code": code, "message": message}}


# -- the app server ------------------------------------------------------------------------------------


class Conn:
    def __init__(self, server: AppServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, number: int) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.number = number
        self.name = ""
        self.pending: dict[Any, asyncio.Future[Any]] = {}

    async def send(self, message: dict[str, Any]) -> None:
        with contextlib.suppress(ConnectionError, RuntimeError):
            self.writer.write((json.dumps(message) + "\n").encode())
            await self.writer.drain()


class Thread:
    def __init__(self, ident: str, cwd: str, owner: Conn, params: dict[str, Any]) -> None:
        self.id = ident
        self.cwd = cwd
        self.owner = owner
        self.subscribers: set[Conn] = {owner}
        self.model = str(params.get("model") or "gpt-5-codex")
        self.params = params
        self.turns: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.active: dict[str, Any] | None = None
        self.task: asyncio.Task[None] | None = None
        self.steers: list[tuple[str, str]] = []
        self.silent = False
        self.created_at = int(time.time())
        self.rollout = codex_home() / "sessions" / time.strftime("%Y/%m/%d") / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{ident}.jsonl"

    def view(self) -> dict[str, Any]:
        first = next((i for i in self.items if i["type"] == "userMessage"), None)
        preview = first["content"][0]["text"][:80] if first else ""
        return {"id": self.id, "preview": preview, "modelProvider": "openai", "createdAt": self.created_at, "cwd": self.cwd, "path": str(self.rollout)}


class AppServer:
    def __init__(self, path: str, config: dict[str, Any], log: Log) -> None:
        self.path = path
        self.config = config
        self.log = log
        self.threads: dict[str, Thread] = {}
        self.conns: list[Conn] = []
        self.next_request = 0
        self.mcp: dict[str, McpClient] = {}
        self.approvals = os.environ.get("FAKE_CODEX_APPROVALS", "all")
        self.faults = Faults.from_env()
        self.turns_done = 0

    def say(self, text: str) -> None:
        sys.stdout.write(text + "\r\n")
        sys.stdout.flush()

    async def serve(self) -> int:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        for name, spec in (self.config.get("mcp_servers") or {}).items():
            client = McpClient(name, str(spec.get("command", "")), [str(a) for a in spec.get("args") or []], {str(k): str(v) for k, v in (spec.get("env") or {}).items()})
            if await client.start():
                self.mcp[name] = client
                self.say(f"mcp server {name}: {len(client.tools)} tools")
            else:
                self.say(f"mcp server {name} failed: {client.error}")
        server = await asyncio.start_unix_server(self.connection, path=self.path, limit=1 << 22)
        self.say(f"codex app-server listening on unix://{self.path}")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (15, 1, 2):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        server.close()
        for client in self.mcp.values():
            await client.close()
        return 0

    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = Conn(self, reader, writer, len(self.conns) + 1)
        self.conns.append(conn)
        self.log("app_server_connection", number=conn.number)
        try:
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    await conn.send(rpc_error(None, -32700, "parse error"))
                    continue
                if "method" in message and "id" in message:
                    asyncio.ensure_future(self.request(conn, message))
                elif "method" in message:
                    pass  # ``initialized`` and other client notifications need no answer
                elif "id" in message:
                    future = conn.pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message.get("result") if "result" in message else {"error": message.get("error")})
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.conns.remove(conn)
            for thread in self.threads.values():
                thread.subscribers.discard(conn)
            for future in conn.pending.values():
                if not future.done():
                    future.set_result(None)
            writer.close()

    async def request(self, conn: Conn, message: dict[str, Any]) -> None:
        ident, method, params = message["id"], message["method"], message.get("params") or {}
        try:
            result = await self.handle(conn, method, params)
        except RpcFailure as exc:
            await conn.send(rpc_error(ident, exc.code, exc.message))
            return
        await conn.send({"id": ident, "result": result})

    def thread_of(self, params: dict[str, Any]) -> Thread:
        thread = self.threads.get(str(params.get("threadId")))
        if thread is None:
            raise RpcFailure(-32600, f"thread not found: {params.get('threadId')}")
        return thread

    async def handle(self, conn: Conn, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            conn.name = str((params.get("clientInfo") or {}).get("name") or "")
            return {"userAgent": f"codex_cli_rs/{installed_version('codex', DEFAULT_VERSION)} (fake)"}
        if method == "config/read":
            return {"config": self.effective_config()}
        if method == "thread/start":
            thread = Thread(new_id(), str(params.get("cwd") or os.getcwd()), conn, params)
            self.threads[thread.id] = thread
            thread.rollout.parent.mkdir(parents=True, exist_ok=True)
            self.rollout(thread, "session_meta", {"id": thread.id, "cwd": thread.cwd, "timestamp": now_iso(), "cli_version": installed_version("codex", DEFAULT_VERSION)})
            await self.notify(thread, "thread/started", {"thread": thread.view()})
            return {"thread": thread.view(), "model": thread.model, "cwd": thread.cwd, "approvalPolicy": params.get("approvalPolicy", "on-request"), "sandbox": params.get("sandbox", "workspace-write")}
        if method == "thread/resume":
            thread = self.thread_of(params)
            thread.subscribers.add(conn)
            return {"thread": {**thread.view(), "turns": thread.turns}, "model": thread.model, "cwd": thread.cwd}
        if method == "thread/loaded/list":
            return {"data": list(self.threads)}
        if method == "turn/start":
            thread = self.thread_of(params)
            if thread.task is not None and not thread.task.done():
                raise RpcFailure(-32600, "a turn is already running on this thread")
            text = text_of(params.get("input"))
            turn: dict[str, Any] = {"id": new_id(), "status": "inProgress", "items": [], "error": None}
            thread.active = turn
            thread.turns.append(turn)
            thread.task = asyncio.ensure_future(self.run_turn(thread, turn, text, str(params.get("clientUserMessageId") or "")))
            return {"turn": {"id": turn["id"], "status": "inProgress", "items": [], "error": None}}
        if method == "turn/steer":
            thread = self.thread_of(params)
            if not params.get("expectedTurnId"):
                raise RpcFailure(-32602, "expectedTurnId is required")
            if thread.active is None or thread.active["id"] != params["expectedTurnId"]:
                raise RpcFailure(-32600, "precondition failed: expectedTurnId does not match the active turn")
            thread.steers.append((text_of(params.get("input")), str(params.get("clientUserMessageId") or "")))
            return {"turnId": thread.active["id"]}
        if method == "turn/interrupt":
            thread = self.thread_of(params)
            if thread.active is None or thread.active["id"] != params.get("turnId"):
                raise RpcFailure(-32600, "no such running turn")
            assert thread.task is not None
            thread.task.cancel()
            return {}
        if method == "thread/turns/list":
            thread = self.thread_of(params)
            return page([{"id": t["id"], "status": t["status"], "error": t["error"]} for t in thread.turns], params)
        if method == "thread/items/list":
            thread = self.thread_of(params)
            items = thread.items if not params.get("turnId") else [i for i in thread.items if i.get("turnId") == params["turnId"]]
            return page(items, params)
        raise RpcFailure(-32601, f"unknown method: {method}")

    def effective_config(self) -> dict[str, Any]:
        merged = file_config()
        deep_merge(merged, self.config)
        return merged

    async def notify(self, thread: Thread, method: str, params: dict[str, Any], *, tui_only: bool = False) -> None:
        for conn in list(thread.subscribers):
            if tui_only and conn.name != "codex-tui":
                continue
            await conn.send({"method": method, "params": params})

    async def ask_clients(self, thread: Thread, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """A server request; the first answer wins, and the others are told it was resolved."""
        targets = [c for c in thread.subscribers if self.approvals == "all" or c is thread.owner]
        self.next_request += 1
        ident = self.next_request
        futures = []
        for conn in targets:
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            conn.pending[ident] = future
            futures.append((conn, future))
            await conn.send({"id": ident, "method": method, "params": params})
        self.log("server_request", method=method, id=ident, clients=[c.name for c in targets])
        if not futures:
            return {}
        try:
            done, _ = await asyncio.wait([f for _, f in futures], return_when=asyncio.FIRST_COMPLETED)
            answer = next(iter(done)).result() or {}
        finally:
            for conn, future in futures:
                conn.pending.pop(ident, None)
                if not future.done():
                    future.cancel()
        winner = next((c for c, f in futures if f.done() and not f.cancelled()), None)
        self.log("server_request_answered", id=ident, by=winner.name if winner else "", answer=answer)
        await self.notify(thread, "serverRequest/resolved", {"threadId": thread.id, "requestId": ident})
        return answer if isinstance(answer, dict) else {}

    def rollout(self, thread: Thread, kind: str, payload: dict[str, Any]) -> None:
        with thread.rollout.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": now_iso(), "type": kind, "payload": payload}) + "\n")

    async def item(self, thread: Thread, turn: dict[str, Any], item: dict[str, Any], *, started: bool = True, completed: bool = True) -> None:
        item.setdefault("id", new_id())
        record = {**item, "turnId": turn["id"]}
        if started:
            await self.notify(thread, "item/started", {"threadId": thread.id, "turnId": turn["id"], "item": item})
        if completed:
            thread.items.append(record)
            turn["items"].append(item["id"])
            await self.notify(thread, "item/completed", {"threadId": thread.id, "turnId": turn["id"], "item": item})
            if item["type"] in ("userMessage", "agentMessage"):
                role = "user" if item["type"] == "userMessage" else "assistant"
                text = item["content"][0]["text"] if role == "user" else item["text"]
                self.rollout(thread, "response_item", {"type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]})

    async def status(self, thread: Thread, status: dict[str, Any], *, tui_only: bool = False) -> None:
        await self.notify(thread, "thread/status/changed", {"threadId": thread.id, "status": status}, tui_only=tui_only)

    async def user_message(self, thread: Thread, turn: dict[str, Any], text: str, client_id: str) -> None:
        item: dict[str, Any] = {"type": "userMessage", "id": new_id(), "content": [{"type": "text", "text": text}]}
        if client_id:
            item["clientId"] = client_id
        await self.item(thread, turn, item)

    async def run_turn(self, thread: Thread, turn: dict[str, Any], text: str, client_id: str) -> None:
        steps = script_of(text)
        # A silent turn, or the missing-end fault, ends only for the TUI: every other client is left
        # with a turn that never finishes.
        silent = any(s.kind == "silent" for s in steps) or self.faults.no_stop_hook
        await self.status(thread, {"type": "active", "activeFlags": []})
        await self.notify(thread, "turn/started", {"threadId": thread.id, "turn": {"id": turn["id"], "status": "inProgress"}})
        await self.user_message(thread, turn, text, client_id)
        outcome, error = "completed", None
        try:
            for step in steps:
                await self.step(thread, turn, step.kind, step.arg)
                await self.take_steers(thread, turn)
        except asyncio.CancelledError:
            outcome = "interrupted"
        except TurnFailure as failure:
            outcome = "failed"
            error = {"message": FAILURE_TEXT.get(failure.kind, failure.kind), "codexErrorInfo": failure.kind}
            await self.notify(thread, "error", {"threadId": thread.id, "turnId": turn["id"], "error": error, "willRetry": False})
        turn["status"], turn["error"] = outcome, error
        thread.active = None
        thread.steers.clear()
        usage = {"inputTokens": 1200, "cachedInputTokens": 800, "outputTokens": 90, "reasoningOutputTokens": 10, "totalTokens": 1290}
        await self.notify(thread, "thread/tokenUsage/updated", {"threadId": thread.id, "turnId": turn["id"], "tokenUsage": {"total": usage, "last": usage}}, tui_only=silent)
        await self.notify(thread, "turn/completed", {"threadId": thread.id, "turn": {"id": turn["id"], "status": outcome, "error": error}}, tui_only=silent)
        await self.status(thread, {"type": "systemError"} if error and error["codexErrorInfo"] == "system" else {"type": "idle"}, tui_only=silent)
        self.turns_done += 1
        if self.faults.exit_after and self.turns_done >= self.faults.exit_after:
            self.log("exit", code=3, crash=True)
            os._exit(3)

    async def take_steers(self, thread: Thread, turn: dict[str, Any]) -> None:
        """Steered messages join the running turn between tool calls, each with its own ``clientId``."""
        while thread.steers:
            steer, steer_id = thread.steers.pop(0)
            await self.user_message(thread, turn, steer, steer_id)
            for extra in script_of(steer):
                await self.step(thread, turn, extra.kind, extra.arg)

    async def step(self, thread: Thread, turn: dict[str, Any], kind: str, arg: str) -> None:
        item: dict[str, Any]
        if kind == "echo":
            await pause(0.3)
            await self.item(thread, turn, {"type": "agentMessage", "text": arg})
        elif kind == "perm":
            item = {"type": "commandExecution", "id": new_id(), "command": arg, "cwd": thread.cwd, "status": "inProgress", "aggregatedOutput": None, "exitCode": None}
            await self.item(thread, turn, item, completed=False)
            if thread.params.get("approvalPolicy") == "never" or thread.params.get("sandbox") == "danger-full-access":
                decision = "accept"
            else:
                await self.status(thread, {"type": "active", "activeFlags": ["waitingOnApproval"]})
                answer = await self.ask_clients(thread, "item/commandExecution/requestApproval", {"threadId": thread.id, "turnId": turn["id"], "itemId": item["id"], "command": arg, "cwd": thread.cwd, "reason": f"run {arg}"})
                decision = str(answer.get("decision") or "decline")
                await self.status(thread, {"type": "active", "activeFlags": []})
            if decision == "cancel":
                raise asyncio.CancelledError
            if decision in ("accept", "acceptForSession"):
                await pause(0.2)
                item.update(status="completed", aggregatedOutput=f"(the fake did not run {arg})", exitCode=0)
                await self.item(thread, turn, item, started=False)
                await self.notify(thread, "turn/diff/updated", {"threadId": thread.id, "turnId": turn["id"], "diff": f"--- a/fake\n+++ b/fake\n@@\n+{arg}\n"})
                await self.item(thread, turn, {"type": "agentMessage", "text": f"Ran {arg}."})
            else:
                item.update(status="declined")
                await self.item(thread, turn, item, started=False)
                await self.item(thread, turn, {"type": "agentMessage", "text": f"Understood, I did not run {arg}."})
        elif kind == "ask":
            spec = ask_arguments(arg)
            await self.status(thread, {"type": "active", "activeFlags": ["waitingOnUserInput"]})
            question = {"id": "q1", "header": "Question", "question": spec["question"], "options": [{"label": o, "description": ""} for o in spec.get("options", [])]}
            answer = await self.ask_clients(thread, "item/tool/requestUserInput", {"threadId": thread.id, "turnId": turn["id"], "itemId": new_id(), "questions": [question]})
            await self.status(thread, {"type": "active", "activeFlags": []})
            chosen = ((answer.get("answers") or {}).get("q1") or {}).get("answers") or ["(no answer)"]
            await self.item(thread, turn, {"type": "agentMessage", "text": f"You chose: {chosen[0]}"})
        elif kind == "fail":
            await pause(0.2)
            raise TurnFailure(arg or "unknown")
        elif kind == "slow":
            loop = asyncio.get_running_loop()
            end, n = loop.time() + scaled(float(arg or 1)), 0
            while loop.time() < end:
                n += 1
                item = {"type": "commandExecution", "id": new_id(), "command": f"sed -n 1,40p file{n}.txt", "cwd": thread.cwd, "status": "inProgress", "aggregatedOutput": None, "exitCode": None}
                await self.item(thread, turn, item, completed=False)
                await pause(0.5)
                item.update(status="completed", aggregatedOutput=f"line {n}", exitCode=0)
                await self.item(thread, turn, item, started=False)
                await self.take_steers(thread, turn)
            await self.item(thread, turn, {"type": "agentMessage", "text": f"Worked through {n} files."})
        elif kind == "silent":
            await pause(3)
        elif kind in ("report", "askorch"):
            name = "Report" if kind == "report" else "AskOrchestrator"
            arguments = report_arguments(arg) if kind == "report" else ask_arguments(arg)
            client = self.mcp.get("daedalus_team")
            item = {"type": "mcpToolCall", "id": new_id(), "server": "daedalus_team", "tool": name, "arguments": arguments, "status": "inProgress", "result": None}
            await self.item(thread, turn, item, completed=False)
            timeout = float(((self.config.get("mcp_servers") or {}).get("daedalus_team") or {}).get("tool_timeout_sec") or 60)
            if client is None:
                result = "error: no MCP server daedalus_team"
            else:
                try:
                    result = await client.call(name, arguments, timeout)
                except (TimeoutError, ConnectionError, RuntimeError) as exc:
                    result = f"error: {exc or 'timed out'}"
            item.update(status="completed", result={"content": [{"type": "text", "text": result}]})
            await self.item(thread, turn, item, started=False)
            await self.item(thread, turn, {"type": "agentMessage", "text": f"{name}: {result}"})


class RpcFailure(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TurnFailure(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def text_of(items: Any) -> str:
    return "".join(str(i.get("text", "")) for i in items or [] if isinstance(i, dict) and i.get("type") == "text")


def page(items: list[Any], params: dict[str, Any]) -> dict[str, Any]:
    start = int(params.get("cursor") or 0)
    limit = int(params.get("limit") or 50)
    chunk = items[start : start + limit]
    return {"data": chunk, "nextCursor": str(start + limit) if start + limit < len(items) else None}


# -- the TUI ------------------------------------------------------------------------------------------


class CodexTui:
    look = Look("codex", ">_ OpenAI Codex (fake)", prompt="› ", idle_hint="⏎ send   ⌃J newline   ⌃C quit", busy_hint="esc to interrupt",
                busy_word="Working", alt_screen=False, collapse_chars=1000, collapse_lines=0, burst_guard_ms=0)

    def __init__(self, socket_path: str, args: Args, log: Log) -> None:
        self.path = socket_path
        self.args = args
        self.log = log
        self.faults = Faults.from_env()
        self.tui = Tui(self.look, faults=self.faults, log=log)
        self.tui.submit = self.submit
        self.tui.busy_enter = self.steer
        self.tui.escape = self.interrupt
        self.tui.quit = self.quit
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.thread_id = ""
        self.turn_id = ""
        self.open_requests: dict[Any, asyncio.Future[Any]] = {}
        self.done = asyncio.Event()
        self.code = 0

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        assert self.writer is not None
        self.next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        self.writer.write((json.dumps({"id": self.next_id, "method": method, "params": params}) + "\n").encode())
        await self.writer.drain()
        return await future

    async def main(self) -> int:
        self.tui.start()
        try:
            try:
                self.reader, self.writer = await asyncio.open_unix_connection(self.path, limit=1 << 22)
            except OSError as exc:
                self.tui.say(f"■ Failed to connect to the app server at unix://{self.path}: {exc}")
                await asyncio.sleep(0.2)
                return 1
            asyncio.ensure_future(self.read())
            await self.call("initialize", {"clientInfo": {"name": "codex-tui", "version": installed_version("codex", DEFAULT_VERSION)}})
            self.writer.write(b'{"method": "initialized"}\n')
            config = (await self.call("config/read", {})).get("config") or {}
            cwd = self.args.get("--cd") or os.getcwd()
            if ((config.get("projects") or {}).get(cwd) or {}).get("trust_level") != "trusted":
                choice = await self.ask("trust", f"You are running Codex in {cwd}", ["Since this folder is not trusted, Codex will ask before running commands."], ["Yes, allow Codex to work in this folder", "No, ask me to approve edits and commands"])
                if choice == 0:
                    trust_folder(cwd)
            current, latest = installed_version("codex", DEFAULT_VERSION), latest_version("codex", DEFAULT_VERSION)
            if current != latest and config.get("check_for_update_on_startup") is not False:
                await self.ask("update", f"✨ Update available! {current} -> {latest}", [], ["Update now", "Skip", "Skip until next version"])
            positional = self.args.positional
            if positional[:1] == ["resume"]:
                if len(positional) < 2:
                    usage_error("codex", "resume needs a thread id with --remote")
                resumed = await self.call("thread/resume", {"threadId": positional[1]})
                if "error" in (resumed or {}):
                    self.tui.say(f"■ {resumed['error'].get('message')}")
                    return 1
                self.thread_id = positional[1]
                self.tui.say(f"resumed thread {self.thread_id}")
                prompt = None
            else:
                started = await self.call("thread/start", {"cwd": cwd, "model": self.args.get("--model") or None})
                self.thread_id = started["thread"]["id"]
                prompt = positional[0] if positional else None
            if self.faults.slow_ready_ms:
                await asyncio.sleep(self.faults.slow_ready_ms / 1000)
            self.tui.ready = True
            self.tui.render()
            self.log("ready", thread=self.thread_id)
            if prompt:
                await self.submit(prompt)
            await self.done.wait()
            return self.code
        finally:
            self.tui.restore()

    async def ask(self, kind: str, title: str, body: list[str], options: list[str]) -> int:
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.tui.open_dialog(Dialog(kind, title, body, options, on_choose=lambda i: settle(future, i), on_escape=lambda: settle(future, len(options) - 1)))
        return await future

    async def read(self) -> None:
        assert self.reader is not None
        while line := await self.reader.readline():
            with contextlib.suppress(json.JSONDecodeError):
                message = json.loads(line)
                if "method" in message and "id" in message:
                    asyncio.ensure_future(self.server_request(message))
                elif "method" in message:
                    self.notification(message["method"], message.get("params") or {})
                else:
                    future = self.pending.pop(message.get("id"), None)
                    if future is not None and not future.done():
                        future.set_result(message.get("result") if "result" in message else {"error": message.get("error")})
        self.tui.say("■ The app server went away.")
        await self.quit(1)

    def notification(self, method: str, params: dict[str, Any]) -> None:
        if params.get("threadId") not in (None, self.thread_id):
            return
        if method == "turn/started":
            self.turn_id = params["turn"]["id"]
            self.tui.busy = True
            self.tui.status = f"• {self.look.busy_word} ({self.look.busy_hint})"
        elif method == "turn/completed":
            status = params["turn"]["status"]
            self.turn_id = ""
            self.tui.busy = False
            self.tui.status = ""
            if status == "interrupted":
                self.tui.say("■ Conversation interrupted - tell the model what to do differently.")
            elif status == "failed":
                self.tui.say(f"■ {(params['turn'].get('error') or {}).get('message', 'the turn failed')}")
        elif method == "serverRequest/resolved":
            future = self.open_requests.pop(params.get("requestId"), None)
            if future is not None and not future.done():
                future.cancel()
                if self.tui.dialog is not None and self.tui.dialog.kind in ("permission", "question"):
                    self.tui.close_dialog()
        elif method == "item/completed":
            item = params["item"]
            if item["type"] == "userMessage":
                self.tui.say(f"› {item['content'][0]['text']}")
            elif item["type"] == "agentMessage":
                self.tui.say(f"• {item['text']}")
            elif item["type"] == "commandExecution":
                self.tui.say(f"• Ran {item['command']}", f"  └ {item.get('aggregatedOutput') or item.get('status')}")
            elif item["type"] == "mcpToolCall":
                self.tui.say(f"• Called {item['server']}.{item['tool']}")
        self.tui.render()

    async def server_request(self, message: dict[str, Any]) -> None:
        """A decision the server asks this client for, as a dialog. When another client answers
        first, ``serverRequest/resolved`` closes the dialog and nothing is sent."""
        assert self.writer is not None
        method, params = message["method"], message.get("params") or {}
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.open_requests[message["id"]] = future
        if method == "item/commandExecution/requestApproval":
            meanings = ["accept", "acceptForSession", "decline"]
            self.tui.open_dialog(Dialog("permission", "Would you like to run the following command?", [f"  $ {params.get('command')}"], ["Yes, proceed", "Yes, and don't ask again for this command", "No, and tell Codex what to do differently"],
                                        on_choose=lambda i: settle(future, {"decision": meanings[i]}), on_escape=lambda: settle(future, {"decision": "decline"})))
        elif method == "item/tool/requestUserInput":
            question = (params.get("questions") or [{}])[0]
            labels = [o["label"] for o in question.get("options") or []] or ["(free text)"]
            key = question.get("id", "q1")
            self.tui.open_dialog(Dialog("question", str(question.get("question")), [], labels, on_choose=lambda i: settle(future, {"answers": {key: {"answers": [labels[i]]}}}),
                                        on_escape=lambda: settle(future, {"answers": {key: {"answers": ["(no answer)"]}}})))
        else:
            self.open_requests.pop(message["id"], None)
            self.writer.write((json.dumps(rpc_error(message["id"], -32601, "unsupported")) + "\n").encode())
            return
        try:
            result = await future
        except asyncio.CancelledError:
            return
        finally:
            self.open_requests.pop(message["id"], None)
        self.writer.write((json.dumps({"id": message["id"], "result": result}) + "\n").encode())
        with contextlib.suppress(ConnectionError):
            await self.writer.drain()

    async def submit(self, text: str) -> None:
        answer = await self.call("turn/start", {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]})
        if "error" in (answer or {}):
            self.tui.say(f"■ {answer['error'].get('message')}")

    async def steer(self, text: str) -> None:
        answer = await self.call("turn/steer", {"threadId": self.thread_id, "input": [{"type": "text", "text": text}], "expectedTurnId": self.turn_id})
        if "error" in (answer or {}):
            await self.submit(text)

    async def interrupt(self) -> None:
        if self.tui.dialog is None and self.turn_id:
            self.log("interrupt", key="esc")
            await self.call("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})

    async def quit(self, code: int = 0) -> None:
        self.log("exit", code=code)
        self.code = code
        self.tui.exiting = True
        self.done.set()


# -- commands -----------------------------------------------------------------------------------------


def main() -> None:
    argv = sys.argv[1:]
    log = Log("codex")
    if argv[:1] in (["--version"], ["-V"]):
        print(f"codex-cli {installed_version('codex', DEFAULT_VERSION)}")
        raise SystemExit(0)
    if argv[:2] == ["login", "status"]:
        print("Logged in using ChatGPT" if logged_in("codex") else "Not logged in")
        raise SystemExit(0 if logged_in("codex") else 1)
    if argv[:2] == ["debug", "models"]:
        print(json.dumps({"models": [{"id": "gpt-5-codex", "displayName": "gpt-5-codex", "defaultReasoningEffort": "medium"}, {"id": "gpt-5", "displayName": "gpt-5", "defaultReasoningEffort": "medium"}]}))
        raise SystemExit(0)
    if argv[:1] == ["update"]:
        current, latest = installed_version("codex", DEFAULT_VERSION), latest_version("codex", DEFAULT_VERSION)
        if current != latest:
            set_version("codex", latest)
        print(f"Updated Codex CLI from {current} to {latest}" if current != latest else f"Codex CLI is up to date ({current})")
        raise SystemExit(0)
    if argv[:1] == ["app-server"]:
        args = Args("codex", argv[1:], flags=GLOBAL_FLAGS, aliases=ALIASES)
        listen = args.get("--listen")
        if not listen.startswith("unix://"):
            usage_error("codex", "the fake app server listens on unix://<path> only")
        server = AppServer(listen[len("unix://") :], parse_overrides(args.all("-c")), log)
        exit_with(server.serve)
        return
    args = Args("codex", argv, flags=GLOBAL_FLAGS, aliases=ALIASES)
    for name, allowed in (("--sandbox", SANDBOXES), ("--ask-for-approval", APPROVALS)):
        if args.get(name) and args.get(name) not in allowed:
            usage_error("codex", f"invalid value '{args.get(name)}' for '{name}'")
    remote = args.get("--remote")
    if not remote.startswith("unix://"):
        usage_error("codex", "the fake TUI runs only against an app server: --remote unix://<path>")
    tui = CodexTui(remote[len("unix://") :], args, log)
    exit_with(tui.main)


if __name__ == "__main__":
    main()
