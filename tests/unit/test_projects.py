"""Projects: the folder is the wall.

A session in a project reads and writes inside its root and nowhere else — through the tools and
through the API — and a session without one behaves exactly as every session did before projects
existed. The rest is the store, the migration that makes room for it, and the paragraph the agent
is told.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host import prompts
from daedalus.host.services import PathOutsideProject, SessionServices, locator
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import ProjectError, ProjectSettings, ProjectStore, normalise_root

HEADERS = {"X-Daedalus-Token": "tok"}


# -- the store and its migration -------------------------------------------------------------


async def test_the_migration_adds_the_table_and_a_nullable_column(db: Database) -> None:
    """Nothing is backfilled: every session that existed keeps a project_id of NULL, which is what
    makes it a session with a directory of its own."""
    columns = {r[1]: r for r in await db.fetchall("PRAGMA table_info(sessions)")}
    assert "project_id" in columns
    assert columns["project_id"][3] == 0, "project_id must be nullable — existing sessions have none"
    tables = {r[0] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "projects" in tables


async def test_a_project_round_trips_and_the_link_to_sessions_is_the_column(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db)
    root = tmp_path / "site"
    root.mkdir()
    project = await store.create("Bakery site", str(root), settings=ProjectSettings(snapshots=True))
    assert project.root == root and project.settings.snapshots is True and project.reachable is True

    await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at) VALUES ('s1', 't', 'one', '', '')")
    await store.attach("s1", project.id)
    assert (await store.for_session("s1")).id == project.id
    assert await store.sessions_of(project.id) == [{"id": "s1", "title": "one"}]
    assert await store.by_session() == {"s1": project.id}

    moved = await store.update(project.id, name="Bakery", root=str(tmp_path / "site2"))
    assert moved.name == "Bakery" and moved.root == tmp_path / "site2" and moved.settings.snapshots is True

    # Removing a project forgets it; the folder and the session are both still there.
    await store.delete(project.id)
    assert await store.for_session("s1") is None
    assert root.is_dir()


async def test_a_folder_that_is_not_one_and_a_folder_already_spoken_for_are_refused(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db)
    with pytest.raises(ProjectError, match="absolute"):
        await store.create("rel", "some/where")
    with pytest.raises(ProjectError, match="needs a name"):
        await store.create("  ", str(tmp_path))
    (tmp_path / "a-file").write_text("x")
    with pytest.raises(ProjectError, match="a file"):
        await store.create("file", str(tmp_path / "a-file"))

    outer = tmp_path / "outer"
    (outer / "inner").mkdir(parents=True)
    await store.create("Outer", str(outer))
    # Nesting is refused in both directions: a boundary that holds one way is not a boundary.
    with pytest.raises(ProjectError, match="inside the project"):
        await store.create("Inner", str(outer / "inner"))
    with pytest.raises(ProjectError, match="contains the project"):
        await store.create("Parent", str(tmp_path))
    with pytest.raises(ProjectError, match="already that folder"):
        await store.create("Again", str(outer))


def test_a_root_is_normalised_without_touching_the_filesystem() -> None:
    """A project may be added in a container before its folder is mounted, so there is nothing to
    resolve against — and nothing that should refuse the row."""
    assert normalise_root("/srv/nope/../work/") == Path("/srv/work")
    assert normalise_root("/does/not/exist/yet") == Path("/does/not/exist/yet")
    with pytest.raises(ProjectError, match="filesystem root"):
        normalise_root("/")


# -- containment ------------------------------------------------------------------------------


def _services(root: Path, session_id: str = "p-contain") -> SessionServices:
    return SessionServices(session_id=session_id, workspace_dir=root, project_root=root)


def test_resolve_refuses_every_way_out_of_the_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours")
    services = _services(root)

    assert services.resolve("src/main.py") == root / "src" / "main.py"
    assert services.resolve(None) == root
    assert services.resolve(str(root / "src")) == root / "src"

    for attempt in ("../outside/secret.txt", str(outside / "secret.txt"), "src/../../outside", "/etc/passwd"):
        with pytest.raises(PathOutsideProject):
            services.resolve(attempt)

    # A symlink is not a loophole: the check is made on the real path, so a link pointing out of the
    # tree is refused exactly as the path it points at would be.
    (root / "escape").symlink_to(outside)
    with pytest.raises(PathOutsideProject):
        services.resolve("escape/secret.txt")

    # And a path that does not exist yet is judged by where it would land, not by whether it is there.
    assert services.resolve("src/new.py") == root / "src" / "new.py"
    with pytest.raises(PathOutsideProject):
        services.resolve("../new.py")


def test_a_session_without_a_project_resolves_anywhere_as_it_always_did(tmp_path: Path) -> None:
    services = SessionServices(session_id="plain", workspace_dir=tmp_path / "ws")
    assert services.resolve("/etc/hostname") == Path("/etc/hostname")
    assert services.resolve("../up") == tmp_path / "ws" / ".." / "up"


def test_the_worktrees_the_host_opened_stay_reachable_inside_a_project(tmp_path: Path) -> None:
    """A project contains the agent's own work, not the host's: a worktree the host deliberately
    opened for this session is still resolvable, or self-development would stop inside a project."""
    root = tmp_path / "project"
    root.mkdir()
    worktree = tmp_path / "worktrees" / "fix"
    worktree.mkdir(parents=True)
    services = _services(root)
    with pytest.raises(PathOutsideProject):
        services.resolve(str(worktree / "file.py"))
    services.writable.append(worktree)
    assert services.resolve(str(worktree / "file.py")) == worktree / "file.py"


async def test_the_file_tools_refuse_a_path_outside_the_project(tmp_path: Path) -> None:
    from daedalus.tools.files import find_files, read_file, write_file

    root = tmp_path / "project"
    root.mkdir()
    (root / "ok.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours", encoding="utf-8")

    locator.register(_services(root, "p-tools"))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="p-tools", metadata={"tool_call_id": "c"})
    try:
        assert "inside" in (await read_file().invoke(ctx, {"path": "ok.txt"})).content
        # The refusal is raised where the path is resolved rather than returned by each tool: a tool
        # that forgot to check would still be refused, and the dispatcher turns it into the error the
        # model reads. Nothing outside the root is read, written or listed.
        for tool, args in (
            (read_file, {"path": str(outside / "secret.txt")}),
            (write_file, {"path": "../outside/planted.txt", "content": "x"}),
            (find_files, {"pattern": "*.txt", "path": str(outside)}),
        ):
            with pytest.raises(PathOutsideProject, match="outside this project"):
                await tool().invoke(ctx, args)
        assert not (outside / "planted.txt").exists()
    finally:
        locator.unregister("p-tools")


# -- the API ------------------------------------------------------------------------------------


def _app(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> Any:
    async def create_session(title: str, *, metadata: dict[str, Any] | None = None, project_id: str | None = None, workspace: Path | None = None) -> Any:
        return await manager.create_session(title, metadata=metadata, project_id=project_id, workspace=workspace)

    return SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=create_session)


async def _client(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> httpx.AsyncClient:
    api = build_app(_app(settings, config, db, manager), "tok")  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")  # type: ignore[arg-type]


async def test_project_crud_and_a_session_that_works_in_one(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "bakery"
    (root / "menu").mkdir(parents=True)
    (root / "menu" / "items.json").write_text("[]", encoding="utf-8")
    try:
        async with await _client(settings, config, db, manager) as client:
            assert (await client.get("/api/projects")).status_code == 401  # the folders the operator works in; not to anyone

            created = await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "root": str(root)})
            assert created.status_code == 200
            project = created.json()
            assert project["root"] == str(root) and project["reachable"] is True and project["settings"] == {"snapshots": False}

            listing = (await client.get("/api/projects", headers=HEADERS)).json()
            assert [p["id"] for p in listing] == [project["id"]] and listing[0]["sessions"] == []

            bad = await client.post("/api/projects", headers=HEADERS, json={"name": "Nested", "root": str(root / "menu")})
            assert bad.status_code == 400 and "inside the project" in bad.json()["detail"]

            made = await client.post("/api/sessions", headers=HEADERS, json={"title": "Menu page", "project_id": project["id"]})
            assert made.status_code == 200
            sid = made.json()["id"]

            detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
            assert detail["workspace"] == str(root) and detail["project"]["name"] == "Bakery"
            rows = (await client.get("/api/sessions", headers=HEADERS)).json()
            assert [(r["id"], r["project_id"], r["project"]) for r in rows if r["id"] == sid] == [(sid, project["id"], "Bakery")]

            # The file endpoints take their scope from the session, so they show the project and refuse the way out.
            files = (await client.get(f"/api/sessions/{sid}/files", headers=HEADERS)).json()
            assert {e["name"] for e in files["entries"]} >= {"menu"}
            escape = await client.get(f"/api/sessions/{sid}/files", headers=HEADERS, params={"path": "../.."})
            assert escape.status_code == 400
            download = await client.get(f"/api/sessions/{sid}/download", headers=HEADERS, params={"path": "../../etc/hostname"})
            assert download.status_code == 400

            # The other agents of a project are its agents, not the ones that happen to name the same
            # directory: a project session carries no workspace in its metadata to be matched on.
            second = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Photos", "project_id": project["id"]})).json()["id"]
            beside = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()["workspace_sessions"]
            assert [u["id"] for u in beside] == [second]

            renamed = await client.patch(f"/api/projects/{project['id']}", headers=HEADERS, json={"name": "Bakery site", "snapshots": True})
            assert renamed.status_code == 200 and renamed.json()["name"] == "Bakery site" and renamed.json()["settings"]["snapshots"] is True
            assert manager.live_state(sid).project.settings.snapshots is True, "a live session must see the change, not the next one to open"

            busy = await client.delete(f"/api/projects/{project['id']}", headers=HEADERS)
            assert busy.status_code == 409 and "2 agents work in" in busy.json()["detail"]
            gone = await client.delete(f"/api/projects/{project['id']}", headers=HEADERS, params={"detach": 1})
            assert sorted(gone.json()["detached"]) == sorted([sid, second]) and gone.status_code == 200
            assert (await client.get("/api/projects", headers=HEADERS)).json() == []
            # Not one file of it was touched.
            assert (root / "menu" / "items.json").is_file()
    finally:
        await manager.close()


async def test_a_session_created_without_a_project_keeps_its_own_workspace(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with await _client(settings, config, db, manager) as client:
            made = await client.post("/api/sessions", headers=HEADERS, json={"title": "Plain"})
            sid = made.json()["id"]
            detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
            assert detail["project"] is None
            assert detail["workspace"] == str(manager.workspace_for(sid))
            assert detail["workspace_own"] is True
            rows = (await client.get("/api/sessions", headers=HEADERS)).json()
            assert [r["project_id"] for r in rows if r["id"] == sid] == [None]
        state = manager.live_state(sid)
        assert state is not None and state.project is None
        assert state.services is not None and state.services.project_root is None
        # Which is the point: it resolves paths the way it always has.
        assert state.services.resolve("/etc/hostname") == Path("/etc/hostname")
    finally:
        await manager.close()


async def test_an_unreachable_folder_is_reported_and_no_agent_is_started_in_it(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """The Docker seam: the row exists so the launcher can write the bind mount, and the app can say
    what is missing instead of showing a project whose files are mysteriously absent."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with await _client(settings, config, db, manager) as client:
            created = await client.post("/api/projects", headers=HEADERS, json={"name": "Not mounted", "root": str(tmp_path / "not-here")})
            assert created.status_code == 200 and created.json()["reachable"] is False
            refused = await client.post("/api/sessions", headers=HEADERS, json={"title": "x", "project_id": created.json()["id"]})
            assert refused.status_code == 409 and "not reachable from here yet" in refused.json()["detail"]
    finally:
        await manager.close()


