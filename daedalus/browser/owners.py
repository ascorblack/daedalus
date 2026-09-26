"""What a browser group's owner means to the browser service: whether it still exists, and its name.

Kept behind a small interface so the service does not hold the session manager: the service is
tested against a fake of this, and the database is the one implementation the application uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from daedalus.browser.model import Owner

if TYPE_CHECKING:
    from daedalus.stores.database import Database


class BrowserOwners(Protocol):
    async def exists(self, owner: Owner) -> bool: ...

    async def label(self, owner: Owner) -> str:
        """The owner as a person reads it in a notification: a session's title, a member's name."""
        ...


class DatabaseOwners:
    """The owners as the database knows them."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def exists(self, owner: Owner) -> bool:
        table = "sessions" if owner.kind == "session" else "staff"
        return await self.db.fetchone(f"SELECT 1 FROM {table} WHERE id = ?", (owner.id,)) is not None  # noqa: S608 — one of two literals

    async def label(self, owner: Owner) -> str:
        if owner.kind == "session":
            row = await self.db.fetchone("SELECT title FROM sessions WHERE id = ?", (owner.id,))
            return str(row["title"] or "") if row is not None else owner.id
        row = await self.db.fetchone("SELECT name FROM staff WHERE id = ?", (owner.id,))
        return str(row["name"] or "") if row is not None else owner.id


__all__ = ["BrowserOwners", "DatabaseOwners"]
