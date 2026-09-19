"""SQLite connection and schema migrations."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

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
    # per-session model preset
    """
    ALTER TABLE live_control ADD COLUMN preset TEXT;
    """,
    # full-text index over the transcript: what the agent searches when older turns were compacted away
    """
    CREATE VIRTUAL TABLE transcript_fts USING fts5(
        session_id UNINDEXED, seq UNINDEXED, role UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2'
    );
    """,
    # inbox: what happened while the operator was away; schedule kinds, failure accounting; lazy reminders
    """
    CREATE TABLE inbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'info',
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        session_id TEXT,
        run_id TEXT,
        read INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX inbox_unread ON inbox(read, at);
    ALTER TABLE schedules ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent';
    ALTER TABLE schedules ADD COLUMN target_session TEXT;
    ALTER TABLE schedules ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE schedules ADD COLUMN last_error TEXT;
    CREATE TABLE lazy_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id TEXT,
        session_id TEXT NOT NULL,
        text TEXT NOT NULL,
        fired_at TEXT NOT NULL,
        delivered_at TEXT,
        promoted_at TEXT
    );
    """,
    # delivery ledger: a final answer that was generated but never confirmed sent is the one thing a restart can lose
    """
    CREATE TABLE deliveries (
        run_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # schedules remember their in-flight run across restarts; lazy notes count promotion attempts
    """
    ALTER TABLE schedules ADD COLUMN active_session_id TEXT;
    ALTER TABLE schedules ADD COLUMN active_run_id TEXT;
    ALTER TABLE lazy_notes ADD COLUMN promote_attempts INTEGER NOT NULL DEFAULT 0;
    """,
    # workspace checkpoints, verification receipts, learning records
    """
    CREATE TABLE checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        seq INTEGER,
        run_id TEXT,
        kind TEXT NOT NULL,
        sha TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE INDEX checkpoints_session ON checkpoints(session_id, seq);
    CREATE TABLE verifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        run_id TEXT,
        criterion TEXT NOT NULL,
        command TEXT NOT NULL,
        cwd TEXT,
        exit_code INTEGER NOT NULL,
        passed INTEGER NOT NULL,
        output_digest TEXT NOT NULL,
        output_head TEXT,
        duration_ms INTEGER,
        at TEXT NOT NULL
    );
    CREATE INDEX verifications_session ON verifications(session_id, at);
    CREATE TABLE learning_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        at TEXT NOT NULL,
        ask TEXT,
        outcome TEXT NOT NULL,
        headline TEXT,
        tools TEXT NOT NULL DEFAULT '{}',
        failures TEXT NOT NULL DEFAULT '[]',
        iterations INTEGER,
        cost_usd REAL,
        duration_s REAL
    );
    """,
    # inbound events: webhook dedupe, standing intents
    """
    CREATE TABLE webhook_deliveries (
        provider TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        at TEXT NOT NULL,
        PRIMARY KEY (provider, delivery_id)
    );
    CREATE TABLE intents (
        id TEXT PRIMARY KEY,
        pattern TEXT NOT NULL,
        action TEXT NOT NULL,
        session_id TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        cooldown_minutes INTEGER NOT NULL DEFAULT 60,
        max_fires INTEGER NOT NULL DEFAULT 20,
        fired_count INTEGER NOT NULL DEFAULT 0,
        last_fired_at TEXT,
        expires_at TEXT,
        created_by_session TEXT,
        created_at TEXT NOT NULL
    );
    """,
    # task board
    """
    CREATE TABLE board_tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 3,
        acceptance TEXT NOT NULL DEFAULT '',
        checklist TEXT NOT NULL DEFAULT '[]',
        depends_on TEXT NOT NULL DEFAULT '[]',
        session_id TEXT,
        run_id TEXT,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        heartbeat_at TEXT
    );
    """,
    # 12 — a receipt says whether the check ran confined
    """
    ALTER TABLE verifications ADD COLUMN sandboxed INTEGER NOT NULL DEFAULT 0;
    """,
    # 13 — a scheduled task can run inside the session that created it
    """
    ALTER TABLE schedules ADD COLUMN run_in TEXT NOT NULL DEFAULT 'new';
    """,
    # 14 — a receipt names the shared dependencies its observation rests on
    """
    ALTER TABLE verifications ADD COLUMN dependencies TEXT NOT NULL DEFAULT '';
    """,
    # 15 — a loop: the standing task a session is woken up for, on an interval or when it says so
    """
    CREATE TABLE loops (
        session_id TEXT PRIMARY KEY,
        instruction TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'interval',
        interval_seconds INTEGER,
        max_runs INTEGER,
        status TEXT NOT NULL DEFAULT 'active',
        next_run_at TEXT,
        last_run_at TEXT,
        run_count INTEGER NOT NULL DEFAULT 0,
        last_reason TEXT,
        stop_reason TEXT,
        pause_note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # 16 — a service: a long-running process a session started and the operator can reach
    """
    CREATE TABLE services (
        session_id TEXT NOT NULL,
        name TEXT NOT NULL,
        command TEXT NOT NULL,
        cwd TEXT NOT NULL,
        port INTEGER,
        pid INTEGER,
        status TEXT NOT NULL DEFAULT 'running',
        restart INTEGER NOT NULL DEFAULT 1,
        log_path TEXT NOT NULL,
        note TEXT,
        started_at TEXT NOT NULL,
        stopped_at TEXT,
        PRIMARY KEY (session_id, name)
    );
    CREATE INDEX services_port ON services(port);
    """,
    # 17 — a service can be shared through the bot's public address: to anyone, or to whoever holds its key
    """
    ALTER TABLE services ADD COLUMN share_mode TEXT NOT NULL DEFAULT 'local';
    ALTER TABLE services ADD COLUMN share_slug TEXT;
    ALTER TABLE services ADD COLUMN share_key TEXT;
    CREATE INDEX services_share_slug ON services(share_slug);
    """,
    # 18 — one row per tool call (the waterfall of a run) and one per network host a call reached
    """
    CREATE TABLE tool_calls (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        run_id TEXT,
        tool_call_id TEXT NOT NULL,
        name TEXT NOT NULL,
        at TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        ok INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX tool_calls_by_session ON tool_calls(session_id);
    CREATE INDEX tool_calls_by_run ON tool_calls(run_id);
    CREATE TABLE egress_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        session_id TEXT NOT NULL,
        run_id TEXT,
        tool TEXT NOT NULL,
        host TEXT NOT NULL,
        action TEXT NOT NULL
    );
    CREATE INDEX egress_log_by_session ON egress_log(session_id);
    CREATE INDEX usage_events_by_run ON usage_events(run_id);
    """,
    # 19 — a task remembers the session that created it: the plan file of a workspace lists its own tasks whoever holds them
    """
    ALTER TABLE board_tasks ADD COLUMN origin_session_id TEXT;
    UPDATE board_tasks SET origin_session_id = session_id WHERE origin_session_id IS NULL;
    """,
    # 20 — a receipt says which tree it ran against and whether it ran any tests: a green run that
    # executed nothing is not evidence, and a reviewer can see the commit the check belongs to
    """
    ALTER TABLE verifications ADD COLUMN tree TEXT NOT NULL DEFAULT '';
    ALTER TABLE verifications ADD COLUMN tests_run INTEGER;
    ALTER TABLE verifications ADD COLUMN tests_skipped INTEGER;
    """,
    # 21 — a receipt carries the content of the files it ran against, so "the check covered these
    # bytes" is a comparison of bytes rather than of clock readings, which a formatter or a rebase
    # moves without changing anything. The digest is taken in the checkout the command ran in.
    """
    ALTER TABLE verifications ADD COLUMN file_digests TEXT NOT NULL DEFAULT '';
    """,
    # 22 — the two ways into the app that need no Telegram: a one-time pairing link, and the
    # browser's own passkey. Codes are stored hashed, so the database never holds a usable link.
    """
    CREATE TABLE pairing_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code_hash TEXT NOT NULL UNIQUE,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );
    CREATE TABLE passkeys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        credential_id TEXT NOT NULL UNIQUE,
        public_key TEXT NOT NULL,
        sign_count INTEGER NOT NULL DEFAULT 0,
        transports TEXT NOT NULL DEFAULT '[]',
        name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        last_used_at TEXT
    );
    """,
    # 23 — the shape the app draws for a row, stored beside the row. Deriving it cost about a
    # millisecond a message, almost all of it secret redaction, and it was derived again for
    # every one of the six hundred turns of every session open. view_key names the code and the
    # redactor that produced the copy, so a row is recomputed when either has moved on.
    """
    ALTER TABLE transcript ADD COLUMN view TEXT;
    ALTER TABLE transcript ADD COLUMN view_key TEXT NOT NULL DEFAULT '';
    """,
    # 24 — the working history is written the way it changes: a round appends its new rows, and
    # only a rewrite of the sequence (a compaction, a revert, a clear) starts a generation, which
    # is written whole and replaces the one before it. Every round used to delete and re-insert
    # the lot. ``key`` is the message identity the append compares against, so it can see whether
    # what it was handed continues what is stored or replaces it.
    """
    ALTER TABLE session_messages ADD COLUMN gen INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE session_messages ADD COLUMN key TEXT NOT NULL DEFAULT '';
    CREATE INDEX session_messages_by_gen ON session_messages(session_id, gen, seq);
    """,
    # 25 — the full-text index stops keeping its own copy of every message. A plain fts5 table
    # stores the indexed text verbatim in a shadow table, which here was a second copy of the
    # transcript: a third of the whole database, duplicating text the transcript already holds.
    # Contentless, the index holds only the index; the row it points at is the transcript row,
    # which is where the text for a snippet comes from now. Dropping the kv watermark is what
    # makes the store rebuild the index from the transcript on the next start.
    """
    DROP TABLE transcript_fts;
    CREATE VIRTUAL TABLE transcript_fts USING fts5(
        text, content = '', contentless_delete = 1, tokenize = 'unicode61 remove_diacritics 2'
    );
    DELETE FROM kv WHERE key = 'transcript_fts_watermark';
    """,
    # 26 — projects: a folder the operator adds, shared by every session that works in it. A
    # session without one keeps the per-session directory under the workspaces root, which is why
    # the column is nullable and nothing backfills it: existing sessions are project-less and stay
    # exactly as they were. ``root`` is the absolute path as the operator gave it; in a container
    # it may name a directory that is not mounted yet, which is a state the API reports rather than
    # a row it refuses to keep.
    """
    CREATE TABLE projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        root TEXT NOT NULL,
        created_at TEXT NOT NULL,
        settings TEXT NOT NULL DEFAULT '{}'
    );
    ALTER TABLE sessions ADD COLUMN project_id TEXT;
    CREATE INDEX sessions_by_project ON sessions(project_id);
    """,
    # what retention took away, per session, so the app can say so instead of offering an undo
    # that would restore nothing
    """
    CREATE TABLE checkpoint_retention (
        session_id TEXT PRIMARY KEY,
        removed_before TEXT NOT NULL,
        removed INTEGER NOT NULL,
        at TEXT NOT NULL
    );
    """,
    # the installation's own projects are one per kind, and the database is what says so. The flag
    # lived only inside the ``settings`` JSON, where no constraint can reach it, and the code that
    # kept it unique read the table and then inserted — with two awaits in between. Two delegations
    # asked for at once therefore made two "Voice" folders, and a system project cannot be deleted,
    # so the installation could not be tidied up from the app at all. The column carries the flag
    # out of the JSON so a partial unique index can hold it; the rows before it are merged onto the
    # oldest of each kind and the sessions of the others are re-pointed at it, which is where their
    # agents were meant to be listed all along.
    """
    ALTER TABLE projects ADD COLUMN system TEXT NOT NULL DEFAULT '';
    UPDATE projects SET system = COALESCE(json_extract(settings, '$.system'), '')
    WHERE json_valid(settings);
    UPDATE sessions SET project_id = (
        SELECT k.id FROM projects k
        WHERE k.system = (SELECT d.system FROM projects d WHERE d.id = sessions.project_id)
        ORDER BY k.created_at, k.rowid LIMIT 1
    )
    WHERE project_id IN (SELECT id FROM projects WHERE system != '');
    DELETE FROM projects WHERE system != '' AND id NOT IN (
        SELECT (SELECT k.id FROM projects k WHERE k.system = p.system ORDER BY k.created_at, k.rowid LIMIT 1)
        FROM projects p WHERE p.system != ''
    );
    CREATE UNIQUE INDEX projects_system ON projects(system) WHERE system != '';
    """,
]


def _project_unification(workspaces_dir: Path) -> str:
    """Build the migration that turns every existing working directory into a project."""
    base = str(Path(os.path.normpath(workspaces_dir.expanduser()))).replace("'", "''")
    return f"""
    UPDATE sessions SET metadata = CASE WHEN json_valid(metadata)
        THEN CASE WHEN json_type(metadata) = 'object' THEN metadata ELSE '{{}}' END
        ELSE '{{}}' END;

    CREATE TEMP TABLE duplicate_projects AS
    SELECT p.id, (SELECT k.id FROM projects k WHERE k.root = p.root
                  ORDER BY (k.system != '') DESC, k.created_at, k.rowid LIMIT 1) AS keeper
    FROM projects p;
    UPDATE sessions SET project_id = (SELECT keeper FROM duplicate_projects WHERE id = sessions.project_id)
    WHERE project_id IN (SELECT id FROM duplicate_projects WHERE id != keeper);
    DELETE FROM projects WHERE id IN (SELECT id FROM duplicate_projects WHERE id != keeper);
    DROP TABLE duplicate_projects;

    INSERT INTO projects(id, name, root, created_at, settings, system)
    SELECT 'project-' || substr(min(id), 1, 12), 'Voice',
           (SELECT CASE
              WHEN json_valid(v.metadata) AND json_type(v.metadata, '$.workspace') = 'text'
                AND trim(json_extract(v.metadata, '$.workspace')) != ''
              THEN rtrim(json_extract(v.metadata, '$.workspace'), '/')
              ELSE '{base}/' || v.id
            END
            FROM sessions v
            WHERE v.project_id IS NULL AND json_valid(v.metadata) AND json_extract(v.metadata, '$.voice') = 1
            ORDER BY v.created_at, v.id LIMIT 1),
           min(created_at),
           '{{"snapshots":false,"system":"voice"}}', 'voice'
    FROM sessions
    WHERE project_id IS NULL AND json_valid(metadata) AND json_extract(metadata, '$.voice') = 1
    HAVING count(*) > 0 AND NOT EXISTS (SELECT 1 FROM projects WHERE system = 'voice');
    UPDATE sessions SET project_id = (SELECT id FROM projects WHERE system = 'voice')
    WHERE project_id IS NULL AND json_valid(metadata) AND json_extract(metadata, '$.voice') = 1;

    CREATE TEMP TABLE session_directories AS
    SELECT s.id,
           CASE
             WHEN json_valid(s.metadata) AND json_type(s.metadata, '$.workspace') = 'text'
               AND trim(json_extract(s.metadata, '$.workspace')) != ''
             THEN rtrim(json_extract(s.metadata, '$.workspace'), '/')
             ELSE '{base}/' || s.id
           END AS directory
    FROM sessions s
    WHERE s.project_id IS NULL;

    CREATE TEMP TABLE session_project_roots AS
    SELECT d.id, d.directory,
           COALESCE(
             (SELECT p.root FROM projects p
              WHERE d.directory = p.root OR substr(d.directory, 1, length(p.root) + 1) = p.root || '/'
              ORDER BY length(p.root), p.root LIMIT 1),
             (SELECT p.directory FROM session_directories p
              WHERE d.directory = p.directory OR substr(d.directory, 1, length(p.directory) + 1) = p.directory || '/'
              ORDER BY length(p.directory), p.directory LIMIT 1),
             d.directory
           ) AS root
    FROM session_directories d;

    INSERT INTO projects(id, name, root, created_at, settings, system)
    SELECT 'project-' || substr(min(s.id), 1, 12),
           COALESCE(NULLIF(trim((SELECT s2.title FROM sessions s2 JOIN session_project_roots r2 ON r2.id = s2.id
                                WHERE r2.root = r.root ORDER BY s2.created_at, s2.id LIMIT 1)), ''), 'Project'),
           r.root, min(s.created_at), '{{"snapshots":true,"system":""}}', ''
    FROM session_project_roots r JOIN sessions s ON s.id = r.id
    WHERE NOT EXISTS (SELECT 1 FROM projects p WHERE p.root = r.root)
    GROUP BY r.root;

    UPDATE sessions
    SET project_id = (SELECT p.id FROM session_project_roots r JOIN projects p ON p.root = r.root WHERE r.id = sessions.id),
        metadata = CASE
          WHEN (SELECT directory FROM session_project_roots WHERE id = sessions.id) =
               (SELECT root FROM session_project_roots WHERE id = sessions.id)
          THEN json_remove(metadata, '$.workspace', '$.own_workspace')
          ELSE json_set(json_remove(metadata, '$.workspace', '$.own_workspace'), '$.directory',
               substr((SELECT directory FROM session_project_roots WHERE id = sessions.id),
                      length((SELECT root FROM session_project_roots WHERE id = sessions.id)) + 2))
        END
    WHERE project_id IS NULL;

    DROP TABLE session_project_roots;
    DROP TABLE session_directories;

    CREATE TABLE sessions_unified (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        last_message_at TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{{}}',
        project_id TEXT NOT NULL REFERENCES projects(id)
    );
    INSERT INTO sessions_unified SELECT id, tenant_id, title, created_at, last_message_at, metadata, project_id FROM sessions;
    DROP TABLE sessions;
    ALTER TABLE sessions_unified RENAME TO sessions;
    CREATE INDEX sessions_by_project ON sessions(project_id);
    CREATE UNIQUE INDEX projects_root ON projects(root);
    """


MIGRATIONS.append("-- generated from the configured project directory")


CACHE_PAGES = -65536
"""Page cache, as negative kibibytes: 64 MiB. The default is two megabytes, which a session
open walks straight through."""
WAL_AUTOCHECKPOINT_PAGES = 2000
"""Write-ahead log pages before a checkpoint folds them back into the file (~8 MiB). Under one
long-lived connection the default lets the log grow into the tens of megabytes."""
VACUUM_PAGES = 1000
"""Free pages handed back to the filesystem per maintenance pass (~4 MiB), so reclaiming a
large freelist is spread over passes rather than holding the lock for all of it at once."""


class Database:
    """A single shared aiosqlite connection guarded by a lock."""

    def __init__(self, path: Path, *, workspaces_dir: Path | None = None) -> None:
        self.path = path
        self.workspaces_dir = workspaces_dir or path.parent / "workspaces"
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._warned_about_freelist = False

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        try:
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._conn.execute("PRAGMA synchronous=NORMAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            # Takes effect for a database created here; an existing one keeps whatever it was made
            # with until a full VACUUM rewrites it (``daedalus db vacuum``).
            await self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            await self._conn.execute(f"PRAGMA cache_size={CACHE_PAGES}")
            await self._conn.execute("PRAGMA temp_store=MEMORY")
            await self._conn.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")
            await self._migrate()
        except BaseException:
            await self.close()
            raise

    async def reclaim(self) -> int:
        """Hand a bounded number of free pages back to the filesystem; returns how many.

        Deleted rows leave their pages on the freelist, and nothing gives them back on its own.
        A database that was created before incremental auto-vacuum was asked for cannot do this at
        all — it says so, once, naming the command that converts it, because the alternative is a
        file that quietly keeps hundreds of megabytes nothing will ever use again.
        """
        row = await self.fetchone("PRAGMA auto_vacuum")
        if row is None or int(row[0]) != 2:
            await self._warn_about_a_freelist_nothing_can_reclaim()
            return 0
        before = await self.fetchone("PRAGMA freelist_count")
        if before is None or not int(before[0]):
            return 0
        await self.execute(f"PRAGMA incremental_vacuum({VACUUM_PAGES})")
        after = await self.fetchone("PRAGMA freelist_count")
        return int(before[0]) - int(after[0]) if after is not None else 0

    FREELIST_WARNING_SHARE = 0.05
    """How much of the file may sit on an unreclaimable freelist before it is worth saying so."""

    async def _warn_about_a_freelist_nothing_can_reclaim(self) -> None:
        """A database made before the setting keeps it until a full VACUUM rewrites the file, and
        incremental reclaim silently does nothing there. Said once per process, with the command."""
        if self._warned_about_freelist:
            return
        self._warned_about_freelist = True
        free = await self.fetchone("PRAGMA freelist_count")
        size = await self.fetchone("PRAGMA page_count")
        pages, total = int(free[0]) if free else 0, int(size[0]) if size else 0
        if not total or pages < total * self.FREELIST_WARNING_SHARE:
            return
        page_size = await self.fetchone("PRAGMA page_size")
        bytes_free = pages * (int(page_size[0]) if page_size else 4096)
        logger.warning(
            "%.0f MB of %s (%d free pages) cannot be given back: this database was created before incremental auto-vacuum and keeps that setting until the file is rewritten. Run `daedalus db vacuum` to reclaim it.",
            bytes_free / 1e6,
            self.path,
            pages,
        )

    async def vacuum(self) -> tuple[int, int]:
        """Rewrite the whole file, converting it to incremental auto-vacuum; returns (before, after) bytes.

        This is the one-off that a database made before the setting needs, and the only way to
        give back a freelist that has already grown large. It holds the connection for the
        whole rewrite, which on a several-hundred-megabyte file is measured in seconds.
        """
        before = self.path.stat().st_size
        await self.execute("PRAGMA auto_vacuum=INCREMENTAL")
        await self.execute("VACUUM")
        await self.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return before, self.path.stat().st_size

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
        known_schema = len(MIGRATIONS)
        if current > known_schema:
            # A database written by a newer build. Opening it anyway works and fails later, at the
            # first write that touches a table the old code remembers differently — which is inside
            # the transaction that appends a message, so the bot runs, answers, and quietly stops
            # keeping any transcript at all.
            raise RuntimeError(
                f"the database is at schema {current} and this build knows {known_schema}: it was written by a newer version of Daedalus. "
                "Run the newer version, or restore the database from before the downgrade."
            )
        for index, script in enumerate(MIGRATIONS, start=1):
            if index <= current:
                continue
            if index == 29:
                script = _project_unification(self.workspaces_dir)
            # The version is written inside the migration's own transaction. Written after it, a
            # process killed in between would leave the schema at N and the version at N-1, and the
            # next start would run migration N again — on an ALTER TABLE, which is not idempotent,
            # that is an install that cannot open its own database and has no way back.
            version = f"INSERT INTO schema_version(version) VALUES ({index});" if current == 0 and index == 1 else f"UPDATE schema_version SET version = {index};"
            async with self._lock:
                await self.conn.executescript(f"BEGIN;\n{script}\n{version}\nCOMMIT;")
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
