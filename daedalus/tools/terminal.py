"""``TerminalRead`` — what is in the terminals of this session, for its own agent. Never a keystroke.

The operator runs tests, servers and builds in the terminal dock under a session's conversation, and
"look at the terminal" is otherwise a request to copy and paste. The tool reads; nothing here can
write into a terminal, and the service behind it shows only the terminals this session owns.
"""

from __future__ import annotations

from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.security.redact import redact
from daedalus.terminals.model import TerminalError, Unsupported
from daedalus.tools._common import FRAME_CHARS, clip, error, ok, output_limit, services_for

WHATS = ("list", "screen", "output", "commands")
DEFAULT_LINES = {"screen": 0, "output": 200, "commands": 20}
MAX_LINES = 2000
TAIL_BYTES = 64 * 1024
"""How far back ``output`` reads when no ``since_seq`` is given: enough for a test run's summary, and
a bound on what one call pulls from the daemon before it is cut to ``lines``."""


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("terminals") if manager is not None else None


def _name(view: dict[str, Any]) -> str:
    return f"{view['id']} ({view.get('title') or 'terminal'})"


def _line(view: dict[str, Any]) -> str:
    state = view["status"] if view["status"] != "exited" or view.get("exit_code") is None else f"exited {view['exit_code']}"
    where = f"{view['env']}, " if view.get("env") == "host" else ""
    parts = [f"- {view['id']} · {view.get('title') or 'terminal'} [{where}{state}] in {view.get('cwd') or '?'}"]
    command = view.get("last_command")
    if isinstance(command, dict) and command.get("command"):
        code = command.get("exit_code")
        parts.append(f"last command `{command['command'][:120]}`" + (f" → exit {code}" if code is not None else ""))
    live = view.get("live")
    if isinstance(live, dict):
        parts.append(f"output at seq {live.get('output_seq', 0)}")
        if live.get("alt_screen"):
            parts.append("a full-screen program is open")
    return " · ".join(parts)


def _tail(text: str, lines: int) -> tuple[str, int]:
    """The last ``lines`` lines of ``text`` and how many were left out before them."""
    rows = text.rstrip("\n").split("\n")
    if lines and len(rows) > lines:
        return "\n".join(rows[-lines:]), len(rows) - lines
    return "\n".join(rows), 0


def _bounded(context: ToolContext, body: str, header: str, footer: str, *, note: str) -> str:
    """Redacted, then cut to the per-call budget around the header and footer, so the cut never takes
    the line that says where to read on."""
    room = max(1000, output_limit(context) - FRAME_CHARS - len(header) - len(footer))
    return "\n".join(part for part in (header, clip(redact(body), room, note=note), footer) if part)


@tool(
    name="TerminalRead",
    description=(
        "Read the terminals of this session — the ones the operator opens in the dock under the conversation "
        "— without typing into them. what='list' shows them with their ids, titles, status and last command; "
        "what='screen' is what the terminal shows now (lines = how many more lines of scrollback above it); "
        "what='output' is the recent output as text (lines = how many of the last lines, default 200; pass "
        "since_seq from the previous call's 'next since_seq' to read only what came after it); "
        "what='commands' is the commands run in a shell with their exit codes (lines = how many, default 20). "
        "terminal is an id or a title, and may be left out when the session has one terminal. Read it when the "
        "operator points at a terminal instead of asking them to paste what it says."
    ),
)
async def terminal_read(context: ToolContext, what: str = "list", terminal: str | None = None, since_seq: int | None = None, lines: int | None = None) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "terminals are not available in this installation")
    if what not in WHATS:
        return error(context, f"what is one of {', '.join(WHATS)}, not {what!r}")
    count = max(0, min(MAX_LINES, int(lines))) if lines is not None else DEFAULT_LINES.get(what, 0)
    try:
        if what == "list":
            views = await hook("list", session_id=context.session_id)
            if not views:
                return ok(context, "This session has no terminals. The operator opens them in the terminal dock under the conversation.")
            return ok(context, redact("\n".join(_line(v) for v in views)), count=len(views))
        view = await hook("resolve", session_id=context.session_id, terminal=terminal or "")
        if what == "screen":
            return await _screen(context, hook, view, count)
        if what == "output":
            return await _output(context, hook, view, since_seq, count or DEFAULT_LINES["output"])
        return await _commands(context, hook, view, count or DEFAULT_LINES["commands"])
    except TerminalError as exc:
        return error(context, exc.message)


