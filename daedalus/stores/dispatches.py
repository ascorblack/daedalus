"""The main orchestrator's hand-overs to projects, and what was said on each.

A dispatch is one piece of work the operator gave a project through the main orchestrator. It is
open until the project's orchestrator closes it with a report (done or blocked), or until the main
orchestrator cancels it. Everything said on it afterwards — a follow-up from the main orchestrator,
a progress report from the project — is one of its messages, so the whole exchange reads in order.

Closing is a compare-and-set on the status: two reports racing to close one dispatch (or a report
and a cancel) close it once, and the loser is told it was already closed.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from daedalus.stores.database import Database

STATUSES = ("open", "done", "blocked", "cancelled")
CLOSED = frozenset({"done", "cancelled"})
"""What a follow-up cannot reopen. A blocked dispatch is waiting for something, and a follow-up is
usually that something, so it opens again."""
KINDS = ("work", "setup")
AUTHORS = ("dispatcher", "orchestrator", "operator", "system")
TITLE_MAX = 120
TEXT_MAX = 8000
RESULT_MAX = 4000
ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
ID_ATTEMPTS = 8


class DispatchError(ValueError):
    """A dispatch the store will not keep or change, said so the caller can do something else."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    """``d`` and five characters a person can read aloud and type on a phone."""
    return "d" + "".join(secrets.choice(ID_ALPHABET) for _ in range(5))


@dataclass(frozen=True, slots=True)
class DispatchMessage:
    id: int
    dispatch_id: str
    at: str
    author: str
    kind: str
    text: str

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "dispatch_id": self.dispatch_id, "at": self.at, "author": self.author, "kind": self.kind, "text": self.text}


@dataclass(frozen=True, slots=True)
class Dispatch:
    id: str
    project_id: str
    seq: int
    from_session: str
    kind: str
    title: str
    text: str
    status: str
    result: str
    created_at: str
    updated_at: str
    closed_at: str | None
    stalled_at: str | None

    @property
    def open(self) -> bool:
        return self.status == "open"

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "seq": self.seq,
            "from_session": self.from_session,
            "kind": self.kind,
            "title": self.title,
            "text": self.text,
            "status": self.status,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
            "stalled_at": self.stalled_at,
        }


def _dispatch(row: Any) -> Dispatch:
    return Dispatch(
        id=row["id"],
        project_id=row["project_id"],
        seq=int(row["seq"]),
        from_session=row["from_session"],
        kind=row["kind"],
        title=row["title"],
        text=row["text"],
        status=row["status"],
        result=row["result"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
        stalled_at=row["stalled_at"],
    )


def _message(row: Any) -> DispatchMessage:
    return DispatchMessage(id=int(row["id"]), dispatch_id=row["dispatch_id"], at=row["at"], author=row["author"], kind=row["kind"], text=row["text"])


def _clean(value: str, field: str, limit: int) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        raise DispatchError(f"{field} is at most {limit} characters")
    return text


class DispatchStore:
    """Every dispatch of every project, with its messages."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, project_id: str, *, text: str, title: str = "", from_session: str = "", kind: str = "work") -> Dispatch:
        body = _clean(text, "a dispatch's text", TEXT_MAX)
        if not body:
            raise DispatchError("a dispatch says what to do")
        if kind not in KINDS:
            raise DispatchError(f"a dispatch is work or setup, not {kind!r}")
        heading = " ".join(_clean(title, "a dispatch's title", 1000).split())[:TITLE_MAX]
        at = _now()
        for _ in range(ID_ATTEMPTS):
            dispatch_id = _new_id()
            try:
                async with self._db.transaction() as conn:
                    cursor = await conn.execute("SELECT COALESCE(max(seq), 0) + 1 FROM dispatches WHERE project_id = ?", (project_id,))
                    row = await cursor.fetchone()
                    await cursor.close()
                    seq = int(row[0]) if row is not None else 1
                    await conn.execute(
                        "INSERT INTO dispatches(id, project_id, seq, from_session, kind, title, text, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                        (dispatch_id, project_id, seq, from_session, kind, heading, body, at, at),
                    )
            except sqlite3.IntegrityError:
                if await self.get(dispatch_id) is None:
                    raise
                continue
            made = await self.get(dispatch_id)
            assert made is not None
            return made
        raise DispatchError(f"no free dispatch id after {ID_ATTEMPTS} draws")

    async def get(self, dispatch_id: str) -> Dispatch | None:
        row = await self._db.fetchone("SELECT * FROM dispatches WHERE id = ?", ((dispatch_id or "").strip().lower(),))
        return _dispatch(row) if row is not None else None

    async def open_for(self, project_id: str | None = None) -> list[Dispatch]:
        """Open dispatches, oldest first; of one project, or of every project."""
        if project_id is None:
            rows = await self._db.fetchall("SELECT * FROM dispatches WHERE status = 'open' ORDER BY created_at, rowid")
        else:
            rows = await self._db.fetchall("SELECT * FROM dispatches WHERE status = 'open' AND project_id = ? ORDER BY created_at, rowid", (project_id,))
        return [_dispatch(r) for r in rows]

    async def recent(self, *, project_id: str | None = None, limit: int = 30) -> list[Dispatch]:
        """The newest dispatches, open or closed, newest first."""
        if project_id is None:
            rows = await self._db.fetchall("SELECT * FROM dispatches ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        else:
            rows = await self._db.fetchall("SELECT * FROM dispatches WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?", (project_id, limit))
        return [_dispatch(r) for r in rows]

    async def first(self, project_id: str) -> Dispatch | None:
        """The project's dispatch #1."""
        row = await self._db.fetchone("SELECT * FROM dispatches WHERE project_id = ? AND seq = 1", (project_id,))
        return _dispatch(row) if row is not None else None

    async def messages(self, dispatch_id: str, *, limit: int = 50) -> list[DispatchMessage]:
        """The dispatch's messages, oldest first (the newest ``limit`` of them)."""
        rows = await self._db.fetchall("SELECT * FROM (SELECT * FROM dispatch_messages WHERE dispatch_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id", (dispatch_id, limit))
        return [_message(r) for r in rows]

    async def last_messages(self, dispatch_ids: list[str]) -> dict[str, DispatchMessage]:
        """The latest message of each dispatch, in one query."""
        if not dispatch_ids:
            return {}
        marks = ", ".join("?" for _ in dispatch_ids)
        rows = await self._db.fetchall(
            f"SELECT m.* FROM dispatch_messages m JOIN (SELECT dispatch_id, max(id) AS id FROM dispatch_messages WHERE dispatch_id IN ({marks}) GROUP BY dispatch_id) last ON last.id = m.id",  # noqa: S608 — only placeholders are formatted in
            tuple(dispatch_ids),
        )
        return {r["dispatch_id"]: _message(r) for r in rows}

    async def add_message(self, dispatch_id: str, *, author: str, kind: str, text: str) -> DispatchMessage:
        """Write a message on a dispatch; it counts as activity, so the dispatch is no longer stalled."""
        if author not in AUTHORS:
            raise DispatchError(f"a dispatch's message comes from {', '.join(AUTHORS)}, not {author!r}")
        body = _clean(text, "a dispatch's message", TEXT_MAX)
        if not body:
            raise DispatchError("a message needs text")
        at = _now()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO dispatch_messages(dispatch_id, at, author, kind, text) VALUES (?, ?, ?, ?, ?)", (dispatch_id, at, author, kind[:30], body)
            )
            message_id = cursor.lastrowid
            await cursor.close()
            await conn.execute("UPDATE dispatches SET updated_at = ?, stalled_at = NULL WHERE id = ?", (at, dispatch_id))
        return DispatchMessage(id=int(message_id or 0), dispatch_id=dispatch_id, at=at, author=author, kind=kind[:30], text=body)

    async def reopen(self, dispatch_id: str) -> bool:
        """A blocked dispatch that got its follow-up is open again; whether it was blocked."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE dispatches SET status = 'open', closed_at = NULL, updated_at = ?, stalled_at = NULL WHERE id = ? AND status = 'blocked'", (_now(), dispatch_id)
            )
            changed = cursor.rowcount == 1
            await cursor.close()
        return changed

    async def close(self, dispatch_id: str, status: str, result: str) -> bool:
        """Close an open dispatch; True for the one call that closed it."""
        if status not in ("done", "blocked", "cancelled"):
            raise DispatchError(f"a dispatch closes as done, blocked or cancelled, not {status!r}")
        text = (result or "").strip()[:RESULT_MAX]
        at = _now()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE dispatches SET status = ?, result = ?, closed_at = ?, updated_at = ? WHERE id = ? AND status = 'open'", (status, text, at, at, dispatch_id)
            )
            changed = cursor.rowcount == 1
            await cursor.close()
        return changed

    async def mark_stalled(self, dispatch_id: str, *, quiet_since: str) -> bool:
        """Mark an open dispatch stalled, once per silence: only while it is still open, not marked,
        and nothing happened on it since ``quiet_since``."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE dispatches SET stalled_at = ? WHERE id = ? AND status = 'open' AND stalled_at IS NULL AND updated_at <= ?", (_now(), dispatch_id, quiet_since)
            )
            changed = cursor.rowcount == 1
            await cursor.close()
        return changed

    async def quiet(self, before: str) -> list[Dispatch]:
        """Open dispatches nothing has happened on since ``before``, and not yet said to be stalled."""
        rows = await self._db.fetchall("SELECT * FROM dispatches WHERE status = 'open' AND stalled_at IS NULL AND updated_at <= ? ORDER BY updated_at", (before,))
        return [_dispatch(r) for r in rows]

    async def touch(self, dispatch_id: str) -> None:
        """Something happened on the dispatch without a message of its own (a linked question answered)."""
        await self._db.execute("UPDATE dispatches SET updated_at = ?, stalled_at = NULL WHERE id = ? AND status = 'open'", (_now(), dispatch_id))


__all__ = ["CLOSED", "Dispatch", "DispatchError", "DispatchMessage", "DispatchStore", "STATUSES"]
