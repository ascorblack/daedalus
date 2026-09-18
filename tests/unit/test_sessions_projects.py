"""Where an agent is listed, and what the concierge may decide about it.

Three things are pinned here. The Voice project — the installation's own folder, made the first
time the concierge needs one, owning the voice session and the agents it delegates, and refusing to
be moved or removed. The listing the Agents screen is built on: a page of rows, the folders beside
them with counts that are not limited to the page, and a free bucket for everything with no project.
And the two decisions ``Delegate`` gained: which folder a new agent works in, and whose project it
belongs to — with the refusals that keep a hallucinated project id from becoming an agent somewhere
the operator never asked for.

The steering tests at the end are the ones that go through a real engine: no fake ``submit``, a
scripted provider, and the assertion that what the concierge said actually reached the agent's next
model call.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.types import MessageRole, TextBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.voice import PROJECT_DIR, PROJECT_KIND, Voice
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.projects import ProjectError, ProjectStore
from tests.support.waiting import until
from tests.unit.test_session_runner import ScriptedProvider, _manager, _wait_finished

HEADERS = {"X-Daedalus-Token": "tok"}


def _app(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> Any:
    async def create_session(title: str, *, metadata: dict[str, Any] | None = None, project_id: str | None = None, workspace: Path | None = None, own_workspace: bool = False) -> Any:
        return await manager.create_session(title, metadata=metadata, project_id=project_id, workspace=workspace, own_workspace=own_workspace)

    return SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=create_session)


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test")  # type: ignore[arg-type]


# -- the store: a project the installation owns ----------------------------------------------


async def test_a_system_project_is_made_once_and_refuses_to_be_moved_or_removed(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db, reserved=[tmp_path / "state"], home=tmp_path)
    root = tmp_path / "state" / "workspaces" / PROJECT_DIR
    made = await store.ensure_system(PROJECT_KIND, name="Voice", root=root)
    assert made.settings.system == PROJECT_KIND and made.root == root
    # The folder is under a reserved directory on purpose: it is the installation's, and an
    # operator's project rooted there is refused by the very check this one steps past.
    with pytest.raises(ProjectError, match="belongs to the installation"):
        await store.create("Mine", str(root))
    # Asking again is the same project, not a second one.
    assert (await store.ensure_system(PROJECT_KIND, name="Voice", root=root)).id == made.id
    assert len(await store.list()) == 1
    assert (await store.system(PROJECT_KIND)).id == made.id
    with pytest.raises(ProjectError, match="cannot be moved"):
        await store.update(made.id, root=str(tmp_path / "elsewhere"))
    with pytest.raises(ProjectError, match="cannot be removed"):
        await store.delete(made.id)
    # What the operator may still change: its name, and whether it snapshots.
    renamed = await store.update(made.id, name="Voice agents")
    assert renamed.name == "Voice agents" and renamed.settings.system == PROJECT_KIND


async def test_several_callers_asking_at_once_make_one_system_project(db: Database, tmp_path: Path) -> None:
    """The concierge is told to fan errands out in parallel, and each of them wants the folder.

    ``ensure_system`` reads the table and then inserts, with awaits in between; nothing used to
    serialise that, so a fresh installation asked twice at once grew two "Voice" folders — and a
    system project cannot be deleted, so there was no way to tidy that up from the app.
    """
    store = ProjectStore(db, reserved=[tmp_path / "state"], home=tmp_path)
    root = tmp_path / "state" / "workspaces" / PROJECT_DIR
    made = await asyncio.gather(*[store.ensure_system(PROJECT_KIND, name="Voice", root=root) for _ in range(8)])
    assert len({p.id for p in made}) == 1, "one folder, whoever won the race"
    assert [p.settings.system for p in await store.list()] == [PROJECT_KIND]
    # …and the table refuses a second one even if the code above ever stops checking.
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO projects(id, name, root, created_at, settings, system) VALUES ('x', 'Voice', ?, '', '{}', ?)",
            (str(root / "other"), PROJECT_KIND),
        )


async def test_the_duplicate_system_projects_an_installation_already_has_are_merged(tmp_path: Path) -> None:
    """The repair for an installation that ran the racing code: one folder, and nobody's agents lost."""
    path = tmp_path / "old.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version(version) VALUES (?);".replace("?", str(len(MIGRATIONS) - 1)))
    for script in MIGRATIONS[: len(MIGRATIONS) - 1]:
        raw.executescript(script)
    for n, name in enumerate(("Voice", "Voice", "Voice")):
        raw.execute(
            "INSERT INTO projects(id, name, root, created_at, settings) VALUES (?, ?, ?, ?, ?)",
            (f"v{n}", name, str(tmp_path / f"voice{n}"), f"2026-09-0{n + 1}T00:00:00+00:00", '{"snapshots": false, "system": "voice"}'),
        )
        raw.execute(
            "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES (?, 't', ?, '', '', '{}', ?)",
            (f"s{n}", f"agent {n}", f"v{n}"),
        )
    raw.execute(
        "INSERT INTO projects(id, name, root, created_at, settings) VALUES ('p0', 'Bakery', ?, '2026-09-01T00:00:00+00:00', '{}')",
        (str(tmp_path / "bakery"),),
    )
    raw.commit()
    raw.close()

    db = Database(path)
    await db.open()
    try:
        store = ProjectStore(db)
        projects = await store.list()
        assert [p.name for p in projects] == ["Bakery", "Voice"], "one Voice folder is left, and the operator's is untouched"
        voice = next(p for p in projects if p.settings.system == PROJECT_KIND)
        assert voice.id == "v0", "the oldest is the one that is kept"
        rows = await db.fetchall("SELECT id, project_id FROM sessions ORDER BY id")
        assert {r["id"]: r["project_id"] for r in rows} == {"s0": "v0", "s1": "v0", "s2": "v0"}, "every agent is still listed under Voice"
    finally:
        await db.close()


