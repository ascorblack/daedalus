"""Sessions and runs: one ``QueryEngine`` per session, one asyncio task per run."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.tool_registry import ToolVisibilityPolicy
from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    Message,
    MessageRole,
    Run,
    RunStatus,
    Session,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from protocore.runtime.context.compaction import CompactionState
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType
from protocore.runtime.live_control import new_queued_prompt
from protocore.runtime.loop_state import LoopState
from protocore.runtime.query_engine import QueryEngine
from protocore.tests_support.adapters import InMemoryToolRegistry
from protocore.tools.ask_user import AskUserTool
from protocore.tools.memory import build_memory_tools

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.checkpoints import CheckpointError, Checkpoints, workspace_size
from daedalus.host.engine_factory import TENANT, EngineDeps, build_engine
from daedalus.host.hooks import DaedalusHookManager
from daedalus.host.services import SessionServices, locator
from daedalus.host.skills import DirectorySkillStore
from daedalus.mcp.manager import McpManager, blocked_for
from daedalus.providers.chain import build_chain
from daedalus.providers.registry import ProviderRegistry
from daedalus.security import redact
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database
from daedalus.stores.persistent import PersistentMemory, PersistentWorkspace
from daedalus.stores.sqlite import (
    LiveControlStore,
    SqliteEventStream,
    SqliteRunStore,
    SqliteSessionStore,
    SqliteUsageSink,
)
from daedalus.tools import discover_tools

logger = logging.getLogger(__name__)
BRIEF_MAX_CHARS = 12_000
"""A spawned agent's brief lives in its system prompt; longer hand-overs belong in files."""

EventSink = Callable[[str, TurnEvent], Awaitable[None]]
RunFinished = Callable[[str, str, str], Awaitable[None]]  # session_id, run_id, status


@dataclass(slots=True)
class Attachment:
    path: Path
    mime_type: str = "application/octet-stream"
    caption: str | None = None


@dataclass(slots=True)
class PendingQuestion:
    session_id: str
    run_id: str
    tool_call_id: str
    kind: str
    payload: dict[str, Any]


