"""Notifications: the one place anything that wants the operator's attention is recorded.

A scheduled run that failed, a heartbeat that found something, a loop that needs an answer, a
service that did not come back: each producer posts a :class:`Draft`, the service stores it as a
row of ``notifications`` and announces it on the event bus as ``notify``. The app lists them (the
Inbox screen, with a badge), Telegram lists them with ``/inbox``, and every other client learns of
a new one from the bus rather than by polling.

Deciding *where* a notification goes (a toast, a push, the desktop, a Telegram line) is not this
module's business yet. The one exception is :attr:`Draft.telegram_general`, which keeps the lines
the application used to send to the General topic — startup, rebuild, budget, balance — going
there until the channels are decided in one place.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, TypedDict, get_args

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.transport.telegram.front import TelegramOutbox
from daedalus.transport.telegram.markdown import split_message

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.events import EventBus
    from daedalus.stores.database import Database
    from daedalus.transport.telegram.front import TelegramFront

logger = logging.getLogger(__name__)

Category = Literal[
    "run_finished",
    "question",
    "permission",
    "run_failed",
    "staff_turn",
    "staff_review",
    "orchestrator_report",
    "agent_notify",
    "reminder",
    "system",
]
Level = Literal["quiet", "normal", "urgent"]
Tone = Literal["ok", "info", "warning", "error"]
View = Literal["all", "unseen", "problems", "needs_you"]
ActionStyle = Literal["primary", "default", "danger", "ghost"]

CATEGORIES: tuple[str, ...] = get_args(Category)
LEVELS: tuple[str, ...] = get_args(Level)
TONES: tuple[str, ...] = get_args(Tone)
VIEWS: tuple[str, ...] = get_args(View)

DEFAULT_LEVEL: dict[str, Level] = {"permission": "urgent"}
"""A category's level when the producer does not name one; everything not listed is ``normal``."""

TITLE_MAX = 300
BODY_MAX = 20_000
EVENT_BODY_MAX = 4_000
"""The body a ``notify`` event carries. The full text is one list request away, and a body at its
stored limit, written in a script of four-byte characters, would pass the bus's payload ceiling."""
LIST_MAX = 500


@dataclass(frozen=True)
class Action:
    """A button on a notification. ``quick`` actions may be taken without opening the app."""

    id: str
    label: str
    style: ActionStyle = "default"
    quick: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "style": self.style, "quick": self.quick}


@dataclass(frozen=True)
class Draft:
    """What a producer wants the operator to know.

    ``kind`` is the producer's own sub-kind (the icon, the grouping); it defaults to the category.
    ``handled`` names the channels the producer already delivered through, so nothing sends the
    same thing there twice.
    """

    category: Category
    title: str
    body: str = ""
    kind: str = ""
    level: Level | None = None
    tone: Tone = "info"
    project_id: str | None = None
    session_id: str | None = None
    staff_id: str | None = None
    terminal_id: str | None = None
    run_id: str | None = None
    link: str = ""
    actions: tuple[Action, ...] = ()
    request_ref: str | None = None
    dedupe_key: str | None = None
    source: str = "system"
    handled: frozenset[str] = field(default_factory=frozenset)
    telegram_general: bool = False
    """Also send the title and body to the General topic, as ``Application.notify`` once did.
    Temporary: it goes when the channels are decided by the operator's preferences."""


class NotificationSummary(TypedDict):
    unseen: int
    needs_you: int


class NotificationView(TypedDict):
    id: int
    at: str
    updated_at: str
    category: str
    kind: str
    level: str
    tone: str
    title: str
    body: str
    link: str
    session_id: str | None
    run_id: str | None
    project_id: str | None
    staff_id: str | None
    terminal_id: str | None
    source: str
    dedupe_key: str | None
    count: int
    actions: list[dict[str, Any]]
    seen: bool
    resolved: str | None
    needs_you: bool
    delivered: dict[str, Any]


