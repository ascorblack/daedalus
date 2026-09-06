"""Sessions and runs: one ``QueryEngine`` per session, one asyncio task per run."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
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
from protocore.runtime.query import query
from protocore.runtime.query_engine import QueryEngine
from protocore.tests_support.adapters import InMemoryToolRegistry
from protocore.tools.ask_user import AskUserTool
from protocore.tools.memory import build_memory_tools

from daedalus.config import RuntimeConfig, Settings
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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialises run starts against history rewrites (compaction)."""

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
        self._pending_restored: list[Callable[[str, PendingQuestion], Awaitable[None]]] = []
        self.service_hooks: dict[str, Any] = {}
        """Callbacks the transport layer installs: send_file, spawn_session, schedule, self_*."""
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
        logger.warning("tools registered: %s", ", ".join(sorted(t.name for t in self.tools.list_all())))

    async def close(self) -> None:
        """Shut down keeping every active run resumable (snapshots stay in place)."""
        self.shutting_down = True
        tasks = [s.task for s in self._states.values() if s.task and not s.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
            blocked = blocked_for(self.mcp, current)
            from dataclasses import replace

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
        self._register_services(state)
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
        self._register_services(state)
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
            await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        if delete_workspace and state.workspace.exists() and state.workspace.is_relative_to(self.settings.workspaces_dir):
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

    async def compact(self, session_id: str, instructions: str = "") -> str:
        """Replace the whole history with one model-written summary; returns the summary.

        Holds the session lock for the whole operation (including the summarising call) so
        no run can start against the history while it is being rewritten. The replaced
        transcript is kept in the workspace as ``.history-<timestamp>.jsonl``.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        async with state.lock:
            return await self._compact_locked(state, instructions)

    async def _compact_locked(self, state: SessionState, instructions: str) -> str:
        session_id = state.session.id
        if state.running and state.engine is not None and state.engine.is_terminal and state.task is not None:
            # The loop has settled; only bookkeeping remains.
            await asyncio.gather(asyncio.shield(state.task), return_exceptions=True)
        if state.running or state.pending is not None:
            raise RuntimeError("the session is busy; stop the run (or answer the question) first")
        exceeded = self.budget_exceeded()
        if exceeded:
            raise RuntimeError(f"daily budget exceeded ({exceeded}); compaction is a paid call")
        history = list(state.engine.history) if state.engine is not None else list(
            await self.sessions.list_messages(session_id, TENANT, limit=10_000)
        )
        if not history:
            raise RuntimeError("nothing to compact")
        rungs, _ = self.resolve_model(await self.live.load(session_id))
        provider, model = rungs[0]  # the session's own model summarises its own history
        language = self.config.answer_language if self.config.answer_language != "auto" else operator_language(history)
        prompt = COMPACT_PROMPT.format(language=language) + (
            f"\n\nThe operator asks to focus on: {instructions.strip()}" if instructions.strip() else ""
        )
        request = LLMRequest(
            model=model,
            messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=prompt + "\n\n" + transcript_for_summary(history))])],
            max_tokens=6000,
            temperature=0.2,
            extra={"enable_thinking": False},
            observability=LLMObservabilityContext(tenant_id=TENANT, session_id=session_id, run_id=state.run_id, call_purpose="compaction", call_category="compaction"),
        )
        response = await provider.complete_text(request)
        summary = "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock)).strip()
        if not summary:
            raise RuntimeError("the model returned an empty summary")
        if state.running or state.pending is not None:  # a run resumed from a snapshot meanwhile
            raise RuntimeError("the session became busy during compaction; nothing was changed")
        backup = state.workspace / f".history-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        try:
            backup.write_text("\n".join(m.model_dump_json() for m in history) + "\n", encoding="utf-8")
        except OSError:
            logger.warning("could not write the history backup %s", backup, exc_info=True)
        message = Message(
            role=MessageRole.user,
            content_blocks=[TextBlock(text=f"<compacted-turn id='manual'>{summary}</compacted-turn>")],
            metadata={
                COMPACTION_SUMMARY_METADATA_KEY: True,
                "daedalus.compaction": {"reason": "manual", "messages": len(history), "at": datetime.now(UTC).isoformat()},
            },
        )
        if state.engine is not None:
            engine = state.engine
            engine.history = [message]
            engine.compact_checkpoint = None  # type: ignore[attr-defined]
            # The core gates automatic compaction on the last measured prompt size; that
            # measurement described the history that no longer exists.
            engine.last_observed_prompt_tokens = 0
            engine.compaction_state = CompactionState()
        await self.sessions.append_transcript(session_id, history, from_history=True)
        await self.sessions.replace_messages(session_id, TENANT, [message])
        await self.sessions.append_transcript(session_id, [message])
        return summary

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

    async def sweep_orphan_workspaces(self) -> list[str]:
        """Delete workspace directories that no session or schedule refers to any more."""
        rows = await self.db.fetchall("SELECT id FROM sessions")
        known = {r["id"] for r in rows}
        sched = await self.db.fetchall("SELECT workspace FROM schedules")
        known_paths = {Path(r["workspace"]).resolve() for r in sched}
        removed: list[str] = []
        for entry in self.settings.workspaces_dir.iterdir():
            if not entry.is_dir() or entry.name in known or entry.resolve() in known_paths:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        return removed

    def _register_services(self, state: SessionState) -> None:
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
    ) -> str:
        """Deliver operator input. Starts a run, or queues a follow-up when one is active."""
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
                session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], metadata={"daedalus.delivery": "follow_up", "daedalus.origin": "operator"})]
            )
            return state.run_id or ""
        exceeded = self.budget_exceeded()
        if exceeded and not state.running:
            raise RuntimeError(f"daily budget exceeded ({exceeded}); runs resume tomorrow or after /budget reset")
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
            await self.live.enqueue(session_id, kind, new_queued_prompt(kind, body).to_dict())  # type: ignore[arg-type]
            # The core folds queued prompts into the model's history later (and compaction may
            # rewrite them); the transcript keeps the operator's words as sent.
            await self.sessions.append_transcript(
                session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], metadata={"daedalus.delivery": kind, "daedalus.origin": "operator"})]
            )
            return state.run_id or ""
        message = Message(
            role=MessageRole.user,
            content_blocks=[TextBlock(text=body)],
            metadata={"daedalus.origin": "operator", **({"image_refs": [{"ref": ref, "mime": mime} for ref, mime in image_refs]} if image_refs else {})},
        )
        await self.sessions.append_transcript(session_id, [message])
        return await self._start_run(state, message)

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
        engine = build_engine(
            deps=deps,
            config=self.config,
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
            extra_notes=state.extra_notes,
            blocked_tools=blocked_for(self.mcp, self.mcp_enabled(state)),
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
            loop = asyncio.get_running_loop()
            loop.create_task(self.sessions.replace_messages(session_id, TENANT, history))
            loop.create_task(self.sessions.append_transcript(session_id, history, from_history=True))

        engine.reload_live_control = reload_live_control  # type: ignore[attr-defined]
        engine.persist_live_control = persist_live_control  # type: ignore[attr-defined]
        engine.persist_session_history = persist_session_history  # type: ignore[attr-defined]

    async def _start_run(
        self, state: SessionState, message: Message | None, *, continue_turn: bool = False
    ) -> str:
        async with state.lock:
            return await self._start_run_locked(state, message, continue_turn=continue_turn)

    async def _start_run_locked(
        self, state: SessionState, message: Message | None, *, continue_turn: bool = False
    ) -> str:
        if state.engine is None or not continue_turn:
            run_id = uuid.uuid4().hex[:12]
            engine = await self._build_engine(state, run_id)
            if state.engine is not None:
                engine.history = list(state.engine.history)
            else:
                engine.history = list(await self.sessions.list_messages(state.session.id, TENANT, limit=10_000))
            state.engine = engine
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
            iterator = query(engine) if continue_turn else engine.run(message)
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
            await self.sessions.replace_messages(session_id, TENANT, list(engine.history))
            await self.sessions.append_transcript(session_id, list(engine.history), from_history=True)
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
            except Exception:  # noqa: BLE001
                logger.exception("run %s bookkeeping failed", run_id)
            for callback in self._finished:
                try:
                    await callback(session_id, run_id, status)
                except Exception:  # noqa: BLE001
                    logger.exception("run-finished callback failed")
            if status == "completed":
                await self._drain_leftover_follow_ups(state)

    async def _drain_leftover_follow_ups(self, state: SessionState) -> None:
        """Input that arrived while the run was settling starts the next turn instead of rotting in the queue."""
        queued = await self.live.load(state.session.id)
        texts = [str(item.get("text") or "").strip() for item in queued["follow_up"] + queued["steer"]]
        texts = [t for t in texts if t]
        if not texts:
            return
        await self.live.save_queues(state.session.id, [], [])
        message = Message(role=MessageRole.user, content_blocks=[TextBlock(text="\n\n".join(texts))])
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
            await self._enforce_run_cap(state, event.run_id)

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

    async def _enforce_run_cap(self, state: SessionState, run_id: str) -> None:
        """Stop a run whose priced spend crossed ``limits.usd_per_run``; unpriced calls cannot count."""
        cap = self.config.limits.usd_per_run
        if cap <= 0 or state.engine is None or not state.running or run_id in self._capped_runs:
            return
        row = await self.db.fetchone(
            "SELECT sum(cost_usd) usd, sum(cost_usd IS NULL) unmetered FROM usage_events WHERE run_id = ?", (run_id,)
        )
        spent = float(row["usd"] or 0.0) if row else 0.0
        if spent < cap:
            return
        self._capped_runs.add(run_id)
        note = f"💸 per-run cap reached: ${spent:.2f} spent of ${cap:.2f} (limits.usd_per_run); stopping this run. Send a message to continue in a new run."
        if row and row["unmetered"]:
            note += f" {int(row['unmetered'])} call(s) had no known price and are not counted."
        logger.warning("run %s stopped at the per-run cap: $%.4f >= $%.2f", run_id, spent, cap)
        state.engine.stop()
        await self._dispatch_event(state, TurnEvent(type=EventType.ERROR, run_id=run_id, payload={"message": note, "kind": "run_cap"}))

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


COMPACT_PROMPT = """Summarise the conversation transcript below so that an agent can continue the work \
in a fresh context. Write the summary in {language}. Include: the goal and what was asked; \
what has been done, with concrete results (paths, commands, numbers, decisions); what is still open; \
constraints and preferences the operator stated; and the exact next steps. Be precise and compact \
(Markdown, at most ~600 words). Do not add commentary."""


def operator_language(history: Sequence[Message]) -> str:
    """A coarse guess at the operator's language from their messages (Cyrillic → Russian, else English)."""
    text = " ".join(
        b.text for m in history if m.role is MessageRole.user for b in m.content_blocks if isinstance(b, TextBlock)
    )
    letters = [c for c in text if c.isalpha()]
    if letters and sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters) > 0.3:
        return "Russian"
    return "English"


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


__all__ = ["Attachment", "PendingQuestion", "SessionManager", "SessionState", "transcript_for_summary"]
