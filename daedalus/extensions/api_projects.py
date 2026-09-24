"""The project routes: projects, their folders, their brief and their journal.

They live in a module of their own so the features built on projects (staff, the board of a project,
its orchestrator) add their routes here rather than growing ``api.py`` further. Like ``api.py`` this
module may import the HTTP framework; nothing below the extensions may.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.stores.projects import FolderSpec, Project, ProjectError, ProjectFolder, ProjectSettings

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

JOURNAL_NOTE_MAX_CHARS = 4000
"""An operator's note is a sentence or a paragraph; the journal is read as a list, not as documents."""
BRIEF_SECTION_MAX_CHARS = 20000
"""A brief section is re-read into the orchestrator's prompt every turn, so it is bounded."""

Env = Literal["container", "host"]


class FolderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    label: str = ""
    env: Env | None = None
    """Empty is the environment this process runs in."""
    readonly: bool = False


class ProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    folders: list[FolderBody] = Field(default_factory=list)
    """In order, the first the primary. None makes a scratch folder of the installation's own."""
    default_env: Env | None = None
    snapshots: bool = False


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    snapshots: bool | None = None
    default_env: Env | None = None
    keep: Literal[True] | None = None
    """Keep a chat's scratch project as a project of its own. There is no way back: an ephemeral
    project is one made implicitly, and nothing the operator does makes one."""


class FolderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    readonly: bool | None = None
    position: int | None = Field(default=None, ge=0)


class BriefBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: str
    body: str = Field(max_length=BRIEF_SECTION_MAX_CHARS)


class JournalNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=JOURNAL_NOTE_MAX_CHARS)


def host_bridge(settings: Any, terminals: Any = None) -> bool:
    """Whether a terminal daemon answers on the host, so a host folder can be worked in by anything.

    With the terminals service running, its live connection is the answer: a daemon that was killed
    leaves its ``endpoint`` behind, and a folder accepted on the strength of that file would be a row
    nothing could open. Without the service (the doctor, a test) the directory is read at the moment
    of asking, because the bridge is installed and removed while this process runs, and the directory
    is mounted either way; an empty or missing one is a bridge that is not installed.
    """
    if terminals is not None and terminals.configured("host"):
        return bool(terminals.available("host"))
    directory = getattr(settings, "terminals_host_dir", None)
    if not directory:
        return False
    return (Path(directory) / "endpoint").is_file()


def environments(settings: Any, local_env: str, terminals: Any = None) -> dict[str, Any]:
    """Which environments a folder of this installation may live in.

    Natively the process is on the host and there is no container. In Docker the container is where
    the process is, and the host is reachable only through its terminal bridge, and only by what runs
    in a terminal: a folder there without a bridge would be a row nothing could ever open.
    """
    bridge = host_bridge(settings, terminals)
    available = [local_env] + (["host"] if local_env == "container" and bridge else [])
    return {"local": local_env, "available": available, "host_bridge": bridge, "docker": local_env == "container"}


