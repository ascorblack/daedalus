"""Projects: the folder is the wall.

A session in a project reads and writes inside its root and nowhere else — through the tools and
through the API — and a session without one behaves exactly as every session did before projects
existed. The rest is the store, the migration that makes room for it, and the paragraph the agent
is told.
"""

from __future__ import annotations

import os
import shutil
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


def test_the_kernels_own_filesystems_are_not_folders_of_work() -> None:
    """``/proc`` has no files in it to edit, and a listing of it is a listing of the machine."""
    for raw in ("/proc", "/proc/1", "/sys", "/sys/class/net", "/dev", "/dev/shm/work", "/run", "/run/user/1000"):
        with pytest.raises(ProjectError, match="kernel"):
            normalise_root(raw)
    # Nothing that merely reads like one: the refusal is by mount point, not by name.
    assert normalise_root("/srv/proc") == Path("/srv/proc")


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
            assert project["root"] == str(root) and project["reachable"] is True and project["settings"] == {"snapshots": False, "system": ""}

            listing = (await client.get("/api/projects", headers=HEADERS)).json()
            assert [p["id"] for p in listing] == [project["id"]] and listing[0]["sessions"] == []

            bad = await client.post("/api/projects", headers=HEADERS, json={"name": "Nested", "root": str(root / "menu")})
            assert bad.status_code == 400 and "inside the project" in bad.json()["detail"]

            made = await client.post("/api/sessions", headers=HEADERS, json={"title": "Menu page", "project_id": project["id"]})
            assert made.status_code == 200
            sid = made.json()["id"]

            detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
            assert detail["workspace"] == str(root) and detail["project"]["name"] == "Bakery"
            listing = (await client.get("/api/sessions", headers=HEADERS)).json()
            rows = listing["sessions"]
            assert [(r["id"], r["project_id"], r["project"]) for r in rows if r["id"] == sid] == [(sid, project["id"], "Bakery")]
            # The folders come back beside the rows, each counting its own agents.
            folder = next(p for p in listing["projects"] if p["id"] == project["id"])
            assert folder["name"] == "Bakery" and folder["total"] == 1 and folder["system"] == ""

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
            listing = (await client.get("/api/sessions", headers=HEADERS)).json()
            rows = listing["sessions"]
            assert [r["project_id"] for r in rows if r["id"] == sid] == [None]
            # A session with no project is in the free bucket, and the bucket counts it.
            assert listing["free"]["total"] >= 1
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


# -- the children a project session makes ----------------------------------------------------------


async def test_a_subagent_of_a_project_session_is_in_the_project(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """The commonest way to make a new worker is the one that must not leave the wall behind: a child
    that carried the leader's directory without the leader's project resolved anything at all."""
    from daedalus.extensions.subagents import Subagents

    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    root.mkdir()
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={})
    try:
        submitted: list[str] = []

        async def fake_submit(session_id: str, text: str, attachments: Any = (), **kw: Any) -> str:
            submitted.append(session_id)
            return "run-x"

        manager.submit = fake_submit  # type: ignore[method-assign]
        project = await manager.projects.create("Repo", str(root))
        leader = await manager.create_session("lead", project_id=project.id)
        result = await Subagents(app).spawn(leader_id=leader.session.id, task="count the files", name="counter")

        child = await manager.get_state(result["session_id"])
        assert child is not None and child.workspace == root
        assert child.project is not None and child.project.id == project.id
        assert child.services is not None and child.services.project_root == root
        with pytest.raises(PathOutsideProject):
            child.services.resolve("/etc/passwd")
        # And it is one of the project's agents in every list, not a session that merely names the folder.
        assert result["session_id"] in {s["id"] for s in await manager.projects.sessions_of(project.id)}
        assert "workspace" not in child.metadata, "the project is the one source for where a session works"
        assert submitted == [result["session_id"]]
    finally:
        await manager.close()


