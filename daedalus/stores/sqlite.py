"""SQLite implementations of the core store contracts."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from protocore.contracts.events import IEventStream
from protocore.contracts.run import IRunStore, RunNotFoundError
from protocore.contracts.session import ISessionStore, SessionNotFoundError
from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    Event,
    Message,
    MessageRole,
    Run,
    RunStatus,
    Session,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from daedalus.providers.openai_compat import UsageRecord, UsageSink
from daedalus.stores.database import Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


def message_text(message: Message) -> str:
    """Everything searchable in a message: text, thinking is skipped, tool calls as name+args, tool results."""
    parts: list[str] = []
    for block in message.content_blocks:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            parts.append(f"{block.name} {block.arguments_json or ''}")
        elif isinstance(block, ToolResultBlock):
            parts.append(block.content or "")
    return "\n".join(p for p in parts if p).strip()


_FTS_TOKEN_RE = re.compile(r"[\w][\w'.-]*", re.UNICODE)


def fts_query(query: str) -> str:
    """Turn free text into an FTS5 MATCH expression: every word required, as a prefix.

    Quotes each token so punctuation in identifiers (``config.toml``, ``run-cap``) cannot
    be read as FTS syntax; an explicitly quoted phrase is passed through as a phrase.
    """
    query = query.strip()
    if query.startswith('"') and query.endswith('"') and len(query) > 2:
        return '"' + query[1:-1].replace('"', '""') + '"'
    tokens = _FTS_TOKEN_RE.findall(query)
    return " ".join(f'"{t.replace(chr(34), "")}"*' for t in tokens if t)


class SqliteSessionStore(ISessionStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, session: Session) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                session.id,
                session.tenant_id,
                session.title,
                session.created_at.isoformat(),
                session.last_message_at.isoformat(),
                json.dumps(session.metadata),
            ),
        )

    async def get(self, session_id: str, tenant_id: str) -> Session:
        row = await self._db.fetchone(
            "SELECT * FROM sessions WHERE id = ? AND tenant_id = ?", (session_id, tenant_id)
        )
        if row is None:
            raise SessionNotFoundError(session_id)
        return _row_to_session(row)

    async def append_message(self, session_id: str, tenant_id: str, message: Message) -> None:
        await self._db.execute(
            "INSERT INTO session_messages(session_id, tenant_id, message) VALUES (?, ?, ?)",
            (session_id, tenant_id, message.model_dump_json()),
        )
        await self._db.execute(
            "UPDATE sessions SET last_message_at = ? WHERE id = ?", (_now(), session_id)
        )

    async def list_messages(
        self, session_id: str, tenant_id: str, *, limit: int = 100, offset: int = 0
    ) -> Sequence[Message]:
        rows = await self._db.fetchall(
            "SELECT message FROM session_messages WHERE session_id = ? AND tenant_id = ?"
            " ORDER BY seq LIMIT ? OFFSET ?",
            (session_id, tenant_id, limit, offset),
        )
        return [Message.model_validate_json(r["message"]) for r in rows]

    # -- host extras --------------------------------------------------------------

    async def list_sessions(self, tenant_id: str, *, limit: int = 100) -> list[Session]:
        rows = await self._db.fetchall(
            "SELECT * FROM sessions WHERE tenant_id = ? ORDER BY last_message_at DESC LIMIT ?",
            (tenant_id, limit),
        )
        return [_row_to_session(r) for r in rows]

    async def update_metadata(self, session_id: str, metadata: dict[str, Any]) -> None:
        await self._db.execute(
            "UPDATE sessions SET metadata = ? WHERE id = ?", (json.dumps(metadata), session_id)
        )

    async def update_title(self, session_id: str, title: str) -> None:
        await self._db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    # -- transcript (display history, append-only) ------------------------------

    @staticmethod
    def transcript_key(message: Message) -> str:
        """Identity of a message across history rewrites: role + creation time (+ tool call id)."""
        extra = ""
        for block in message.content_blocks:
            call_id = getattr(block, "tool_call_id", None)
            if call_id:
                extra = f":{call_id}"
                break
        return f"{message.role.value}:{message.created_at.isoformat()}{extra}"

    async def append_transcript(self, session_id: str, messages: Sequence[Message], *, from_history: bool = False) -> int:
        """Append messages not yet in the transcript (by key); returns how many were added.

        ``from_history`` marks a sync from the model's working history: a user-role message
        there that the host did not tag as the operator's was injected by the core (a nudge,
        a budget notice) and is recorded as such so the UI can hide it.
        """
        if not messages:
            return 0
        if from_history:
            messages = [
                m.model_copy(update={"metadata": {**m.metadata, "daedalus.origin": "core"}})
                if m.role is MessageRole.user and "daedalus.origin" not in m.metadata and not m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)
                else m
                for m in messages
            ]
        rows = await self._db.fetchall("SELECT key FROM transcript WHERE session_id = ?", (session_id,))
        known = {r["key"] for r in rows}
        fresh = []
        for message in messages:
            key = self.transcript_key(message)
            if key in known:
                continue
            known.add(key)
            fresh.append((session_id, key, message.model_dump_json()))
        if not fresh:
            return 0
        async with self._db.transaction() as conn:
            for session, key, dumped in fresh:
                cursor = await conn.execute("INSERT OR IGNORE INTO transcript(session_id, key, message) VALUES (?, ?, ?)", (session, key, dumped))
                if cursor.rowcount:
                    message = Message.model_validate_json(dumped)
                    text = message_text(message)
                    if text:
                        await conn.execute(
                            "INSERT INTO transcript_fts(session_id, seq, role, text) VALUES (?, ?, ?, ?)",
                            (session, cursor.lastrowid, message.role.value, text),
                        )
        return len(fresh)

    async def backfill_transcript_index(self) -> int:
        """Index transcript rows written before the full-text table existed; returns how many."""
        rows = await self._db.fetchall(
            "SELECT t.seq, t.session_id, t.message FROM transcript t WHERE NOT EXISTS (SELECT 1 FROM transcript_fts f WHERE f.seq = t.seq)"
        )
        if not rows:
            return 0
        async with self._db.transaction() as conn:
            for row in rows:
                message = Message.model_validate_json(row["message"])
                text = message_text(message)
                if text:
                    await conn.execute(
                        "INSERT INTO transcript_fts(session_id, seq, role, text) VALUES (?, ?, ?, ?)",
                        (row["session_id"], row["seq"], message.role.value, text),
                    )
        return len(rows)

    async def search_transcript(self, query: str, *, session_id: str | None, limit: int = 10) -> list[dict[str, Any]]:
        """Full-text search; ``session_id=None`` searches every session. Returns seq, role, session, snippet."""
        match = fts_query(query)
        if not match:
            return []
        if session_id is None:
            rows = await self._db.fetchall(
                "SELECT f.seq, f.role, f.session_id, snippet(transcript_fts, 3, '[', ']', ' … ', 24) snippet, s.title"
                " FROM transcript_fts f LEFT JOIN sessions s ON s.id = f.session_id"
                " WHERE transcript_fts MATCH ? ORDER BY bm25(transcript_fts) LIMIT ?",
                (match, limit),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT f.seq, f.role, f.session_id, snippet(transcript_fts, 3, '[', ']', ' … ', 24) snippet, NULL title"
                " FROM transcript_fts f WHERE f.session_id = ? AND transcript_fts MATCH ? ORDER BY bm25(transcript_fts) LIMIT ?",
                (session_id, match, limit),
            )
        return [dict(r) for r in rows]

    async def expand_transcript(self, session_id: str, from_seq: int, to_seq: int) -> list[tuple[int, Message]]:
        rows = await self._db.fetchall(
            "SELECT seq, message FROM transcript WHERE session_id = ? AND seq BETWEEN ? AND ? ORDER BY seq",
            (session_id, from_seq, to_seq),
        )
        return [(int(r["seq"]), Message.model_validate_json(r["message"])) for r in rows]

    async def transcript_seqs(self, session_id: str, keys: Sequence[str]) -> list[int]:
        if not keys:
            return []
        marks = ",".join("?" for _ in keys)
        rows = await self._db.fetchall(
            f"SELECT seq FROM transcript WHERE session_id = ? AND key IN ({marks}) ORDER BY seq", (session_id, *keys)
        )
        return [int(r["seq"]) for r in rows]

    async def list_transcript(self, session_id: str, *, limit: int = 0) -> list[Message]:
        if limit > 0:
            rows = await self._db.fetchall(
                "SELECT message FROM (SELECT seq, message FROM transcript WHERE session_id = ? ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                (session_id, limit),
            )
        else:
            rows = await self._db.fetchall("SELECT message FROM transcript WHERE session_id = ? ORDER BY seq", (session_id,))
        return [Message.model_validate_json(r["message"]) for r in rows]

    async def replace_messages(self, session_id: str, tenant_id: str, messages: Sequence[Message]) -> None:
        rows = [(session_id, tenant_id, m.model_dump_json()) for m in messages]
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
            await conn.executemany(
                "INSERT INTO session_messages(session_id, tenant_id, message) VALUES (?, ?, ?)", rows
            )


def _row_to_session(row: Any) -> Session:
    return Session(
        id=row["id"],
        tenant_id=row["tenant_id"],
        title=row["title"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_message_at=datetime.fromisoformat(row["last_message_at"]),
        metadata=json.loads(row["metadata"] or "{}"),
    )


class SqliteRunStore(IRunStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, run: Run) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO runs(id, tenant_id, session_id, status, created_at, updated_at, detail_blob_ref)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.tenant_id,
                run.session_id,
                run.status.value,
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
                run.detail_blob_ref,
            ),
        )

    async def get(self, run_id: str, tenant_id: str) -> Run:
        row = await self._db.fetchone(
            "SELECT * FROM runs WHERE id = ? AND tenant_id = ?", (run_id, tenant_id)
        )
        if row is None:
            raise RunNotFoundError(run_id)
        return _row_to_run(row)

    async def update_status(self, run_id: str, tenant_id: str, status: RunStatus) -> None:
        await self._db.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ? AND tenant_id = ?",
            (status.value, _now(), run_id, tenant_id),
        )

    async def list(
        self,
        tenant_id: str,
        *,
        session_id: str | None = None,
        status: RunStatus | None = None,
        limit: int = 100,
    ) -> Sequence[Run]:
        sql = "SELECT * FROM runs WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [_row_to_run(r) for r in await self._db.fetchall(sql, params)]

    async def flush_terminal(self, run_id: str, tenant_id: str, **_: Any) -> None:
        return None

    async def increment_tool_errors_count(self, run_id: str, by: int = 1) -> None:
        return None


def _row_to_run(row: Any) -> Run:
    return Run(
        id=row["id"],
        tenant_id=row["tenant_id"],
        session_id=row["session_id"],
        status=RunStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        detail_blob_ref=row["detail_blob_ref"],
    )


class SqliteEventStream(IEventStream):
    """Durable per-run event log with in-process live subscribers.

    ``state_snapshot`` events are the engine's resume points; they are stored in
    their own table (latest wins) rather than appended to the log.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._subscribers: dict[str, list[asyncio.Queue[Event | None]]] = {}
        self._session_of_run: dict[str, str] = {}

    def bind_run(self, run_id: str, session_id: str) -> None:
        self._session_of_run[run_id] = session_id

    async def emit(self, event: Event) -> None:
        tenant_id = str(event.payload.get("tenant_id") or "default")
        if event.name == "state_snapshot":
            snapshot = event.payload.get("snapshot") or {}
            await self._db.execute(
                "INSERT INTO snapshots(run_id, tenant_id, session_id, state, snapshot, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id) DO UPDATE SET state = excluded.state,"
                " snapshot = excluded.snapshot, updated_at = excluded.updated_at",
                (
                    event.run_id,
                    tenant_id,
                    self._session_of_run.get(event.run_id) or str(snapshot.get("session_id") or ""),
                    str(snapshot.get("state") or ""),
                    json.dumps(snapshot, default=str),
                    _now(),
                ),
            )
            return
        await self._db.execute(
            "INSERT INTO events(id, run_id, tenant_id, name, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.run_id,
                tenant_id,
                event.name,
                json.dumps(event.payload, default=str),
                event.created_at.isoformat(),
            ),
        )
        for queue in list(self._subscribers.get(event.run_id, [])):
            queue.put_nowait(event)

    async def subscribe(
        self, run_id: str, tenant_id: str, *, from_event_id: str | None = None
    ) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(queue)
        try:
            rows = await self._db.fetchall(
                "SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            )
            started = from_event_id is None
            for row in rows:
                if not started:
                    if row["id"] == from_event_id:
                        started = True
                    continue
                yield _row_to_event(row)
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            subscribers = self._subscribers.get(run_id, [])
            if queue in subscribers:
                subscribers.remove(queue)

    async def trim(self, run_id: str, tenant_id: str, *, max_len: int) -> None:
        await self._db.execute(
            "DELETE FROM events WHERE run_id = ? AND seq NOT IN"
            " (SELECT seq FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT ?)",
            (run_id, run_id, max_len),
        )

    def close_run(self, run_id: str) -> None:
        for queue in self._subscribers.get(run_id, []):
            queue.put_nowait(None)

    # -- host extras --------------------------------------------------------------

    async def list_events(
        self, run_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[tuple[int, Event]]:
        rows = await self._db.fetchall(
            "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (run_id, after_seq, limit),
        )
        return [(int(r["seq"]), _row_to_event(r)) for r in rows]

    async def load_snapshot(self, run_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchone("SELECT snapshot FROM snapshots WHERE run_id = ?", (run_id,))
        return json.loads(row["snapshot"]) if row else None

    async def unfinished_snapshots(self) -> list[dict[str, Any]]:
        rows = await self._db.fetchall(
            "SELECT run_id, session_id, state, snapshot FROM snapshots"
            " WHERE state IN ('running', 'compacting', 'pending', 'awaiting')"
        )
        return [
            {
                "run_id": r["run_id"],
                "session_id": r["session_id"],
                "state": r["state"],
                "snapshot": json.loads(r["snapshot"]),
            }
            for r in rows
        ]

    async def delete_snapshot(self, run_id: str) -> None:
        await self._db.execute("DELETE FROM snapshots WHERE run_id = ?", (run_id,))


def _row_to_event(row: Any) -> Event:
    return Event(
        id=row["id"],
        run_id=row["run_id"],
        name=row["name"],
        payload=json.loads(row["payload"] or "{}"),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class SqliteUsageSink(UsageSink):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, record: UsageRecord) -> None:
        n = record.normalized
        await self._db.execute(
            "INSERT INTO usage_events(at, provider_id, model, purpose, run_id, session_id,"
            " input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, cost_usd, duration_ms, raw)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(),
                record.provider_id,
                record.model,
                record.purpose,
                record.run_id,
                record.session_id,
                int(n.get("input_tokens", 0)),
                int(n.get("output_tokens", 0)),
                int(n.get("cache_read_tokens", 0)),
                int(n.get("reasoning_tokens", 0)),
                record.cost_usd,
                record.duration_ms,
                json.dumps(record.raw),
            ),
        )


class LiveControlStore:
    """Per-session steer / follow-up queues and live model overrides."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self, session_id: str) -> dict[str, Any]:
        row = await self._db.fetchone("SELECT * FROM live_control WHERE session_id = ?", (session_id,))
        if row is None:
            return {"steer": [], "follow_up": [], "model_name": None, "provider": None, "preset": None, "thinking_enabled": None, "reasoning_effort": None}
        return {
            "steer": json.loads(row["steer_queue"] or "[]"),
            "follow_up": json.loads(row["follow_up_queue"] or "[]"),
            "model_name": row["model_name"],
            "provider": row["provider"],
            "preset": row["preset"],
            "thinking_enabled": None if row["thinking_enabled"] is None else bool(row["thinking_enabled"]),
            "reasoning_effort": row["reasoning_effort"],
        }

    async def save_queues(self, session_id: str, steer: list[dict[str, Any]], follow_up: list[dict[str, Any]]) -> None:
        await self._db.execute(
            "INSERT INTO live_control(session_id, steer_queue, follow_up_queue, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET steer_queue = excluded.steer_queue,"
            " follow_up_queue = excluded.follow_up_queue, updated_at = excluded.updated_at",
            (session_id, json.dumps(steer), json.dumps(follow_up), _now()),
        )

    async def enqueue(self, session_id: str, kind: str, item: dict[str, Any]) -> None:
        column = "steer_queue" if kind == "steer" else "follow_up_queue"
        async with self._db.transaction() as conn:
            cursor = await conn.execute("SELECT steer_queue, follow_up_queue FROM live_control WHERE session_id = ?", (session_id,))
            row = await cursor.fetchone()
            await cursor.close()
            current = json.loads(row[column] or "[]") if row else []
            current.append(item)
            await conn.execute(
                f"INSERT INTO live_control(session_id, {column}, updated_at) VALUES (?, ?, ?)"
                f" ON CONFLICT(session_id) DO UPDATE SET {column} = excluded.{column}, updated_at = excluded.updated_at",
                (session_id, json.dumps(current), _now()),
            )

    async def set_model(
        self,
        session_id: str,
        *,
        model_name: str | None = None,
        provider: str | None = None,
        preset: str | None = None,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        state = await self.load(session_id)
        if preset is not None or provider is not None or model_name is not None:
            # A new model choice replaces the previous one wholesale (preset xor manual pair).
            state.update({"preset": None, "provider": None, "model_name": None})
        await self._db.execute(
            "INSERT INTO live_control(session_id, steer_queue, follow_up_queue, model_name, provider, preset, thinking_enabled, reasoning_effort, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET model_name = excluded.model_name, provider = excluded.provider, preset = excluded.preset,"
            " thinking_enabled = excluded.thinking_enabled, reasoning_effort = excluded.reasoning_effort,"
            " updated_at = excluded.updated_at",
            (
                session_id,
                json.dumps(state["steer"]),
                json.dumps(state["follow_up"]),
                model_name if model_name is not None else state["model_name"],
                provider if provider is not None else state["provider"],
                preset if preset is not None else state["preset"],
                None if thinking_enabled is None and state["thinking_enabled"] is None else int(
                    thinking_enabled if thinking_enabled is not None else state["thinking_enabled"]
                ),
                reasoning_effort if reasoning_effort is not None else state["reasoning_effort"],
                _now(),
            ),
        )

    async def clear_overrides(self, session_id: str) -> None:
        await self._db.execute(
            "UPDATE live_control SET model_name = NULL, provider = NULL, preset = NULL, thinking_enabled = NULL, reasoning_effort = NULL WHERE session_id = ?",
            (session_id,),
        )


__all__ = [
    "LiveControlStore",
    "SqliteEventStream",
    "SqliteRunStore",
    "SqliteSessionStore",
    "SqliteUsageSink",
]


class DeliveryLedger:
    """Rows around the send of a final answer: pending → attempting → delivered | failed.

    Honest at-least-once: a row left ``attempting`` when the process died is re-sent after
    a restart with a visible "recovered" marker, because Telegram may already have it.
    Poison rows cannot spin — three attempts, one day of staleness — and the ledger never
    blocks a send: every method swallows its own errors.
    """

    MAX_ATTEMPTS = 3
    MAX_AGE_HOURS = 24
    KEEP_DAYS = 7

    def __init__(self, db: Database) -> None:
        self._db = db

    async def begin(self, run_id: str, session_id: str, text: str) -> None:
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO deliveries(run_id, session_id, text, status, attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, 'attempting', COALESCE((SELECT attempts FROM deliveries WHERE run_id = ?), 0) + 1, ?, ?)",
                (run_id, session_id, text[:200_000], run_id, _now(), _now()),
            )
        except Exception:  # noqa: BLE001
            pass

    async def settle(self, run_id: str, *, delivered: bool, error: str = "") -> None:
        try:
            await self._db.execute(
                "UPDATE deliveries SET status = ?, error = ?, updated_at = ? WHERE run_id = ?",
                ("delivered" if delivered else "failed", error[:500] or None, _now(), run_id),
            )
        except Exception:  # noqa: BLE001
            pass

    async def recoverable(self) -> list[dict[str, Any]]:
        """Rows a restart should re-send: not delivered, under the attempt cap, not stale."""
        cutoff = (datetime.now(UTC) - timedelta(hours=self.MAX_AGE_HOURS)).isoformat()
        try:
            rows = await self._db.fetchall(
                "SELECT * FROM deliveries WHERE status IN ('pending', 'attempting', 'failed') AND attempts < ? AND created_at >= ? ORDER BY created_at",
                (self.MAX_ATTEMPTS, cutoff),
            )
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    async def prune(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=self.KEEP_DAYS)).isoformat()
        try:
            await self._db.execute("DELETE FROM deliveries WHERE updated_at < ?", (cutoff,))
        except Exception:  # noqa: BLE001
            pass
