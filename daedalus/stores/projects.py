"""Projects: a folder the operator adds, and the sessions that work inside it.

A session without a project keeps the directory of its own under the workspaces root — that is
what every session had before projects existed and what a session created without one still gets.
A session *with* a project works in the project's root: that root is its workspace, the only place
its file tools may reach, and the writable set the sandbox gives it.

The root is stored as the operator typed it, absolute, and is not required to exist: in a container
a folder becomes reachable only once it is bind-mounted, and a row the API refuses to keep is a row
the launcher cannot mount. Reachability is reported, not enforced.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daedalus.stores.database import Database


class ProjectError(ValueError):
    """A project the store will not keep: a relative root, a file, or a root tangled with another's."""


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    """What the operator decides per project.

    ``snapshots`` is off by default and that is deliberate. A per-session workspace holds what one
    agent made and is snapshotted before every turn; a project root is somebody's repository, and
    committing a hundred thousand files into ``.checkpoints`` twice a turn costs more than the undo
    is worth. Switched on, a project snapshots exactly as a workspace does, ``ops.checkpoint_max_gb``
    included.
    """

    snapshots: bool = False

    @classmethod
    def load(cls, raw: Any) -> ProjectSettings:
        data = raw if isinstance(raw, dict) else {}
        return cls(snapshots=bool(data.get("snapshots", False)))

    def dump(self) -> dict[str, Any]:
        return {"snapshots": self.snapshots}


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    root: Path
    created_at: datetime
    settings: ProjectSettings = field(default_factory=ProjectSettings)

    @property
    def reachable(self) -> bool:
        """Whether this process can actually reach the folder — false in a container until it is mounted."""
        return self.root.is_dir() and os.access(self.root, os.R_OK)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "root": str(self.root),
            "created_at": self.created_at.isoformat(),
            "settings": self.settings.dump(),
            "reachable": self.reachable,
            "writable": self.reachable and os.access(self.root, os.W_OK),
        }


def _row(row: Any) -> Project:
    try:
        settings = json.loads(row["settings"] or "{}")
    except (TypeError, ValueError):
        settings = {}
    return Project(
        id=row["id"],
        name=row["name"],
        root=Path(row["root"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        settings=ProjectSettings.load(settings),
    )


def normalise_root(raw: str) -> Path:
    """The path a project is anchored at: absolute, ``~`` expanded, no trailing slash, no ``..``.

    Not resolved against the filesystem — a root that is not mounted yet has nothing to resolve
    against, and a root that IS mounted is realpath'd by the containment check every time it is
    used, which is where a symlink swapped later would have to be caught anyway.
    """
    text = (raw or "").strip()
    if not text:
        raise ProjectError("a project needs a folder")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ProjectError(f"a project folder must be an absolute path, not {text!r}")
    path = Path(os.path.normpath(path))
    if path == Path(path.root):
        raise ProjectError("the filesystem root is not a project")
    if path.is_file():
        raise ProjectError(f"{path} is a file, not a folder")
    return path


class ProjectStore:
    """The projects table, and the one column on ``sessions`` that points into it."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self) -> list[Project]:
        rows = await self._db.fetchall("SELECT * FROM projects ORDER BY name COLLATE NOCASE")
        return [_row(r) for r in rows]

    async def get(self, project_id: str) -> Project | None:
        row = await self._db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        return _row(row) if row is not None else None

    async def create(self, name: str, root: str, *, settings: ProjectSettings | None = None) -> Project:
        label = (name or "").strip()
        if not label:
            raise ProjectError("a project needs a name")
        path = normalise_root(root)
        await self._refuse_overlap(path)
        project = Project(id=uuid.uuid4().hex[:12], name=label, root=path, created_at=datetime.now(UTC), settings=settings or ProjectSettings())
        await self._db.execute(
            "INSERT INTO projects(id, name, root, created_at, settings) VALUES (?, ?, ?, ?, ?)",
            (project.id, project.name, str(project.root), project.created_at.isoformat(), json.dumps(project.settings.dump())),
        )
        return project

    async def update(self, project_id: str, *, name: str | None = None, root: str | None = None, settings: ProjectSettings | None = None) -> Project:
        project = await self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        label = project.name if name is None else (name or "").strip()
        if not label:
            raise ProjectError("a project needs a name")
        path = project.root if root is None else normalise_root(root)
        if path != project.root:
            await self._refuse_overlap(path, ignore=project_id)
        merged = project.settings if settings is None else settings
        await self._db.execute(
            "UPDATE projects SET name = ?, root = ?, settings = ? WHERE id = ?",
            (label, str(path), json.dumps(merged.dump()), project_id),
        )
        return Project(id=project.id, name=label, root=path, created_at=project.created_at, settings=merged)

    async def delete(self, project_id: str) -> None:
        """Forget the project. Nothing on disk is touched: the folder is the operator's, not ours."""
        await self._db.execute("UPDATE sessions SET project_id = NULL WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    async def _refuse_overlap(self, path: Path, *, ignore: str = "") -> None:
        """Two projects may not nest, because containment would then mean two different things at once.

        A session in the outer project may write anywhere in the inner one while the inner project's
        own sessions may not see out — a boundary that holds in one direction is not a boundary.
        """
        for other in await self.list():
            if other.id == ignore:
                continue
            if other.root == path:
                raise ProjectError(f"{other.name} is already that folder")
            if path in other.root.parents:
                raise ProjectError(f"that folder contains the project {other.name} ({other.root})")
            if other.root in path.parents:
                raise ProjectError(f"that folder is inside the project {other.name} ({other.root})")

    # -- the link to sessions ------------------------------------------------------

    async def for_session(self, session_id: str) -> Project | None:
        row = await self._db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (session_id,))
        pid = row["project_id"] if row is not None else None
        return await self.get(pid) if pid else None

    async def attach(self, session_id: str, project_id: str | None) -> None:
        await self._db.execute("UPDATE sessions SET project_id = ? WHERE id = ?", (project_id, session_id))

    async def sessions_of(self, project_id: str) -> list[dict[str, str]]:
        rows = await self._db.fetchall("SELECT id, title FROM sessions WHERE project_id = ? ORDER BY last_message_at DESC", (project_id,))
        return [{"id": r["id"], "title": r["title"]} for r in rows]

    async def by_session(self) -> dict[str, str]:
        """``{session_id: project_id}`` for every session that has one, in one query."""
        rows = await self._db.fetchall("SELECT id, project_id FROM sessions WHERE project_id IS NOT NULL")
        return {r["id"]: r["project_id"] for r in rows}


__all__ = ["Project", "ProjectError", "ProjectSettings", "ProjectStore", "normalise_root"]
