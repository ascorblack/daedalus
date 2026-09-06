"""Inbox: the one place asynchronous outcomes land — scheduled runs, heartbeats, caps, failures.

The chat shows what happens while the operator watches; the inbox answers "what happened
while I was away". Entries are posted by other extensions and by the session runner, read
in the Mini App (with an unread badge) and with ``/inbox`` in Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.transport.telegram.front import TelegramOutbox
from daedalus.transport.telegram.markdown import split_message

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

SEVERITIES = ("info", "notice", "warning", "error")


class Inbox:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def post(
        self,
        kind: str,
        title: str,
        body: str = "",
        *,
        severity: str = "info",
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> int:
        if severity not in SEVERITIES:
            severity = "info"
        async with self.app.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO inbox(at, kind, severity, title, body, session_id, run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), kind, severity, title[:300], body[:20_000], session_id, run_id),
            )
            return int(cursor.lastrowid or 0)

    async def list(self, *, limit: int = 100, unread_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE read = 0" if unread_only else ""
        rows = await self.app.db.fetchall(f"SELECT * FROM inbox {where} ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    async def unread_count(self) -> int:
        row = await self.app.db.fetchone("SELECT count(*) c FROM inbox WHERE read = 0")
        return int(row["c"]) if row else 0

    async def mark_read(self, ids: list[int] | None = None) -> int:
        if ids is None:
            row = await self.app.db.fetchone("SELECT count(*) c FROM inbox WHERE read = 0")
            await self.app.db.execute("UPDATE inbox SET read = 1 WHERE read = 0")
            return int(row["c"]) if row else 0
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        await self.app.db.execute(f"UPDATE inbox SET read = 1 WHERE id IN ({marks})", ids)
        return len(ids)

    async def delete(self, entry_id: int) -> None:
        await self.app.db.execute("DELETE FROM inbox WHERE id = ?", (entry_id,))

    # -- producers wired here ------------------------------------------------------------

    async def on_event(self, session_id: str, event: TurnEvent) -> None:
        """Run-level outcomes that deserve an entry even when the operator is watching."""
        if event.type is EventType.ERROR and event.payload.get("kind") == "run_cap":
            await self.post("run_cap", "A run hit its spend cap", str(event.payload.get("message") or ""), severity="warning", session_id=session_id, run_id=event.run_id)

    async def prune(self, keep_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        row = await self.app.db.fetchone("SELECT count(*) c FROM inbox WHERE read = 1 AND at < ?", (cutoff,))
        await self.app.db.execute("DELETE FROM inbox WHERE read = 1 AND at < ?", (cutoff,))
        return int(row["c"]) if row else 0

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        if status == "failed":
            state = await self.app.manager.get_state(session_id) if self.app.manager else None
            if state is not None and (state.session.metadata.get("unattended") or state.session.metadata.get("heartbeat")):
                return  # the scheduler / heartbeat post their own, richer entry
            title = state.session.title if state else session_id
            await self.post("run_failed", f"Run failed in '{title}'", "The run ended with an error; see the session for details.", severity="error", session_id=session_id, run_id=run_id)


def format_entries(entries: list[dict[str, Any]]) -> str:
    icons = {"info": "·", "notice": "•", "warning": "⚠️", "error": "❌"}
    lines = []
    for e in entries:
        when = e["at"][5:16].replace("T", " ")
        body = (e.get("body") or "").strip().replace("\n", " ")
        lines.append(f"{icons.get(e['severity'], '·')} {when} **{e['title']}**" + (f" — {body[:200]}" if body else ""))
    return "\n".join(lines)


async def install(app: Application) -> list[asyncio.Task[None]]:
    inbox = Inbox(app)
    app.extensions["inbox"] = inbox
    assert app.manager is not None
    app.manager.add_sink(inbox.on_event)
    app.manager.on_finished(inbox.on_run_finished)
    front = app.front
    if front is not None:

        async def cmd_inbox(message, command) -> None:  # type: ignore[no-untyped-def]
            arg = (command.args or "").strip().lower()
            if arg == "clear":
                n = await inbox.mark_read()
                await message.answer(f"marked {n} entr{'y' if n == 1 else 'ies'} as read")
                return
            entries = await inbox.list(limit=15, unread_only=arg != "all")
            if not entries:
                await message.answer("Inbox: nothing unread." if arg != "all" else "Inbox is empty.")
                return
            unread = await inbox.unread_count()
            text = f"📥 **Inbox** — {unread} unread\n\n" + format_entries(entries)
            if arg != "all":
                text += "\n\n_(shown entries are now marked read; /inbox all shows everything)_"
            outbox = TelegramOutbox(front.bot, message.chat.id, message.message_thread_id if message.is_topic_message else None)
            for chunk in split_message(text):
                await outbox.send_text(chunk)
            if arg != "all":
                await inbox.mark_read([int(e["id"]) for e in entries])  # only after the digest went out

        front.command_hooks["inbox"] = cmd_inbox
    return []


__all__ = ["Inbox", "format_entries", "install"]