@pytest.mark.parametrize("settings", ["not json at all", "", "null"])
async def test_unreadable_project_settings_do_not_prevent_migration(tmp_path: Path, settings: str) -> None:
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as raw:
        raw.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL);")
        raw.execute("INSERT INTO schema_version VALUES (?)", (len(MIGRATIONS) - 1,))
        for script in MIGRATIONS[:-1]:
            raw.executescript(script)
        raw.execute(
            "INSERT INTO projects(id, name, root, created_at, settings) VALUES ('p', 'Bakery', ?, '', ?)",
            (str(tmp_path / "bakery"), settings),
        )
        raw.execute(
            "INSERT INTO projects(id, name, root, created_at, settings) VALUES ('v', 'Voice', ?, '', ?)",
            (str(tmp_path / "voice"), '{"system": "voice"}'),
        )
        raw.execute(
            "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES ('s', 't', 'agent', '', '', '{}', 'p')",
        )
    raw.close()

    db = Database(path)
    for _ in range(2):
        await db.open()
        try:
            rows = await db.fetchall("SELECT id, settings, system FROM projects ORDER BY id")
            assert [dict(row) for row in rows] == [
                {"id": "p", "settings": settings, "system": ""},
                {"id": "v", "settings": '{"system": "voice"}', "system": "voice"},
            ]
            rows = await db.fetchall("SELECT project_id FROM sessions WHERE id = 's'")
            assert rows[0]["project_id"] == "p"
            rows = await db.fetchall("SELECT version FROM schema_version")
            assert rows[0]["version"] == len(MIGRATIONS)
        finally:
            await db.close()


async def test_the_counts_beside_the_folders_are_of_the_table_and_not_of_a_page(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db)
    root = tmp_path / "bakery"
    root.mkdir()
    project = await store.create("Bakery", str(root))
    loop = '{"loop": {"status": "active"}}'
    for n in range(3):
        await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata) VALUES (?, 't', ?, '', ?, ?)", (f"p{n}", f"in it {n}", f"2026-09-0{n + 1}", loop if n == 0 else "{}"))
        await store.attach(f"p{n}", project.id)
    await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata) VALUES ('free1', 't', 'alone', '', '2026-09-09', '{}')")

    counts = await store.summary(active={"p1"})
    assert counts[project.id] == {"total": 3, "active": 1, "loops": 1, "last_message_at": "2026-09-03"}
    assert counts[""] == {"total": 1, "active": 0, "loops": 0, "last_message_at": "2026-09-09"}

    # A subagent and a fork are drawn inside the row they belong to, so the header over the folder
    # must not count them: it used to say "3 agents" over a list of two.
    for sid, metadata in (("p3", '{"subagent_of": "p0"}'), ("p4", '{"forked_from": {"session_id": "p0"}}')):
        await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata) VALUES (?, 't', ?, '', '2026-09-04', ?)", (sid, sid, metadata))
        await store.attach(sid, project.id)
    # …and one whose leader is gone hangs under nothing, so it is counted, which is what the screen does.
    await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata) VALUES ('p5', 't', 'orphan', '', '2026-09-05', '{\"subagent_of\": \"vanished\"}')")
    await store.attach("p5", project.id)
    assert (await store.summary())[project.id]["total"] == 4


