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
from collections.abc import Iterable
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
    system: str = ""
    """Non-empty on a project the installation made for itself rather than the operator: ``"voice"``
    is the folder the concierge and the agents it delegates to work in. A system project keeps its
    name and its snapshot switch editable and refuses the two things that would break the feature
    behind it — being moved to another folder, and being removed."""

    @classmethod
    def load(cls, raw: Any) -> ProjectSettings:
        data = raw if isinstance(raw, dict) else {}
        return cls(snapshots=bool(data.get("snapshots", False)), system=str(data.get("system") or ""))

    def dump(self) -> dict[str, Any]:
        return {"snapshots": self.snapshots, "system": self.system}


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
            "system": self.settings.system,
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


PSEUDO_FILESYSTEMS = (Path("/proc"), Path("/sys"), Path("/dev"), Path("/run"))
"""Kernel interfaces the operating system mounts, not folders with work in them. A project rooted on
one of them would list a running machine's processes and devices as if they were files to edit."""


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
    for pseudo in PSEUDO_FILESYSTEMS:
        if path == pseudo or pseudo in path.parents:
            raise ProjectError(f"{pseudo} is the kernel's, not a folder of work; a project is a folder with files in it")
    if path.is_file():
        raise ProjectError(f"{path} is a file, not a folder")
    return path


