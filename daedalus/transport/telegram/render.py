"""Turn core events into what the operator sees in a topic.

One live status message per run (edited at most once per second) plus the final
answer as ordinary messages. Everything the transport knows about a run lives in
:class:`RunView`; the Telegram side is behind the small :class:`Outbox` protocol
so the renderer can be exercised without a bot.
"""

from __future__ import annotations

import asyncio
import html
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.security.redact import redact
from daedalus.transport.telegram.markdown import DOCUMENT_THRESHOLD, split_message

_TOOL_ICONS = {
    "Exec": "⚙️",
    "Read": "📖",
    "Write": "✏️",
    "Edit": "✏️",
    "Find": "🔎",
    "Search": "🔎",
    "WebFetch": "🌐",
    "WebSearch": "🌐",
    "SendFile": "📎",
    "ImageView": "🖼",
    "AskUser": "❓",
    "Remember": "🧠",
    "Recall": "🧠",
    "SelfPropose": "🛠",
    "SelfRebuild": "🔄",
    "McpEnable": "🔌",
    "McpDisable": "🔌",
}


class Outbox(Protocol):
    async def send_text(self, text: str, *, markdown: bool = True) -> int: ...
    async def send_html(self, html: str) -> int: ...
    """Send rich HTML (headings, lists, details, pre); degrades to plain text where unsupported."""
    async def edit_text(self, message_id: int, text: str, *, html: bool = False) -> None: ...
    """Edit a message; ``html`` marks rich HTML rather than plain text."""
    async def send_document(self, path: Path, caption: str | None = None) -> int: ...
    async def delete(self, message_id: int) -> None: ...
    async def send_draft(self, draft_id: int, text: str) -> bool: ...
    """Show ``text`` as a live draft; returns False when drafts are not possible here."""


@dataclass(slots=True)
class RunView:
    run_id: str
    model: str
    verbosity: int = 1
    status_message_id: int | None = None
    state: str = "running"
    text_buffer: str = ""
    narration: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    tool_names: dict[str, str] = field(default_factory=dict)
    current_tool: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    changed_files: set[str] = field(default_factory=set)
    started: float = field(default_factory=time.monotonic)
    dirty: bool = False
    last_edit: float = 0.0
    last_rendered: str = ""
    progress_line: str = ""
    final_sent: bool = False
    draft_id: int = 0
    draft_sent: str = ""