# -- the listing the screen is built on ------------------------------------------------------


async def test_the_listing_answers_the_rows_the_folders_and_the_free_bucket(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = _app(settings, config, db, manager)
    voice = Voice(app)
    root = tmp_path / "bakery"
    root.mkdir()
    try:
        async with _client(app) as client:
            project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "root": str(root)})).json()
            await client.post("/api/sessions", headers=HEADERS, json={"title": "Menu", "project_id": project["id"]})
            free = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Alone"})).json()["id"]
            # The Voice project does not exist until something needs it.
            assert [p["system"] for p in (await client.get("/api/sessions", headers=HEADERS)).json()["projects"]] == [""]

            voice_id = await voice.session_id()
            listing = (await client.get("/api/sessions", headers=HEADERS)).json()
            folders = {p["name"]: p for p in listing["projects"]}
            assert folders["Voice"]["system"] == PROJECT_KIND and folders["Voice"]["total"] == 1
            assert folders["Bakery"]["total"] == 1 and folders["Bakery"]["system"] == ""
            assert listing["free"]["total"] == 1
            rows = {r["id"]: r for r in listing["sessions"]}
            assert rows[voice_id]["project_id"] == folders["Voice"]["id"]
            assert rows[free]["project_id"] is None and rows[free]["workspace_own"] is True
            # The whole path is in the row, because the list no longer groups by the folder's name.
            assert rows[free]["workspace_path"] == str(manager.workspace_for(free))
            # And the concierge's own folder is not offered as a workspace to attach an agent to.
            assert PROJECT_DIR not in {w["name"] for w in (await client.get("/api/workspaces", headers=HEADERS)).json()}
    finally:
        await manager.close()


async def test_a_session_moves_between_projects_without_a_file_moving(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = _app(settings, config, db, manager)
    root = tmp_path / "bakery"
    root.mkdir()
    try:
        async with _client(app) as client:
            project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "root": str(root)})).json()
            sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Alone"})).json()["id"]
            own = manager.workspace_for(sid)
            (own / "notes.md").write_text("mine", encoding="utf-8")

            # Into the project, keeping its own directory: listed there, sharing none of its files.
            moved = (await client.post(f"/api/sessions/{sid}/project", headers=HEADERS, json={"project_id": project["id"]})).json()
            assert moved["project"] == "Bakery" and moved["workspace"] == str(own)
            state = manager.live_state(sid)
            assert state is not None and state.project is not None and state.workspace == own
            assert state.services is not None and state.services.project_root == own
            assert (own / "notes.md").read_text(encoding="utf-8") == "mine"

            # And then into the project's own folder, which is where the rest of it works.
            shared = (await client.post(f"/api/sessions/{sid}/project", headers=HEADERS, json={"project_id": project["id"], "use_project_folder": True})).json()
            assert shared["workspace"] == str(root)
            assert manager.live_state(sid).workspace == root  # type: ignore[union-attr]
            assert (own / "notes.md").exists(), "moving a session must not move its files"

            # Out again: still the folder it was working in a moment ago, because nothing on disk
            # moved. Sending it back to the directory it had before the project would leave
            # everything it wrote in the project's folder behind, unreachable to it.
            out = (await client.post(f"/api/sessions/{sid}/project", headers=HEADERS, json={"project_id": None})).json()
            assert out["project_id"] is None and out["workspace"] == str(root)
            assert manager.live_state(sid).services.project_root is None  # type: ignore[union-attr]
            assert (await client.get("/api/sessions", headers=HEADERS)).json()["free"]["total"] == 1

            assert (await client.post(f"/api/sessions/{sid}/project", headers=HEADERS, json={"project_id": "nope"})).status_code == 404
    finally:
        await manager.close()