class ProjectStore:
    """The projects table, and the one column on ``sessions`` that points into it."""

    def __init__(self, db: Database, *, reserved: Iterable[Path] = (), home: Path | None = None) -> None:
        self._db = db
        self._roots: tuple[Path, ...] = ()
        self._reserved = tuple(dict.fromkeys(Path(os.path.normpath(Path(p).expanduser())) for p in reserved))
        """This installation's own directories. A project may not be one, contain one or sit inside one."""
        self._home = Path(os.path.normpath(Path(home).expanduser())) if home is not None else None
        """The operator's home folder, refused as a whole and allowed one folder in: every project
        lives inside it, and taking the whole of it is what switches off the rule that asks before a
        path in the home folder is touched."""

    @property
    def roots(self) -> tuple[Path, ...]:
        """The folders that are projects, as the last read of the table saw them.

        The tool policy is built while a call is being judged, which is not a place to wait on a
        query. Every write below refreshes this, and the manager reads the table on the way up, so
        the list is current whenever a project has ever existed in this process.
        """
        return self._roots

    async def list(self) -> list[Project]:
        rows = await self._db.fetchall("SELECT * FROM projects ORDER BY name COLLATE NOCASE")
        projects = [_row(r) for r in rows]
        self._roots = tuple(p.root for p in projects)
        return projects

    async def get(self, project_id: str) -> Project | None:
        row = await self._db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        return _row(row) if row is not None else None

    async def create(self, name: str, root: str, *, settings: ProjectSettings | None = None) -> Project:
        label = (name or "").strip()
        if not label:
            raise ProjectError("a project needs a name")
        path = normalise_root(root)
        self._refuse_reserved(path)
        await self._refuse_overlap(path)
        project = Project(id=uuid.uuid4().hex[:12], name=label, root=path, created_at=datetime.now(UTC), settings=settings or ProjectSettings())
        await self._db.execute(
            "INSERT INTO projects(id, name, root, created_at, settings) VALUES (?, ?, ?, ?, ?)",
            (project.id, project.name, str(project.root), project.created_at.isoformat(), json.dumps(project.settings.dump())),
        )
        await self.list()
        return project

    async def ensure_system(self, kind: str, *, name: str, root: Path) -> Project:
        """The installation's own project of this kind, made the first time something needs it.

        Its folder is ours, not the operator's: it sits under the workspaces root, which
        :meth:`_refuse_reserved` forbids an operator project precisely because it belongs to the
        installation. So the reserved check is skipped here and nowhere else, and the overlap check
        is kept — an operator project that already contains this folder would make containment mean
        two things at once, whoever created which first.
        """
        for existing in await self.list():
            if existing.settings.system == kind:
                return existing
        path = Path(os.path.normpath(Path(root).expanduser()))
        await self._refuse_overlap(path)
        project = Project(id=uuid.uuid4().hex[:12], name=name, root=path, created_at=datetime.now(UTC), settings=ProjectSettings(system=kind))
        await self._db.execute(
            "INSERT INTO projects(id, name, root, created_at, settings) VALUES (?, ?, ?, ?, ?)",
            (project.id, project.name, str(project.root), project.created_at.isoformat(), json.dumps(project.settings.dump())),
        )
        await self.list()
        return project

    async def system(self, kind: str) -> Project | None:
        """The installation's project of this kind if it has been made; ``None`` before it is needed."""
        return next((p for p in await self.list() if p.settings.system == kind), None)

    async def summary(self, active: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
        """Per project — and under ``""`` the sessions with no project — how many, how busy, how recently.

        One query over the sessions table and no transcript read at all: the counts are right for an
        installation with more sessions than any one page of the list shows, which is the whole
        reason they are not counted from the rows the app was sent.
        """
        working = set(active)
        out: dict[str, dict[str, Any]] = {}
        rows = await self._db.fetchall("SELECT id, project_id, last_message_at, metadata FROM sessions")
        for row in rows:
            bucket = out.setdefault(row["project_id"] or "", {"total": 0, "active": 0, "loops": 0, "last_message_at": ""})
            bucket["total"] += 1
            if row["id"] in working:
                bucket["active"] += 1
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            loop = metadata.get("loop") if isinstance(metadata, dict) else None
            if isinstance(loop, dict) and loop.get("status") == "active":
                bucket["loops"] += 1
            last = str(row["last_message_at"] or "")
            if last > bucket["last_message_at"]:
                bucket["last_message_at"] = last
        return out

    async def update(self, project_id: str, *, name: str | None = None, root: str | None = None, settings: ProjectSettings | None = None) -> Project:
        project = await self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        label = project.name if name is None else (name or "").strip()
        if not label:
            raise ProjectError("a project needs a name")
        path = project.root if root is None else normalise_root(root)
        if path != project.root:
            if project.settings.system:
                raise ProjectError(f"{project.name} is the installation's own folder and cannot be moved")
            self._refuse_reserved(path)
            await self._refuse_overlap(path, ignore=project_id)
        merged = project.settings if settings is None else settings
        if merged.system != project.settings.system:
            # The flag is what makes the two refusals above stick; nothing outside this module sets it.
            merged = ProjectSettings(snapshots=merged.snapshots, system=project.settings.system)
        await self._db.execute(
            "UPDATE projects SET name = ?, root = ?, settings = ? WHERE id = ?",
            (label, str(path), json.dumps(merged.dump()), project_id),
        )
        await self.list()
        return Project(id=project.id, name=label, root=path, created_at=project.created_at, settings=merged)

    async def delete(self, project_id: str) -> None:
        """Forget the project. Nothing on disk is touched: the folder is the operator's, not ours."""
        project = await self.get(project_id)
        if project is not None and project.settings.system:
            raise ProjectError(f"{project.name} is the installation's own project and cannot be removed")
        await self._db.execute("UPDATE sessions SET project_id = NULL WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await self.list()

    def _refuse_reserved(self, path: Path) -> None:
        """The folders that are the installation's, or are the whole of the operator's home.

        A project root is inside the wall as well as outside it: every path under it is reachable to
        the agents of that project, and it is in the policy's open roots, which is what makes the
        home-folder question stop being asked for anything under it. So the state directory, the
        secrets, the workspaces root and the two checkouts are refused in both directions — as the
        root, above it and below it — and the home folder is refused as a whole while any folder
        inside it stays the ordinary case.
        """
        if self._home is not None and path == self._home:
            raise ProjectError(f"{path} is your home folder; a project is a folder inside it, not the whole of it")
        for reserved in self._reserved:
            if path == reserved:
                raise ProjectError(f"{path} belongs to the installation itself and cannot be a project")
            if reserved in path.parents:
                raise ProjectError(f"{path} is inside {reserved}, which belongs to the installation itself")
            if path in reserved.parents:
                raise ProjectError(f"{path} contains {reserved}, which belongs to the installation itself")

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
