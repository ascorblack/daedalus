"""What a terminal's owner means to the terminals service: whether it exists, its name, its project,
where its terminals start and what a sandboxed one of them may write.

Kept behind a small interface so the service does not hold the session manager: the service is
tested against a fake of this, and the manager is the one implementation the application uses.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from daedalus.terminals.model import Owner

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager


class Owners(Protocol):
    async def exists(self, owner: Owner) -> bool: ...

    async def labels(self, owners: Iterable[Owner]) -> dict[Owner, str]: ...

    async def project_of(self, owner: Owner) -> str | None: ...

    async def default_cwd(self, env: str, owner: Owner, project_id: str | None) -> str | None:
        """An absolute path in ``env``'s filesystem, or ``None`` for the environment's home."""
        ...

    async def sandbox_writable(self, env: str, owner: Owner, project_id: str | None, cwd: str) -> list[str]: ...


class ManagerOwners:
    """The owners as the session manager and the database know them."""

    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager
        self.db = manager.db
        self.local_env = manager.projects.local_env

    async def exists(self, owner: Owner) -> bool:
        if owner.kind == "free":
            return True
        table = {"session": "sessions", "project": "projects", "staff": "staff"}[owner.kind]
        return await self.db.fetchone(f"SELECT 1 FROM {table} WHERE id = ?", (owner.id,)) is not None  # noqa: S608 — the table is one of three literals

    async def labels(self, owners: Iterable[Owner]) -> dict[Owner, str]:
        wanted: dict[str, set[str]] = {}
        for owner in owners:
            if owner.id:
                wanted.setdefault(owner.kind, set()).add(owner.id)
        out: dict[Owner, str] = {}
        for kind, column, table in (("session", "title", "sessions"), ("project", "name", "projects"), ("staff", "name", "staff")):
            ids = sorted(wanted.get(kind, ()))
            if not ids:
                continue
            marks = ",".join("?" * len(ids))
            for row in await self.db.fetchall(f"SELECT id, {column} AS label FROM {table} WHERE id IN ({marks})", ids):  # noqa: S608 — literals and placeholders only
                out[Owner(kind, row["id"])] = str(row["label"] or "")
        return out

    async def project_of(self, owner: Owner) -> str | None:
        if owner.kind == "project":
            return owner.id
        if owner.kind == "session":
            row = await self.db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (owner.id,))
            return row["project_id"] if row else None
        if owner.kind == "staff":
            row = await self.db.fetchone("SELECT project_id FROM staff WHERE id = ?", (owner.id,))
            return row["project_id"] if row else None
        return None

    async def default_cwd(self, env: str, owner: Owner, project_id: str | None) -> str | None:
        """Where the owner's work is, in the environment the terminal runs in.

        A session in this process's own environment starts in its workspace — the same directory its
        agent's tools work in. Anywhere else the paths of this process mean nothing, so a terminal
        starts in the owner's project's first folder of that environment, or at home.
        """
        if owner.kind == "session" and env == self.local_env:
            state = await self.manager.get_state(str(owner.id))
            if state is not None:
                return str(state.workspace)
        project = await self.manager.projects.get(project_id) if project_id else None
        if project is None:
            return None
        for folder in project.folders:
            if folder.env == env and Path(folder.path).is_absolute():
                return str(folder.path)
        return None

    async def sandbox_writable(self, env: str, owner: Owner, project_id: str | None, cwd: str) -> list[str]:
        """What a sandboxed terminal may write: exactly what the owner's agent may write, when there is
        one here; otherwise the owner's project folders of that environment, or only the directory it
        starts in."""
        if owner.kind == "session" and env == self.local_env:
            services = self.manager.locator_services(str(owner.id))
            if services is None and await self.manager.get_state(str(owner.id)) is not None:
                services = self.manager.locator_services(str(owner.id))
            if services is not None:
                return [str(p) for p in services.sandbox_writable()]
        project = await self.manager.projects.get(project_id) if project_id else None
        if project is not None:
            folders = [str(f.path) for f in project.folders if f.env == env and not f.readonly]
            if folders:
                return folders
        return [cwd] if cwd else []


__all__ = ["ManagerOwners", "Owners"]