async def test_keeping_its_own_directory_keeps_the_one_it_is_working_in(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    """A move with the folder checkbox off must not strand what the agent has written.

    The sheet says "its files stay where they are". They only do if the move re-points the project
    and nothing else: a session that had taken a project's folder has no directory of its own left
    in its metadata, so falling back to the default handed it a fresh empty one and left its work in
    a folder it could no longer reach.
    """
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = _app(settings, config, db, manager)
    bakery = tmp_path / "bakery"
    garage = tmp_path / "garage"
    bakery.mkdir()
    garage.mkdir()
    try:
        async with _client(app) as client:
            first = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "root": str(bakery)})).json()
            second = (await client.post("/api/projects", headers=HEADERS, json={"name": "Garage", "root": str(garage)})).json()
            sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Baker", "project_id": first["id"]})).json()["id"]
            assert manager.live_state(sid).workspace == bakery  # type: ignore[union-attr]
            (bakery / "note.txt").write_text("the recipe", encoding="utf-8")

            moved = (await client.post(f"/api/sessions/{sid}/project", headers=HEADERS, json={"project_id": second["id"], "use_project_folder": False})).json()
            assert moved["project"] == "Garage"
            assert moved["workspace"] == str(bakery), "the same folder, not a fresh empty one"
            state = manager.live_state(sid)
            assert state is not None and state.workspace == bakery
            assert (state.workspace / "note.txt").read_text(encoding="utf-8") == "the recipe"
            assert state.services is not None and state.services.project_root == bakery, "walled at the folder it works in"
    finally:
        await manager.close()


# -- what Delegate decides -------------------------------------------------------------------


@pytest.fixture
async def voice_app(settings: Settings, db: Database) -> Any:
    from tests.support.models import model_config

    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    application = _app(settings, manager.config, db, manager)
    yield application
    await manager.close()


def _no_runs(manager: SessionManager) -> list[tuple[str, str]]:
    """The brief a delegated agent was handed, without a model to hand it to."""
    submitted: list[tuple[str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments: Any = (), *, steer: bool = False, as_answer: bool = True, origin: str = "operator") -> str:
        submitted.append((session_id, text))
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    return submitted


async def test_a_delegated_agent_shares_the_concierges_folder_unless_it_asks_for_its_own(voice_app: Any) -> None:
    manager: SessionManager = voice_app.manager
    voice = Voice(voice_app)
    _no_runs(manager)
    project = await voice.project()

    shared = await voice.delegate(title="Invoices", task="read them")
    other = await voice.delegate(title="Photos", task="crop them")
    mine = await voice.delegate(title="Errand", task="unrelated", workspace="own")

    for result in (shared, other):
        assert result["workspace"] == "shared" and result["project"] == "Voice"
        assert manager.live_state(result["session_id"]).workspace == project.root  # type: ignore[union-attr]
    # Two agents on one errand see the same files; a third one sees none of them.
    assert manager.live_state(mine["session_id"]).workspace == manager.workspace_for(mine["session_id"])  # type: ignore[union-attr]
    assert mine["workspace"] == "own"
    own_state = manager.live_state(mine["session_id"])
    assert own_state is not None and own_state.project is not None and own_state.project.id == project.id
    # Listed under Voice, and walled at its own directory rather than at the shared one.
    assert own_state.services is not None and own_state.services.project_root == own_state.workspace
    # The wall holds both ways. Nested inside the shared folder it did not: a shared agent is walled
    # at the folder that *contains* every private directory, so it could read all of them.
    shared_state = manager.live_state(shared["session_id"])
    assert shared_state is not None and shared_state.workspace not in own_state.workspace.parents
    assert own_state.workspace not in shared_state.workspace.parents
    assert not shared_state.services.contains(own_state.workspace / "private.txt")  # type: ignore[union-attr]
    assert not own_state.services.contains(shared_state.workspace / "shared.txt")
    assert {s["id"] for s in await manager.projects.sessions_of(project.id)} >= {shared["session_id"], mine["session_id"]}


async def test_an_agent_can_be_started_in_a_project_the_operator_named_and_nowhere_else(voice_app: Any, tmp_path: Path) -> None:
    manager: SessionManager = voice_app.manager
    voice = Voice(voice_app)
    _no_runs(manager)
    root = tmp_path / "bakery"
    root.mkdir()
    bakery = await manager.projects.create("Bakery", str(root))

    listed = await voice.project_list()
    # The Voice project is not offered: it is where an agent goes when no project is named.
    assert [(p["id"], p["name"], p["agents"]) for p in listed] == [(bakery.id, "Bakery", 0)]

    started = await voice.delegate(title="Prices", task="redo them", project_id=bakery.id)
    assert started["project"] == "Bakery" and manager.live_state(started["session_id"]).workspace == root  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="no project has the id"):
        await voice.delegate(title="x", task="y", project_id="deadbeef")
    with pytest.raises(ValueError, match="works in the project's own folder"):
        await voice.delegate(title="x", task="y", project_id=bakery.id, workspace="own")
    with pytest.raises(ValueError, match="'shared' or 'own'"):
        await voice.delegate(title="x", task="y", workspace="somewhere")
    unreachable = await manager.projects.create("Gone", str(tmp_path / "not-mounted"))
    with pytest.raises(ValueError, match="not reachable"):
        await voice.delegate(title="x", task="y", project_id=unreachable.id)
    # Nothing was started by any of the four refusals.
    assert len(await manager.projects.sessions_of(bakery.id)) == 1


