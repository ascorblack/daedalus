"""What the schema does when a migration is interrupted, and when the build is older than the file.

Both are one-way doors on a desktop install: there is no operator with a sqlite3 CLI behind them, so
the failure has to be one the next start can still open the database after.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from daedalus.stores import database as database_module
from daedalus.stores.database import Database


async def test_a_migration_and_its_version_land_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A process killed between the migration's COMMIT and the version bump would leave the schema at
    N and the version at N-1, and the next start would run migration N again. Migrations that add a
    column are not idempotent: the install then cannot open its own database at all, and there is no
    way back from it short of editing schema_version by hand."""
    path = tmp_path / "state.sqlite"
    db = Database(path)
    await db.open()
    await db.close()

    monkeypatch.setattr(database_module, "MIGRATIONS", [*database_module.MIGRATIONS, "ALTER TABLE kv ADD COLUMN interrupted TEXT;"])
    scripts: list[str] = []
    monkeypatch.setattr(database_module.aiosqlite.Connection, "executescript", _watching(database_module.aiosqlite.Connection.executescript, scripts))
    db = Database(path)
    await db.open()
    assert scripts, "the migration did not run"
    for script in scripts:
        body, _, tail = script.partition("ALTER TABLE kv ADD COLUMN interrupted")
        assert body.strip().startswith("BEGIN")
        assert "schema_version" in tail.partition("COMMIT")[0], "the version is written after the transaction the script runs in"
    assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(database_module.MIGRATIONS)
    await db.close()

    # And this is what it costs if they ever come apart: the script is replayed on a schema that
    # already has it.
    db = Database(path)
    await db.open()
    await db.execute("UPDATE schema_version SET version = ?", (len(database_module.MIGRATIONS) - 1,))
    await db.close()
    db = Database(path)
    with pytest.raises(Exception, match="duplicate column"):
        await db.open()
    await db.close()


def _watching(original: Any, into: list[str]) -> Any:
    async def watching(conn: Any, script: str) -> Any:
        into.append(script)
        return await original(conn, script)

    return watching


async def test_a_database_from_a_newer_build_is_refused_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening it anyway succeeds and fails later, at the first write — inside the transaction that
    appends a message, so the bot runs, answers, and quietly keeps no transcript."""
    path = tmp_path / "state.sqlite"
    db = Database(path)
    await db.open()
    await db.close()

    monkeypatch.setattr(database_module, "MIGRATIONS", database_module.MIGRATIONS[:-3])
    db = Database(path)
    with pytest.raises(RuntimeError, match="written by a newer version"):
        await db.open()
    await db.close()


async def test_the_event_ring_is_created_and_a_second_open_leaves_it_alone(tmp_path: Path) -> None:
    """The bus's cursors live in this table; a reopen that recreated it would hand numbers out again."""
    path = tmp_path / "state.sqlite"
    db = Database(path)
    await db.open()
    columns = [r["name"] for r in await db.fetchall("PRAGMA table_info(app_events)")]
    assert columns == ["seq", "at", "type", "project_id", "session_id", "staff_id", "terminal_id", "payload_json"]
    indexes = {r["name"] for r in await db.fetchall("PRAGMA index_list(app_events)")}
    assert {"app_events_by_at", "app_events_by_project"} <= indexes
    await db.execute("INSERT INTO app_events(at, type, payload_json) VALUES ('2026-01-01T00:00:00.000Z', 'terminal.bell', '{}')")
    version = (await db.fetchone("SELECT version FROM schema_version"))["version"]
    await db.close()

    db = Database(path)
    await db.open()
    assert (await db.fetchone("SELECT version FROM schema_version"))["version"] == version
    assert (await db.fetchone("SELECT count(*) AS n FROM app_events"))["n"] == 1
    await db.close()
