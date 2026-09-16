"""SQLite implementations of the core store contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

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

UNFINISHED_RUN_STATUSES = ", ".join(f"'{status.value}'" for status in (RunStatus.queued, RunStatus.running, RunStatus.paused))
"""The statuses of a run that is not over. Its events are what the live view reads and what a resume
replays, so nothing sweeps them by age."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _like_literal(value: str) -> str:
    """``value`` as a LIKE pattern that matches itself: a call id is an opaque string and may hold
    a wildcard as easily as any other character."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _mentions_call(message: Message, call_id: str) -> bool:
    return any(getattr(block, "tool_call_id", None) == call_id for block in message.content_blocks)


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
    if query.startswith('"') and query.endswith('"') and len(query) > 2 and '"' not in query[1:-1]:
        return '"' + query[1:-1] + '"'
    tokens = _FTS_TOKEN_RE.findall(query)
    return " ".join(f'"{t.replace(chr(34), "")}"*' for t in tokens if t)


def snippet(text: str, query: str, *, window: int = 160) -> str:
    """A window of ``text`` around the first query word, with every query word bracketed.

    fts5 renders this itself when it keeps a copy of the indexed text; a contentless index
    does not keep one, and the transcript row it points at has the text anyway.
    """
    words = [w for w in (t.lower() for t in _FTS_TOKEN_RE.findall(query)) if w]
    flat = " ".join(text.split())
    if not words or not flat:
        return flat[:window]
    lowered = flat.lower()
    at = min((p for p in (lowered.find(w) for w in words) if p >= 0), default=-1)
    start = max(0, at - window // 3) if at >= 0 else 0
    start = flat.rfind(" ", 0, start) + 1 if start else 0
    cut = flat[start : start + window]
    marked = _FTS_TOKEN_RE.sub(lambda m: f"[{m.group(0)}]" if m.group(0).lower().startswith(tuple(words)) else m.group(0), cut)
    return ("… " if start else "") + marked + (" …" if start + window < len(flat) else "")


class TranscriptView(Protocol):
    """What turns a transcript row into the shape a client draws, and names its own version.

    The store keeps the result beside the row and never looks inside it; the host supplies
    the implementation (:class:`daedalus.host.transcript_view.TranscriptViewBuilder`), which
    is why the view can depend on redaction and prompts without the store doing so.
    """

    def key(self) -> str: ...

    def build(self, message: Message) -> dict[str, Any]: ...


class SqliteSessionStore(ISessionStore):
    def __init__(self, db: Database, *, view: TranscriptView | None = None) -> None:
        self._db = db
        self._view = view

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

    CURRENT_GEN = "(SELECT coalesce(max(gen), 0) FROM session_messages WHERE session_id = ? AND tenant_id = ?)"
    """The generation a session's working history is currently in; older ones are not read. Filtered by
    tenant like every read and every write of this table: a read that missed the filter would answer
    with an empty history and the write after it would delete another tenant's generations."""

    async def append_message(self, session_id: str, tenant_id: str, message: Message) -> None:
        await self._db.execute(
            f"INSERT INTO session_messages(session_id, tenant_id, gen, key, message) VALUES (?, ?, {self.CURRENT_GEN}, ?, ?)",
            (session_id, tenant_id, session_id, tenant_id, self.transcript_key(message), message.model_dump_json()),
        )
        await self._db.execute(
            "UPDATE sessions SET last_message_at = ? WHERE id = ?", (_now(), session_id)
        )

    async def list_messages(
        self, session_id: str, tenant_id: str, *, limit: int = 100, offset: int = 0
    ) -> Sequence[Message]:
        rows = await self._db.fetchall(
            f"SELECT message FROM session_messages WHERE session_id = ? AND tenant_id = ? AND gen = {self.CURRENT_GEN}"
            " ORDER BY seq LIMIT ? OFFSET ?",
            (session_id, tenant_id, session_id, tenant_id, limit, offset),
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
        """Identity of a message across history rewrites: role + creation time (+ tool call id).

        A message that asks for two things at once carries two call ids and is named by the first of
        them — the key is an identity, not an index, and changing its shape would make every message
        already stored a different message. ``messages_for_call`` is where the other calls of a batch
        are found, and it does not assume the key names the one it was asked about.
        """
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
        seen: set[str] = set()
        candidates: list[tuple[str, Message]] = []
        for message in messages:
            key = self.transcript_key(message)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((key, message))
        # The caller hands over the whole history every round and almost all of it is already
        # here. Asking which keys are present is one indexed query; without it every round
        # serialised — and, since the view is built here, redacted — a history's worth of
        # messages to write one. INSERT OR IGNORE below still settles a race.
        if len(candidates) > 1:
            present = await self.transcript_keys_present(session_id, [key for key, _ in candidates])
            candidates = [(key, message) for key, message in candidates if key not in present]
        if not candidates:
            return 0
        added = 0
        last_seq = 0
        async with self._db.transaction() as conn:
            # UNIQUE(session_id, key) + INSERT OR IGNORE is the dedup; rowcount says whether the row is new.
            for key, message in candidates:
                cursor = await conn.execute(
                    "INSERT OR IGNORE INTO transcript(session_id, key, message, view, view_key) VALUES (?, ?, ?, ?, ?)",
                    (session_id, key, message.model_dump_json(), *self._view_of(message)),
                )
                if cursor.rowcount:
                    await conn.execute("UPDATE sessions SET last_message_at = ? WHERE id = ?", (_now(), session_id))
                if cursor.rowcount:
                    added += 1
                    last_seq = max(last_seq, int(cursor.lastrowid or 0))
                    text = message_text(message)
                    if text:
                        await conn.execute("INSERT INTO transcript_fts(rowid, text) VALUES (?, ?)", (cursor.lastrowid, text))
            if added:
                # Rows written here are indexed here; the backfill watermark must never fall behind them.
                await conn.execute(
                    "INSERT INTO kv(key, value) VALUES ('transcript_fts_watermark', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = CASE WHEN CAST(kv.value AS INTEGER) < excluded.value THEN excluded.value ELSE kv.value END",
                    (json.dumps(last_seq),),
                )
        return added

    BACKFILL_BATCH = 500

    async def backfill_transcript_index(self, progress: Callable[[int, int], None] | None = None) -> int:
        """Index transcript rows written before the full-text table existed; returns how many.

        Works up from a watermark in ``kv`` in batches, each in its own short transaction, so a
        large transcript is indexed without holding the connection for the whole pass. ``progress``
        is called with (rows walked, rows to walk) after each batch: while this runs a search finds
        nothing in what it has not reached yet, and "nothing" reads as "that was never said".
        """
        watermark_row = await self._db.kv_get("transcript_fts_watermark", None)
        counts = await self._db.fetchone(
            "SELECT (SELECT count(*) FROM transcript_fts) fts, (SELECT count(*) FROM transcript) rows,"
            " (SELECT count(*) FROM transcript WHERE seq > ?) pending",
            (int(watermark_row or 0),),
        )
        total = int(counts["pending"]) if counts else 0
        if watermark_row is None or (counts is not None and int(counts["fts"]) > int(counts["rows"])):
            # No watermark yet, or more index rows than transcript rows (an older indexer double-counted):
            # the index is derived data, so rebuild it from the transcript rather than reason about it.
            await self._db.execute("DELETE FROM transcript_fts")
            await self._db.kv_set("transcript_fts_watermark", 0)
            watermark_row = 0
            total = int(counts["rows"]) if counts else 0
        watermark = int(watermark_row or 0)
        indexed = 0
        walked = 0
        if progress is not None and total:
            progress(0, total)
        while True:
            rows = await self._db.fetchall(
                "SELECT seq, session_id, message FROM transcript WHERE seq > ? ORDER BY seq LIMIT ?", (watermark, self.BACKFILL_BATCH)
            )
            if not rows:
                break
            async with self._db.transaction() as conn:
                for row in rows:
                    message = Message.model_validate_json(row["message"])
                    text = message_text(message)
                    if text:
                        await conn.execute("INSERT INTO transcript_fts(rowid, text) VALUES (?, ?)", (row["seq"], text))
                        indexed += 1
                watermark = int(rows[-1]["seq"])
                await conn.execute(
                    "INSERT INTO kv(key, value) VALUES ('transcript_fts_watermark', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (json.dumps(watermark),),
                )
            walked += len(rows)
            if progress is not None and total:
                progress(min(walked, total), total)
            await asyncio.sleep(0)  # let the loop breathe between batches
        if progress is not None and total:
            progress(total, total)
        return indexed

    SNIPPET_WINDOW = 160
    """Characters of context a search hit is shown with, centred on the first match."""

    async def search_transcript(self, query: str, *, session_id: str | None, limit: int = 10) -> list[dict[str, Any]]:
        """Full-text search; ``session_id=None`` searches every session. Returns seq, role, session, snippet.

        The index is contentless, so the hit names a transcript row and the row supplies the
        text — which is where it was all along. A handful of rows are read per search.
        """
        match = fts_query(query)
        if not match:
            return []
        where = " AND t.session_id = ?" if session_id is not None else ""
        params: list[Any] = [match] + ([session_id] if session_id is not None else []) + [limit]
        rows = await self._db.fetchall(
            "SELECT t.seq, t.session_id, t.message, s.title FROM transcript_fts f"
            " JOIN transcript t ON t.seq = f.rowid LEFT JOIN sessions s ON s.id = t.session_id"
            f" WHERE transcript_fts MATCH ?{where} ORDER BY bm25(transcript_fts) LIMIT ?",
            tuple(params),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            message = Message.model_validate_json(row["message"])
            out.append(
                {
                    "seq": int(row["seq"]),
                    "session_id": row["session_id"],
                    "role": message.role.value,
                    "title": row["title"] if session_id is None else None,
                    "snippet": snippet(message_text(message), query, window=self.SNIPPET_WINDOW),
                }
            )
        return out

    async def expand_transcript(self, session_id: str, from_seq: int, to_seq: int) -> list[tuple[int, Message]]:
        rows = await self._db.fetchall(
            "SELECT seq, message FROM transcript WHERE session_id = ? AND seq BETWEEN ? AND ? ORDER BY seq",
            (session_id, from_seq, to_seq),
        )
        return [(int(r["seq"]), Message.model_validate_json(r["message"])) for r in rows]

    async def transcript_seqs(self, session_id: str, keys: Sequence[str]) -> list[int]:
        out: list[int] = []
        for start in range(0, len(keys), 500):  # stay under SQLite's bind-parameter limit
            chunk = list(keys[start : start + 500])
            marks = ",".join("?" for _ in chunk)
            rows = await self._db.fetchall(
                f"SELECT seq FROM transcript WHERE session_id = ? AND key IN ({marks})", (session_id, *chunk)
            )
            out.extend(int(r["seq"]) for r in rows)
        return sorted(out)

    def _view_of(self, message: Message) -> tuple[str | None, str]:
        """The stored view of a message and the key naming what produced it, for the row's two columns."""
        if self._view is None:
            return None, ""
        return json.dumps(self._view.build(message), default=str, ensure_ascii=False), self._view.key()

    async def list_transcript_views(self, session_id: str, *, limit: int = 0, before_seq: int = 0) -> list[dict[str, Any]]:
        """One page of the session as the app draws it, without parsing a message.

        Rows whose stored view was produced by older code or an older redactor are rebuilt
        from the message and written back, so the next read of the page is free again.
        """
        if self._view is None:
            return [self._view_only_fallback(m) for m in await self.list_transcript(session_id, limit=limit, before_seq=before_seq)]
        params: list[Any] = [session_id]
        where = "session_id = ?"
        if before_seq > 0:
            where += " AND seq < ?"
            params.append(before_seq)
        if limit > 0:
            params.append(limit)
            rows = await self._db.fetchall(
                f"SELECT seq, view, view_key, message FROM (SELECT seq, view, view_key, message FROM transcript WHERE {where} ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                tuple(params),
            )
        else:
            rows = await self._db.fetchall(f"SELECT seq, view, view_key, message FROM transcript WHERE {where} ORDER BY seq", tuple(params))
        current = self._view.key()
        out: list[dict[str, Any]] = []
        stale: list[tuple[str, str, int]] = []
        for row in rows:
            seq = int(row["seq"])
            if row["view"] and row["view_key"] == current:
                view = json.loads(row["view"])
            else:
                message = Message.model_validate_json(row["message"])
                view = self._view.build(message)
                stale.append((json.dumps(view, default=str, ensure_ascii=False), current, seq))
            view["seq"] = seq
            out.append(view)
        if stale:
            await self._db.executemany("UPDATE transcript SET view = ?, view_key = ? WHERE seq = ?", stale)
        return out

    def _view_only_fallback(self, message: Message) -> dict[str, Any]:
        """A store built without a view builder still answers, with the message itself."""
        return {"seq": message.metadata.get("daedalus.seq"), "role": message.role.value, "text": "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))}

    async def transcript_keys_present(self, session_id: str, keys: Sequence[str]) -> set[str]:
        """Which of ``keys`` the transcript already holds; the UNIQUE(session_id, key) index answers it."""
        found: set[str] = set()
        for start in range(0, len(keys), 500):  # stay under SQLite's bind-parameter limit
            chunk = list(keys[start : start + 500])
            marks = ",".join("?" for _ in chunk)
            rows = await self._db.fetchall(
                f"SELECT key FROM transcript WHERE session_id = ? AND key IN ({marks})", (session_id, *chunk)
            )
            found.update(str(r["key"]) for r in rows)
        return found

    async def list_transcript(self, session_id: str, *, limit: int = 0, before_seq: int = 0) -> list[Message]:
        """Transcript rows as messages; each carries its row number in ``metadata["daedalus.seq"]``.

        ``limit`` is the newest N rows and ``before_seq`` the page older than a row already
        shown: both are answered by the ``(session_id, seq)`` index, so the cost is the page
        and not the session. An unbounded call reads the whole transcript and is only for the
        few places that genuinely want all of it (the Markdown export, the bench runner).
        """
        params: list[Any] = [session_id]
        where = "session_id = ?"
        if before_seq > 0:
            where += " AND seq < ?"
            params.append(before_seq)
        if limit > 0:
            params.append(limit)
            rows = await self._db.fetchall(
                f"SELECT seq, message FROM (SELECT seq, message FROM transcript WHERE {where} ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                tuple(params),
            )
        else:
            rows = await self._db.fetchall(f"SELECT seq, message FROM transcript WHERE {where} ORDER BY seq", tuple(params))
        out = []
        for r in rows:
            message = Message.model_validate_json(r["message"])
            out.append(message.model_copy(update={"metadata": {**message.metadata, "daedalus.seq": int(r["seq"])}}))
        return out

    async def transcript_bounds(self, session_id: str) -> tuple[int, int]:
        """Lowest and highest transcript seq of a session; ``(0, 0)`` when it has none."""
        row = await self._db.fetchone("SELECT min(seq) lo, max(seq) hi FROM transcript WHERE session_id = ?", (session_id,))
        return (int(row["lo"] or 0), int(row["hi"] or 0)) if row else (0, 0)

    TOOL_CALL_SCAN_PAGE = 200
    TOOL_CALL_BATCH_PAGES = 5
    """How far back a key hit is followed to find the call it belongs to. A tool call and its result are
    neighbours in the transcript; this is room for the batch around them and not for the session."""

    async def messages_for_call(self, session_id: str, call_id: str) -> list[Message]:
        """Transcript messages carrying a block of ``call_id``, newest first — without loading the session.

        The transcript key names one call of a message that carries any, so a result row is found from
        the key index alone. The message that *made* the call may be keyed by another call of the same
        batch — a model that asks for two things at once is keyed by the first of them — and is then
        found by walking back from the hit a page at a time. Both are needed: one carries the result and
        the other the arguments, and the file a ``SendFile`` handed over is named in the arguments.
        """
        hits = [
            int(r["seq"])
            for r in await self._db.fetchall(
                "SELECT seq FROM transcript WHERE session_id = ? AND key LIKE ? ESCAPE '\\' ORDER BY seq DESC",
                (session_id, "%:" + _like_literal(call_id)),
            )
        ]
        found = [m for m in await self._messages_at(session_id, hits) if _mentions_call(m, call_id)]
        if any(isinstance(b, ToolUseBlock) and b.tool_call_id == call_id for m in found for b in m.content_blocks):
            return found
        # Either nothing was keyed by this call at all, or what was is the result of a call made in a
        # batch. A hit gives the scan a place to start and a reason to stop early; without one there is
        # nowhere to start but the end of the transcript.
        before = min(hits) if hits else 0
        pages = self.TOOL_CALL_BATCH_PAGES if hits else 0
        seen = set(hits)
        page = 0
        while not pages or page < pages:
            params: list[Any] = [session_id]
            where = "session_id = ?"
            if before:
                where += " AND seq < ?"
                params.append(before)
            rows = await self._db.fetchall(f"SELECT seq FROM transcript WHERE {where} ORDER BY seq DESC LIMIT ?", (*params, self.TOOL_CALL_SCAN_PAGE))
            if not rows:
                break
            seqs = [int(r["seq"]) for r in rows]
            before, page = seqs[-1], page + 1
            for message in await self._messages_at(session_id, [q for q in seqs if q not in seen]):
                if _mentions_call(message, call_id):
                    found.append(message)
            if found and any(isinstance(b, ToolUseBlock) and b.tool_call_id == call_id for m in found for b in m.content_blocks):
                break
        return found

    async def _messages_at(self, session_id: str, seqs: Sequence[int]) -> list[Message]:
        """The named transcript rows, in the order given."""
        if not seqs:
            return []
        marks = ",".join("?" for _ in seqs)
        rows = await self._db.fetchall(
            f"SELECT seq, message FROM transcript WHERE session_id = ? AND seq IN ({marks})", (session_id, *seqs)
        )
        by_seq = {int(r["seq"]): Message.model_validate_json(r["message"]) for r in rows}
        return [by_seq[s] for s in seqs if s in by_seq]

    async def replace_transcript_message(self, session_id: str, key: str, message: Message) -> bool:
        """Rewrite one transcript row in place (same key, same seq); the search index follows."""
        row = await self._db.fetchone("SELECT seq FROM transcript WHERE session_id = ? AND key = ?", (session_id, key))
        if row is None:
            return False
        view, view_key = self._view_of(message)
        async with self._db.transaction() as conn:
            await conn.execute("UPDATE transcript SET message = ?, view = ?, view_key = ? WHERE session_id = ? AND key = ?", (message.model_dump_json(), view, view_key, session_id, key))
            await conn.execute("DELETE FROM transcript_fts WHERE rowid = ?", (int(row["seq"]),))
            text = message_text(message)
            if text:
                await conn.execute("INSERT INTO transcript_fts(rowid, text) VALUES (?, ?)", (int(row["seq"]), text))
        return True

    async def transcript_row(self, session_id: str, seq: int) -> tuple[str, Message] | None:
        row = await self._db.fetchone("SELECT key, message FROM transcript WHERE session_id = ? AND seq = ?", (session_id, seq))
        return (row["key"], Message.model_validate_json(row["message"])) if row else None

    async def replace_messages(self, session_id: str, tenant_id: str, messages: Sequence[Message]) -> None:
        """Start a new generation of the working history: the sequence itself changed.

        A compaction, a revert or a clear replaces the history rather than continuing it. The
        new generation is written whole and the one before it dropped, in one transaction, so a
        reader never sees a half-written history and never sees two.
        """
        row = await self._db.fetchone("SELECT coalesce(max(gen), 0) + 1 next FROM session_messages WHERE session_id = ? AND tenant_id = ?", (session_id, tenant_id))
        gen = int(row["next"]) if row else 1
        rows = [(session_id, tenant_id, gen, self.transcript_key(m), m.model_dump_json()) for m in messages]
        async with self._db.transaction() as conn:
            await conn.executemany(
                "INSERT INTO session_messages(session_id, tenant_id, gen, key, message) VALUES (?, ?, ?, ?, ?)", rows
            )
            await conn.execute("DELETE FROM session_messages WHERE session_id = ? AND tenant_id = ? AND gen < ?", (session_id, tenant_id, gen))

    async def sync_messages(self, session_id: str, tenant_id: str, messages: Sequence[Message]) -> int:
        """Bring the stored working history up to what the engine holds; returns rows written.

        Nearly every round adds to the end of the same sequence, so what is already stored is
        left where it is and only the new messages are appended. When the sequence did not grow
        but changed — a compaction rewrote it — the whole thing becomes a new generation, which
        is the only time this table is rewritten.
        """
        keys = [self.transcript_key(m) for m in messages]
        rows = await self._db.fetchall(
            f"SELECT key FROM session_messages WHERE session_id = ? AND tenant_id = ? AND gen = {self.CURRENT_GEN} ORDER BY seq", (session_id, tenant_id, session_id, tenant_id)
        )
        stored = [str(r["key"]) for r in rows]
        if stored != keys[: len(stored)]:
            await self.replace_messages(session_id, tenant_id, messages)
            return len(messages)
        fresh = list(messages)[len(stored) :]
        if not fresh:
            return 0
        gen_row = await self._db.fetchone("SELECT coalesce(max(gen), 0) gen FROM session_messages WHERE session_id = ? AND tenant_id = ?", (session_id, tenant_id))
        gen = int(gen_row["gen"]) if gen_row else 0
        await self._db.executemany(
            "INSERT INTO session_messages(session_id, tenant_id, gen, key, message) VALUES (?, ?, ?, ?, ?)",
            [(session_id, tenant_id, gen, self.transcript_key(m), m.model_dump_json()) for m in fresh],
        )
        return len(fresh)


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

    LIVE_ONLY = ("content_block_delta", "tool_use_input_delta")
    """Streaming fragments: a subscriber that is watching wants them, and nothing else ever does.

    They were the larger part of the event log — one autocommit transaction per token, kept
    for the fifteen minutes until the run ended and the log was trimmed — and the text they
    carry is in the transcript by the time the message stops. They are fanned out and dropped.
    """

    BY_REFERENCE = {"tool_surface_advertised": "tools"}
    """Events whose payload repeats the same large value run after run: the value is kept once
    under its digest and the row names the digest. The whole tool surface is thirty kilobytes
    and identical across hundreds of runs."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._subscribers: dict[str, list[asyncio.Queue[Event | None]]] = {}
        self._session_of_run: dict[str, str] = {}

    def bind_run(self, run_id: str, session_id: str) -> None:
        self._session_of_run[run_id] = session_id

    async def _payload_by_reference(self, event: Event) -> dict[str, Any]:
        """The payload as it is stored: a repeated large member replaced by its digest."""
        field = self.BY_REFERENCE.get(event.name)
        value = event.payload.get(field) if field else None
        if field is None or value is None:
            return event.payload
        body = json.dumps(value, default=str, sort_keys=True)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        await self._db.execute("INSERT OR IGNORE INTO kv(key, value) VALUES (?, ?)", (f"event_blob:{digest}", body))
        return {**event.payload, field: {"digest": digest, "count": len(value) if isinstance(value, (list, dict)) else 1}}

    async def emit(self, event: Event) -> None:
        tenant_id = str(event.payload.get("tenant_id") or "default")
        if event.name in self.LIVE_ONLY:
            for queue in list(self._subscribers.get(event.run_id, [])):
                queue.put_nowait(event)
            return
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
                json.dumps(await self._payload_by_reference(event), default=str),
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

    async def prune(self, *, keep_days: int, max_rows: int) -> int:
        """Drop the event log of runs that are over and old; returns how many rows went.

        "Over" is read from the run's status, not from the age of the row: the age sweep is what
        keeps the table bounded and the status is what keeps it from taking a run in progress.

        ``trim`` bounds a run, and every finished run sits at its cap — but runs accumulate
        for as long as the installation does, so the log has no ceiling without this. The
        transcript is the record; events are what the live view was fed.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        before = await self._db.fetchone("SELECT count(*) c FROM events")
        # Only the events of runs that are over. By wall-clock age alone, a run still going after
        # ``keep_days`` — a loop, a long-running agent — would lose its early events while it runs.
        await self._db.execute(
            "DELETE FROM events WHERE created_at < ? AND (run_id IS NULL OR run_id NOT IN"
            f" (SELECT id FROM runs WHERE status IN ({UNFINISHED_RUN_STATUSES})))",
            (cutoff,),
        )
        await self._db.execute(
            "DELETE FROM events WHERE seq NOT IN (SELECT seq FROM events ORDER BY seq DESC LIMIT ?)", (max_rows,)
        )
        # A kept blob is the tool surface of some run; one that no surviving row names is gone with it.
        await self._db.execute(
            "DELETE FROM kv WHERE key LIKE 'event_blob:%'"
            " AND NOT EXISTS (SELECT 1 FROM events WHERE payload LIKE '%' || substr(kv.key, 12) || '%')"
        )
        after = await self._db.fetchone("SELECT count(*) c FROM events")
        return int(before["c"] or 0) - int(after["c"] or 0) if before and after else 0

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
            "SELECT run_id, session_id, state, snapshot, updated_at FROM snapshots"
            " WHERE state IN ('running', 'compacting', 'pending', 'awaiting') ORDER BY updated_at DESC"
        )
        return [
            {
                "run_id": r["run_id"],
                "session_id": r["session_id"],
                "state": r["state"],
                "snapshot": json.loads(r["snapshot"]),
                "updated_at": r["updated_at"],
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

    async def load_models(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        """The model overrides of many sessions in one query: {session_id: {model_name, provider, preset}} for those that have any."""
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(session_ids), 400):
            chunk = session_ids[start : start + 400]
            if not chunk:
                continue
            marks = ",".join("?" for _ in chunk)
            rows = await self._db.fetchall(f"SELECT session_id, model_name, provider, preset FROM live_control WHERE session_id IN ({marks})", tuple(chunk))
            for row in rows:
                out[row["session_id"]] = {"model_name": row["model_name"], "provider": row["provider"], "preset": row["preset"]}
        return out

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
    Poison rows cannot spin — a few attempts, a day of staleness measured from the first
    attempt — and the ledger never blocks a send: every method swallows its own errors.
    """

    MAX_ATTEMPTS = 3
    MAX_AGE_HOURS = 24
    KEEP_DAYS = 7
    MAX_TEXT_CHARS = 200_000

    def __init__(self, db: Database, *, max_attempts: int = MAX_ATTEMPTS, max_age_hours: int = MAX_AGE_HOURS, keep_days: int = KEEP_DAYS) -> None:
        self._db = db
        self.max_attempts = max_attempts
        self.max_age_hours = max_age_hours
        self.keep_days = keep_days

    async def begin(self, run_id: str, session_id: str, text: str) -> None:
        if len(text) > self.MAX_TEXT_CHARS:
            text = text[: self.MAX_TEXT_CHARS] + "\n\n[… the recovered copy is truncated; the full answer is in the Mini App transcript]"
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO deliveries(run_id, session_id, text, status, attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, 'attempting', COALESCE((SELECT attempts FROM deliveries WHERE run_id = ?), 0) + 1,"
                " COALESCE((SELECT created_at FROM deliveries WHERE run_id = ?), ?), ?)",
                (run_id, session_id, text, run_id, run_id, _now(), _now()),
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
        cutoff = (datetime.now(UTC) - timedelta(hours=self.max_age_hours)).isoformat()
        try:
            rows = await self._db.fetchall(
                "SELECT * FROM deliveries WHERE status IN ('attempting', 'failed') AND attempts < ? AND created_at >= ? ORDER BY created_at",
                (self.max_attempts, cutoff),
            )
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    async def prune(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=self.keep_days)).isoformat()
        try:
            await self._db.execute("DELETE FROM deliveries WHERE updated_at < ?", (cutoff,))
        except Exception:  # noqa: BLE001
            pass