async def test_a_subagent_of_a_plain_session_still_shares_its_directory(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    from daedalus.extensions.subagents import Subagents

    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={})
    try:

        async def fake_submit(session_id: str, text: str, attachments: Any = (), **kw: Any) -> str:
            return "run-x"

        manager.submit = fake_submit  # type: ignore[method-assign]
        leader = await manager.create_session("lead")
        result = await Subagents(app).spawn(leader_id=leader.session.id, task="count", name="counter")
        child = await manager.get_state(result["session_id"])
        assert child is not None and child.workspace == leader.workspace and child.project is None
        assert child.services is not None and child.services.project_root is None
    finally:
        await manager.close()


async def test_a_fork_of_a_project_session_stays_in_it_and_copies_nothing(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """A fork used to copy the whole of the operator's repository into the state directory and leave
    the copy uncontained. Two sessions of one project share the folder; there is nothing to copy."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]", encoding="utf-8")
    (root / "big.bin").write_bytes(b"x" * 4096)
    try:
        async with await _client(settings, config, db, manager) as client:
            project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Repo", "root": str(root)})).json()
            sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "work", "project_id": project["id"]})).json()["id"]
            forked = await client.post(f"/api/sessions/{sid}/fork", headers=HEADERS, json={"seq": 1})
            assert forked.status_code == 200
            body = forked.json()
            assert body["workspace_copied"] is False and body["workspace_shared"] is True

            fork = await manager.get_state(body["id"])
            assert fork is not None and fork.workspace == root
            assert fork.project is not None and fork.project.id == project["id"]
            assert fork.services is not None and fork.services.project_root == root
            with pytest.raises(PathOutsideProject):
                fork.services.resolve("/etc/passwd")
            # Not one byte of the folder was duplicated into the state directory.
            assert not (manager.workspace_for(body["id"]) / ".git").exists()
    finally:
        await manager.close()


async def test_a_fork_of_a_workspace_over_the_size_cap_is_refused_by_name(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    """A fork copies a whole directory tree. ``ops.checkpoint_max_gb`` guarded the snapshot and said
    nothing about this one, so a session on a large tree wrote it twice without being asked."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    manager.config.ops.checkpoint_max_gb = 1e-6  # a kilobyte
    try:
        source = await manager.create_session("big")
        (source.workspace / "payload.bin").write_bytes(b"x" * 8192)
        target = await manager.create_session("fork")
        with pytest.raises(RuntimeError, match="over ops.checkpoint_max_gb"):
            await manager.fork_into(source.session.id, 1, target)
        assert not (target.workspace / "payload.bin").exists()
    finally:
        await manager.close()


async def test_a_spawned_agent_of_a_project_session_stays_in_the_project(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """SpawnAgent resolved its files correctly and then handed the absolute paths to a child with no
    project: one tool call moved the operator's repository into an uncontained session."""
    from daedalus.transport.telegram.front import TelegramFront

    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "howto.md").write_text("step 1", encoding="utf-8")
    try:

        async def fake_submit(session_id: str, text: str, attachments: Any = (), **kw: Any) -> str:
            return "run-x"

        manager.submit = fake_submit  # type: ignore[method-assign]
        front = TelegramFront.__new__(TelegramFront)
        front.manager = manager

        async def create_session_topic(title: str, *, metadata: dict[str, Any] | None = None, project_id: str | None = None, **kw: Any) -> Any:
            return await manager.create_session(title, metadata=metadata, project_id=project_id), None

        front.create_session_topic = create_session_topic  # type: ignore[method-assign]

        project = await manager.projects.create("Repo", str(root))
        parent = await manager.create_session("parent", project_id=project.id)
        child_id = await front._service_spawn_agent(
            parent.session.id, title="Helper", brief="help", files=[str(root / "howto.md")],
            first_message=None, preset=None, mode=None, mcp=[], peer_name=None,
        )
        child = await manager.get_state(child_id)
        assert child is not None and child.project is not None and child.project.id == project.id
        assert child.workspace == root
        with pytest.raises(PathOutsideProject):
            child.services.resolve("/etc/passwd")  # type: ignore[union-attr]
        # The file was already where the new agent works, so it was not copied anywhere.
        assert not (root / "inbox" / "howto.md").exists()
    finally:
        await manager.close()


async def test_a_scheduled_task_of_a_project_session_fires_in_the_project(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    from daedalus.extensions.scheduler import Scheduler

    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "notes.md").write_text("n", encoding="utf-8")
    fired: list[str] = []

    async def create_session(title: str, *, metadata: dict[str, Any] | None = None, project_id: str | None = None, workspace: Path | None = None) -> Any:
        return await manager.create_session(title, metadata=metadata, project_id=project_id, workspace=workspace)

    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, create_session=create_session)
    try:

        async def fake_submit(session_id: str, text: str, attachments: Any = (), **kw: Any) -> str:
            fired.append(session_id)
            return "run-x"

        manager.submit = fake_submit  # type: ignore[method-assign]
        scheduler = Scheduler(app)  # type: ignore[arg-type]
        project = await manager.projects.create("Repo", str(root))
        owner = await manager.create_session("owner", project_id=project.id)

        created = await scheduler.create(
            name="nightly", prompt="tidy up", cron="0 3 * * *", run_at=None,
            files=[str(root / "notes.md")], created_by_session=owner.session.id,
        )
        # The task runs in the folder, and the file it was given is not copied out of it.
        assert created["workspace"] == str(root)
        assert not (settings.workspaces_dir / f"sched-{created['id']}").exists()

        row = dict(await db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],)))
        session_id = await scheduler._fire_agent(row)
        task = await manager.get_state(session_id)
        assert task is not None and task.project is not None and task.project.id == project.id
        assert task.workspace == root and fired == [session_id]
        with pytest.raises(PathOutsideProject):
            task.services.resolve("/etc/passwd")  # type: ignore[union-attr]
    finally:
        await manager.close()


