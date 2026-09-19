"""Projects: the folder and grouping every session belongs to."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
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

    ``snapshots`` is off by default for a folder the operator points at: it may be a large existing
    repository, and committing a hundred thousand files into ``.checkpoints`` twice a turn costs
    more than the undo is worth. Automatically created projects opt in because they begin empty.
    """

    snapshots: bool = False
    system: str = ""
    """Non-empty on a project the installation made for itself rather than the operator: ``"voice"``
    is the folder the concierge and the agents it delegates to work in. A system project keeps its
    name and snapshot switch editable but cannot be removed."""

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
    managed: bool = False
    """Whether the folder is one the installation chose for itself — a root under the managed
    projects tree, made when the project was made — rather than a folder the operator pointed at.

    The two are missing for opposite reasons, so they cannot share one answer. A managed folder that
    is not there was never created or has been swept away, and nobody but this installation was ever
    going to create it: it is remade on demand. A folder the operator named is theirs, and an empty
    directory conjured at its path would shadow the real one when the mount or the disk comes back —
    so that one stays the error it is, naming the path so they can restore or mount it.
    """

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


def _row(row: Any, managed_root: Path | None = None) -> Project:
    try:
        settings = json.loads(row["settings"] or "{}")
    except (TypeError, ValueError):
        settings = {}
    root = Path(row["root"])
    return Project(
        id=row["id"],
        name=row["name"],
        root=root,
        created_at=datetime.fromisoformat(row["created_at"]),
        settings=ProjectSettings.load(settings),
        managed=_is_managed(root, managed_root),
    )


def _is_managed(root: Path, managed_root: Path | None) -> bool:
    """Whether this root is one of ours: a folder *inside* the managed projects tree.

    The tree itself is not a project and never a root, so containment is the whole test — a path
    equal to the tree is as foreign here as one outside it.
    """
    return managed_root is not None and managed_root in root.parents


