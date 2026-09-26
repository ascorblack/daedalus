"""The migration that names a staff message by when it goes in: two tables are rebuilt, and no message,
delivery fact or watch may come out of it changed in anything but the words for its timing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from daedalus.stores.database import MIGRATIONS, Database

TIMING = next(index for index, migration in enumerate(MIGRATIONS) if isinstance(migration, str) and "staff_messages_rebuilt" in migration)
"""The schema this migration upgrades from, found by what it does rather than by its number."""

AT = "2026-09-01T00:00:00+00:00"


def seed(path: Path) -> None:
    """Three messages, one of each old kind, two of them with delivery facts, and two watches."""
    raw = sqlite3.connect(path)
    raw.executescript(f"CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES ({TIMING});")
    opening = SimpleNamespace(workspaces_dir=path.parent / "workspaces", local_env="container")
    for script in MIGRATIONS[:TIMING]:
        raw.executescript(script(opening) if callable(script) else script)
    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute("INSERT INTO projects(id, name, created_at) VALUES ('p-work', 'Work', ?)", (AT,))
    raw.execute("INSERT INTO staff(id, project_id, name, harness, created_by, created_at) VALUES ('st-1', 'p-work', 'Ada', 'opencode', 'operator', ?)", (AT,))
    raw.execute("INSERT INTO staff_sessions(id, staff_id, kind, status, status_at, started_at) VALUES ('ss-1', 'st-1', 'cli', 'working', ?, ?)", (AT, AT))
    raw.execute("INSERT INTO harness_launches(launch_id, staff_session_id, harness, env, terminal_id, started_at) VALUES ('l-1', 'ss-1', 'opencode', 'container', 't-1', ?)", (AT,))
    # The same instant for all three: only their rowids keep their order.
    for mid, mode, state in (("sm-a", "queue", "acknowledged"), ("sm-b", "steer", "queued"), ("sm-c", "interrupt", "failed")):
        raw.execute(
            "INSERT INTO staff_messages(id, staff_id, staff_session_id, origin, text, mode, state, attempts, created_at, updated_at) VALUES (?, 'st-1', 'ss-1', 'orchestrator', ?, ?, ?, 1, ?, ?)",
            (mid, f"message {mid}", mode, state, AT, AT),
        )
    raw.execute("INSERT INTO harness_deliveries(message_id, launch_id, via, degraded_to, enters, written_at) VALUES ('sm-a', 'l-1', 'paste', '', 2, ?)", (AT,))
    raw.execute("INSERT INTO harness_deliveries(message_id, launch_id, via, degraded_to, client_ref) VALUES ('sm-b', 'l-1', 'prompt_async', 'queue', 'msg_1')")
    for wid, action in (
        ("w-tell", {"action": "tell", "staff": "Ada", "staff_id": "st-1", "text": "check again", "mode": "steer"}),
        ("w-wake", {"action": "wake", "note": "look"}),
    ):
        raw.execute(
            "INSERT INTO watches(id, project_id, pattern_json, action_json, created_by, created_at) VALUES (?, 'p-work', '{}', ?, 'orchestrator', ?)",
            (wid, json.dumps(action), AT),
        )
    raw.commit()
    raw.close()


async def test_messages_facts_and_watches_keep_everything_but_the_words_for_their_timing(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    seed(path)
    db = Database(path, local_env="container")
    await db.open()
    try:
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        rows = await db.fetchall("SELECT id, mode, state, attempts, text FROM staff_messages ORDER BY created_at DESC, rowid DESC")
        assert [tuple(r) for r in rows] == [
            ("sm-c", "interrupt", "failed", 1, "message sm-c"),
            ("sm-b", "now", "queued", 1, "message sm-b"),
            ("sm-a", "after_turn", "acknowledged", 1, "message sm-a"),
        ]
        facts = await db.fetchall("SELECT message_id, via, degraded_to, enters, client_ref, written_at FROM harness_deliveries ORDER BY message_id")
        assert [tuple(r) for r in facts] == [("sm-a", "paste", "", 2, "", AT), ("sm-b", "prompt_async", "after_turn", 0, "msg_1", None)]
        watches = {r["id"]: json.loads(r["action_json"]) for r in await db.fetchall("SELECT id, action_json FROM watches")}
        assert watches["w-tell"] == {"action": "tell", "staff": "Ada", "staff_id": "st-1", "text": "check again", "when": "now"}
        assert watches["w-wake"] == {"action": "wake", "note": "look"}
        assert [r[0] for r in await db.fetchall("PRAGMA integrity_check")] == ["ok"]
        assert await db.fetchall("PRAGMA foreign_key_check") == []
        # The delivery facts still belong to their messages: removing one takes its fact along.
        await db.execute("DELETE FROM staff_messages WHERE id = 'sm-a'")
        assert [r[0] for r in await db.fetchall("SELECT message_id FROM harness_deliveries")] == ["sm-b"]
    finally:
        await db.close()
    # Opening again runs nothing twice.
    again = Database(path, local_env="container")
    await again.open()
    await again.close()