# -- the folder is the operator's ------------------------------------------------------------------


async def test_an_unmounted_root_is_never_created_and_keeps_saying_it_is_not_mounted(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """``mkdir(parents=True)`` on an unmounted root made the whole path, and the mount point became a
    local empty folder that answered ``reachable`` for ever after — erasing the one signal the Docker
    story rests on, and shadowing the real folder when it came back."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "mounted"
    root.mkdir()
    try:
        project = await manager.projects.create("Mounted", str(root))
        state = await manager.create_session("worker", project_id=project.id)
        sid = state.session.id
        assert (root / "inbox").is_dir()

        # The mount goes away and the process is restarted: the state is loaded from the database again.
        shutil.rmtree(root)
        manager._states.pop(sid)
        assert (await manager.projects.get(project.id)).reachable is False
        reloaded = await manager.get_state(sid)
        assert reloaded is not None
        assert not root.exists(), "the folder the operator added is theirs to create"
        assert (await manager.projects.get(project.id)).reachable is False
    finally:
        await manager.close()


async def test_a_project_may_not_be_the_installation_or_the_whole_home_folder(db: Database, tmp_path: Path) -> None:
    """A project root is reachable to every agent in it and is an open root to the policy, so the
    state directory, the checkouts and the home folder as a whole are refused — in both directions."""
    state_dir = tmp_path / "own" / "state"
    workspaces = tmp_path / "own" / "workspaces"
    home = tmp_path / "own" / "home"
    for path in (state_dir, workspaces, home, home / "work"):
        path.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(db, reserved=[state_dir, workspaces], home=home)

    with pytest.raises(ProjectError, match="belongs to the installation"):
        await store.create("State", str(state_dir))
    with pytest.raises(ProjectError, match="is inside"):
        await store.create("Inside", str(state_dir / "blobs"))
    with pytest.raises(ProjectError, match="contains"):
        await store.create("Above", str(tmp_path / "own"))
    with pytest.raises(ProjectError, match="your home folder"):
        await store.create("Home", str(home))
    # A folder inside the home folder is the ordinary case and stays allowed.
    assert (await store.create("Work", str(home / "work"))).root == home / "work"


async def test_a_project_cannot_be_moved_or_removed_while_an_agent_is_working_in_it(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """Re-pointing a loaded session's workspace mid-turn means its next tool call lands in a different
    directory from its earlier reads — the refusal ``revert`` and ``fork`` already make."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    try:
        async with await _client(settings, config, db, manager) as client:
            project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Repo", "root": str(root)})).json()
            sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "worker", "project_id": project["id"]})).json()["id"]
            manager.live_state(sid).pending = SimpleNamespace(payload={})  # a question outstanding: the turn is in flight

            listing = (await client.get("/api/projects", headers=HEADERS)).json()
            assert listing[0]["sessions"] == [{"id": sid, "title": "worker", "running": True}]

            moved = await client.patch(f"/api/projects/{project['id']}", headers=HEADERS, json={"root": str(tmp_path / "elsewhere")})
            assert moved.status_code == 409 and "working in Repo right now" in moved.json()["detail"]
            removed = await client.delete(f"/api/projects/{project['id']}", headers=HEADERS, params={"detach": 1})
            assert removed.status_code == 409 and "mid-turn" in removed.json()["detail"]
            # A rename touches no directory and is not refused.
            assert (await client.patch(f"/api/projects/{project['id']}", headers=HEADERS, json={"name": "Repo 2"})).status_code == 200

            manager.live_state(sid).pending = None
            assert (await client.delete(f"/api/projects/{project['id']}", headers=HEADERS, params={"detach": 1})).status_code == 200
    finally:
        await manager.close()