def _nested_under(session_id: str, metadata: dict[str, Any], known: set[str]) -> bool:
    """Whether the Agents screen draws this session inside another row rather than as one of its own.

    The same rule the screen nests by, and it has to stay the same rule or the count over a folder
    stops describing the list under it: a subagent hangs under its leader, a fork under the session
    it was taken from, and one whose leader or origin no longer exists hangs under nothing.
    """
    leader = metadata.get("subagent_of")
    if isinstance(leader, str) and leader in known:
        return True
    forked = metadata.get("forked_from")
    origin = forked.get("session_id") if isinstance(forked, dict) else None
    return isinstance(origin, str) and origin != session_id and origin in known


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

    def __init__(self, db: Database, *, managed_root: Path | None = None, reserved: Iterable[Path] = (), home: Path | None = None) -> None:
        self._db = db
        self._write = asyncio.Lock()
        """Held across the whole of a read-check-insert. Both ways of making a project look the table
        up and then write to it, with awaits in between; nothing else serialised them, so two callers
        that asked at the same moment each saw a table without the row the other was about to add."""
        self._roots: tuple[Path, ...] = ()
        self._reserved = tuple(dict.fromkeys(Path(os.path.normpath(Path(p).expanduser())) for p in reserved))
        """This installation's own directories. A project may not be one, contain one or sit inside one."""
        self._home = Path(os.path.normpath(Path(home).expanduser())) if home is not None else None
        """The operator's home folder, refused as a whole and allowed one folder in: every project
        lives inside it, and taking the whole of it is what switches off the rule that asks before a
        path in the home folder is touched."""
        self._managed_root = Path(os.path.normpath(managed_root.expanduser())) if managed_root is not None else None

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
        projects = [_row(r, self._managed_root) for r in rows]
        self._roots = tuple(p.root for p in projects)
        return projects

    async def get(self, project_id: str) -> Project | None:
        row = await self._db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        return _row(row, self._managed_root) if row is not None else None

    async def create(self, name: str, root: str | None = None, *, settings: ProjectSettings | None = None, project_id: str | None = None) -> Project:
        label = (name or "").strip()
        if not label:
            raise ProjectError("a project needs a name")
        project_id = project_id or uuid.uuid4().hex[:12]
        if root is None:
            if self._managed_root is None:
                raise ProjectError("automatic project folders are not configured")
            path = self._managed_root / project_id
        else:
            path = normalise_root(root)
            self._refuse_reserved(path)
        async with self._write:
            await self._refuse_overlap(path)
            if root is None:
                self._make_root(path)
            project = Project(
                id=project_id,
                name=label,
                root=path,
                created_at=datetime.now(UTC),
                settings=settings or ProjectSettings(),
                managed=_is_managed(path, self._managed_root),
            )
            await self._insert(project)
            await self.list()
        return project

    async def for_root(self, root: Path) -> Project | None:
        target = Path(os.path.normpath(root.expanduser()))
        return next((project for project in await self.list() if project.root == target), None)

    async def adopt_directory(self, name: str, root: Path) -> Project:
        """Make an internal working directory a project, or return the project already owning it."""
        existing = await self.for_root(root)
        if existing is not None:
            return existing
        label = (name or "").strip() or root.name or "Project"
        path = Path(os.path.normpath(root.expanduser()))
        async with self._write:
            existing = await self.for_root(path)
            if existing is not None:
                return existing
            await self._refuse_overlap(path)
            project = Project(
                id=uuid.uuid4().hex[:12],
                name=label,
                root=path,
                created_at=datetime.now(UTC),
                settings=ProjectSettings(snapshots=True),
                managed=_is_managed(path, self._managed_root),
            )
            await self._insert(project)
            await self.list()
            return project

    async def _insert(self, project: Project) -> None:
        """One row, with the system flag written to its own column as well as into the settings blob.

        The column exists for the partial unique index over it: the flag is what makes a project the
        installation's own, and an invariant a query enforces holds even when the code that was
        meant to check it lost a race.
        """
        await self._db.execute(
            "INSERT INTO projects(id, name, root, created_at, settings, system) VALUES (?, ?, ?, ?, ?, ?)",
            (
                project.id,
                project.name,
                str(project.root),
                project.created_at.isoformat(),
                json.dumps(project.settings.dump()),
                project.settings.system,
            ),
        )

    async def ensure_system(self, kind: str, *, name: str, root: Path) -> Project:
        """The installation's own project of this kind, made the first time something needs it.

        Its folder sits under the managed projects root. The overlap check is kept — another project
        that already contains this folder would make containment mean two things at once.

        One per kind, and the database says so: the check and the insert are held under one lock, and
        a partial unique index over the ``system`` column is what makes the invariant survive a lost
        race rather than turn into a second undeletable folder.
        """
        async with self._write:
            existing = await self.system(kind)
            if existing is not None:
                # The row can outlive its folder — an upgrade that wrote the row from the sessions it
                # found, a sweep, a restore of the database without the tree beside it. This is the
                # one call every user of a system project already makes, so it is where the folder is
                # put back rather than at each of them.
                await self.ensure_reachable(existing)
                return existing
            path = Path(os.path.normpath(Path(root).expanduser()))
            await self._refuse_overlap(path)
            self._make_root(path)
            project = Project(
                id=uuid.uuid4().hex[:12],
                name=name,
                root=path,
                created_at=datetime.now(UTC),
                settings=ProjectSettings(system=kind),
                managed=_is_managed(path, self._managed_root),
            )
            try:
                await self._insert(project)
            except sqlite3.IntegrityError:
                # The unique index refused a second project of this kind. Somebody else made it —
                # from another process against the same file, which is the one race the lock above
                # cannot see. Read theirs rather than raising at a caller that only asked for it.
                made = await self.system(kind)
                if made is None:
                    raise
                return made
            await self.list()
        return project

    async def ensure_reachable(self, project: Project) -> bool:
        """Whether the folder can be worked in — making it first when it is one of ours.

        Every guard that refuses to start something in a project asks this instead of reading
        ``reachable`` directly. For a folder under the managed tree the answer is a folder: nothing
        outside this installation was ever going to create it, and a row whose directory is missing
        is a gap in our own bookkeeping, not news for the operator. For a folder they pointed at the
        answer is the plain truth, and the caller raises with the path in it.
        """
        if project.managed and not project.root.is_dir():
            await asyncio.to_thread(self._make_root, project.root)
        return project.reachable

    async def ensure_roots(self) -> list[Project]:
        """Read the table and put back any of our own folders that are not on disk.

        Called on the way up, so a run started straight after a start does not have to be the thing
        that discovers the gap.
        """
        projects = await self.list()
        for project in projects:
            if project.managed and not project.root.is_dir():
                await asyncio.to_thread(self._make_root, project.root)
        return projects

    def _make_root(self, path: Path) -> None:
        """Create one of our own project folders the way the ones beside it were created.

        Mode and ownership are copied from a sibling under the managed tree, or from the tree itself
        when there is no sibling yet: a folder made by a process running as somebody else would
        otherwise be a workspace the bot cannot write into, which is the same outage wearing a
        different message. Both are best effort — a filesystem that will not take a chown is not a
        reason to leave the folder unmade.
        """
        template = self._template_root(path)
        fresh = [p for p in (path, *path.parents) if self._managed_root is not None and self._managed_root in p.parents and not p.exists()]
        path.mkdir(parents=True, exist_ok=True)
        inbox = path / "inbox"
        if not inbox.exists():
            inbox.mkdir()
            fresh.append(inbox)
        if template is None:
            return
        try:
            stat = template.stat()
        except OSError:
            return
        for made in fresh:
            try:
                os.chmod(made, stat.st_mode & 0o7777)
            except OSError:
                pass
            try:
                os.chown(made, stat.st_uid, stat.st_gid)
            except (OSError, AttributeError):
                pass

    def _template_root(self, path: Path) -> Path | None:
        """A folder already under the managed tree to copy mode and ownership from."""
        root = self._managed_root
        if root is None or not root.is_dir():
            return None
        try:
            siblings = sorted(child for child in root.iterdir() if child.is_dir() and child != path)
        except OSError:
            siblings = []
        return siblings[0] if siblings else root

    async def system(self, kind: str) -> Project | None:
        """The installation's project of this kind if it has been made; ``None`` before it is needed."""
        return next((p for p in await self.list() if p.settings.system == kind), None)

    async def summary(self, active: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
        """Per project — and under ``""`` the sessions with no project — how many, how busy, how recently.

        One query over the sessions table and no transcript read at all: the counts are right for an
        installation with more sessions than any one page of the list shows, which is the whole
        reason they are not counted from the rows the app was sent.

        A session the screen draws *inside* another row is not counted: a subagent is listed under
        its leader and a fork under the session it was taken from, so counting them made a folder
        header say "3 agents" over a body that listed two. Whose leader or origin is gone is nobody's
        child and is counted, which is exactly the rule the screen nests by.
        """
        working = set(active)
        out: dict[str, dict[str, Any]] = {}
        rows = await self._db.fetchall("SELECT id, project_id, last_message_at, metadata FROM sessions")
        parsed: list[tuple[Any, dict[str, Any]]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            parsed.append((row, metadata if isinstance(metadata, dict) else {}))
        known = {row["id"] for row, _ in parsed}
        for row, metadata in parsed:
            if _nested_under(row["id"], metadata, known):
                continue
            bucket = out.setdefault(str(row["project_id"]), {"total": 0, "active": 0, "loops": 0, "last_message_at": ""})
            bucket["total"] += 1
            if row["id"] in working:
                bucket["active"] += 1
            loop = metadata.get("loop")
            if isinstance(loop, dict) and loop.get("status") == "active":
                bucket["loops"] += 1
            last = str(row["last_message_at"] or "")
            if last > bucket["last_message_at"]:
                bucket["last_message_at"] = last
        return out

    async def update(self, project_id: str, *, name: str | None = None, settings: ProjectSettings | None = None) -> Project:
        project = await self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        label = project.name if name is None else (name or "").strip()
        if not label:
            raise ProjectError("a project needs a name")
        path = project.root
        merged = project.settings if settings is None else settings
        if merged.system != project.settings.system:
            # The flag is what makes the two refusals above stick; nothing outside this module sets it.
            merged = ProjectSettings(snapshots=merged.snapshots, system=project.settings.system)
        await self._db.execute(
            "UPDATE projects SET name = ?, root = ?, settings = json_patch(CASE WHEN json_valid(settings) THEN settings ELSE '{}' END, ?), system = ? WHERE id = ?",
            (label, str(path), json.dumps(merged.dump()), merged.system, project_id),
        )
        await self.list()
        return Project(id=project.id, name=label, root=path, created_at=project.created_at, settings=merged)

    async def delete(self, project_id: str) -> None:
        """Forget an empty project. Nothing on disk is touched."""
        project = await self.get(project_id)
        if project is not None and project.settings.system:
            raise ProjectError(f"{project.name} is the installation's own project and cannot be removed")
        if await self.sessions_of(project_id):
            raise ProjectError(f"{project.name if project else 'this project'} still has sessions")
        await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await self.list()

    def _refuse_reserved(self, path: Path) -> None:
        """The folders that are the installation's, or are the whole of the operator's home.

        A project root is inside the wall as well as outside it: every path under it is reachable to
        the agents of that project, and it is in the policy's open roots, which is what makes the
        home-folder question stop being asked for anything under it. So the state directory, the
        secrets and the two checkouts are refused in both directions — as the root, above it and
        below it. The managed projects root is refused as a whole but its children are exactly where
        automatic and adopted projects belong. The home folder is refused as a whole while any
        folder inside it stays the ordinary case.
        """
        if self._home is not None and path == self._home:
            raise ProjectError(f"{path} is your home folder; a project is a folder inside it, not the whole of it")
        for reserved in self._reserved:
            if path == reserved:
                raise ProjectError(f"{path} belongs to the installation itself and cannot be a project")
            if self._managed_root is not None and reserved == self._managed_root:
                if path in reserved.parents:
                    raise ProjectError(f"{path} contains {reserved}, which belongs to the installation itself")
                continue
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

    async def attach(self, session_id: str, project_id: str) -> None:
        await self._db.execute("UPDATE sessions SET project_id = ? WHERE id = ?", (project_id, session_id))

    async def sessions_of(self, project_id: str) -> list[dict[str, str]]:
        rows = await self._db.fetchall("SELECT id, title FROM sessions WHERE project_id = ? ORDER BY last_message_at DESC", (project_id,))
        return [{"id": r["id"], "title": r["title"]} for r in rows]

    async def by_session(self) -> dict[str, str]:
        """``{session_id: project_id}`` for every session that has one, in one query."""
        rows = await self._db.fetchall("SELECT id, project_id FROM sessions")
        return {r["id"]: r["project_id"] for r in rows}


__all__ = ["Project", "ProjectError", "ProjectSettings", "ProjectStore", "normalise_root"]
