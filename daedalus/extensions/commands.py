"""Slash commands for the Mini App: the same commands the chat knows, run for a named session.

The chat handlers take a Telegram message; the Mini App gives them a stand-in that names the
session outright and collects every reply as text. A few commands that answer with a keyboard
or send through the chat are handled here directly, with the same effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from daedalus.doctor import DoctorContext, render_text, run_checks
from daedalus.extensions.inbox import format_entries
from daedalus.host.prompts import DEFAULT_RULES
from daedalus.security import redact

if TYPE_CHECKING:
    from daedalus.app import Application


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    args: str
    description: str
    scope: str = "session"
    """``session``: acts on the open session; ``global``: the same everywhere."""
    confirm: bool = False
    """The Mini App asks before running it (destructive or expensive)."""


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("compact", "[focus]", "replace this session's history with a summary", confirm=True),
    CommandSpec("stop", "", "stop the current run"),
    CommandSpec("model", "[preset | provider/model | default]", "the model for this session (no argument: list)"),
    CommandSpec("thinking", "on|off|low|medium|high", "thinking for this session"),
    CommandSpec("mode", "[quick|deep|careful|default]", "limits and rules for this session (no argument: list)"),
    CommandSpec("rename", "<title>", "rename this session and its topic"),
    CommandSpec("cap", "<usd | none>", "spend cap for this session over all of its runs"),
    CommandSpec("brief", "[text]", "the standing brief in this session's system prompt (no argument: show)"),
    CommandSpec("usage", "", "spend today and in this session"),
    CommandSpec("status", "", "what is running", scope="global"),
    CommandSpec("sessions", "", "every session", scope="global"),
    CommandSpec("new", "<title>", "a new session with its own topic", scope="global"),
    CommandSpec("delete", "<id>", "delete a session with its workspace", scope="global", confirm=True),
    CommandSpec("cleanup", "[confirm]", "delete every session whose topic is closed", scope="global", confirm=True),
    CommandSpec("inbox", "[all|clear]", "the inbox", scope="global"),
    CommandSpec("board", "[all]", "the task board", scope="global"),
    CommandSpec("schedules", "", "scheduled tasks", scope="global"),
    CommandSpec("schedule", "run|on|off|delete <id>", "act on a scheduled task", scope="global"),
    CommandSpec("intents", "[delete <id>]", "standing intents", scope="global"),
    CommandSpec("peer", "here <name> | list | forget <name>", "name this session as a peer", scope="session"),
    CommandSpec("loop", "[10m] <instruction> | status | pause | resume | stop | remove", "this session's loop: a standing task it is woken up for", scope="session"),
    CommandSpec("heartbeat", "[on|off|run]", "the periodic check", scope="global"),
    CommandSpec("balance", "", "provider balances", scope="global"),
    CommandSpec("doctor", "[fix]", "health checks", scope="global"),
    CommandSpec("prompt", "", "the working rules", scope="global"),
    CommandSpec("settings", "", "a summary of the configuration", scope="global"),
    CommandSpec("verbosity", "0|1|2", "how much of a run the chat shows", scope="global"),
    CommandSpec("approval", "manual|auto", "how self-change proposals are approved", scope="global"),
    CommandSpec("rebuild", "", "rebuild and restart from main", scope="global", confirm=True),
    CommandSpec("rollback", "[n]", "roll back to a known-good build", scope="global", confirm=True),
)
BY_NAME = {c.name: c for c in COMMANDS}


def parse(line: str) -> tuple[str, str] | None:
    """``/name args`` → (name, args); None when the line is not a command."""
    text = line.strip()
    if not text.startswith("/") or len(text) < 2:
        return None
    head, _, rest = text[1:].partition(" ")
    name = head.split("@", 1)[0].lower()
    return (name, rest.strip()) if name.isidentifier() else None


@dataclass
class _Reply:
    texts: list[str] = field(default_factory=list)

    async def answer(self, text: str, **_: Any) -> _Reply:
        self.texts.append(str(text))
        return self

    reply = answer

    async def edit_text(self, text: str, **_: Any) -> _Reply:
        self.texts.append(str(text))
        return self

    async def delete(self) -> None:
        return None


class MiniAppMessage(_Reply):
    """What a chat handler needs from a message: the owner, a chat that looks like the session's topic, and answer()."""

    def __init__(self, *, owner_id: int, session_id: str, chat_id: int, thread_id: int, text: str) -> None:
        super().__init__()
        self.forced_session_id = session_id
        self.from_user = SimpleNamespace(id=owner_id)
        self.chat = SimpleNamespace(id=chat_id, type="supergroup" if thread_id else "private", is_forum=bool(thread_id))
        self.message_thread_id = thread_id or None
        self.is_topic_message = bool(thread_id)
        self.reply_to_message = None
        self.message_id = 0
        self.text = text