class NotificationPage(TypedDict):
    entries: list[NotificationView]
    next_before: int | None
    summary: NotificationSummary


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _view(row: Any, now: str) -> NotificationView:
    held = row["held_until"] is not None and row["held_until"] > now
    return NotificationView(
        id=int(row["id"]),
        at=row["at"],
        updated_at=row["updated_at"],
        category=row["category"],
        kind=row["kind"],
        level=row["level"],
        tone=row["tone"],
        title=row["title"],
        body=row["body"],
        link=row["link"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        project_id=row["project_id"],
        staff_id=row["staff_id"],
        terminal_id=row["terminal_id"],
        source=row["source"],
        dedupe_key=row["dedupe_key"],
        count=int(row["count"]),
        actions=_json(row["actions_json"], []),
        seen=row["seen_at"] is not None,
        resolved=row["resolution"] if row["resolved_at"] is not None else None,
        needs_you=row["request_ref"] is not None and row["resolved_at"] is None and not held,
        delivered=_json(row["delivered_json"], {}),
    )


# A row counts as unseen when nobody has looked at it and it is worth looking at: a quiet row is a
# record ("the heartbeat checked, nothing new") and never badges. A held row is waiting for the
# orchestrator to answer it first, so the operator has not been told about it yet. Each clause takes
# the current time as its parameters, one per ``?``.
_NOT_HELD = "(held_until IS NULL OR held_until <= ?)"
_UNSEEN = f"seen_at IS NULL AND level != 'quiet' AND {_NOT_HELD}"
_NEEDS_YOU = f"request_ref IS NOT NULL AND resolved_at IS NULL AND {_NOT_HELD}"
_VIEW_WHERE: dict[str, str] = {
    "all": _NOT_HELD,
    "unseen": _UNSEEN,
    "problems": f"tone IN ('warning', 'error') AND {_NOT_HELD}",
    "needs_you": _NEEDS_YOU,
}


class NotificationService:
    """The store and its announcements. Every write is one transaction followed by one bus event."""

    def __init__(self, db: Database, bus: EventBus | None = None, *, front: Callable[[], TelegramFront | None] | None = None) -> None:
        self.db = db
        self.bus = bus
        self._front = front or (lambda: None)

    # -- writing -----------------------------------------------------------------------

    async def post(self, draft: Draft) -> NotificationView:
        """Record a notification, or merge it into the open one with the same ``dedupe_key``.

        A merge counts the repeat, replaces the text and makes the entry unseen again: the operator
        who dismissed "the service is down" must hear that it is down once more, but in one row.
        """
        if draft.category not in CATEGORIES:
            raise ValueError(f"unknown notification category {draft.category!r}")
        level = draft.level or DEFAULT_LEVEL.get(draft.category, "normal")
        if level not in LEVELS:
            raise ValueError(f"unknown notification level {level!r}")
        tone = draft.tone if draft.tone in TONES else "info"
        title, body = draft.title.strip()[:TITLE_MAX] or (draft.kind or draft.category), draft.body[:BODY_MAX]
        now = _now()
        delivered = {channel: "handled" for channel in sorted(draft.handled)}
        actions = json.dumps([a.as_dict() for a in draft.actions], ensure_ascii=False)
        merged = False
        async with self.db.transaction() as conn:
            row_id = 0
            if draft.dedupe_key:
                cursor = await conn.execute(
                    "SELECT id FROM notifications WHERE dedupe_key = ? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                    (draft.dedupe_key,),
                )
                found = await cursor.fetchone()
                await cursor.close()
                if found is not None:
                    row_id, merged = int(found["id"]), True
                    await conn.execute(
                        "UPDATE notifications SET updated_at = ?, title = ?, body = ?, count = count + 1, seen_at = NULL,"
                        " level = CASE WHEN ? = 'urgent' THEN 'urgent' WHEN level = 'quiet' THEN ? ELSE level END,"
                        " tone = ?, actions_json = ?, run_id = COALESCE(?, run_id) WHERE id = ?",
                        (now, title, body, level, level, tone, actions, draft.run_id, row_id),
                    )
            if not merged:
                cursor = await conn.execute(
                    "INSERT INTO notifications(at, updated_at, kind, category, level, tone, title, body, link, session_id, run_id,"
                    " project_id, staff_id, terminal_id, source, dedupe_key, actions_json, request_ref, delivered_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now, now, draft.kind or draft.category, draft.category, level, tone, title, body, draft.link,
                        draft.session_id, draft.run_id, draft.project_id, draft.staff_id, draft.terminal_id,
                        draft.source, draft.dedupe_key, actions, draft.request_ref, json.dumps(delivered),
                    ),
                )
                row_id = int(cursor.lastrowid or 0)
                await cursor.close()
        telegram = "telegram" in draft.handled
        if draft.telegram_general:
            telegram = await self._send_general(row_id, draft, title, body) or telegram
        view = await self.get(row_id)
        assert view is not None  # written above, under the same lock nobody deletes through
        await self._announce(view, merged=merged, telegram=telegram)
        return view

    async def _send_general(self, row_id: int, draft: Draft, title: str, body: str) -> bool:
        front = self._front()
        if front is None:
            return False
        text = title + (f"\n\n{body}" if body.strip() else "")
        try:
            await front.notify(text, markdown=False)
        except Exception as exc:  # noqa: BLE001 — the record stands whether or not the chat took the line
            logger.warning("notification %s could not be sent to Telegram", row_id, exc_info=True)
            await self._delivered(row_id, "telegram", f"failed: {type(exc).__name__}")
            return False
        await self._delivered(row_id, "telegram", "general")
        return True

    async def _delivered(self, row_id: int, channel: str, outcome: str) -> None:
        await self.db.execute(
            "UPDATE notifications SET delivered_json = json_set(delivered_json, ?, ?) WHERE id = ?",
            (f"$.{channel}", outcome, row_id),
        )

    async def _announce(self, view: NotificationView, *, merged: bool, telegram: bool) -> None:
        if self.bus is None:
            return
        shown = dict(view)
        if len(view["body"]) > EVENT_BODY_MAX:
            shown["body"] = view["body"][:EVENT_BODY_MAX]
            shown["body_truncated"] = True
        payload = {
            "notification": shown,
            "toast": view["level"] != "quiet",
            "deliver": {"push": False, "desktop": False, "telegram": telegram},
            "merged": merged,
            "summary": await self.summary(),
        }
        try:
            event = await self.bus.publish(
                "notify", payload,
                project_id=view["project_id"], session_id=view["session_id"], staff_id=view["staff_id"], terminal_id=view["terminal_id"],
            )
        except Exception:  # noqa: BLE001 — the row is the record; a client that missed the event sees it on its next list
            logger.warning("notification %s was stored but not announced", view["id"], exc_info=True)
            return
        if event.seq:
            await self.db.execute("UPDATE notifications SET event_seq = ? WHERE id = ?", (event.seq, view["id"]))

    async def mark_seen(self, ids: Sequence[int] | None = None, *, everything: bool = False, session_id: str | None = None) -> int:
        """Mark entries seen: the listed ids, every unseen one, or every one of a session. Returns how many changed."""
        now = _now()
        params: list[str | int]
        announced: list[int] | Literal["all"] | None
        if everything:
            where, params, announced = "seen_at IS NULL", [], "all"
        elif session_id is not None:
            where, params, announced = "seen_at IS NULL AND session_id = ?", [session_id], None
        else:
            wanted = sorted({int(i) for i in ids or ()})
            if not wanted:
                return 0
            where, params, announced = f"seen_at IS NULL AND id IN ({','.join('?' for _ in wanted)})", list(wanted), None
        async with self.db.transaction() as conn:
            cursor = await conn.execute(f"SELECT id FROM notifications WHERE {where}", params)
            changed = [int(r["id"]) for r in await cursor.fetchall()]
            await cursor.close()
            if changed:
                await conn.execute(f"UPDATE notifications SET seen_at = ? WHERE {where}", [now, *params])
        if changed and self.bus is not None:
            try:
                await self.bus.publish("notify.seen", {"ids": announced or changed, "summary": await self.summary()}, session_id=session_id)
            except Exception:  # noqa: BLE001
                logger.warning("notify.seen was not announced", exc_info=True)
        return len(changed)

    async def delete(self, entry_id: int) -> bool:
        async with self.db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM notifications WHERE id = ?", (entry_id,))
            removed = cursor.rowcount > 0
            await cursor.close()
        return removed

    async def prune(self, keep_days: int) -> int:
        """Drop old entries that are finished with: seen, or quiet, and not waiting for an answer.

        Quiet rows go unseen or not: they never badge, so an operator who never opens the list
        would otherwise keep every hourly "the heartbeat found nothing" forever.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM notifications WHERE updated_at < ? AND (seen_at IS NOT NULL OR level = 'quiet')"
                " AND (request_ref IS NULL OR resolved_at IS NOT NULL)",
                (cutoff,),
            )
            removed = cursor.rowcount
            await cursor.close()
        return max(removed, 0)

    # -- reading -----------------------------------------------------------------------

    async def get(self, entry_id: int) -> NotificationView | None:
        row = await self.db.fetchone("SELECT * FROM notifications WHERE id = ?", (entry_id,))
        return _view(row, _now()) if row is not None else None

    async def list(self, view: View = "all", *, project_id: str | None = None, before: int | None = None, limit: int = 100) -> NotificationPage:
        """A page of entries, newest first. ``next_before`` is the cursor of the next page, or None at the end."""
        if view not in VIEWS:
            raise ValueError(f"unknown view {view!r}")
        limit = max(1, min(int(limit), LIST_MAX))
        now = _now()
        where = _VIEW_WHERE[view]
        clauses = [where]
        params: list[str | int] = [now] * where.count("?")
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if before is not None:
            clauses.append("id < ?")
            params.append(int(before))
        rows = await self.db.fetchall(f"SELECT * FROM notifications WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?", [*params, limit + 1])
        entries = [_view(r, now) for r in rows[:limit]]
        return NotificationPage(
            entries=entries,
            next_before=entries[-1]["id"] if len(rows) > limit else None,
            summary=await self.summary(),
        )

    async def summary(self) -> NotificationSummary:
        row = await self.db.fetchone(
            f"SELECT count(*) FILTER (WHERE {_UNSEEN}) AS unseen, count(*) FILTER (WHERE {_NEEDS_YOU}) AS needs_you"
            " FROM notifications WHERE seen_at IS NULL OR request_ref IS NOT NULL",
            [_now()] * (_UNSEEN.count("?") + _NEEDS_YOU.count("?")),
        )
        return NotificationSummary(unseen=int(row["unseen"] or 0) if row else 0, needs_you=int(row["needs_you"] or 0) if row else 0)

    # -- producers wired here ------------------------------------------------------------

    async def on_event(self, session_id: str, event: TurnEvent) -> None:
        """Run-level outcomes that deserve an entry even when the operator is watching."""
        if event.type is EventType.ERROR and event.payload.get("kind") == "run_cap":
            await self.post(Draft(
                "run_failed", "A run hit its spend cap", str(event.payload.get("message") or ""),
                kind="run_cap", tone="warning", session_id=session_id, run_id=event.run_id,
            ))


def format_entries(entries: Iterable[NotificationView]) -> str:
    """The ``/inbox`` digest: one line an entry, newest first."""
    icons = {"ok": "✓", "info": "·", "warning": "⚠️", "error": "❌"}
    lines = []
    for e in entries:
        when = e["at"][5:16].replace("T", " ")
        body = (e.get("body") or "").strip().replace("\n", " ")
        repeat = f" ×{e['count']}" if e["count"] > 1 else ""
        lines.append(f"{icons.get(e['tone'], '·')} {when} **{e['title']}**{repeat}" + (f" — {body[:200]}" if body else ""))
    return "\n".join(lines)


async def install(app: Application) -> list[asyncio.Task[None]]:
    assert app.manager is not None
    service = NotificationService(app.db, app.manager.bus, front=lambda: app.front)
    app.notifications = service
    app.extensions["notifications"] = service
    app.manager.add_sink(service.on_event)
    manager = app.manager

    async def on_run_finished(session_id: str, run_id: str, status: str) -> None:
        if status != "failed":
            return
        state = await manager.get_state(session_id)
        if state is not None and (state.session.metadata.get("unattended") or state.session.metadata.get("heartbeat")):
            return  # the scheduler / heartbeat post their own, richer entry
        title = state.session.title if state else session_id
        # The error's own words, not a pointer to them: a provider that refused every request
        # says why, and an entry that only says "see the session" hides the one useful sentence.
        body = (state.last_error_message.strip() if state else "") or "The run ended with an error; see the session for details."
        await service.post(Draft(
            "run_failed", f"Run failed in '{title}'", body[:500], kind="run_failed", tone="error",
            session_id=session_id, run_id=run_id, project_id=state.project.id if state is not None and state.project is not None else None,
        ))

    manager.on_finished(on_run_finished)
    front = app.front
    if front is not None:

        async def cmd_inbox(message, command) -> None:  # type: ignore[no-untyped-def]
            arg = (command.args or "").strip().lower()
            if arg == "clear":
                n = await service.mark_seen(everything=True)
                await message.answer(f"marked {n} entr{'y' if n == 1 else 'ies'} as read")
                return
            page = await service.list("all" if arg == "all" else "unseen", limit=15)
            entries = page["entries"]
            if not entries:
                await message.answer("Inbox: nothing unread." if arg != "all" else "Inbox is empty.")
                return
            text = f"📥 **Inbox** — {page['summary']['unseen']} unread\n\n" + format_entries(entries)
            if arg != "all":
                text += "\n\n_(shown entries are now marked read; /inbox all shows everything)_"
            outbox = TelegramOutbox(front.bot, message.chat.id, message.message_thread_id if message.is_topic_message else None)
            for chunk in split_message(text):
                await outbox.send_text(chunk)
            if arg != "all":
                await service.mark_seen([e["id"] for e in entries])  # only after the digest went out

        front.command_hooks["inbox"] = cmd_inbox
    return []


__all__ = [
    "CATEGORIES",
    "LEVELS",
    "TONES",
    "VIEWS",
    "Action",
    "Category",
    "Draft",
    "Level",
    "NotificationPage",
    "NotificationService",
    "NotificationSummary",
    "NotificationView",
    "Tone",
    "View",
    "format_entries",
    "install",
]