# -- the prompt ----------------------------------------------------------------------------------


def test_the_agent_is_told_where_the_project_is_and_that_it_stays_inside_it() -> None:
    root = Path("/work/bakery")
    section = prompts.environment_section(
        workspace=root, bot_repo=Path("/srv/daedalus"), core_repo=Path("/srv/core"), session_title="Menu", model="m", project="Bakery site"
    )
    assert "project Bakery site" in section and str(root) in section
    assert "refused" in section and "ask" in section
    # A session with no project says nothing about one.
    plain = prompts.environment_section(workspace=root, bot_repo=Path("/srv/daedalus"), core_repo=Path("/srv/core"), session_title="Menu", model="m")
    assert "project" not in plain.lower()


async def test_a_project_is_not_snapshotted_unless_the_operator_asked(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """A project root is somebody's repository. Snapshotting it before every turn and after every run
    is a cost the undo does not repay, so it is opt-in — and where it is opted in, it behaves as a
    workspace does."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("hello", encoding="utf-8")
    try:
        project = await manager.projects.create("Repo", str(root))
        state = await manager.create_session("in the project", project_id=project.id)
        assert await manager.checkpoint(state, kind="before") is None
        assert not (root / ".checkpoints").exists()

        updated = await manager.projects.update(project.id, settings=ProjectSettings(snapshots=True))
        await manager.reload_project(updated, project.id)
        sha = await manager.checkpoint(state, kind="before")
        assert sha and (root / ".checkpoints" / "HEAD").exists()

        # A session with a workspace of its own snapshots as it always has, with nothing asked for.
        plain = await manager.create_session("on its own")
        assert await manager.checkpoint(plain, kind="before")
    finally:
        await manager.close()
