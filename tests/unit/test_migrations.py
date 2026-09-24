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


def _index_of(fragment: str) -> int:
    return next(i for i, script in enumerate(database_module.MIGRATIONS) if isinstance(script, str) and fragment in script)


async def test_the_inbox_becomes_notifications_with_its_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reshaping keeps every entry and its number, and splits the old severity into how loud an
    entry is and what colour it has: an 'info' entry was a record, never worth a badge."""
    path = tmp_path / "state.sqlite"
    before = _index_of("CREATE TABLE notifications")
    monkeypatch.setattr(database_module, "MIGRATIONS", database_module.MIGRATIONS[:before])
    db = Database(path)
    await db.open()
    rows = [
        (3, "2026-09-01T10:00:00+00:00", "run_failed", "error", "Run failed in 'a'", "boom", "s1", "r1", 0),
        (4, "2026-09-01T11:00:00+00:00", "heartbeat", "info", "Heartbeat: quiet", "", "s2", "r2", 1),
        (5, "2026-09-01T12:00:00+00:00", "loop_paused", "warning", "Loop paused: needs you", "x", "s3", None, 0),
        (6, "2026-09-01T13:00:00+00:00", "loop", "notice", "Loop", "done", "s3", None, 0),
        (7, "2026-09-01T14:00:00+00:00", "schedule_run", "notice", "Daily", "", None, None, 1),
        (8, "2026-09-01T15:00:00+00:00", "service", "warning", "Service 'web' is not running", "", "s1", None, 0),
        (9, "2026-09-01T16:00:00+00:00", "run_cap", "warning", "A run hit its spend cap", "", "s1", "r9", 0),
    ]
    await db.executemany("INSERT INTO inbox(id, at, kind, severity, title, body, session_id, run_id, read) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    # A deleted entry at the top of the inbox: its number was handed out and must stay used.
    await db.execute("INSERT INTO inbox(id, at, kind, title) VALUES (12, '2026-09-02T00:00:00+00:00', 'x', 'gone')")
    await db.execute("DELETE FROM inbox WHERE id = 12")
    await db.close()

    monkeypatch.undo()
    db = Database(path)
    await db.open()
    assert not await db.fetchall("SELECT name FROM sqlite_master WHERE name = 'inbox'")
    got = {r["id"]: dict(r) for r in await db.fetchall("SELECT * FROM notifications")}
    assert sorted(got) == [3, 4, 5, 6, 7, 8, 9]
    assert {i: (r["category"], r["level"], r["tone"]) for i, r in got.items()} == {
        3: ("run_failed", "normal", "error"),
        4: ("reminder", "quiet", "info"),
        5: ("question", "normal", "warning"),
        6: ("agent_notify", "normal", "info"),
        7: ("reminder", "normal", "info"),
        8: ("system", "normal", "warning"),
        9: ("run_failed", "normal", "warning"),
    }
    assert got[4]["seen_at"] == got[4]["at"] and got[3]["seen_at"] is None
    assert got[3]["updated_at"] == got[3]["at"] and got[3]["source"] == "system" and got[3]["count"] == 1
    assert (got[3]["session_id"], got[3]["run_id"], got[3]["title"], got[3]["body"]) == ("s1", "r1", "Run failed in 'a'", "boom")
    await db.execute("INSERT INTO notifications(at, updated_at, kind, category, title) VALUES ('t', 't', 'k', 'system', 'new')")
    assert (await db.fetchone("SELECT max(id) AS n FROM notifications"))["n"] == 13
    indexes = {r["name"] for r in await db.fetchall("PRAGMA index_list(notifications)")}
    assert {"notifications_unseen", "notifications_open", "notifications_dedupe", "notifications_by_session", "notifications_by_project"} <= indexes
    version = (await db.fetchone("SELECT version FROM schema_version"))["version"]
    await db.close()

    db = Database(path)
    await db.open()
    assert (await db.fetchone("SELECT version FROM schema_version"))["version"] == version
    assert (await db.fetchone("SELECT count(*) AS n FROM notifications"))["n"] == 8
    await db.close()


async def test_the_terminal_tables_are_created_and_a_second_open_leaves_them_alone(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = Database(path)
    await db.open()
    columns = [r["name"] for r in await db.fetchall("PRAGMA table_info(terminals)")]
    assert columns[:18] == ["id", "env", "project_id", "owner_kind", "owner_id", "title", "cwd", "argv_json", "profile", "sandbox", "status", "exit_code", "created_at", "exited_at", "last_output_at", "last_input_at", "cols", "rows"]
    assert {"ptyd_instance", "exit_signal", "final_preview_json", "created_by"} <= set(columns)
    indexes = {r["name"] for r in await db.fetchall("PRAGMA index_list(terminals)")}
    assert {"terminals_by_owner", "terminals_by_project", "terminals_by_status"} <= indexes
    # No key to the owner: owners are four kinds, and the audit outlives the terminal it is about.
    assert await db.fetchall("PRAGMA foreign_key_list(terminals)") == []
    await db.execute("INSERT INTO terminals(id, env, owner_kind, cwd, created_at) VALUES ('t1', 'container', 'free', '/w', '2026-01-01T00:00:00.000Z')")
    await db.execute("INSERT INTO terminal_audit(at, terminal_id, env, actor, action) VALUES ('2026-01-01T00:00:00.000Z', 'gone', 'host', 'operator', 'attach')")
    version = (await db.fetchone("SELECT version FROM schema_version"))["version"]
    await db.close()

    db = Database(path)
    await db.open()
    assert (await db.fetchone("SELECT version FROM schema_version"))["version"] == version
    row = await db.fetchone("SELECT * FROM terminals WHERE id = 't1'")
    assert (row["status"], row["profile"], row["argv_json"], row["cols"], row["rows"], row["created_by"]) == ("running", "shell", "[]", 80, 24, "operator")
    assert (await db.fetchone("SELECT count(*) AS n FROM terminal_audit"))["n"] == 1
    await db.close()


async def test_the_harness_tables_are_created_keyed_to_the_staff_rows_and_a_second_open_leaves_them_alone(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = Database(path)
    await db.open()
    catalog = [r["name"] for r in await db.fetchall("PRAGMA table_info(harness_catalog)")]
    assert catalog[:4] == ["env", "harness", "installed_version", "latest_version"]
    assert {"logged_in", "agents_json", "models_json", "checked_at", "error", "binary_path", "login_detail", "latest_checked_at", "self_check_json"} <= set(catalog)
    # The orchestrator's tables are keyed to, never widened: no harness column lands on them.
    for table in ("staff_sessions", "staff_messages", "asks"):
        assert not {r["name"] for r in await db.fetchall(f"PRAGMA table_info({table})")} & {"launch_id", "delivered_via", "degraded_to", "client_ref", "companion_terminal_id"}
    launch_keys = {(r["table"], r["on_delete"]) for r in await db.fetchall("PRAGMA foreign_key_list(harness_launches)")}
    assert launch_keys == {("staff_sessions", "CASCADE")}
    delivery_keys = {(r["table"], r["on_delete"]) for r in await db.fetchall("PRAGMA foreign_key_list(harness_deliveries)")}
    assert delivery_keys == {("staff_messages", "CASCADE"), ("harness_launches", "CASCADE")}
    assert {"harness_launches_open", "harness_launches_by_terminal"} <= {r["name"] for r in await db.fetchall("PRAGMA index_list(harness_launches)")}
    await db.execute("INSERT INTO harness_catalog(env, harness) VALUES ('host', 'codex')")
    version = (await db.fetchone("SELECT version FROM schema_version"))["version"]
    await db.close()

    db = Database(path)
    await db.open()
    assert (await db.fetchone("SELECT version FROM schema_version"))["version"] == version
    row = await db.fetchone("SELECT * FROM harness_catalog")
    assert (row["logged_in"], row["agents_json"], row["self_check_json"], row["installed_version"]) == ("unknown", "[]", "{}", "")
    await db.close()