async def run_command(app: Application, session_id: str, line: str) -> str:
    """Run one slash command for ``session_id`` and return what the chat would have shown."""
    parsed = parse(line)
    if parsed is None:
        raise ValueError("not a command")
    name, args = parsed
    spec = BY_NAME.get(name)
    if spec is None:
        raise KeyError(name)
    manager = app.manager
    front = app.front
    assert manager is not None
    state = await manager.get_state(session_id)
    if state is None:
        raise KeyError("no such session")
    # Commands the chat answers with a keyboard or through the outbox: done here, same effect.
    if name == "compact":
        if state.running:
            return "Stop the run first."
        summary = await manager.compact(session_id, args)
        return "🗜 History compacted. The session continues from this summary.\n\n" + summary
    if name == "cap":
        if args.lower() in ("none", "off", "", "-"):
            await manager.set_session_cap(session_id, None)
            return "Session cap removed; the global limits still apply."
        try:
            cap = float(args)
        except ValueError:
            return "usage: /cap <usd> | none"
        await manager.set_session_cap(session_id, cap)
        spent, _ = await manager.spend(session_id=session_id)
        return f"Session cap: ${cap:.2f} (spent so far ${spent:.2f})."
    if name == "loop":
        loops = app.extensions.get("loops")
        if loops is None:
            return "loops are not installed"
        head = args.split(" ", 1)[0].lower() if args else "status"
        if head in ("", "status", "list"):
            loop = await loops.get(session_id)
            return ("Loop: " + loops.note(loop).lstrip("- ")) if loop else "No loop. /loop 10m <instruction> wakes this session every 10 minutes for it; /loop <instruction> lets the agent pace itself."
        if head in ("pause", "resume", "stop", "remove"):
            try:
                if head == "pause":
                    await loops.pause(session_id, "paused by the operator")
                elif head == "resume":
                    await loops.resume(session_id)
                elif head == "stop":
                    await loops.stop(session_id, "stopped by the operator")
                else:
                    await loops.remove(session_id)
            except ValueError as exc:
                return str(exc)
            return f"Loop {head}d." if head != "remove" else "Loop removed."
        interval = None
        text = args
        match = re.match(r"^(\d+)\s*(s|m|h|d)\s+(.+)$", args, re.S)
        if match:
            interval = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
            text = match.group(3)
        try:
            loop = await loops.create(session_id, instruction=text, mode="interval" if interval else "dynamic", interval_seconds=interval, max_runs=None, start_now=True)
        except ValueError as exc:
            return str(exc)
        return "Loop started: " + loops.note(loop).lstrip("- ")
    if name == "brief":
        if not args:
            current = str(state.metadata.get("brief") or "")
            return ("Brief:\n" + current) if current else "No brief. /brief <text> sets one; it lives in this session's system prompt."
        await manager.set_brief(session_id, args)
        return "Brief updated; it applies from the next run."
    if name == "doctor":
        ctx = DoctorContext(settings=app.settings, config=app.config, db=app.db, manager=manager, front=front, extensions=dict(app.extensions), guard=app.guard, fix=args.lower() == "fix")
        return redact.redact(render_text(await run_checks(ctx)))
    if name == "prompt":
        rules = app.config.prompt.rules.strip() or DEFAULT_RULES.strip()
        return "Working rules" + (" (default)" if not app.config.prompt.rules.strip() else "") + ":\n" + rules
    if name == "inbox":
        inbox = app.extensions.get("inbox")
        if inbox is None:
            return "The inbox is not installed."
        if args.lower() == "clear":
            n = await inbox.mark_read()  # type: ignore[attr-defined]
            return f"marked {n} entr{'y' if n == 1 else 'ies'} as read"
        entries = await inbox.list(limit=15, unread_only=args.lower() != "all")  # type: ignore[attr-defined]
        if not entries:
            return "Inbox: nothing unread." if args.lower() != "all" else "Inbox is empty."
        text = format_entries(entries)
        if args.lower() != "all":
            await inbox.mark_read([int(e["id"]) for e in entries])  # type: ignore[attr-defined]
        return text
    if name == "cleanup":
        closed = await manager.closed_topic_sessions()
        if not closed:
            return "No sessions with closed topics."
        if args.lower() != "confirm":
            return "Sessions whose topics are closed:\n" + "\n".join(f"- {s['title']} ({s['session_id']})" for s in closed) + "\n\n/cleanup confirm deletes them with their workspaces."
        removed = 0
        for s in closed:
            removed += int(await manager.delete_session(s["session_id"]))
        return f"Deleted {removed} session(s) with their workspaces."
    if front is None:
        raise RuntimeError("chat commands need the Telegram front")
    topic = await app.db.fetchone("SELECT chat_id, thread_id FROM topics WHERE session_id = ? AND closed_at IS NULL", (session_id,))
    chat_id = int(topic["chat_id"]) if topic else int(app.config.telegram.forum_chat_id or app.settings.owner_user_id)
    thread_id = int(topic["thread_id"] or 0) if topic else 0
    message = MiniAppMessage(owner_id=app.settings.owner_user_id, session_id=session_id, chat_id=chat_id, thread_id=thread_id, text=line)
    command = SimpleNamespace(prefix="/", command=name, args=args or None)  # what the chat handlers read of aiogram's CommandObject
    handler = getattr(front, f"cmd_{name}", None)
    if handler is not None and name not in ("operator",):
        await handler(message, command) if name in ("new", "rename", "delete", "model", "thinking", "mode") else await handler(message)
    else:
        await front.cmd_operator(message, command)
    return "\n\n".join(message.texts) or "done"


__all__ = ["BY_NAME", "COMMANDS", "CommandSpec", "MiniAppMessage", "parse", "run_command"]