def test_a_resolved_path_comes_back_as_the_path_that_was_judged(tmp_path: Path) -> None:
    """``contains`` judges the real path; returning the candidate left a window in which a name
    inside the root could be turned into a link out of it before the caller opened it."""
    root = Path(os.path.realpath(tmp_path)) / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "real.txt").write_text("inside", encoding="utf-8")
    (root / "link.txt").symlink_to(root / "src" / "real.txt")
    services = _services(root)
    assert services.resolve("link.txt") == root / "src" / "real.txt"


async def test_the_agent_directories_are_excluded_from_the_operators_checkout(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """A project root is somebody's repository: what a session writes into it must not turn up in
    their ``git status`` or be swept into a commit by ``git add -A``."""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    try:
        project = await manager.projects.create("Repo", str(root))
        await manager.create_session("worker", project_id=project.id)
        written = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        for name in ("inbox/", ".exec/", ".jobs/", ".services/", ".checkpoints/"):
            assert f"/{name}" in written
        # Loading another session of the same project does not write the block a second time.
        await manager.create_session("second", project_id=project.id)
        assert (root / ".git" / "info" / "exclude").read_text(encoding="utf-8") == written

        # A root that is not a git repository has nothing to write and nothing to worry about.
        plain = tmp_path / "docs"
        plain.mkdir()
        other = await manager.projects.create("Docs", str(plain))
        await manager.create_session("third", project_id=other.id)
        assert not (plain / ".git").exists()
    finally:
        await manager.close()


async def test_a_service_started_in_a_project_cannot_choose_a_directory_outside_it(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """Every other path-taking tool goes through the session's resolve; ``ServiceStart`` took its cwd
    raw, which made it the one way into the filesystem the project's containment did not judge — and
    a service is the one thing here that outlives the turn."""
    from daedalus.extensions.inbox import Inbox
    from daedalus.extensions.services import Services

    settings.services_port_range = "18140-18142"
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "repo"
    (root / "site").mkdir(parents=True)
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={})
    app.extensions["inbox"] = Inbox(app)  # type: ignore[arg-type]
    try:
        services = Services(app)  # type: ignore[arg-type]
        project = await manager.projects.create("Repo", str(root))
        state = await manager.create_session("host", project_id=project.id)
        with pytest.raises(PathOutsideProject):
            await services.start(state.session.id, name="escape", command="sleep 30", cwd="/", port=None)
        started = await services.start(state.session.id, name="inside", command="sleep 30", cwd="site", port=None)
        assert started["cwd"] == str(root / "site")
        await services.stop(state.session.id, "inside")
    finally:
        await manager.close()