def reach(folder: ProjectFolder, local_env: str, bridge: bool) -> str:
    """Who can work in a folder: ``agents`` (every agent, this process's own tools included),
    ``terminals`` (only what runs in a host terminal: CLI staff and the operator's shells), or
    ``none``. Whether the folder is mounted yet is ``reachable``; this is whether it ever can be."""
    if folder.local(local_env):
        return "agents"
    if folder.env == "host" and bridge:
        return "terminals"
    return "none"


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None
    settings = app.settings

    def view(project: Project, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        bridge = host_bridge(settings, app.extensions.get("terminals"))
        body = project.view()
        for folder_view, folder in zip(body["folders"], project.folders, strict=True):
            folder_view["reach"] = reach(folder, manager.projects.local_env, bridge)
        return {**body, "sessions": sessions}

    async def sessions_of(project_id: str) -> list[dict[str, Any]]:
        # The narrower question on purpose: a session whose run has ended and whose snapshot is
        # still being written may not be rewritten, but it is not working, and a list that draws it
        # as running contradicts its own screen — which says idle, because it is.
        busy = manager.active_sessions()
        return [{**s, "running": s["id"] in busy} for s in await manager.projects.sessions_of(project_id)]

    async def existing(project_id: str) -> Project:
        project = await manager.projects.get(project_id)
        if project is None:
            raise HTTPException(404, "no such project")
        return project

    def refuse_env(env: str | None) -> None:
        """A folder or a default in an environment this installation cannot reach is refused at the door."""
        if env is None:
            return
        offered = environments(settings, manager.projects.local_env, app.extensions.get("terminals"))
        if env in offered["available"]:
            return
        if env == "host":
            raise HTTPException(400, "a host folder needs the host terminal bridge, which is not installed here; install it, or add the folder to the container")
        raise HTTPException(400, "this installation runs on the host without a container; a folder here is a host folder")

    async def changed(project_id: str, change: str) -> None:
        """Tell loaded sessions and everyone listening. A folder added, locked or removed changes what
        a loaded session may read and write, so its services are rebuilt at once, not at its next load."""
        project = await manager.projects.get(project_id)
        await manager.reload_project(project, project_id)
        await manager.bus.publish("project.changed", {"change": change, "actor": "operator"}, project_id=project_id)

    def named(sessions: Sequence[dict[str, str]]) -> str:
        return ", ".join(s["title"] or s["id"] for s in sessions)

    @api.get("/api/projects")
    async def list_projects(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """Every project, with who works in it and, per folder, whether this process can reach it.

        ``reachable`` is the Docker seam: the row exists as soon as the operator adds the folder, but
        in a container the folder is only there once it is bind-mounted, so the app can say "restart
        to mount this" instead of showing a project whose files are mysteriously absent. ``reach``
        is who could ever work there.
        """
        return [view(project, await sessions_of(project.id)) for project in await manager.projects.list()]

    @api.get("/api/project-environments")
    async def project_environments(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Where a folder may live, for the environment choice when a folder is added."""
        return environments(settings, manager.projects.local_env, app.extensions.get("terminals"))

    @api.post("/api/projects")
    async def create_project(body: ProjectBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        for folder in body.folders:
            refuse_env(folder.env)
        refuse_env(body.default_env)
        specs = [FolderSpec(f.path, label=f.label, env=f.env, readonly=f.readonly) for f in body.folders]
        # A scratch folder begins empty, so undo costs nothing there; a folder the operator points at
        # may be a large repository, where it is theirs to switch on.
        project_settings = ProjectSettings(snapshots=True if not specs else body.snapshots, default_env=body.default_env or "")
        try:
            project = await manager.projects.create(body.name, specs or None, settings=project_settings)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await manager.bus.publish("project.changed", {"change": "created", "actor": "operator"}, project_id=project.id)
        return view(project, [])

    @api.patch("/api/projects/{project_id}")
    async def patch_project(project_id: str, body: ProjectPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        refuse_env(body.default_env)
        try:
            project = await manager.projects.update(project_id, name=body.name, snapshots=body.snapshots, default_env=body.default_env, ephemeral=False if body.keep else None)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        # Loaded sessions keep their own immutable project value, so refresh their editable label
        # and snapshot setting after the row changes.
        await changed(project_id, "kept" if body.keep else "settings")
        return view(project, await sessions_of(project_id))

    @api.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget an empty project. Files are never deleted."""
        project = await existing(project_id)
        sessions = await manager.projects.sessions_of(project_id)
        if sessions:
            one = len(sessions) == 1
            raise HTTPException(409, f"{len(sessions)} agent{'' if one else 's'} {'works' if one else 'work'} in {project.name}; move or remove {'it' if one else 'them'} first")
        # What the project itself holds (its terminals) ends before the row goes, so nothing is left
        # running for an owner that no longer exists.
        for hook in manager.project_delete_hooks:
            try:
                await hook(project_id)
            except Exception:  # noqa: BLE001 — a hook that fails must not keep the project
                logger.exception("project delete hook failed for %s", project_id)
        try:
            await manager.projects.delete(project_id)
        except ProjectError as exc:
            raise HTTPException(409, str(exc)) from exc
        await manager.reload_project(None, project_id)
        await manager.bus.publish("project.changed", {"change": "removed", "actor": "operator"}, project_id=project_id)
        return {"ok": True}

    # -- folders -----------------------------------------------------------------------

    @api.post("/api/projects/{project_id}/folders")
    async def add_folder(project_id: str, body: FolderBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Another folder for the project. The answer is the whole project, so the new folder's
        ``reach`` and ``reachable`` say at once who can work in it and whether it is mounted yet."""
        await existing(project_id)
        refuse_env(body.env)
        try:
            await manager.projects.add_folder(project_id, body.path, label=body.label, env=body.env, readonly=body.readonly)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await changed(project_id, "folders")
        return view(await existing(project_id), await sessions_of(project_id))

    @api.patch("/api/projects/{project_id}/folders/{folder_id}")
    async def patch_folder(project_id: str, folder_id: str, body: FolderPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        project = await existing(project_id)
        folder = project.folder(folder_id)
        if folder is None:
            raise HTTPException(404, "no such folder in this project")
        if body.position is not None:
            # A session that names no folder works in the primary, whichever folder that is. Changing
            # which one is first would move it into another folder mid-work, silently.
            becomes_primary = body.position == 0 and folder.position != 0
            stops_primary = folder.position == 0 and body.position != 0
            if becomes_primary or stops_primary:
                working = await manager.projects.sessions_by_default(project_id)
                if working:
                    one = len(working) == 1
                    raise HTTPException(409, f"{named(working)} {'works' if one else 'work'} in the primary folder {project.primary.path}; changing which folder is first would move {'it' if one else 'them'}")
        try:
            await manager.projects.update_folder(project_id, folder_id, label=body.label, readonly=body.readonly, position=body.position)
        except KeyError as exc:
            raise HTTPException(404, "no such folder in this project") from exc
        await changed(project_id, "folders")
        return view(await existing(project_id), await sessions_of(project_id))

    @api.delete("/api/projects/{project_id}/folders/{folder_id}")
    async def remove_folder(project_id: str, folder_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget a folder; nothing on disk is touched. Refused, by name, while a session works in it."""
        project = await existing(project_id)
        folder = project.folder(folder_id)
        if folder is None:
            raise HTTPException(404, "no such folder in this project")
        if len(project.folders) == 1:
            raise HTTPException(409, f"{folder.path} is the only folder of {project.name}; a project needs at least one")
        working = await manager.projects.sessions_in_folder(project_id, folder_id)
        if working:
            one = len(working) == 1
            raise HTTPException(409, f"{named(working)} {'works' if one else 'work'} in {folder.path}; move or remove {'it' if one else 'them'} first")
        try:
            await manager.projects.remove_folder(project_id, folder_id)
        except ProjectError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "no such folder in this project") from exc
        await changed(project_id, "folders")
        return view(await existing(project_id), await sessions_of(project_id))

    # -- the brief and the journal -----------------------------------------------------

    @api.get("/api/projects/{project_id}/brief")
    async def get_brief(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        sections = await manager.projects.brief(project_id)
        return {"sections": [{"section": s.section, "body": s.body, "updated_at": s.updated_at, "updated_by": s.updated_by} for s in sections.values()]}

    @api.put("/api/projects/{project_id}/brief")
    async def put_brief(project_id: str, body: BriefBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator writes a section. Every section is theirs to write, including the one that
        bounds what may be granted without asking them."""
        await existing(project_id)
        try:
            written = await manager.projects.set_brief(project_id, body.section, body.body, "operator")
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await manager.bus.publish("project.changed", {"change": "brief", "actor": "operator"}, project_id=project_id)
        return {"section": written.section, "body": written.body, "updated_at": written.updated_at, "updated_by": written.updated_by}

    @api.get("/api/projects/{project_id}/journal")
    async def get_journal(project_id: str, before: int | None = None, limit: int = 50, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Newest first, one page; ``next_before`` is what to ask with for the page after, or null at the end."""
        await existing(project_id)
        entries = await manager.projects.journal(project_id, before=before, limit=limit)
        wanted = max(1, min(int(limit), 200))
        return {"entries": [e.view() for e in entries], "next_before": entries[-1].id if len(entries) == wanted else None}

    @api.post("/api/projects/{project_id}/journal")
    async def post_journal(project_id: str, body: JournalNote, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "a note needs some text")
        entry = await manager.projects.record(project_id, "operator", "note", text)
        await manager.bus.publish("project.changed", {"change": "journal", "actor": "operator"}, project_id=project_id)
        return entry.view()


__all__ = ["environments", "host_bridge", "reach", "register"]
