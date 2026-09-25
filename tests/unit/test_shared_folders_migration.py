"""The migration that lets a folder belong to more than one project: the folders table is rebuilt, and
nothing that pointed at a folder may come out of the rebuild pointing at nothing."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.projects import FolderSpec, ProjectError, ProjectStore

SHARED = next(index for index, migration in enumerate(MIGRATIONS) if isinstance(migration, str) and "project_folders_rebuilt" in migration)
"""The schema this migration upgrades from, found by what it does rather than by its number."""

AT = "2026-09-01T00:00:00+00:00"


def seed(path: Path) -> None:
    """An installation as it stood before: two projects, a member with a default folder, a task with
    a folder, and a staff session working in one — every column that refers to a folder, filled."""
    raw = sqlite3.connect(path)
    raw.executescript(f"CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES ({SHARED});")
    opening = SimpleNamespace(workspaces_dir=path.parent / "workspaces", local_env="container")
    for script in MIGRATIONS[:SHARED]:
        raw.executescript(script(opening) if callable(script) else script)
    raw.execute("PRAGMA foreign_keys=ON")
    for pid, name in (("p-work", "Work"), ("p-labs", "Labs")):
        raw.execute("INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)", (pid, name, AT))
    folders = [("f-gateway", "p-work", "/srv/work/gateway", 0), ("f-labs", "p-labs", "/srv/labs", 0), ("f-docs", "p-labs", "/srv/docs", 1)]
    for fid, pid, where, position in folders:
        raw.execute("INSERT INTO project_folders(id, project_id, path, label, env, position, created_at) VALUES (?, ?, ?, 'x', 'host', ?, ?)", (fid, pid, where, position, AT))
    raw.execute("INSERT INTO staff(id, project_id, name, harness, default_folder_id, created_by, created_at) VALUES ('st-1', 'p-labs', 'Lev', 'claude', 'f-labs', 'operator', ?)", (AT,))
    raw.execute("INSERT INTO staff(id, project_id, name, harness, created_by, created_at) VALUES ('st-2', 'p-work', 'Ira', 'claude', 'operator', ?)", (AT,))
    raw.execute("INSERT INTO board_tasks(id, title, status, created_at, updated_at, project_id, folder_id) VALUES ('t-1', 'Survey', 'doing', ?, ?, 'p-labs', 'f-docs')", (AT, AT))
    raw.execute("INSERT INTO board_tasks(id, title, status, created_at, updated_at, project_id) VALUES ('t-2', 'Loose', 'todo', ?, ?, 'p-work')", (AT, AT))
    raw.execute("INSERT INTO staff_sessions(id, staff_id, kind, status, status_at, started_at, folder_id, task_id) VALUES ('ss-1', 'st-1', 'cli', 'idle', ?, ?, 'f-docs', 't-1')", (AT, AT))
    raw.commit()
    raw.close()


def references(path: Path) -> dict[str, str | None]:
    with sqlite3.connect(path) as raw:
        rows = [
            *(("staff:" + r[0], r[1]) for r in raw.execute("SELECT id, default_folder_id FROM staff")),
            *(("task:" + r[0], r[1]) for r in raw.execute("SELECT id, folder_id FROM board_tasks")),
            *(("session:" + r[0], r[1]) for r in raw.execute("SELECT id, folder_id FROM staff_sessions")),
        ]
    return dict(rows)


async def test_the_rebuild_keeps_every_folder_and_everything_that_points_at_one(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    seed(path)
    before = references(path)
    assert before["staff:st-1"] == "f-labs" and before["task:t-1"] == "f-docs" and before["session:ss-1"] == "f-docs"
    with sqlite3.connect(path) as raw:
        folders_before = raw.execute("SELECT * FROM project_folders ORDER BY id").fetchall()

    db = Database(path, local_env="container")
    await db.open()
    try:
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        assert [tuple(r) for r in await db.fetchall("SELECT * FROM project_folders ORDER BY id")] == folders_before
        assert [r[0] for r in await db.fetchall("PRAGMA integrity_check")] == ["ok"]
        assert await db.fetchall("PRAGMA foreign_key_check") == []

        store = ProjectStore(db)
        # The same folder in a second project is a row of its own now; a second time in one project is not.
        await store.add_folder("p-work", "/srv/labs", env="host")
        with pytest.raises(ProjectError, match="already has the folder"):
            await store.add_folder("p-work", "/srv/labs", env="host")
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("INSERT INTO project_folders(id, project_id, path, env, created_at) VALUES ('f-again', 'p-work', '/srv/labs', 'host', ?)", (AT,))
        assert [p.name for p in await store.list() if any(str(f.path) == "/srv/labs" for f in p.folders)] == ["Labs", "Work"]
        # The project that had it first stays the one a working directory there is adopted into.
        first = await store.for_path(Path("/srv/labs"))
        assert first is not None and first.name == "Labs"
        await store.create("Third", [FolderSpec("/srv/labs", env="host")])
        assert await store.holders("/srv/labs", besides="p-work") == ["Labs", "Third"]
    finally:
        await db.close()
    assert references(path) == before, "the rebuild nulled a reference to a folder"

    # Opened again, nothing runs a second time and nothing moves.
    again = Database(path, local_env="container")
    await again.open()
    await again.close()
    assert references(path) == before