class RunRenderer:
    """Consumes events for one run and drives an :class:`Outbox`."""

    def __init__(
        self,
        outbox: Outbox,
        view: RunView,
        *,
        edit_interval: float = 1.0,
        cost_lookup: Callable[[str], Awaitable[float | None]] | None = None,
        streaming: bool = False,
        draft_interval: float = 0.35,
    ) -> None:
        self.outbox = outbox
        self.view = view
        self.edit_interval = edit_interval
        self.cost_lookup = cost_lookup
        self.streaming = streaming
        self.draft_interval = draft_interval
        self._flush_task: asyncio.Task[None] | None = None
        self._draft_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # -- events ---------------------------------------------------------------------

    async def handle(self, event: TurnEvent) -> None:
        v = self.view
        p = event.payload
        t = event.type
        if t is EventType.MESSAGE_START:
            v.text_buffer = ""
            v.draft_id = random.randint(1, 2**31 - 1)
            v.draft_sent = ""
        elif t is EventType.CONTENT_BLOCK_DELTA:
            delta = p.get("delta") or {}
            if delta.get("type") == "text_delta":
                v.text_buffer += delta.get("text") or ""
                self._mark_draft()
        elif t is EventType.TOOL_USE_START:
            name = str(p.get("tool_name") or "")
            v.tool_names[str(p.get("tool_call_id"))] = name
            v.current_tool = name
            v.progress_line = ""
            self._mark()
        elif t is EventType.TOOL_USE_STOP:
            name = v.tool_names.get(str(p.get("tool_call_id")), "?")
            args = p.get("final_input") or {}
            v.tools.append(f"{_TOOL_ICONS.get(name, '•')} {name} {_args_summary(name, args)}")
            if name in ("Write", "Edit") and args.get("path"):
                v.changed_files.add(str(args["path"]))
            self._mark()
        elif t is EventType.TOOL_RESULT:
            v.current_tool = None
            if v.verbosity >= 2:
                content = str(p.get("content") or "")
                if content:
                    v.narration.append(f"↳ {redact(content[:300])}")
            self._mark()
        elif t is EventType.MESSAGE_STOP:
            used = p.get("tokens_used") or {}
            if used:
                v.tokens = {k: int(val) for k, val in used.items() if isinstance(val, int | float)}
            if p.get("stop_reason") == "tool_use":
                narration = v.text_buffer.strip()
                if narration and v.verbosity >= 1:
                    v.narration.append(narration[:400])
                v.text_buffer = ""
            self._mark()
        elif t is EventType.ERROR:
            v.narration.append(f"⚠️ {p.get('message') or json.dumps(p)[:300]}")
            self._mark()
        elif t is EventType.STATE_CHANGED:
            v.state = str(p.get("to") or v.state)
            self._mark()
        elif t is EventType.COMPACTION_STARTED:
            v.narration.append("🗜 compacting context")
            self._mark()
        elif t is EventType.MODEL_CHANGED:
            v.model = str(p.get("model_name") or p.get("to") or v.model)
            self._mark()

    def _mark_draft(self) -> None:
        if not self.streaming:
            return
        if self._draft_task is None or self._draft_task.done():
            self._draft_task = asyncio.create_task(self._draft_soon())

    async def _draft_soon(self) -> None:
        await asyncio.sleep(self.draft_interval)
        await self.push_draft()

    async def push_draft(self) -> None:
        """Send the accumulated answer text as a live draft (plain text, capped at the message limit)."""
        v = self.view
        text = v.text_buffer.strip()[:3800]
        if not self.streaming or not text or text == v.draft_sent or not v.draft_id:
            return
        try:
            ok = await self.outbox.send_draft(v.draft_id, text)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            self.streaming = False  # this chat cannot show drafts; fall back to the final message only
            return
        v.draft_sent = text

    async def progress(self, line: str) -> None:
        self.view.progress_line = line
        self._mark()

    def _mark(self) -> None:
        self.view.dirty = True
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_soon())

    async def _flush_soon(self) -> None:
        wait = self.edit_interval - (time.monotonic() - self.view.last_edit)
        if wait > 0:
            await asyncio.sleep(wait)
        await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            v = self.view
            if not v.dirty:
                return
            text = self.render_status_html()
            v.dirty = False
            if text == v.last_rendered:
                return
            try:
                await self._show_status(text)
                v.last_rendered = text
            except Exception:  # noqa: BLE001 — a failed edit must never break the run
                pass
            v.last_edit = time.monotonic()

    async def _show_status(self, html_text: str) -> None:
        """Create the status message on first use, edit it afterwards (rich HTML)."""
        v = self.view
        if v.status_message_id is None:
            v.status_message_id = await self.outbox.send_html(html_text)
        else:
            await self.outbox.edit_text(v.status_message_id, html_text, html=True)

    # -- rendering ------------------------------------------------------------------

    def _head(self, cost: float | None = None) -> str:
        v = self.view
        elapsed = int(time.monotonic() - v.started)
        icon = {"running": "⏳", "awaiting": "❓", "completed": "✅", "failed": "❌", "cancelled": "⏹", "compacting": "🗜", "interrupted": "⏸"}.get(v.state, "⏳")
        head = f"{icon} {v.state} · {v.model} · {_duration(elapsed)}"
        if v.tokens:
            head += f" · {v.tokens.get('total', 0):,} tok"
            if v.tokens.get("cache_read"):
                head += f" ({v.tokens['cache_read']:,} cached)"
        if cost is not None:
            head += f" · ${cost:.4f}"
        return head

    def render_status_html(self, cost: float | None = None) -> str:
        """The status message as rich HTML: one line of vitals, the tool log folded away, the latest note."""
        v = self.view
        parts = [f"<p><b>{_esc(self._head(cost))}</b></p>"]
        if v.current_tool:
            line = f"▶ {v.current_tool}"
            if v.progress_line:
                line += f": {v.progress_line}"
            parts.append(f"<p>{_esc(line)}</p>")
        if v.tools:
            shown = v.tools[-40:]
            while len(shown) > 3 and sum(len(t) for t in shown) > 7000:
                shown = shown[1:]
            items = "".join(f"<li>{_esc(t[:200])}</li>" for t in shown)
            if len(v.tools) > len(shown):
                items = f"<li>… {len(v.tools) - len(shown)} earlier</li>" + items
            open_attr = " open" if v.state in ("running", "awaiting", "compacting") and len(v.tools) <= 6 else ""
            parts.append(f"<details{open_attr}><summary>{len(v.tools)} tool call{'s' if len(v.tools) != 1 else ''}</summary><ul>{items}</ul></details>")
        if v.narration and v.verbosity >= 1:
            notes = v.narration[-3:] if v.state in ("running", "awaiting") else v.narration[-1:]
            parts.append("<blockquote>" + "<br/>".join(_esc(n[:400]) for n in notes) + "</blockquote>")
        if v.changed_files:
            parts.append("<p>files: " + ", ".join(f"<code>{_esc(f[:120])}</code>" for f in sorted(v.changed_files)[:20]) + "</p>")
        return "".join(parts)

    def render_status(self) -> str:
        """Plain-text status (the fallback when rich editing is unavailable, and for tests)."""
        v = self.view
        lines = [self._head()]
        if v.current_tool:
            line = f"▶ {v.current_tool}"
            if v.progress_line:
                line += f": {v.progress_line}"
            lines.append(line)
        if v.tools:
            lines.append("")
            lines.extend(v.tools[-8:])
            if len(v.tools) > 8:
                lines.append(f"… {len(v.tools) - 8} earlier tool calls")
        if v.narration and v.verbosity >= 1:
            lines.append("")
            lines.extend(n.replace("\n", " ") for n in v.narration[-3:])
        if v.changed_files:
            lines.append("")
            lines.append("files: " + ", ".join(sorted(v.changed_files))[:300])
        return "\n".join(lines)[:4000]

    async def finish(self, status: str, *, workspace: Path) -> None:
        """Send the final answer and freeze the status message."""
        v = self.view
        v.state = status
        final = v.text_buffer.strip()
        v.text_buffer = ""
        cost: float | None = None
        if self.cost_lookup is not None:
            try:
                cost = await self.cost_lookup(v.run_id)
            except Exception:  # noqa: BLE001
                cost = None
        if final and not v.final_sent:
            v.final_sent = True
            if len(final) > DOCUMENT_THRESHOLD:
                out = workspace / "answer.md"
                out.write_text(final, encoding="utf-8")
                await self.outbox.send_document(out, caption=final[:900])
            else:
                for chunk in split_message(final):
                    try:
                        await self.outbox.send_text(chunk)
                    except Exception:  # noqa: BLE001 — one bad chunk must not truncate the answer
                        try:
                            await self.outbox.send_text(chunk, markdown=False)
                        except Exception:  # noqa: BLE001
                            continue
            if self.streaming and v.draft_id and v.draft_sent:
                # The real message is in the chat now; drop the live draft so the client stops the dots.
                try:
                    await self.outbox.send_draft(v.draft_id, "")
                except Exception:  # noqa: BLE001
                    pass
                v.draft_sent = ""
        elif not final and status == "completed" and not v.tools:
            await self.outbox.send_text("(the agent finished without a reply)", markdown=False)
        summary = self.render_status_html(cost)
        v.dirty = False
        if v.status_message_id is not None:
            try:
                await self.outbox.edit_text(v.status_message_id, summary, html=True)
            except Exception:  # noqa: BLE001
                pass
        elif status != "completed" or v.tools:
            try:
                await self._show_status(summary)
            except Exception:  # noqa: BLE001
                pass


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _args_summary(name: str, args: dict[str, Any]) -> str:
    """One line of arguments for the tool log, with secrets masked before they reach the chat."""
    if name == "Exec":
        return redact(str(args.get("command", "")))[:120]
    for key in ("path", "pattern", "url", "query", "skill", "title", "name"):
        if key in args:
            return redact(str(args[key]))[:120]
    return redact(json.dumps(args, ensure_ascii=False))[:120] if args else ""


__all__ = ["Outbox", "RunRenderer", "RunView"]
