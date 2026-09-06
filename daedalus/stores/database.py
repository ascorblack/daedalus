"""SQLite connection and schema migrations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

MIGRATIONS: list[str] = [
    # 1 — core stores
    """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        last_message_at TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE session_messages (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        message TEXT NOT NULL
    );
    CREATE INDEX session_messages_by_session ON session_messages(session_id, seq);
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        detail_blob_ref TEXT
    );
    CREATE INDEX runs_by_session ON runs(session_id, created_at);
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX events_by_run ON events(run_id, seq);
    CREATE TABLE snapshots (
        run_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        state TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE live_control (
        session_id TEXT PRIMARY KEY,
        steer_queue TEXT NOT NULL DEFAULT '[]',
        follow_up_queue TEXT NOT NULL DEFAULT '[]',
        model_name TEXT,
        thinking_enabled INTEGER,
        reasoning_effort TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE usage_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        model TEXT NOT NULL,
        purpose TEXT NOT NULL,
        run_id TEXT,
        session_id TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        raw TEXT NOT NULL
    );
    CREATE INDEX usage_events_by_at ON usage_events(at);
    CREATE INDEX usage_events_by_session ON usage_events(session_id);
    CREATE TABLE memory_records (
        tenant_id TEXT NOT NULL,
        id TEXT NOT NULL,
        record TEXT NOT NULL,
        PRIMARY KEY (tenant_id, id)
    );
    CREATE TABLE workspace_units (
        key TEXT PRIMARY KEY,
        unit TEXT NOT NULL
    );
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2 — chat bindings, approvals, schedules
    """
    CREATE TABLE topics (
        chat_id INTEGER NOT NULL,
        thread_id INTEGER NOT NULL,
        session_id TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        closed_at TEXT,
        PRIMARY KEY (chat_id, thread_id)
    );
    CREATE TABLE pending_questions (
        session_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        chat_id INTEGER,
        thread_id INTEGER,
        message_id INTEGER,
        answers TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE TABLE change_proposals (
        id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        branch TEXT NOT NULL,
        pr_number INTEGER,
        pr_url TEXT,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        session_id TEXT,
        status TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT,
        message_id INTEGER
    );
    CREATE TABLE schedules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        cron TEXT,
        run_at TEXT,
        prompt TEXT NOT NULL,
        files TEXT NOT NULL DEFAULT '[]',
        model TEXT,
        recurring INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        workspace TEXT NOT NULL,
        last_summary TEXT,
        last_run_at TEXT,
        next_run_at TEXT,
        topic_thread_id INTEGER,
        created_by_session TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE balance_alerts (
        threshold TEXT PRIMARY KEY,
        fired_at TEXT
    );
    """,
    # transcript: everything the operator and the agent ever exchanged, for display.
    # session_messages is the model's working history and shrinks under compaction.
    """
    CREATE TABLE transcript (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        key TEXT NOT NULL,
        message TEXT NOT NULL,
        UNIQUE (session_id, key)
    );
    CREATE INDEX transcript_session ON transcript(session_id, seq);
    ALTER TABLE live_control ADD COLUMN provider TEXT;
    """,
]


class Database:
    """A single shared aiosqlite connection guarded by a lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not open")
        return self._conn

    async def _migrate(self) -> None:
        await self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = await (await self.conn.execute("SELECT version FROM schema_version")).fetchone()
        current = int(row["version"]) if row else 0
        for index, script in enumerate(MIGRATIONS, start=1):
            if index <= current:
                continue
            async with self._lock:
                # executescript runs the statements atomically inside its own transaction.
                await self.conn.executescript(f"BEGIN;\n{script}\nCOMMIT;")
                if current == 0 and index == 1:
                    await self.conn.execute("INSERT INTO schema_version(version) VALUES (?)", (index,))
                else:
                    await self.conn.execute("UPDATE schema_version SET version = ?", (index,))
            current = index

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._lock:
            await self.conn.execute(sql, tuple(params))

    class _Transaction:
        """Holds the connection lock across BEGIN … COMMIT so multi-statement writes are atomic."""

        def __init__(self, db: Database) -> None:
            self.db = db

        async def __aenter__(self) -> aiosqlite.Connection:
            await self.db._lock.acquire()
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
            except Exception:
                self.db._lock.release()
                raise
            return self.db.conn

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            try:
                await self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.db._lock.release()

    def transaction(self) -> Database._Transaction:
        return Database._Transaction(self)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        async with self._lock:
            await self.conn.executemany(sql, [tuple(r) for r in rows])

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._lock:
            cursor = await self.conn.execute(sql, tuple(params))
            row = await cursor.fetchone()
            await cursor.close()
            return row

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self.conn.execute(sql, tuple(params))
            rows = await cursor.fetchall()
            await cursor.close()
            return list(rows)

    # -- tiny KV for settings that are not worth a table ---------------------------

    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


__all__ = ["Database", "MIGRATIONS"]