@dataclass(slots=True)
class SessionState:
    session: Session
    workspace: Path
    engine: QueryEngine | None = None
    task: asyncio.Task[None] | None = None
    run_id: str | None = None
    pending: PendingQuestion | None = None
    services: SessionServices | None = None
    context_window: int | None = None
    """Per-session override; ``None`` follows the configured model window."""
    extra_notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    run_origin: str = "operator"
    """Who started the current run (``operator``, ``schedule``, ``reminder``, ``subagent:…``): StaySilent and the reply routing read it."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialises run starts against history rewrites (compaction)."""
    history_keys: list[str] = field(default_factory=list)
    """Transcript keys of the working history at the last persist, to see what a compaction removed."""
    persist_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialises history persistence so an older snapshot can never overwrite a newer one."""
    persist_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    persist_gen: int = 0
    """Bumped by every history rewrite (manual compaction); a persist captured before the bump is dropped."""
    run_history_start: int = 0
    """Length of the working history when the current run began: what this run added starts here."""
    checkpoint_capped: bool = False
    """Set once the size cap has suppressed a snapshot, so the warning is logged once per session."""

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        config: RuntimeConfig,
        *,
        db: Database,
        governance_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.config = config
        self.db = db
        self.sessions = SqliteSessionStore(db)
        self.runs = SqliteRunStore(db)
        self.events = SqliteEventStream(db)
        self.usage = SqliteUsageSink(db)
        self.live = LiveControlStore(db)
        self.blobs = FileBlobStore(settings.blobs_dir)
        self.memory = PersistentMemory(db)
        self.workspace_units = PersistentWorkspace(db)
        self.skills = DirectorySkillStore(settings.skills_dir)
        self.redactor = redact.shared()
        self._configure_redactor(settings, config)
        self.hooks = DaedalusHookManager(self.redactor)
        self.tools = InMemoryToolRegistry()
        self.providers = ProviderRegistry(
            settings, config, usage_sink=self.usage, image_loader=self._load_image
        )
        self.mcp = McpManager(config.mcp.servers, self.tools, token_dir=settings.state_dir / "mcp")
        self.governance_path = governance_path or (settings.bot_repo_dir / "GOVERNANCE.md")
        self._states: dict[str, SessionState] = {}
        self._sinks: list[EventSink] = []
        self._finished: list[RunFinished] = []
        self.compaction_hooks: list[Callable[[str, dict[str, Any]], Awaitable[None]]] = []
        """Called after an automatic compaction with what changed, so the chat can say so in one line."""
        self._pending_restored: list[Callable[[str, PendingQuestion], Awaitable[None]]] = []
        self.service_hooks: dict[str, Any] = {}
        """Callbacks the transport layer installs: send_file, spawn_session, schedule, self_*."""
        self.prompt_hooks: list[Callable[[str, str], Awaitable[str]]] = []
        """``(session_id, text) -> text`` applied to a message that starts a new run (fired reminders ride along)."""
        self.run_started_hooks: list[Callable[[str, str], Awaitable[None]]] = []
        """``(session_id, run_id)`` after a run was actually created — the point where a prompt hook's side effects may be committed."""
        self.shutting_down = False
        self.budget_flag = settings.state_dir / "BUDGET_EXCEEDED"
        self._capped_runs: set[str] = set()
        """Runs already stopped at the per-run cap (the stop is cooperative; the notice fires once)."""

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        await self.memory.load()
        await self.workspace_units.load()
        for tool in discover_tools():
            self.tools.register(tool)
        for tool in build_memory_tools(self.memory):
            self.tools.register(tool)
        self.tools.register(AskUserTool())
        self.service_hooks.setdefault("mcp", self.mcp_service)
        locator.default = None
        self._backfill_task = asyncio.create_task(self._backfill_index(), name="transcript-index")
        self._backfill_task.add_done_callback(_log_task_failure)
        logger.warning("tools registered: %s", ", ".join(sorted(t.name for t in self.tools.list_all())))

    async def _backfill_index(self) -> None:
        indexed = await self.sessions.backfill_transcript_index()
        if indexed:
            logger.warning("transcript search index: %d older turns indexed", indexed)

    async def close(self) -> None:
        """Shut down keeping every active run resumable (snapshots stay in place)."""
        self.shutting_down = True
        tasks = [s.task for s in self._states.values() if s.task and not s.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        pending = [t for s in self._states.values() for t in s.persist_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)  # the last history write must land
        backfill = getattr(self, "_backfill_task", None)
        if backfill is not None and not backfill.done():
            backfill.cancel()
            await asyncio.gather(backfill, return_exceptions=True)
        await self.mcp.close()
        await self.providers.aclose()

    def budget_exceeded(self) -> str | None:
        if self.budget_flag.exists():
            return self.budget_flag.read_text(encoding="utf-8").strip()
        return None

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def on_finished(self, callback: RunFinished) -> None:
        self._finished.append(callback)

    def on_pending_restored(self, callback: Callable[[str, PendingQuestion], Awaitable[None]]) -> None:
        """Called after a restart for every session still waiting on a question."""
        self._pending_restored.append(callback)

    def _configure_redactor(self, settings: Settings, config: RuntimeConfig) -> None:
        """Every credential this process holds is a value the redactor masks wherever it shows up."""
        values: list[str] = [
            settings.telegram_bot_token,
            settings.deepseek_api_key,
            settings.openrouter_api_key,
            settings.vllm_api_key,
            settings.github_token,
            settings.telegram_api_hash,
        ]
        values.extend(p.api_key for p in config.providers.values())
        for server in config.mcp.servers.values():
            values.extend(server.headers.values())
            values.extend(server.env.values())
        self.redactor.replace_values(values)

    def reload_config(self, config: RuntimeConfig) -> None:
        self.config = config
        self._configure_redactor(self.settings, config)
        self.providers.reload(config)
        self.mcp.reload(config.mcp.servers)
        for state in self._states.values():
            if state.services is not None:
                state.services.tool_timeout_seconds = config.limits.tool_timeout_seconds
                state.services.max_tool_output_chars = config.tools.exec.max_output_chars

    def _vision(self) -> tuple[Any, str, FileBlobStore, str] | None:
        found = self.config.vision_preset()
        if found is None:
            return None
        _, preset = found
        try:
            provider = self.providers.get(preset.provider)
        except KeyError:
            return None
        return provider, preset.model, self.blobs, TENANT

    def resolve_model(self, overrides: dict[str, Any]) -> tuple[list[tuple[Any, str]], Any]:
        """Rungs and the effective preset for a session, from its live overrides.

        A chosen preset wins; a manual provider/model pair runs with the default preset's
        thinking and window settings; otherwise the global default preset applies.
        """
        if overrides.get("preset") and overrides["preset"] in self.config.presets:
            pid = overrides["preset"]
            return self.providers.rungs_for(self.config, pid), self.config.presets[pid]
        _, default = self.config.preset()
        if overrides.get("provider") and overrides.get("model_name"):
            return self.providers.rungs_for_pair(self.config, overrides["provider"], overrides["model_name"]), default
        return self.providers.rungs_for(self.config), default

    # -- MCP per session --------------------------------------------------------------

    def mcp_enabled(self, state: SessionState) -> list[str]:
        return [s for s in state.metadata.get("mcp_enabled", []) if s in self.mcp.available()]

    async def set_mcp(self, session_id: str, server: str, enabled: bool) -> list[str]:
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        current = self.mcp_enabled(state)
        if enabled:
            await self.mcp.ensure(server)
            if server not in current:
                current.append(server)
        else:
            current = [s for s in current if s != server]
        state.metadata["mcp_enabled"] = current
        state.session.metadata["mcp_enabled"] = current
        await self.sessions.update_metadata(session_id, state.session.metadata)
        if state.engine is not None:
            blocked = blocked_for(self.mcp, current) | self.tools_off(state)
            state.engine.config = replace(
                state.engine.config,
                tool_visibility_policy=ToolVisibilityPolicy(
                    pinned={t.name for t in self.tools.list_all()} - blocked, blocked=blocked
                ),
            )
        return current

    async def mcp_service(
        self, op: str, *, session_id: str, server: str | None = None, redirect_url: str = ""
    ) -> str:
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        if op == "list":
            enabled = set(self.mcp_enabled(state))
            lines = []
            for item in self.mcp.status():
                mark = "on " if item["name"] in enabled else "off"
                tools = ", ".join(item["tools"]) if item["tools"] else ("connected, no tools" if item["connected"] else "not connected yet")
                err = f" (error: {item['error']})" if item["error"] else ""
                lines.append(f"[{mark}] {item['name']} — {item['description'] or 'no description'}: {tools}{err}")
            return "\n".join(lines) or "no MCP servers are configured"
        if server is None:
            raise KeyError("server is required")
        if op == "oauth_status":
            return "\n".join(f"{key}: {value}" for key, value in self.mcp.oauth_status(server).items())
        if op == "oauth_begin":
            url = await self.mcp.oauth_begin(server)
            return (
                "Give this authorization URL to the owner to open in a browser (it creates or links the account on "
                "the server's own page):\n\n"
                f"{url}\n\n"
                "After approving, the browser redirects to a local address that cannot load — copy the FULL address "
                "from the URL bar and paste it back, then run McpOAuthFinish."
            )
        if op == "oauth_finish":
            if not redirect_url:
                raise KeyError("redirect_url is required for oauth_finish")
            result = await self.mcp.oauth_finish(server, redirect_url)
            return f"linked {server}: scopes {result.get('scope')}, access token valid for {result.get('expires_in')}s. Now run McpEnable(server={server!r})."
        if op == "oauth_disconnect":
            removed = await self.mcp.oauth_disconnect(server)
            return f"removed stored OAuth tokens for {server}" if removed else f"{server} is not OAuth-configured"
        current = await self.set_mcp(session_id, server, enabled=(op == "enable"))
        if op == "enable":
            names = sorted(self.mcp.tool_names(server))
            return f"enabled {server}; tools available from your next step: {', '.join(names) or '(none)'}"
        return f"disabled {server}; enabled now: {', '.join(current) or 'none'}"

    async def _load_image(self, ref: str) -> tuple[bytes, str]:
        data = await self.blobs.get(TENANT, ref)
        meta = await self.blobs.head(TENANT, ref)
        return data, meta.content_type

    # -- sessions -------------------------------------------------------------------

    def workspace_for(self, session_id: str) -> Path:
        return self.settings.workspaces_dir / session_id

    def locator_services(self, session_id: str) -> SessionServices | None:
        state = self._states.get(session_id)
        return state.services if state is not None else None

    async def create_session(
        self,
        title: str,
        *,
        session_id: str | None = None,
        workspace: Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionState:
        sid = session_id or uuid.uuid4().hex[:12]
        workspace = workspace or self.workspace_for(sid)
        (workspace / "inbox").mkdir(parents=True, exist_ok=True)
        session = Session(id=sid, tenant_id=TENANT, title=title, metadata=dict(metadata or {}))
        await self.sessions.create(session)
        state = SessionState(session=session, workspace=workspace, metadata=dict(metadata or {}))
        self._states[sid] = state
        self.register_services(state)
        return state

    async def get_state(self, session_id: str) -> SessionState | None:
        state = self._states.get(session_id)
        if state is not None:
            return state
        try:
            session = await self.sessions.get(session_id, TENANT)
        except Exception:
            return None
        workspace = Path(session.metadata.get("workspace") or self.workspace_for(session_id))
        (workspace / "inbox").mkdir(parents=True, exist_ok=True)
        state = SessionState(session=session, workspace=workspace, metadata=dict(session.metadata))
        self._states[session_id] = state
        self.register_services(state)
        return state

    async def transcript(self, session_id: str, *, tail: int = 0) -> list[Message]:
        """Display history: the durable transcript plus whatever the live engine has not persisted yet."""
        rows = await self.sessions.list_transcript(session_id)
        if not rows:
            # Sessions from before the transcript existed: seed it from the working history.
            history = list(await self.sessions.list_messages(session_id, TENANT, limit=10_000))
            if history:
                await self.sessions.append_transcript(session_id, history)
                rows = history
        state = self._states.get(session_id)
        if state is not None and state.engine is not None and state.running:
            known = {self.sessions.transcript_key(m) for m in rows}
            rows = rows + [m for m in state.engine.history if self.sessions.transcript_key(m) not in known]
        return rows[-tail:] if tail > 0 else rows

    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.sessions.list_sessions(TENANT, limit=limit)
        out: list[dict[str, Any]] = []
        for session in rows:
            state = self._states.get(session.id)
            status = "idle"
            if state is not None:
                if state.running:
                    status = "running"
                elif state.pending is not None:
                    status = "waiting"
                elif state.engine is not None and state.engine.state is LoopState.FAILED:
                    status = "failed"
            out.append(
                {
                    "id": session.id,
                    "title": session.title,
                    "status": status,
                    "created_at": session.created_at.isoformat(),
                    "last_message_at": session.last_message_at.isoformat(),
                    "run_id": state.run_id if state else None,
                    "metadata": session.metadata,
                }
            )
        return out

    async def delete_session(self, session_id: str, *, delete_workspace: bool = True) -> bool:
        """Remove a session entirely: its run, records, events and (optionally) its workspace."""
        state = await self.get_state(session_id)
        if state is None:
            return False
        if state.running and state.engine is not None:
            state.engine.stop()
            if state.task is not None:
                state.task.cancel()
                await asyncio.gather(state.task, return_exceptions=True)
        self._states.pop(session_id, None)
        locator.unregister(session_id)
        runs = await self.db.fetchall("SELECT id FROM runs WHERE session_id = ?", (session_id,))
        async with self.db.transaction() as conn:
            for row in runs:
                await conn.execute("DELETE FROM events WHERE run_id = ?", (row["id"],))
                await conn.execute("DELETE FROM snapshots WHERE run_id = ?", (row["id"],))
            await conn.execute("DELETE FROM runs WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM transcript WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM live_control WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM pending_questions WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM topics WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM verifications WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM learning_records WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        # A subagent shares its leader's workspace: only a session's own directory is ever removed.
        if delete_workspace and state.workspace == self.workspace_for(session_id) and state.workspace.exists() and state.workspace.is_relative_to(self.settings.workspaces_dir):
            shutil.rmtree(state.workspace, ignore_errors=True)
        return True

    async def rename_session(self, session_id: str, title: str) -> SessionState:
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        title = title.strip()[:128]
        if not title:
            raise ValueError("empty title")
        await self.sessions.update_title(session_id, title)
        await self.db.execute("UPDATE topics SET title = ? WHERE session_id = ?", (title, session_id))
        state.session = state.session.model_copy(update={"title": title})
        return state

    async def compact(self, session_id: str, instructions: str = "", *, keep_recent: int = 0) -> str:
        """Replace the history (all of it, or all but the last ``keep_recent`` messages) with one summary.

        Holds the session lock for the whole operation (including the summarising call) so
        no run can start against the history while it is being rewritten. The replaced
        transcript is kept in the workspace as ``.history-<timestamp>.jsonl``.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        async with state.lock:
            return await self._compact_locked(state, instructions, keep_recent=keep_recent)

    async def _maybe_auto_compact(self, state: SessionState) -> None:
        """After a run: when the last prompt filled ``compaction.auto_ratio`` of the window, compact before the next one."""
        cfg = self.config.compaction
        if cfg.auto_ratio <= 0 or state.pending is not None:
            return
        status = await self.context_status(state)
        if not status["window"] or status["tokens"] < cfg.auto_ratio * status["window"] or status["messages"] < cfg.min_messages:
            return
        async with state.lock:
            try:
                started = time.monotonic()
                await self._compact_locked(state, "", keep_recent=cfg.keep_recent_messages, reason="auto", own_task_ok=True)
                after = await self.context_status(state)
                logger.warning("session %s: auto-compacted %d → %d messages in %.0fs (prompt was %d of %d tokens)", state.session.id, status["messages"], after["messages"], time.monotonic() - started, status["tokens"], status["window"])
                for hook in self.compaction_hooks:
                    try:
                        await hook(state.session.id, {"before_messages": status["messages"], "after_messages": after["messages"], "before_tokens": status["tokens"], "window": status["window"], "seconds": round(time.monotonic() - started)})
                    except Exception:  # noqa: BLE001
                        logger.exception("compaction hook failed")
            except Exception:  # noqa: BLE001 — the next run must start even when the summary could not be made
                logger.exception("auto-compaction failed for session %s", state.session.id)

    async def _compact_locked(self, state: SessionState, instructions: str, *, keep_recent: int = 0, reason: str = "manual", own_task_ok: bool = False) -> str:
        session_id = state.session.id
        if state.running and state.engine is not None and state.engine.is_terminal and state.task is not None and not own_task_ok:
            # The loop has settled; only bookkeeping remains.
            await asyncio.gather(asyncio.shield(state.task), return_exceptions=True)
        busy = state.running and not (own_task_ok and state.task is asyncio.current_task())
        if busy or state.pending is not None:
            raise RuntimeError("the session is busy; stop the run (or answer the question) first")
        pending = [t for t in state.persist_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)  # no straggler may write the old history later
        exceeded = self.budget_exceeded()
        if exceeded:
            raise RuntimeError(f"daily budget exceeded ({exceeded}); compaction is a paid call")
        full = list(state.engine.history) if state.engine is not None else list(
            await self.sessions.list_messages(session_id, TENANT, limit=10_000)
        )
        if not full:
            raise RuntimeError("nothing to compact")
        cut = compaction_cut(full, keep_recent)
        history, tail = full[:cut], full[cut:]
        if not history:
            raise RuntimeError("nothing to compact: the whole history is inside the kept tail")
        rungs, _ = self.resolve_model(await self.live.load(session_id))
        provider, model = rungs[0]  # the session's own model summarises its own history
        language = self.config.answer_language if self.config.answer_language != "auto" else operator_language(history)
        observability = LLMObservabilityContext(tenant_id=TENANT, session_id=session_id, run_id=state.run_id, call_purpose="compaction", call_category="compaction")
        summary = await self._summarise_history(provider, model, history, language=language, instructions=instructions, observability=observability)
        # What the operator said is written by code, never by the summariser: rules do not decay.
        summary = self.redactor.redact(summary + operator_quotes(history) + identifier_index(history) + verbatim_tail(history))
        busy = state.running and not (own_task_ok and state.task is asyncio.current_task())
        if busy or state.pending is not None:  # a run resumed from a snapshot meanwhile
            raise RuntimeError("the session became busy during compaction; nothing was changed")
        backup = state.workspace / f".history-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        try:
            backup.write_text("\n".join(m.model_dump_json() for m in history) + "\n", encoding="utf-8")
        except OSError:
            logger.warning("could not write the history backup %s", backup, exc_info=True)
        await self.sessions.append_transcript(session_id, history, from_history=True)
        seqs = await self.sessions.transcript_seqs(session_id, [self.sessions.transcript_key(m) for m in history])
        archive_note = f"\n\n[archived turns seq {seqs[0]}–{seqs[-1]}: HistoryExpand({seqs[0]}, {seqs[-1]}) returns them verbatim]" if seqs else ""
        message = Message(
            role=MessageRole.user,
            content_blocks=[TextBlock(text=f"<compacted-turn id='{reason}'>{summary}{archive_note}</compacted-turn>")],
            metadata={
                COMPACTION_SUMMARY_METADATA_KEY: True,
                "daedalus.compaction": {"reason": reason, "messages": len(history), "kept": len(tail), "at": datetime.now(UTC).isoformat()},
                "daedalus.archived": {"from_seq": seqs[0], "to_seq": seqs[-1], "seqs": seqs} if seqs else {"seqs": []},
            },
        )
        rebuilt = [message, *tail]
        state.persist_gen += 1  # any persist captured before this point describes a history that is gone
        if state.engine is not None:
            engine = state.engine
            engine.history = rebuilt
            engine.compact_checkpoint = None  # type: ignore[attr-defined]
            # The core gates automatic compaction on the last measured prompt size; that
            # measurement described the history that no longer exists.
            engine.last_observed_prompt_tokens = 0
            engine.compaction_state = CompactionState()
        state.history_keys = [self.sessions.transcript_key(m) for m in rebuilt]
        await self.sessions.replace_messages(session_id, TENANT, rebuilt)
        await self.sessions.append_transcript(session_id, [message])
        return summary

    async def _summarise_history(self, provider: Any, model: str, history: Sequence[Message], *, language: str, instructions: str, observability: LLMObservabilityContext) -> str:
        """One structured summary of ``history``: a single call, or parallel part summaries merged when the transcript is long."""
        cfg = self.config.compaction
        focus = f"\n\nThe operator asks to focus on: {instructions.strip()}" if instructions.strip() else ""
        transcript_text = transcript_for_summary(history)
        parts = split_transcript(transcript_text, cfg.chunk_tokens)
        if len(parts) == 1:
            return await self._summary_call(provider, model, COMPACT_PROMPT.format(language=language, max_words=cfg.max_words) + focus, transcript_text, observability)
        part_words = max(300, cfg.max_words // 2)
        partials = await asyncio.gather(*(
            self._summary_call(provider, model, CHUNK_PROMPT.format(index=i + 1, total=len(parts), language=language, max_words=part_words) + focus, part, observability, strict=False)
            for i, part in enumerate(parts)
        ))
        joined = "\n\n".join(f"<part {i + 1}>\n{text}\n</part {i + 1}>" for i, text in enumerate(partials) if text)
        return await self._summary_call(provider, model, MERGE_PROMPT.format(language=language, max_words=cfg.max_words) + focus, joined, observability)

    async def _summary_call(self, provider: Any, model: str, prompt: str, body: str, observability: LLMObservabilityContext, *, strict: bool = True) -> str:
        summary = ""
        candidate = ""
        problem = ""
        timeout = self.config.compaction.call_timeout_seconds
        for attempt in range(2 if strict else 1):
            reminder = f"\n\nYour previous attempt was rejected: {problem}. Produce every section, each exactly once, in the given order." if problem else ""
            request = LLMRequest(
                model=model,
                messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=prompt + reminder + "\n\n" + body)])],
                max_tokens=6000,
                temperature=0.2,
                extra={"enable_thinking": False},
                observability=observability,
            )
            try:
                response = await asyncio.wait_for(provider.complete_text(request), timeout=timeout)
            except TimeoutError:
                # A provider stall must not hold the session: one more try, then the compaction waits for the next run.
                logger.warning("compaction summariser call exceeded %.0fs; retrying once", timeout)
                response = await asyncio.wait_for(provider.complete_text(request), timeout=timeout)
            candidate = "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock)).strip()
            problem = validate_summary_sections(candidate)
            if not problem:
                summary = candidate
                break
            logger.warning("compaction summary rejected (attempt %d): %s", attempt + 1, problem)
        if not summary:
            if not candidate:
                raise RuntimeError("the model returned an empty summary")
            logger.warning("compaction summary accepted without the fixed sections: %s", problem)
            summary = candidate  # a usable summary beats a session the operator cannot compact
        return summary

    # -- checkpoints, revert, fork ------------------------------------------------------

    async def checkpoint(self, state: SessionState, *, kind: str, seq: int | None = None, run_id: str | None = None) -> str | None:
        """Snapshot the workspace (bounded by ``ops.checkpoint_max_gb``); returns the commit id or None."""
        limit = self.config.ops.checkpoint_max_gb
        try:
            if limit and await asyncio.to_thread(workspace_size, state.workspace) > limit * 1e9:
                if not state.checkpoint_capped:
                    state.checkpoint_capped = True
                    logger.warning("session %s: workspace exceeds ops.checkpoint_max_gb=%s; no snapshots, revert restores the history only", state.session.id, limit)
                return None
            state.checkpoint_capped = False
            sha = await Checkpoints(state.workspace).snapshot(f"{kind} seq={seq} run={run_id}")
        except (CheckpointError, OSError) as exc:
            logger.warning("checkpoint failed for %s: %s", state.session.id, exc)
            return None
        await self.db.execute(
            "INSERT INTO checkpoints(session_id, seq, run_id, kind, sha, at) VALUES (?, ?, ?, ?, ?, ?)",
            (state.session.id, seq, run_id, kind, sha, datetime.now(UTC).isoformat()),
        )
        return sha

    async def checkpoint_before(self, session_id: str, seq: int) -> str | None:
        """The snapshot taken right before the operator turn at transcript ``seq``."""
        row = await self.db.fetchone("SELECT sha FROM checkpoints WHERE session_id = ? AND kind = 'before' AND seq = ? ORDER BY id DESC LIMIT 1", (session_id, seq))
        return row["sha"] if row else None

    async def revert(self, session_id: str, seq: int) -> dict[str, Any]:
        """Undo everything from the operator turn at transcript ``seq`` on: history and workspace.

        The turn must still be in the working history (not compacted away); the transcript
        keeps the undone turns and a marker says where the history now ends.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        async with state.lock:
            if state.running or state.pending is not None:
                raise RuntimeError("the session is busy; stop the run (or answer the question) first")
            pending = [t for t in state.persist_tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            row = await self.sessions.transcript_row(session_id, seq)
            if row is None:
                raise ValueError(f"no transcript turn {seq}")
            key, target = row
            if target.role is not MessageRole.user or target.metadata.get("daedalus.origin") in ("core", "revert") or target.metadata.get(COMPACTION_SUMMARY_METADATA_KEY):
                raise ValueError("revert points at a turn that started a run (an operator, schedule, inbound or peer message)")
            history = list(state.engine.history) if state.engine is not None else list(await self.sessions.list_messages(session_id, TENANT, limit=10_000))
            keys = [self.sessions.transcript_key(m) for m in history]
            if key not in keys:
                raise ValueError("that turn is no longer in the working history (compacted); fork from it instead")
            cut = keys.index(key)
            kept = history[:cut]
            dropped = len(history) - cut
            sha = await self.checkpoint_before(session_id, seq)
            restored = False
            untouched: list[str] = []
            if sha:
                try:
                    untouched = await Checkpoints(state.workspace).restore(sha)
                    restored = True
                except CheckpointError as exc:
                    logger.warning("workspace restore failed: %s", exc)
            state.persist_gen += 1
            if state.engine is not None:
                state.engine.history = kept
                state.engine.last_observed_prompt_tokens = 0
                state.engine.compaction_state = CompactionState()
            state.history_keys = [self.sessions.transcript_key(m) for m in kept]
            await self.sessions.replace_messages(session_id, TENANT, kept)
            # Input queued during the undone turns and any run snapshot that could resume them are pre-revert by definition.
            await self.live.save_queues(session_id, [], [])
            for run in await self.db.fetchall("SELECT id FROM runs WHERE session_id = ?", (session_id,)):
                await self.events.delete_snapshot(run["id"])
            workspace_note = ""
            if restored:
                workspace_note = "; workspace restored" + (f" ({len(untouched)} nested repositories untouched: {', '.join(untouched)[:200]})" if untouched else "")
            marker = Message(
                role=MessageRole.user,
                content_blocks=[TextBlock(text=f"[reverted to before seq {seq}: {dropped} message(s) left the working history{workspace_note}]")],
                metadata={"daedalus.origin": "revert", "daedalus.revert": {"seq": seq, "dropped": dropped, "workspace_restored": restored, "untouched": untouched}},
            )
            await self.sessions.append_transcript(session_id, [marker])
            return {"dropped": dropped, "workspace_restored": restored, "untouched": untouched, "kept": len(kept)}

    async def fork_into(self, source_id: str, seq: int, target: SessionState) -> dict[str, Any]:
        """Give ``target`` the source's history before transcript ``seq`` and a copy of its workspace as of then.

        Turns a revert undid and turns a compaction summary already stands for are left out of
        the working history; the archived originals still go into the fork's transcript, and
        the summary is re-pointed at the fork's own seqs so HistoryExpand keeps working there.
        """
        source = await self.get_state(source_id)
        if source is None:
            raise KeyError(source_id)
        async with source.lock:
            if source.running:
                raise RuntimeError("the source session is running; wait for the run to finish (or stop it) first")
            pending = [t for t in source.persist_tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            rows = await self.sessions.list_transcript(source_id)
            reverted: set[int] = set()
            archived: set[int] = set()
            for m in rows:
                own = int(m.metadata.get("daedalus.seq", 0))
                if own >= seq:
                    continue
                if m.metadata.get("daedalus.origin") == "revert":
                    cut = int((m.metadata.get("daedalus.revert") or {}).get("seq") or 0)
                    reverted.update(range(cut, own))
                if m.metadata.get("daedalus.archived"):
                    archived.update(int(x) for x in (m.metadata["daedalus.archived"].get("seqs") or []))
            before = [m for m in rows if int(m.metadata.get("daedalus.seq", 0)) < seq and int(m.metadata.get("daedalus.seq", 0)) not in reverted and m.metadata.get("daedalus.origin") != "revert"]
            transcript = [m.model_copy(update={"metadata": {k: v for k, v in m.metadata.items() if k != "daedalus.seq"}}) for m in before]
            await self.sessions.append_transcript(target.session.id, transcript)
            keys = [self.sessions.transcript_key(m) for m in transcript]
            new_seqs = await self.sessions.transcript_seqs(target.session.id, keys)
            mapping = {int(m.metadata.get("daedalus.seq", 0)): new for m, new in zip(before, new_seqs, strict=False)}
            history: list[Message] = []
            for original, message in zip(before, transcript, strict=True):
                own = int(original.metadata.get("daedalus.seq", 0))
                if own in archived:
                    continue
                if message.metadata.get("daedalus.archived"):
                    message = repoint_summary(message, mapping)
                    await self.sessions.replace_transcript_message(target.session.id, self.sessions.transcript_key(original), message)
                history.append(message)
            # A fork taken mid-turn must not start on a tool call nobody answered.
            while history and history[-1].role is MessageRole.assistant and any(isinstance(b, ToolUseBlock) for b in history[-1].content_blocks):
                history.pop()
            await self.sessions.replace_messages(target.session.id, TENANT, history)
            target.history_keys = [self.sessions.transcript_key(m) for m in history]
            copied = False
            if source.workspace.exists():
                await asyncio.to_thread(shutil.copytree, source.workspace, target.workspace, dirs_exist_ok=True)
                copied = True
                checkpoints = Checkpoints(target.workspace)
                await checkpoints.relocate()
                sha = await self.checkpoint_before(source_id, seq)
                if sha:
                    try:
                        await checkpoints.restore(sha)
                    except CheckpointError as exc:
                        logger.warning("fork workspace restore failed: %s", exc)
        await self.sessions.update_metadata(target.session.id, {**target.session.metadata, "forked_from": {"session_id": source_id, "seq": seq}})
        return {"messages": len(history), "workspace_copied": copied}

    async def closed_topic_sessions(self) -> list[dict[str, Any]]:
        """Sessions whose topic is closed but whose data is still on disk."""
        rows = await self.db.fetchall(
            "SELECT t.session_id, t.title, t.chat_id, t.thread_id FROM topics t WHERE t.closed_at IS NOT NULL"
        )
        out = []
        for row in rows:
            state = await self.get_state(row["session_id"])
            size = 0
            if state is not None and state.workspace.exists():
                size = sum(f.stat().st_size for f in state.workspace.rglob("*") if f.is_file())
            out.append({"session_id": row["session_id"], "title": row["title"], "chat_id": row["chat_id"], "thread_id": row["thread_id"], "bytes": size})
        return out

    async def orphan_workspaces(self) -> list[Path]:
        """Workspace directories that no session, schedule or standing task refers to.

        The one definition every caller shares: a session's own folder, any folder named in a
        session's metadata (scheduled and heartbeat runs), and every schedule workspace are kept.
        """
        rows = await self.db.fetchall("SELECT id, metadata FROM sessions")
        known = {r["id"] for r in rows}
        known_paths: set[Path] = set()
        for r in rows:
            try:
                ws = json.loads(r["metadata"] or "{}").get("workspace")
            except (TypeError, ValueError):
                ws = None
            if ws:
                known_paths.add(Path(ws).resolve())
        sched = await self.db.fetchall("SELECT workspace FROM schedules")
        known_paths.update(Path(r["workspace"]).resolve() for r in sched)
        known_paths.add((self.settings.workspaces_dir / "heartbeat").resolve())
        out: list[Path] = []
        if not self.settings.workspaces_dir.exists():
            return out
        for entry in sorted(self.settings.workspaces_dir.iterdir()):
            if not entry.is_dir() or entry.name in known or entry.resolve() in known_paths:
                continue
            out.append(entry)
        return out

    async def sweep_orphan_workspaces(self) -> list[str]:
        """Delete the directories :meth:`orphan_workspaces` reports."""
        removed: list[str] = []
        for entry in await self.orphan_workspaces():
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        return removed

    def running_run_ids(self) -> set[str]:
        return {s.run_id for s in self._states.values() if s.run_id and (s.running or s.pending is not None)}

    def register_services(self, state: SessionState) -> None:
        hooks = self.service_hooks
        services = SessionServices(
            session_id=state.session.id,
            workspace_dir=state.workspace,
            protected_paths=(
                self.governance_path,
                Path("/opt/launcher"),
                self.settings.secrets_dir,
            ),
            tool_timeout_seconds=self.config.limits.tool_timeout_seconds,
            max_tool_output_chars=self.config.tools.exec.max_output_chars,
            send_file=_bind(hooks.get("send_file"), state.session.id),
            spawn_session=_bind(hooks.get("spawn_session"), state.session.id),
            spawn_agent=_bind(hooks.get("spawn_agent"), state.session.id),
            schedule=hooks.get("schedule"),
            self_propose=hooks.get("self_propose"),
            self_rebuild=hooks.get("self_rebuild"),
            self_rollback=hooks.get("self_rollback"),
            progress=_bind(hooks.get("progress"), state.session.id),
            extra={"skill_store": self.skills, "manager": self, "vision": _LiveVision(self)},
        )
        state.services = services
        locator.register(services)

    # -- input --------------------------------------------------------------------

    async def submit(
        self,
        session_id: str,
        text: str,
        attachments: Sequence[Attachment] = (),
        *,
        steer: bool = False,
        as_answer: bool = True,
        origin: str = "operator",
    ) -> str:
        """Deliver input. Starts a run, or queues a follow-up when one is active.

        ``origin`` names who wrote the text (``operator``, or a system source such as
        ``reminder``); the transcript and the Mini App show it accordingly.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        body, image_refs = await self._ingest_attachments(state, text, attachments)
        if state.pending is not None:
            if as_answer:
                # Free-text reply to a pending question counts as a custom answer.
                return await self.answer(session_id, [{"custom": body}])
            await self.live.enqueue(session_id, "follow_up", new_queued_prompt("follow_up", body).to_dict())
            await self.sessions.append_transcript(
                session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], metadata={"daedalus.delivery": "follow_up", "daedalus.origin": origin})]
            )
            return state.run_id or ""
        exceeded = self.budget_exceeded()
        if exceeded and not state.running:
            raise RuntimeError(f"daily budget exceeded ({exceeded}); runs resume tomorrow or after /budget reset")
        if not state.running:
            provider_id: str | None = None
            try:
                rungs, _ = self.resolve_model(await self.live.load(session_id))
                provider_id = rungs[0][0].endpoint.id if rungs else None
            except Exception:  # noqa: BLE001 — a model problem surfaces when the run starts, not here
                provider_id = None
            breach = await self.cap_breach(state, provider_id)
            if breach is not None:
                raise RuntimeError(breach[1])
        if state.running and state.engine is not None and state.engine.is_terminal and state.task is not None:
            # The loop has settled and the task is only doing bookkeeping: let it finish and start a new turn.
            try:
                await asyncio.shield(state.task)
            except Exception:  # noqa: BLE001
                pass
        if state.running:
            # A message sent while the agent works is a steer: the core places it before the
            # next model call (after the current tool batch). follow_up would wait for the end.
            kind = "follow_up" if not steer and state.metadata.get("queue_mode") == "follow_up" else "steer"
            await self.live.enqueue(session_id, kind, {**new_queued_prompt(kind, body).to_dict(), "origin": origin})  # type: ignore[arg-type]
            # The core folds queued prompts into the model's history later (and compaction may
            # rewrite them); the transcript keeps the operator's words as sent.
            await self.sessions.append_transcript(
                session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], metadata={"daedalus.delivery": kind, "daedalus.origin": origin})]
            )
            return state.run_id or ""
        # A new run starts: hooks may decorate the message (a fired reminder rides along); their
        # side effects are committed only once the run exists, so a refused start loses nothing.
        for hook in self.prompt_hooks:
            try:
                body = await hook(session_id, body)
            except Exception:  # noqa: BLE001
                logger.exception("prompt hook failed")
        message = Message(
            role=MessageRole.user,
            content_blocks=[TextBlock(text=body)],
            metadata={"daedalus.origin": origin, **({"image_refs": [{"ref": ref, "mime": mime} for ref, mime in image_refs]} if image_refs else {})},
        )
        await self.sessions.append_transcript(session_id, [message])
        seqs = await self.sessions.transcript_seqs(session_id, [self.sessions.transcript_key(message)])
        await self.checkpoint(state, kind="before", seq=seqs[0] if seqs else None)
        run_id = await self._start_run(state, message)
        for started in self.run_started_hooks:
            try:
                await started(session_id, run_id)
            except Exception:  # noqa: BLE001
                logger.exception("run-started hook failed")
        return run_id

    async def _ingest_attachments(
        self, state: SessionState, text: str, attachments: Sequence[Attachment]
    ) -> tuple[str, list[tuple[str, str]]]:
        if not attachments:
            return text, []
        inbox = state.workspace / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        image_refs: list[tuple[str, str]] = []
        for attachment in attachments:
            target = inbox / attachment.path.name
            if attachment.path.resolve() != target.resolve():
                counter = 1
                while target.exists():
                    target = inbox / f"{attachment.path.stem}-{counter}{attachment.path.suffix}"
                    counter += 1
                shutil.copy2(attachment.path, target)
            lines.append(f"- {target} ({attachment.mime_type}, {target.stat().st_size} bytes)")
            if attachment.mime_type.startswith("image/"):
                meta = await self.blobs.put(TENANT, target.read_bytes(), content_type=attachment.mime_type)
                image_refs.append((meta.ref, attachment.mime_type))
        body = (text.strip() + "\n\n" if text.strip() else "") + "Attached files:\n" + "\n".join(lines)
        return body, image_refs

    async def answer(self, session_id: str, answers: list[dict[str, Any]]) -> str:
        """Resume a run paused on AskUser with the operator's answers."""
        state = await self.get_state(session_id)
        if state is None or state.pending is None or state.engine is None:
            raise RuntimeError("no pending question for this session")
        if state.running:
            raise RuntimeError("the run is still finishing; try again in a moment")
        pending = state.pending
        questions = [q.get("question", "") for q in pending.payload.get("questions", [])]
        shaped = []
        for index, answer in enumerate(answers):
            shaped.append(
                {
                    "question": answer.get("question") or (questions[index] if index < len(questions) else ""),
                    "selected": list(answer.get("selected") or []),
                    "custom": answer.get("custom"),
                }
            )
        result = json.dumps({"answers": shaped, "source": "user"}, ensure_ascii=False)
        state.engine.history.append(
            Message(
                role=MessageRole.tool,
                content_blocks=[ToolResultBlock(tool_call_id=pending.tool_call_id, content=result)],
            )
        )
        state.engine.clear_pending_approval(pending.tool_call_id)
        state.engine.transition_to(LoopState.RUNNING)
        state.pending = None
        await self.db.execute("DELETE FROM pending_questions WHERE session_id = ?", (session_id,))
        return await self._start_run(state, None, continue_turn=True)

    async def set_mode(self, session_id: str, mode: str | None) -> str:
        """Switch a session to a configured mode (limits, prompt rules, verbosity); empty = default."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        name = (mode or "").strip()
        if name and name not in self.config.modes:
            raise ValueError(f"no such mode {name!r}; configured: {', '.join(self.config.modes) or 'none'}")
        if name:
            state.metadata["mode"] = name
            state.session.metadata["mode"] = name
        else:
            state.metadata.pop("mode", None)
            state.session.metadata.pop("mode", None)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        return name

    def mode_for(self, state: SessionState) -> Any:
        name = str(state.metadata.get("mode") or "")
        return self.config.modes.get(name) if name else None

    async def stop(self, session_id: str) -> bool:
        state = self._states.get(session_id)
        if state is None or not state.running or state.engine is None:
            return False
        state.engine.stop()
        return True

    async def set_model(
        self,
        session_id: str,
        *,
        model_name: str | None = None,
        provider: str | None = None,
        preset: str | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        clear: bool = False,
    ) -> None:
        if clear:
            await self.live.clear_overrides(session_id)
            return
        if preset is not None and preset not in self.config.presets:
            raise ValueError(f"no such model preset {preset!r}")
        if provider is not None and provider not in self.providers.available():
            raise ValueError(f"unknown or unusable provider {provider!r}")
        await self.live.set_model(
            session_id, model_name=model_name, provider=provider, preset=preset, thinking_enabled=thinking, reasoning_effort=reasoning_effort
        )
        state = self._states.get(session_id)
        if state is not None and state.engine is not None and state.running:
            state.engine.apply_live_controls(
                model_name=model_name, thinking_enabled=thinking, reasoning_effort=reasoning_effort
            )

    # -- runs -----------------------------------------------------------------------

    async def _build_engine(self, state: SessionState, run_id: str) -> QueryEngine:
        overrides = await self.live.load(state.session.id)
        rungs, preset = self.resolve_model(overrides)
        deps = EngineDeps(
            tool_registry=self.tools,
            event_stream=self.events,
            blob_store=self.blobs,
            skill_store=self.skills,
            hook_manager=self.hooks,
            bot_repo=self.settings.bot_repo_dir,
            core_repo=self.settings.core_repo_dir,
            governance_path=self.governance_path,
        )
        mode_name = str(state.metadata.get("mode") or "")
        mode = self.config.modes.get(mode_name) if mode_name else None
        enabled = self.mcp_enabled(state)
        if enabled:
            # Re-establish connections for servers this session left enabled (e.g. after a
            # host restart or a dropped transport) so their tools are visible again instead
            # of silently missing until the agent re-runs McpEnable. Bounded: a dead or
            # slow server must never block the run.
            for server in enabled:
                try:
                    await asyncio.wait_for(self.mcp.ensure(server), timeout=5)
                except TimeoutError:
                    logger.warning("MCP warm-up for %s timed out; tools may be unavailable this run", server)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.warning("MCP warm-up for %s failed; tools may be unavailable this run", server, exc_info=True)
        engine = build_engine(
            deps=deps,
            config=self.config,
            mode=mode,
            run_id=run_id,
            session_id=state.session.id,
            session_title=state.session.title,
            workspace=state.workspace,
            rungs=rungs,
            provider_chain=build_chain(rungs),
            model_name=rungs[0][1],
            thinking=preset.thinking if overrides.get("thinking_enabled") is None else bool(overrides["thinking_enabled"]),
            reasoning_effort=overrides.get("reasoning_effort") or preset.reasoning_effort,
            context_window=state.context_window or preset.context_window,
            max_output_tokens=preset.max_output_tokens,
            extra_notes=self.notes_for(state),
            blocked_tools=blocked_for(self.mcp, enabled) | self.tools_off(state),
        )
        self._attach_hooks(engine, state)
        return engine

    def _attach_hooks(self, engine: QueryEngine, state: SessionState) -> None:
        session_id = state.session.id

        async def reload_live_control(eng: QueryEngine) -> None:
            data = await self.live.load(session_id)
            eng._steer_queue = list(data["steer"])  # type: ignore[attr-defined]
            eng._follow_up_queue = list(data["follow_up"])  # type: ignore[attr-defined]

        async def persist_live_control(eng: QueryEngine) -> None:
            await self.live.save_queues(
                session_id,
                list(getattr(eng, "_steer_queue", []) or []),
                list(getattr(eng, "_follow_up_queue", []) or []),
            )

        def persist_session_history(eng: QueryEngine) -> None:
            history = list(eng.history)
            previous = state.history_keys
            state.history_keys = [self.sessions.transcript_key(m) for m in history]
            task = asyncio.get_running_loop().create_task(self._persist_history(state, history, previous, state.persist_gen))
            state.persist_tasks.add(task)
            task.add_done_callback(_log_task_failure)
            task.add_done_callback(state.persist_tasks.discard)

        engine.reload_live_control = reload_live_control  # type: ignore[attr-defined]
        engine.persist_live_control = persist_live_control  # type: ignore[attr-defined]
        engine.persist_session_history = persist_session_history  # type: ignore[attr-defined]

    async def _persist_history(self, state: SessionState, history: list[Message], previous_keys: list[str], gen: int | None = None) -> None:
        """Persist the working history and the transcript, then label fresh summaries with what they replaced.

        A compaction summary the core just produced is tagged with the transcript seqs of the
        turns it stands for, in its metadata (for the Mini App) and in its text (so the model
        knows what HistoryExpand would return). Persists are serialised per session and a
        snapshot taken before a history rewrite is dropped, so an older picture never lands last.
        """
        session_id = state.session.id
        async with state.persist_lock:
            if gen is not None and gen != state.persist_gen:
                return
            current = {self.sessions.transcript_key(m) for m in history}
            removed = [k for k in previous_keys if k not in current]
            fresh = [i for i, m in enumerate(history) if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY) and "daedalus.archived" not in m.metadata]
            if fresh:
                seqs = await self.sessions.transcript_seqs(session_id, removed) if removed else []
                if removed and len(seqs) < len(removed):
                    logger.warning("session %s: %d of %d archived turns were never in the transcript", session_id, len(removed) - len(seqs), len(removed))
                for index in fresh:
                    original = history[index]
                    if seqs:
                        contiguous = seqs[-1] - seqs[0] + 1 == len(seqs)
                        span = f"seq {seqs[0]}–{seqs[-1]}" if contiguous else f"within seq {seqs[0]}–{seqs[-1]} ({len(seqs)} turns)"
                        note = f"[archived turns {span}: HistoryExpand({seqs[0]}, {seqs[-1]}) returns them verbatim]"
                        annotated = annotate_summary(original, note, seqs)
                    else:
                        annotated = original.model_copy(update={"metadata": {**original.metadata, "daedalus.archived": {"seqs": []}}})
                    history[index] = annotated
                    # Locate the live message by identity, never by position: the core may have
                    # reshaped its history since this snapshot was taken.
                    key = self.sessions.transcript_key(original)
                    if state.engine is not None:
                        live = state.engine.history
                        for li, lm in enumerate(live):
                            if lm.metadata.get(COMPACTION_SUMMARY_METADATA_KEY) and "daedalus.archived" not in lm.metadata and self.sessions.transcript_key(lm) == key:
                                live[li] = annotated
                                break
            await self.sessions.append_transcript(session_id, history, from_history=True)
            await self.sessions.replace_messages(session_id, TENANT, history)

    async def _start_run(
        self, state: SessionState, message: Message | None, *, continue_turn: bool = False
    ) -> str:
        async with state.lock:
            return await self._start_run_locked(state, message, continue_turn=continue_turn)

    async def _start_run_locked(
        self, state: SessionState, message: Message | None, *, continue_turn: bool = False
    ) -> str:
        if message is not None:
            state.run_origin = str(message.metadata.get("daedalus.origin") or "operator")
        if state.engine is None or not continue_turn:
            run_id = uuid.uuid4().hex[:12]
            engine = await self._build_engine(state, run_id)
            if state.engine is not None:
                engine.history = list(state.engine.history)
            else:
                engine.history = list(await self.sessions.list_messages(state.session.id, TENANT, limit=10_000))
            state.engine = engine
            state.run_history_start = len(engine.history)
        else:
            run_id = state.run_id or uuid.uuid4().hex[:12]
            engine = state.engine
        state.run_id = run_id
        if not continue_turn:
            await self.runs.create(Run(id=run_id, tenant_id=TENANT, session_id=state.session.id, status=RunStatus.running))
        else:
            await self.runs.update_status(run_id, TENANT, RunStatus.running)
        state.task = asyncio.create_task(self._drive(state, engine, message, continue_turn), name=f"run:{run_id}")
        state.task.add_done_callback(_log_task_failure)
        return run_id

    async def _drive(
        self, state: SessionState, engine: QueryEngine, message: Message | None, continue_turn: bool
    ) -> None:
        session_id = state.session.id
        run_id = engine.config.run_id
        status = "completed"
        try:
            # A continuation drives the model against the history as it stands; nothing is appended.
            iterator = engine.run(None) if continue_turn else engine.run(message)
            async for event in iterator:
                await self._dispatch_event(state, event)
            if engine.state is LoopState.AWAITING and state.pending is not None:
                status = "awaiting"
            elif engine.state is LoopState.FAILED:
                status = "failed"
            elif engine.state is LoopState.CANCELLED:
                status = "cancelled"
        except asyncio.CancelledError:
            status = "interrupted" if self.shutting_down else "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced to the operator, never swallowed
            logger.exception("run %s crashed", run_id)
            status = "failed"
            await self._dispatch_event(
                state,
                TurnEvent(type=EventType.ERROR, run_id=run_id, payload={"message": f"{type(exc).__name__}: {exc}"}),
            )
        finally:
            await self._persist_history(state, list(engine.history), state.history_keys)
            state.history_keys = [self.sessions.transcript_key(m) for m in engine.history]
            try:
                if status == "interrupted":
                    pass  # snapshot stays; resume_unfinished() continues the run after restart
                elif status == "awaiting":
                    await self.runs.update_status(run_id, TENANT, RunStatus.paused)
                else:
                    run_status = {
                        "completed": RunStatus.completed,
                        "failed": RunStatus.error,
                        "cancelled": RunStatus.cancelled,
                    }.get(status, RunStatus.completed)
                    await self.runs.update_status(run_id, TENANT, run_status)
                    await self.events.delete_snapshot(run_id)
                    self.events.close_run(run_id)
                    await self.events.trim(run_id, TENANT, max_len=self.config.ops.events_keep_per_run)
            except Exception:  # noqa: BLE001
                logger.exception("run %s bookkeeping failed", run_id)
            if status in ("completed", "failed", "cancelled"):
                await self.checkpoint(state, kind="after", run_id=run_id)
            for callback in self._finished:
                try:
                    await callback(session_id, run_id, status)
                except Exception:  # noqa: BLE001
                    logger.exception("run-finished callback failed")
            if status in ("completed", "failed", "cancelled"):
                await self._maybe_auto_compact(state)
            if status in ("completed", "failed"):
                # A run that ended in an error still owes an answer to what arrived meanwhile.
                await self._drain_leftover_follow_ups(state)

    async def _drain_leftover_follow_ups(self, state: SessionState) -> None:
        """Input that arrived while the run was settling starts the next turn instead of rotting in the queue."""
        queued = await self.live.load(state.session.id)
        items = [item for item in queued["follow_up"] + queued["steer"] if str(item.get("text") or "").strip()]
        if not items:
            return
        await self.live.save_queues(state.session.id, [], [])
        texts = [str(item["text"]).strip() for item in items]
        origins = {str(item.get("origin") or "operator") for item in items}
        # The transcript already holds each item as it was sent; this copy only opens the run and stays hidden.
        origin = "operator" if "operator" in origins else next(iter(origins))
        message = Message(role=MessageRole.user, content_blocks=[TextBlock(text="\n\n".join(texts))], metadata={"daedalus.origin": origin, "daedalus.delivery": "drained"})
        await self.sessions.append_transcript(state.session.id, [message])
        seqs = await self.sessions.transcript_seqs(state.session.id, [self.sessions.transcript_key(message)])
        await self.checkpoint(state, kind="before", seq=seqs[0] if seqs else None)
        await self._start_run(state, message)

    async def _dispatch_event(self, state: SessionState, event: TurnEvent) -> None:
        if event.type is EventType.TOOL_CALL_PENDING and event.payload.get("kind") == "ask_user":
            pending = PendingQuestion(
                session_id=state.session.id,
                run_id=event.run_id,
                tool_call_id=str(event.payload.get("tool_call_id")),
                kind="ask_user",
                payload=dict(event.payload.get("ask_user_payload") or {}),
            )
            state.pending = pending
            await self.db.execute(
                "INSERT OR REPLACE INTO pending_questions(session_id, run_id, tool_call_id, kind, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (pending.session_id, pending.run_id, pending.tool_call_id, pending.kind, json.dumps(pending.payload), datetime.now(UTC).isoformat()),
            )
        self._redact_event(state, event)
        durable = event.to_event()
        durable.payload.setdefault("tenant_id", TENANT)
        durable.payload.setdefault("event_type", event.type.value)
        await self.events.emit(durable)
        for sink in self._sinks:
            try:
                await sink(state.session.id, event)
            except Exception:  # noqa: BLE001
                logger.exception("event sink failed")
        if event.type is EventType.MESSAGE_STOP:
            await self._enforce_caps(state, event.run_id)

    def _redact_event(self, state: SessionState, event: TurnEvent) -> None:
        """Mask secrets the core's hook did not see: failure results, error text, tool arguments.

        A tool that returned normally was masked by the PostToolUse hook before its result
        entered the history. A tool that raised takes the dispatcher's failure path, which
        skips the hook, so its message is masked here — in the event every consumer sees and
        in the history message the core already appended.
        """
        p = event.payload
        if event.type is EventType.TOOL_RESULT:
            content = p.get("content")
            if isinstance(content, str):
                cleaned = self.redactor.redact(content)
                if cleaned != content:
                    p["content"] = cleaned
                    self._redact_history_result(state, str(p.get("tool_call_id")), cleaned)
        elif event.type is EventType.TOOL_USE_STOP and isinstance(p.get("final_input"), dict):
            p["final_input"] = self.redactor.redact_any(p["final_input"])
        elif event.type is EventType.ERROR and isinstance(p.get("message"), str):
            p["message"] = self.redactor.redact(p["message"])

    @staticmethod
    def _redact_history_result(state: SessionState, tool_call_id: str, cleaned: str) -> None:
        engine = state.engine
        if engine is None:
            return
        for index in range(len(engine.history) - 1, -1, -1):
            message = engine.history[index]
            if message.role is not MessageRole.tool:
                continue
            blocks = list(message.content_blocks)
            changed = False
            for bi, block in enumerate(blocks):
                if isinstance(block, ToolResultBlock) and block.tool_call_id == tool_call_id:
                    blocks[bi] = block.model_copy(update={"content": cleaned})
                    changed = True
            if changed:
                engine.history[index] = message.model_copy(update={"content_blocks": blocks})
                return

    async def spend(self, *, run_id: str | None = None, session_id: str | None = None, provider_id: str | None = None, since: str | None = None) -> tuple[float, int]:
        """Priced spend in USD (and the number of unpriced calls) over the given slice of usage events."""
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("run_id", run_id), ("session_id", session_id), ("provider_id", provider_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since:
            clauses.append("at >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = await self.db.fetchone(f"SELECT sum(cost_usd) usd, sum(cost_usd IS NULL) unmetered FROM usage_events{where}", tuple(params))
        return (float(row["usd"] or 0.0), int(row["unmetered"] or 0)) if row else (0.0, 0)

    async def context_status(self, state: SessionState) -> dict[str, Any]:
        """What the model actually sees right now: the last prompt size against the window, and what the history is made of.

        The prompt size is the provider's own count from the latest call (the engine keeps it
        while a run is live; the usage log has it between runs), so it includes the system
        prompt and tool schemas, not only the history.
        """
        tokens = int(state.engine.last_observed_prompt_tokens) if state.engine is not None else 0
        if not tokens:
            row = await self.db.fetchone("SELECT input_tokens FROM usage_events WHERE session_id = ? AND purpose = 'stream' ORDER BY seq DESC LIMIT 1", (state.session.id,))
            tokens = int(row["input_tokens"] or 0) if row else 0
        try:
            _, preset = self.resolve_model(await self.live.load(state.session.id))
            window = int(state.context_window or preset.context_window or 0)
        except Exception:  # noqa: BLE001 — no usable model is reported elsewhere
            window = int(state.context_window or 0)
        history = list(state.engine.history) if state.engine is not None else list(await self.sessions.list_messages(state.session.id, TENANT, limit=10_000))
        summaries = sum(1 for m in history if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY))
        operator = sum(1 for m in history if m.role is MessageRole.user and m.metadata.get("daedalus.origin") not in (None, "core") and not m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY))
        return {"tokens": tokens, "window": window, "messages": len(history), "summaries": summaries, "operator_turns": operator}

    def tools_off(self, state: SessionState) -> set[str]:
        """Tools the operator switched off for this session (``metadata["tools_off"]``); unknown names are ignored."""
        known = {t.name for t in self.tools.list_all()}
        return {str(n) for n in (state.metadata.get("tools_off") or ()) if str(n) in known}

    async def set_tools_off(self, session_id: str, names: list[str]) -> list[str]:
        """Switch tools off (or back on, by omission) for a session; applies from the next model call."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        known = {t.name for t in self.tools.list_all()}
        chosen = sorted({str(n) for n in names if str(n) in known})
        for meta in (state.metadata, state.session.metadata):
            if chosen:
                meta["tools_off"] = chosen
            else:
                meta.pop("tools_off", None)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        if state.engine is not None:
            blocked = blocked_for(self.mcp, self.mcp_enabled(state)) | set(chosen)
            state.engine.config = replace(state.engine.config, tool_visibility_policy=ToolVisibilityPolicy(pinned=known - blocked, blocked=blocked))
        return chosen

    def notes_for(self, state: SessionState) -> str:
        """What the system prompt says about this session beyond the environment: the brief it was created with."""
        parts = [state.extra_notes.strip()] if state.extra_notes.strip() else []
        available = set(self.providers.available())
        models = [pid for pid, preset in self.config.presets.items() if preset.provider in available and preset.model]
        if models:
            parts.append("- Models SubAgent accepts (preset ids): " + ", ".join(models))
        brief = str(state.metadata.get("brief") or "").strip()
        if brief:
            origin = state.metadata.get("spawned_by") or state.metadata.get("subagent_of")
            parts.append("- Your brief" + (f" (from session {origin})" if origin else "") + ", the standing instructions for this session:\n" + brief)
        return "\n".join(parts)

    async def set_brief(self, session_id: str, brief: str) -> str:
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        brief = brief.strip()[:BRIEF_MAX_CHARS]
        for meta in (state.metadata, state.session.metadata):
            if brief:
                meta["brief"] = brief
            else:
                meta.pop("brief", None)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        return brief

    @staticmethod
    def session_cap(state: SessionState) -> float | None:
        """This session's own spend cap (all of its runs), or None when it follows the global limits only."""
        raw = state.metadata.get("usd_cap")
        return float(raw) if raw is not None else None

    async def set_session_cap(self, session_id: str, cap: float | None) -> float | None:
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        if cap is not None and cap < 0:
            raise ValueError("a session cap cannot be negative")
        for meta in (state.metadata, state.session.metadata):
            if cap is None:
                meta.pop("usd_cap", None)
            else:
                meta["usd_cap"] = float(cap)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        return cap

    async def cap_breach(self, state: SessionState, provider_id: str | None) -> tuple[str, str] | None:
        """``(kind, note)`` when the session, its provider or everything together has spent its cap.

        Three caps stack above the per-run one: the session's own (session settings), the
        provider's total across every session (``limits.usd_total_per_provider``) and the grand
        total (``limits.usd_total``); the last two count from ``limits.total_since``.
        """
        limits = self.config.limits
        since = limits.total_since or None
        session_cap = self.session_cap(state)
        if session_cap is not None:
            spent, _ = await self.spend(session_id=state.session.id)
            if spent >= session_cap:
                return "session_cap", f"session cap reached: ${spent:.2f} spent of ${session_cap:.2f}; raise it in the session settings to continue"
        provider_cap = float(limits.usd_total_per_provider.get(provider_id or "", 0) or 0)
        if provider_id and provider_cap > 0:
            spent, _ = await self.spend(provider_id=provider_id, since=since)
            if spent >= provider_cap:
                return "provider_cap", f"total cap for provider {provider_id!r} reached: ${spent:.2f} spent of ${provider_cap:.2f} (limits.usd_total_per_provider); raise it or reset the counter in Settings → Limits"
        if limits.usd_total > 0:
            spent, _ = await self.spend(since=since)
            if spent >= limits.usd_total:
                return "total_cap", f"total spend cap reached: ${spent:.2f} spent of ${limits.usd_total:.2f} (limits.usd_total); raise it or reset the counter in Settings → Limits"
        return None

    async def _enforce_caps(self, state: SessionState, run_id: str) -> None:
        """Stop a run that crossed a spend cap: its own, the session's, its provider's or the total."""
        if state.engine is None or not state.running or run_id in self._capped_runs:
            return
        mode = self.mode_for(state)
        mode_cap = mode.usd_per_run if mode is not None else None
        run_cap = mode_cap if mode_cap is not None else self.config.limits.usd_per_run
        spent, unmetered = await self.spend(run_id=run_id)
        note: str | None = None
        kind = "run_cap"
        if (run_cap > 0 or mode_cap is not None) and spent >= run_cap:
            note = f"💸 per-run cap reached: ${spent:.2f} spent of ${run_cap:.2f} (limits.usd_per_run); stopping this run. Send a message to continue in a new run."
            if unmetered:
                note += f" {unmetered} call(s) had no known price and are not counted."
        else:
            last = await self.db.fetchone("SELECT provider_id FROM usage_events WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,))
            breach = await self.cap_breach(state, last["provider_id"] if last else None)
            if breach is not None:
                kind, note = breach[0], f"💸 {breach[1]}; stopping this run."
        if note is None:
            return
        self._capped_runs.add(run_id)
        logger.warning("run %s stopped (%s): %s", run_id, kind, note)
        state.engine.stop()
        await self._dispatch_event(state, TurnEvent(type=EventType.ERROR, run_id=run_id, payload={"message": note, "kind": kind}))

    # -- recovery -------------------------------------------------------------------

    async def resume_unfinished(self) -> list[str]:
        """Continue runs that were mid-flight when the process last stopped."""
        resumed: list[str] = []
        if self.budget_exceeded():
            logger.warning("budget exceeded; unfinished runs stay parked until the cap is lifted")
            return resumed
        for entry in await self.events.unfinished_snapshots():
            session_id = entry["session_id"] or entry["snapshot"].get("session_id")
            state = await self.get_state(session_id)
            if state is None:
                await self.events.delete_snapshot(entry["run_id"])
                continue
            try:
                engine = await self._build_engine(state, entry["run_id"])
                await engine.resume_from_snapshot(entry["snapshot"])
            except Exception:  # noqa: BLE001
                logger.exception("could not resume run %s", entry["run_id"])
                await self.events.delete_snapshot(entry["run_id"])
                continue
            state.engine = engine
            state.run_id = entry["run_id"]
            state.history_keys = [self.sessions.transcript_key(m) for m in engine.history]
            if engine.state is LoopState.AWAITING:
                row = await self.db.fetchone("SELECT * FROM pending_questions WHERE session_id = ?", (session_id,))
                if row is None:
                    await self.events.delete_snapshot(entry["run_id"])
                    continue
                state.pending = PendingQuestion(
                    session_id=session_id,
                    run_id=row["run_id"],
                    tool_call_id=row["tool_call_id"],
                    kind=row["kind"],
                    payload=json.loads(row["payload"]),
                )
                for callback in self._pending_restored:
                    try:
                        await callback(session_id, state.pending)
                    except Exception:  # noqa: BLE001
                        logger.exception("pending-restored callback failed")
                continue
            if not engine.history:
                await self.events.delete_snapshot(entry["run_id"])
                continue
            if engine.state is not LoopState.RUNNING:
                engine.transition_to(LoopState.RUNNING)
            state.task = asyncio.create_task(self._drive(state, engine, None, True), name=f"resume:{entry['run_id']}")
            state.task.add_done_callback(_log_task_failure)
            resumed.append(entry["run_id"])
        return resumed


SUMMARY_SECTIONS = ("Goal", "Constraints", "State", "Discoveries", "Open", "Next steps", "Unknowns", "Identifiers")

COMPACT_PROMPT = """Summarise the conversation transcript below so that an agent can continue the work \
in a fresh context. Write the summary in {language}, as Markdown with exactly these eight sections, \
each once, in this order, as level-2 headings whose titles stay in English verbatim whatever the \
language of the body: ## Goal · ## Constraints · ## State · ## Discoveries · ## Open · ## Next steps · \
## Unknowns · ## Identifiers.
Goal: what was asked and why, with the success criteria. Constraints: rules, preferences and decisions \
the operator stated — quote them, do not paraphrase. State: what is done, with concrete results (paths, \
commands, numbers, decisions) — every identifier verbatim, never rounded or guessed. Discoveries: \
technical facts learned, errors and how they were resolved, approaches that failed and why. Open: what \
is in progress or untouched. Next steps: the exact actions to take next, in order. Unknowns: what the \
transcript does not show. The absence of a tool result or confirmation means the outcome is UNKNOWN, \
not that it did not happen or that it succeeded; put such items under Unknowns rather than asserting \
them. Identifiers: one line per path, id, URL, port, command, name or number that later work may need, \
each exactly as it appeared. Never include credentials or tokens. At most ~{max_words} words. No \
commentary outside the sections."""

CHUNK_PROMPT = """The text below is ONE PART of a longer conversation transcript (part {index} of {total}). \
Summarise this part in {language} with exactly these eight level-2 headings, each once, in this order, \
titles in English: ## Goal · ## Constraints · ## State · ## Discoveries · ## Open · ## Next steps · \
## Unknowns · ## Identifiers. Keep every identifier (paths, ids, URLs, ports, commands, numbers) \
verbatim; quote operator rules rather than paraphrasing; an outcome the part does not show is UNKNOWN. \
Never include credentials or tokens. At most ~{max_words} words. No commentary outside the sections."""

MERGE_PROMPT = """Below are summaries of consecutive parts of one conversation, oldest first. Merge them \
into ONE summary in {language} with exactly these eight level-2 headings, each once, in this order, \
titles in English: ## Goal · ## Constraints · ## State · ## Discoveries · ## Open · ## Next steps · \
## Unknowns · ## Identifiers. Keep the latest known state of each thing and every identifier verbatim; \
keep every quoted operator rule; an item still unknown stays under Unknowns. At most ~{max_words} \
words. No commentary outside the sections."""

VERBATIM_TAIL_MESSAGES = 3
OPERATOR_QUOTE_CHARS = 400
"""Older operator messages are quoted by code, each clipped to this many characters, so a rule never depends on the summariser."""


def validate_summary_sections(summary: str) -> str:
    """The reason a summary is rejected, or an empty string when every section appears once, in order."""
    positions: list[int] = []
    for name in SUMMARY_SECTIONS:
        pattern = re.compile(rf"^##\s+{re.escape(name)}\s*$", re.MULTILINE | re.IGNORECASE)
        found = pattern.findall(summary)
        if len(found) != 1:
            return f"section '## {name}' appears {len(found)} times (expected once)"
        positions.append(pattern.search(summary).start())  # type: ignore[union-attr]
    if positions != sorted(positions):
        return "sections are out of order"
    return ""


def operator_quotes(history: Sequence[Message], *, skip_last: int = VERBATIM_TAIL_MESSAGES, clip: int = OPERATOR_QUOTE_CHARS) -> str:
    """Every older operator message, quoted by code: constraints survive compaction unchanged.

    The summariser is asked to quote rules too, but a model paraphrases under pressure; this
    section is written without it. The most recent ``skip_last`` operator messages are left to
    :func:`verbatim_tail`, which prints them whole.
    """
    operator = [
        m for m in history
        if m.role is MessageRole.user and m.metadata.get("daedalus.origin") == "operator" and not m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)
    ]
    older = operator[:-skip_last] if skip_last else operator
    lines: list[str] = []
    seen: set[str] = set()
    for m in older:
        text = " ".join("".join(b.text for b in m.content_blocks if isinstance(b, TextBlock)).split())
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append("- " + (text[:clip] + "…" if len(text) > clip else text))
    return ("\n\n## Operator said (verbatim, oldest first)\n" + "\n".join(lines)) if lines else ""


IDENTIFIER_RE = re.compile(
    r"(?<![\w/])(?:/[\w.@~-]+(?:/[\w.@~-]+)+|https?://[^\s'\"<>)]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{12,40}\b|\b[A-Za-z][\w-]*\.(?:py|md|toml|json|txt|yaml|yml|ts|tsx|sh|sql)\b|\bPR\s?#?\d{1,6}\b|#\d{1,6}\b|\b\d{4,5}\b)"
)
IDENTIFIER_INDEX_MAX = 150
"""Identifiers pinned by code below the summary: paths, URLs, ids, file names, PR numbers, ports."""


def identifier_index(history: Sequence[Message], *, limit: int = IDENTIFIER_INDEX_MAX) -> str:
    """Every identifier the operator or the agent's tool calls named, in first-seen order, written by code.

    The summariser keeps some of these; this list keeps all of them, because a path or an
    id that is gone from context is gone from the work. Tool results are not scanned: they
    carry directory listings and logs, which would bury the ones that matter.
    """
    seen: dict[str, None] = {}
    for m in history:
        if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY):
            continue
        for b in m.content_blocks:
            if isinstance(b, TextBlock) and (m.role is MessageRole.assistant or m.metadata.get("daedalus.origin") == "operator"):
                source = b.text
            elif isinstance(b, ToolUseBlock):
                source = b.arguments_json or ""
            else:
                continue
            for found in IDENTIFIER_RE.findall(source):
                token = found.strip().rstrip(".,;:")
                if token.isdigit() and (len(token) < 4 or 1900 <= int(token) <= 2100):
                    continue  # bare years and short numbers are noise
                if len(token) > 200 or token in seen:
                    continue
                seen[token] = None
    if not seen:
        return ""
    items = list(seen)[-limit:]
    return "\n\n## Identifiers seen (extracted)\n" + "\n".join(f"- {t}" for t in items)


def compaction_cut(history: Sequence[Message], keep_recent: int) -> int:
    """Index where the kept tail starts: at most ``keep_recent`` messages, never inside a tool exchange.

    The tail begins at a user-role message that is not a tool result (a turn boundary), so a
    tool call is never separated from its result and a summary never ends on an unanswered call.
    """
    if keep_recent <= 0 or len(history) <= keep_recent:
        return len(history) if keep_recent <= 0 else 0
    cut = len(history) - keep_recent
    while cut > 0:
        m = history[cut]
        if m.role is MessageRole.user and not any(isinstance(b, ToolResultBlock) for b in m.content_blocks):
            break
        cut -= 1
    return cut


def verbatim_tail(history: Sequence[Message], count: int = VERBATIM_TAIL_MESSAGES) -> str:
    """The operator's last messages, appended by code so the summary can never lose their wording."""
    operator = [
        m for m in history
        if m.role is MessageRole.user and m.metadata.get("daedalus.origin") == "operator" and not m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)
    ]
    tail = operator[-count:]
    if not tail:
        return ""
    lines = ["", "", "## Recent operator messages (verbatim)"]
    for m in tail:
        text = "".join(b.text for b in m.content_blocks if isinstance(b, TextBlock)).strip()
        lines.append(f"- [{m.created_at.strftime('%Y-%m-%d %H:%M')}] {text[:1500]}")
    return "\n".join(lines)


def annotate_summary(message: Message, note: str, seqs: Sequence[int]) -> Message:
    """Append the archived-range note inside a summary's wrapper and record the exact seqs in its metadata."""
    blocks = list(message.content_blocks)
    for i, block in enumerate(blocks):
        if isinstance(block, TextBlock):
            text = block.text
            if text.rstrip().endswith("</compacted-turn>"):
                cut = text.rstrip()[: -len("</compacted-turn>")]
                text = f"{cut.rstrip()}\n\n{note}</compacted-turn>"
            else:
                text = f"{text.rstrip()}\n\n{note}"
            blocks[i] = block.model_copy(update={"text": text})
            break
    return message.model_copy(update={"content_blocks": blocks, "metadata": {**message.metadata, "daedalus.archived": {"from_seq": seqs[0], "to_seq": seqs[-1], "seqs": list(seqs)}}})


def repoint_summary(message: Message, mapping: dict[int, int]) -> Message:
    """A compaction summary copied into another session: its archived seqs and inline note now name that session's rows."""
    archived = message.metadata.get("daedalus.archived") or {}
    seqs = sorted(mapping[int(x)] for x in archived.get("seqs") or [] if int(x) in mapping)
    blocks = list(message.content_blocks)
    for i, block in enumerate(blocks):
        if isinstance(block, TextBlock) and "[archived turns " in block.text:
            text = re.sub(r"\[archived turns [^\]]*\]", "", block.text).rstrip()
            if seqs:
                contiguous = seqs[-1] - seqs[0] + 1 == len(seqs)
                span = f"seq {seqs[0]}–{seqs[-1]}" if contiguous else f"within seq {seqs[0]}–{seqs[-1]} ({len(seqs)} turns)"
                note = f"[archived turns {span}: HistoryExpand({seqs[0]}, {seqs[-1]}) returns them verbatim]"
                text = f"{text[: -len('</compacted-turn>')].rstrip()}\n\n{note}</compacted-turn>" if text.endswith("</compacted-turn>") else f"{text}\n\n{note}"
            blocks[i] = block.model_copy(update={"text": text})
            break
    meta = {"from_seq": seqs[0], "to_seq": seqs[-1], "seqs": seqs} if seqs else {"seqs": []}
    return message.model_copy(update={"content_blocks": blocks, "metadata": {**message.metadata, "daedalus.archived": meta}})


def operator_language(history: Sequence[Message]) -> str:
    """A coarse guess at the operator's language from their messages (Cyrillic → Russian, else English)."""
    text = " ".join(
        b.text for m in history if m.role is MessageRole.user for b in m.content_blocks if isinstance(b, TextBlock)
    )
    letters = [c for c in text if c.isalpha()]
    if letters and sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters) > 0.3:
        return "Russian"
    return "English"


def split_transcript(text: str, chunk_tokens: int) -> list[str]:
    """Consecutive parts of a rendered transcript, each about ``chunk_tokens`` (4 chars per token), split on line ends."""
    limit = max(1, chunk_tokens) * 4
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 > limit and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def transcript_for_summary(history: Sequence[Message], *, result_chars: int = 600) -> str:
    """A compact textual rendering of the history for the summariser."""
    lines: list[str] = []
    for message in history:
        if message.role is MessageRole.system:
            continue
        for block in message.content_blocks:
            if isinstance(block, TextBlock):
                text = block.text.strip()
                if text:
                    lines.append(f"[{message.role.value}] {text[:4000]}")
            elif isinstance(block, ToolUseBlock):
                lines.append(f"[tool call] {block.name} {(block.arguments_json or '')[:300]}")
            elif isinstance(block, ToolResultBlock):
                body = (block.content or "").strip()
                if len(body) > result_chars:
                    body = body[: result_chars // 2] + " … " + body[-result_chars // 2 :]
                lines.append(f"[tool result{' ERROR' if block.is_error else ''}] {body}")
            elif isinstance(block, ThinkingBlock):
                continue
    return "\n".join(lines)[-200_000:]


class _LiveVision:
    """Resolves the ImageView provider/model at call time, so settings edits apply immediately."""

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager

    def __iter__(self):  # type: ignore[no-untyped-def]
        resolved = self._manager._vision()
        if resolved is None:
            raise ValueError("no vision model is configured")
        return iter(resolved)

    def __bool__(self) -> bool:
        return self._manager._vision() is not None


def _log_task_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("run task %s failed: %r", task.get_name(), exc, exc_info=exc)


def _bind(fn: Callable[..., Awaitable[Any]] | None, session_id: str) -> Callable[..., Awaitable[Any]] | None:
    if fn is None:
        return None

    async def bound(*args: Any, **kwargs: Any) -> Any:
        return await fn(session_id, *args, **kwargs)

    return bound


__all__ = ["Attachment", "PendingQuestion", "SessionManager", "SessionState", "annotate_summary", "transcript_for_summary", "validate_summary_sections", "verbatim_tail"]