async def _screen(context: ToolContext, hook: Any, view: dict[str, Any], scrollback: int) -> ToolResult:
    try:
        screen = await hook("screen", session_id=context.session_id, terminal_id=view["id"], scrollback=scrollback)
    except Unsupported:
        screen = None
    rows = [str(r) for r in (screen or {}).get("lines") or []]
    while rows and not rows[-1].strip():
        rows.pop()
    if not rows:
        # The terminal service answers screen reads only once it emulates the screen; until then,
        # and for a screen that is really blank, the recent output says what happened.
        return await _output(context, hook, view, None, 60, note="the screen could not be read (this terminal service does not keep screens yet, or it is blank); the recent output instead")
    assert screen is not None
    cursor = screen.get("cursor") or {}
    header = f"terminal {_name(view)} — screen {screen.get('cols')}×{screen.get('rows')}" + (", a full-screen program" if screen.get("alt_screen") else "") + f", cursor on row {int(cursor.get('y') or 0) + 1}"
    return ok(context, _bounded(context, "\n".join(rows), header, f"(output seq {screen.get('seq', 0)})", note="read output with lines for the rest"), terminal_id=view["id"])


async def _output(context: ToolContext, hook: Any, view: dict[str, Any], since_seq: int | None, lines: int, *, note: str = "") -> ToolResult:
    head = int((view.get("live") or {}).get("output_seq") or 0)
    start = max(0, head - TAIL_BYTES) if since_seq is None else max(0, since_seq)
    chunk = await hook("output", session_id=context.session_id, terminal_id=view["id"], since_seq=start, max_bytes=TAIL_BYTES)
    text, left_out = _tail(str(chunk.data), lines)
    header = f"terminal {_name(view)} — output {chunk.from_seq}…{chunk.to_seq}" + (f" ({note})" if note else "")
    if chunk.gap:
        header += "; some output before this was already dropped from the terminal's memory"
    if left_out:
        header += f"; the last {lines} lines, {left_out} earlier ones left out"
    more = chunk.to_seq < chunk.head_seq
    footer = f"next since_seq={chunk.to_seq}" + (" (more output follows; call again with it)" if more else "")
    body = text if text.strip() else "(no output)"
    return ok(context, _bounded(context, body, header, footer, note="pass since_seq to read on"), terminal_id=view["id"], next_since_seq=chunk.to_seq)


async def _commands(context: ToolContext, hook: Any, view: dict[str, Any], last: int) -> ToolResult:
    try:
        commands = await hook("commands", session_id=context.session_id, terminal_id=view["id"], last=last)
    except Unsupported:
        command = view.get("last_command")
        known = f" The last one known: `{command.get('command')}` → exit {command.get('exit_code')}." if isinstance(command, dict) and command.get("command") else ""
        return ok(context, f"terminal {_name(view)}: its commands are not recorded (it is not a shell started with its integration); read the output instead.{known}")
    if not commands:
        return ok(context, f"terminal {_name(view)}: no commands recorded (the shell may not report them).")
    rows = []
    for c in commands:
        code = c.get("exit_code")
        rows.append(f"{c.get('n', '?')}. `{c.get('command') or ''}`" + (f" → exit {code}" if code is not None else " (running)" if not c.get("finished_at") else "") + (f" in {c['cwd']}" if c.get("cwd") else ""))
    return ok(context, _bounded(context, "\n".join(rows), f"terminal {_name(view)} — the last {len(rows)} commands", "", note="ask for fewer lines"), terminal_id=view["id"])


TOOLS = [terminal_read]

__all__ = ["TOOLS"]