async def test_steering_an_agent_names_no_folder_and_no_project(voice_app: Any) -> None:
    voice = Voice(voice_app)
    manager: SessionManager = voice_app.manager
    _no_runs(manager)
    started = await voice.delegate(title="Invoices", task="read them")
    with pytest.raises(ValueError, match="where it was started"):
        await voice.delegate(title="", task="also the old ones", session_id=started["session_id"], workspace="own")


async def test_a_voice_session_from_before_the_project_existed_is_taken_into_it(voice_app: Any) -> None:
    """The concierge session of an installation that had one before this: it joins, it does not restart."""
    manager: SessionManager = voice_app.manager
    voice = Voice(voice_app)
    legacy = await manager.create_session("Voice", metadata={"voice": True})
    await voice_app.db.kv_set("voice_session", legacy.session.id)
    assert legacy.project is None

    assert await voice.session_id() == legacy.session.id
    state = manager.live_state(legacy.session.id)
    assert state is not None and state.project is not None and state.project.settings.system == PROJECT_KIND


# -- steering, through a real engine ----------------------------------------------------------


async def _voice_over(settings: Settings, db: Database, provider: ScriptedProvider) -> tuple[SessionManager, Voice]:
    manager = await _manager(settings, db, provider)
    app = _app(settings, manager.config, db, manager)
    return manager, Voice(app)


async def test_what_the_concierge_says_to_a_running_agent_reaches_its_next_model_call(settings: Settings, db: Database) -> None:
    """Delegate(session_id, task) on an agent that is working: the steer queue, and then the prompt.

    Nothing is faked below the extension: a real ``submit``, a real run, and the words are read back
    out of the request the provider was handed after the tool call returned.
    """
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 1"}}, {"text": "both done"}])
    manager, voice = await _voice_over(settings, db, provider)
    try:
        started = await voice.delegate(title="Invoices", task="read the invoices")
        agent = started["session_id"]
        waiter = asyncio.create_task(_wait_finished(manager))
        state = manager.live_state(agent)
        await until(lambda: state is not None and state.running, "the delegated agent started running")

        relayed = await voice.delegate(title="", task="the old ones as well, please", session_id=agent)
        assert relayed["steered"] is True and relayed["answered"] is False
        queued = await manager.live.load(agent)
        assert [q["text"] for q in queued["steer"]] == ["the old ones as well, please"]

        await waiter
        texts = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
        assert any("the old ones as well" in t for t in texts), "the steer never reached the agent's prompt"
        # And it is in the agent's own history, as words the operator sent it.
        history = await manager.sessions.list_transcript(agent)
        assert any(isinstance(b, TextBlock) and "the old ones as well" in b.text for m in history if m.role is MessageRole.user for b in m.content_blocks)
    finally:
        await manager.close()


async def test_the_concierge_answers_an_agent_that_is_waiting_on_a_question(settings: Settings, db: Database) -> None:
    """The operator's answer, relayed by the concierge, answers the AskUser rather than queueing behind it."""
    provider = ScriptedProvider(
        [
            {"tool": "AskUser", "args": {"questions": [{"question": "which photos, the seasonal ones?", "allow_custom": True}]}},
            {"text": "cropped the seasonal ones"},
        ]
    )
    manager, voice = await _voice_over(settings, db, provider)
    try:
        waiter = asyncio.create_task(_wait_finished(manager))
        started = await voice.delegate(title="Photos", task="crop the photos")
        agent = started["session_id"]
        assert (await waiter)[-1][2] == "awaiting"
        state = manager.live_state(agent)
        assert state is not None and state.pending is not None

        resumed = asyncio.create_task(_wait_finished(manager))
        answered = await voice.delegate(title="", task="the seasonal ones", session_id=agent)
        assert answered["answered"] is True and answered["steered"] is True
        assert (await resumed)[-1][2] == "completed"
        tool_message = [m for m in provider.requests[-1].messages if m.role is MessageRole.tool][-1]
        assert "the seasonal ones" in str(tool_message.content_blocks[0].content)  # type: ignore[union-attr]
        assert manager.live_state(agent).pending is None  # type: ignore[union-attr]
    finally:
        await manager.close()
