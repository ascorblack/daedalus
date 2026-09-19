"""Sessions and runs: one ``QueryEngine`` per session, one asyncio task per run."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
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
from protocore.runtime.soft_stop import CAUSE_PROVIDER_ERROR
from protocore.tests_support.adapters import InMemoryToolRegistry
from protocore.tools.ask_user import AskUserTool
from protocore.tools.memory import build_memory_tools

from daedalus.config import VOICE_ONLY_TOOLS, VOICE_TOOLS, NoModelConfigured, RuntimeConfig, Settings
from daedalus.host import capabilities, launcher_bridge, prompts
from daedalus.host.checkpoint_retention import CheckpointRetention, RetentionBounds, RetentionReport
from daedalus.host.checkpoints import DIR_NAME as CHECKPOINT_DIR_NAME
from daedalus.host.checkpoints import CheckpointError, Checkpoints, scan_workspace
from daedalus.host.engine_factory import TENANT, EngineDeps, PolicyAdapter, build_engine
from daedalus.host.hooks import DaedalusHookManager
from daedalus.host.policy import Decision, Policy, Rule, canonical
from daedalus.host.services import SessionServices, locator
from daedalus.host.skills import DirectorySkillStore
from daedalus.host.transcript_view import TranscriptViewBuilder, message_view
from daedalus.mcp.manager import McpManager, blocked_for
from daedalus.providers.chain import build_chain
from daedalus.providers.registry import ProviderRegistry
from daedalus.security import redact
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database
from daedalus.stores.persistent import PersistentMemory, PersistentWorkspace
from daedalus.stores.projects import Project, ProjectSettings, ProjectStore
from daedalus.stores.sqlite import (
    LiveControlStore,
    SqliteEventStream,
    SqliteRunStore,
    SqliteSessionStore,
    SqliteUsageSink,
    message_text,
)
from daedalus.tools import discover_tools

logger = logging.getLogger(__name__)

PROVIDER_OUTAGE_KINDS = frozenset({"llm_provider_error", "llm_timeout", "llm_stream_idle", "llm_rate_limit"})
"""Terminal error kinds that mean the endpoint, not the request, failed: the run is driven again once the wait is over."""
RECOVERY_REASONS = frozenset({"transient_llm_error_retry", "model_fallback_triggered", "soft_stop_notified", "llm_context_window_exceeded", "context_window_recovered", "reasoning_length_cut_retry", "continue_prompt_injected", "max_output_token_recovery"})
"""The state changes worth a log line: each is a round the run had to recover from, and the log is where the reason survives."""
MODEL_METADATA_KEY = "daedalus.model"

BACKGROUND_SHUTDOWN_SECONDS = 5.0
"""How long a shutdown waits for a cancelled background task — the price refresh, the index
backfill — before leaving it. Neither owes anything to disk; what they can owe is a socket."""

MODEL_STAMPS_KEPT = 512
"""How many un-persisted turn stamps a session holds at once. A run writes its rounds as they finish,
so the map is normally one or two entries deep; the cap is only there so a store that is refusing
every write cannot turn it into a leak."""
"""Message metadata naming what produced an assistant turn: provider, model, the configured model, and the fallback if it was one."""
FALLBACK_REASONS = {
    "llm_rate_limit": "rate_limit",
    "llm_timeout": "outage",
    "llm_stream_idle": "outage",
    "llm_provider_error": "outage",
}
"""The core's error class behind a demotion, as the one word the app and the operator read."""
BRIEF_MAX_CHARS = 12_000
"""A spawned agent's brief lives in its system prompt; longer hand-overs belong in files."""
WORKSPACE_NOTES_CHARS = 6000
GRANT_TTL_SECONDS = 2 * 3600

STEER_CARD_CHARS = 200
"""How much of a queued steer travels with a change event. The app draws a card, not the message."""

STEER_CARD_LIMIT = 20
"""Cards one change event carries. Past this the count is the answer; nobody reads the twenty-first card."""
"""How long an approval key stays spendable: long enough for the agent to retry, short enough that a forgotten grant does not wait for a later call."""
"""How much of the workspace AGENTS.md rides along in the prompt; the rest is one Read away."""

EventSink = Callable[[str, Any], Awaitable[None]]
"""A session's listener. It takes the loop's ``TurnEvent`` and the host's own ``HostEvent`` alike: both carry ``type``, ``run_id`` and ``payload``."""
RunFinished = Callable[[str, str, str], Awaitable[None]]  # session_id, run_id, status


class HostEventType(StrEnum):
    """Event kinds the host raises beside the core's turn taxonomy."""

    STEER_CHANGED = "steer_changed"


@dataclass(slots=True)
class HostEvent:
    """One of those, shaped like a ``TurnEvent`` so the same sinks carry it.

    It travels to the session's listeners — the app's stream — and no further: it is not a run
    event, so it is never written to the durable event log. Its ``type`` is deliberately outside
    ``EventType``, so a sink that switches on the core's taxonomy falls through it untouched and
    only a sink that reads the name off the wire (the stream does) sees it at all.
    """

    type: HostEventType
    run_id: str
    payload: dict[str, Any]


@dataclass(slots=True)
class Attachment:
    path: Path
    mime_type: str = "application/octet-stream"
    caption: str | None = None


def _set_event() -> asyncio.Event:
    """An event that starts raised: a session with no run behind it has nothing to wait for."""
    event = asyncio.Event()
    event.set()
    return event


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
    last_error_kind: str = ""
    """The kind of the error that ended the current run, from the core's ERROR event (``llm_context_window_exceeded`` …)."""
    last_error_message: str = ""
    """The text of that error, masked, as the fronts were shown it: what the inbox entry quotes."""
    soft_stop_cause: str = ""
    """Which bound wound the current run down, from the core's ``soft_stop_notified`` state change; empty when none did."""
    soft_stop_detail: str = ""
    """What that bound said in its own words — the upstream's error text for a provider failure. The operator is shown this,
    not an inference from the reply: a run the provider refused ends with the model writing a closing message, and without
    this the message is the only trace of a failure that produced nothing."""
    overflow_streak: int = 0
    """Consecutive runs that overflowed the context window; recovery stops after a few so a hopeless history cannot loop."""
    outage_streak: int = 0
    """Consecutive runs the model provider failed; each one waits longer before the work is driven again."""
    outage_task: asyncio.Task[None] | None = None
    """The wait before the next attempt after a provider failure, so a session sleeps at most once."""
    housekeeping: asyncio.Task[None] | None = None
    """What the last run left to do after its answer was on the screen: the snapshot, the run-finished
    callbacks, the queue it drains, the compaction check. The run itself is over while this is going —
    that is the point of it — so the app draws an idle session and the work goes on behind it."""
    settled: asyncio.Event = field(default_factory=lambda: _set_event())
    """Lowered when a run starts, raised again once its history is persisted and its snapshot taken.
    The next run waits for it: the files a revert would restore must describe the turn that just ended,
    not the one starting. The rest of the housekeeping nobody waits for."""
    compacting: dict[str, Any] | None = None
    """A compaction in flight: reason, stage (summarising/merging/writing), parts done of total, started_at.
    The Mini App and the list read it; ``None`` when none is running."""
    run_active_since: float = 0.0
    """Monotonic time the current run (re)started driving the model: the time cap counts from here, not from
    the run's creation, so an hour waiting on the operator's answer is not an hour of run time."""
    tool_starts: dict[str, tuple[float, str]] = field(default_factory=dict)
    """Tool calls in flight: ``call_id -> (monotonic start, tool name)``, for the timing rows."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialises run starts against history rewrites (compaction)."""
    submit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialises the start-or-queue decision: two inputs arriving together (an operator message and a
    loop tick, say) must become one run plus one steer, never two runs driving one history."""
    history_keys: list[str] = field(default_factory=list)
    """Transcript keys of the working history at the last persist, to see what a compaction removed."""
    persist_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialises history persistence so an older snapshot can never overwrite a newer one."""
    persist_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    persist_chain: asyncio.Task[None] | None = None
    """The persist queued last. Each hand-over waits for it before writing, so the writes of a session
    land in the order the rounds happened however the loop schedules the tasks."""
    persist_rewrite: bool = False
    """Set when a write did not land and nobody has rewritten the history since: there is a hole in the
    stored history, and the next persist writes it whole instead of appending onto rows that are not there."""
    persist_epoch: int = 0
    """Bumped by every failed write. A hand-over queued before the bump describes a history the store no
    longer matches, so it is written whole rather than appended, however late it runs."""
    persist_repair: asyncio.Task[None] | None = None
    """The rewrite scheduled by a failed write, so a store that is down is retried once and not in a loop."""
    persist_gen: int = 0
    """Bumped by every history rewrite (manual compaction); a persist captured before the bump is dropped."""
    configured_model: str = ""
    """``provider:model`` the session is set to answer with: the first rung of the chain this run was built on.
    A live override (a preset chosen for the session, the voice concierge pointing at its own) is part of it —
    what the operator chose is never a fallback, however far it is from the global default."""
    effective_model: str = ""
    """``provider:model`` that actually answered last, as the core reported it at the message it started."""
    model_change_reason: str = ""
    """Why the next change of model happened, taken from the core's own account of the demotion; empty means nobody said."""
    model_stamps: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Which model produced each assistant turn, recorded the moment the turn ended and keyed by its
    transcript key. Persists are fire-and-forget, so a turn is very often written after the chain has
    already stepped down; reading the model at write time therefore names whoever is answering *now*.
    An entry is taken out of the map when the turn it describes is stamped."""
    run_history_start: int = 0
    """Length of the working history when the current run began: what this run added starts here."""
    checkpoint_capped: bool = False
    """Set once the size cap has suppressed a snapshot, so the warning is logged once per session."""
    project: Project | None = None
    """The project this session works in: its root is the workspace, and the only place the session's
    tools may reach. ``None`` is a session with a directory of its own."""
    observed_prompt_tokens: int = 0
    """The host's own estimate of the next prompt after it rewrote the history, until a real call
    measures one. A compaction makes every earlier measurement describe a history that is gone."""
    usage_floor_seq: int = 0
    """Usage rows up to here were recorded before the last history rewrite; reading one of them as
    the current prompt size is what made a compaction fire again on the very next turn."""
    steer_seen: list[str] = field(default_factory=list)
    """Ids of the steers the running engine was handed at its last reload, in order. What is written
    back is judged against these: an id that was never handed over was enqueued mid-round and is
    still waiting, and an id that was handed over and not returned is one the model has read."""
    follow_up_seen: list[str] = field(default_factory=list)
    """The same for the follow-up queue."""
    steer_withdrawn: set[str] = field(default_factory=set)
    """Ids taken back since that reload. A reload already waiting on the database returns the row as
    it was before the removal, so it is filtered through this on the way into the engine; cleared at
    the next persist, by which time the store and the engine agree."""

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


def _ensure_inbox(workspace: Path, project: Project | None) -> None:
    """Make the session's inbox — but never make a project root that is not there.

    ``mkdir(parents=True)`` on an unmounted root creates the whole path, and the mount point is then
    a local empty folder that answers ``reachable`` for ever after: the "not mounted — mount it and
    restart" signal the whole Docker story rests on would be erased by loading the session. On a
    removable or network mount it is worse than that, because the real folder is shadowed by the
    empty one when it comes back. A folder the operator added is theirs to create.
    """
    if project is not None and not project.reachable:
        return
    (workspace / "inbox").mkdir(parents=True, exist_ok=True)
    if project is not None:
        _exclude_artefacts(workspace)


# The directories a session writes into the folder it works in: the inbox files arrive in, and the
# per-tool scratch of Exec, its background jobs, the services it hosts and the snapshots. In a
# workspace of the session's own they are the whole of the directory. In a project they land in the
# operator's repository, where they have no business showing up in `git status` or being swept into a
# commit by `git add -A`.
def _steer_cards(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The waiting steers as the composer draws them: a recognisable amount of each, and not all of them."""
    cards: list[dict[str, Any]] = []
    for item in items:
        text = str(item.get("text") or "")
        if not text.strip():
            continue
        if len(cards) >= STEER_CARD_LIMIT:
            break
        card: dict[str, Any] = {"id": str(item.get("id") or ""), "text": text[:STEER_CARD_CHARS], "queued_at": item.get("queued_at")}
        if len(text) > STEER_CARD_CHARS:
            card["truncated"] = True
        cards.append(card)
    return cards


SESSION_ARTEFACTS = ("inbox/", ".exec/", ".jobs/", ".services/", ".checkpoints/", ".agents/")
_EXCLUDE_MARKER = "# daedalus: what an agent working in this folder writes into it"


def _exclude_artefacts(root: Path) -> None:
    """Keep the agent's own directories out of the operator's git status.

    ``.git/info/exclude`` rather than ``.gitignore``: the ignore file is the repository's and is
    committed, and a project is somebody else's repository — this is our note to their checkout, not
    a change to their project. A root that is not a git repository has nothing to write and nothing
    to worry about.
    """
    info = root / ".git" / "info"
    if not (root / ".git").is_dir():
        return
    try:
        info.mkdir(parents=True, exist_ok=True)
        path = info / "exclude"
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if _EXCLUDE_MARKER in current:
            return
        prefix = "" if not current or current.endswith("\n") else "\n"
        path.write_text(current + prefix + _EXCLUDE_MARKER + "\n" + "".join(f"/{name}\n" for name in SESSION_ARTEFACTS), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not write %s: %s", info / "exclude", exc)


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
        self.sessions = SqliteSessionStore(db, view=TranscriptViewBuilder())
        self.runs = SqliteRunStore(db)
        self.events = SqliteEventStream(db)
        self.usage = SqliteUsageSink(db)
        self.live = LiveControlStore(db)
        # The installation's own folders are not projects, and neither is the whole home folder:
        # a project root is reachable to every agent in it and is an open root to the policy.
        self.projects = ProjectStore(
            db,
            managed_root=settings.workspaces_dir,
            reserved=[Path(p) for p in settings.sandbox_never_writable] + [settings.secrets_dir, settings.workspaces_dir],
            home=Path.home(),
        )
        self.checkpoint_retention = CheckpointRetention(db, workspaces_dir=settings.workspaces_dir, busy=self.busy_sessions, occupants=self.store_occupants)
        self.blobs = FileBlobStore(settings.blobs_dir)
        self.memory = PersistentMemory(db)
        self.workspace_units = PersistentWorkspace(db)
        self.skills = DirectorySkillStore(settings.skills_dir)
        self.redactor = redact.shared()
        self.capabilities = capabilities.resolve(settings, config)
        """What this installation can do, decided once: the tools, the routes, the prompt and the
        app all read the same answer, and a configuration change reaches them on the next start."""
        capabilities.publish(self.capabilities, settings.state_dir)
        """And the supervisor reads it from there rather than resolving it a second time."""
        self._configure_redactor(settings, config)
        self.hooks = DaedalusHookManager(self.redactor, hooks_config=lambda: self.config.hooks)
        self._background: set[asyncio.Task[Any]] = set()
        self._jobs: dict[str, dict[str, Any]] = {}
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
        self.delete_hooks: list[Callable[[str], Awaitable[None]]] = []
        """Called with the session id before a session is removed (extensions release what they hold for it)."""
        """Callbacks the transport layer installs: send_file, spawn_agent, schedule, self_*."""
        self.prompt_hooks: list[Callable[[str, str], Awaitable[str]]] = []
        """``(session_id, text) -> text`` applied to a message that starts a new run (fired reminders ride along)."""
        self.idle_work = asyncio.Lock()
        self.run_started_hooks: list[Callable[[str, str], Awaitable[None]]] = []
        """``(session_id, run_id)`` after a run was actually created — the point where a prompt hook's side effects may be committed."""
        self.shutting_down = False
        self.recovering = True
        """True from construction until boot recovery has decided the fate of every run the previous process left behind."""
        self.unloadable_sessions: dict[str, str] = {}
        """Sessions whose stored directory no longer resolves inside their project, by id, with the
        reason. ``get_state`` puts one here instead of raising through whoever asked for it; the entry
        is dropped the moment the session opens again, so what is here is what is broken now."""
        self.stale_runs: list[str] = []
        """Runs the previous process was driving that this one could not pick up — no snapshot to resume
        from, so the row is closed as cancelled. Empty on a clean stop; a line in the operator's inbox
        when it is not, because a run that simply vanishes is the thing they would otherwise go looking
        for in the logs."""
        self.index_rebuild: dict[str, int] | None = None
        """How far the search index still has to be rebuilt, while it is being rebuilt. A search that
        answers "nothing" from an index that has not reached those rows yet reads as "that was never
        said" — to the operator and to the agent asking it of its own history."""
        self.budget_flag = settings.state_dir / "BUDGET_EXCEEDED"
        self._capped_runs: set[str] = set()
        """Runs already stopped at the per-run cap (the stop is cooperative; the notice fires once)."""

    # -- lifecycle ------------------------------------------------------------------

    async def start(self, *, recovering: bool | None = None) -> None:
        """Open the stores; ``recovering`` (default: whether a previous process left runs behind) gates new runs until resume_unfinished()."""
        await self.memory.load()
        await self.workspace_units.load()
        # The policy is built inside a tool call and cannot wait on a query; this is where the project
        # roots it compares against are read — and where a folder of our own that is not on disk is
        # put back, so the first run after a start is not the thing that discovers it missing.
        await self.projects.ensure_roots()
        # New runs wait until resume_unfinished() has continued what the previous process left behind;
        # a process that finds nothing to resume (tests, a first start) opens the gate at once.
        self.recovering = recovering if recovering is not None else bool(await self.events.unfinished_snapshots())
        self.stale_runs = []
        # A tool this installation cannot honour is not registered at all: an unusable name in the
        # list is an invitation the model accepts and a failure it cannot understand.
        disabled = self.capabilities.selfdev.disabled_tools
        for tool in discover_tools():
            if tool.name in disabled:
                continue
            self.tools.register(tool)
        for tool in build_memory_tools(self.memory):
            self.tools.register(tool)
        self.tools.register(AskUserTool())
        self.service_hooks.setdefault("mcp", self.mcp_service)
        locator.default = None
        self.index_rebuild = None
        self._backfill_task = asyncio.create_task(self._backfill_index(), name="transcript-index")
        self._backfill_task.add_done_callback(_log_task_failure)
        # Its own task, not a background write: the flush that waits for those would wait for a day.
        self._price_task = asyncio.create_task(self._refresh_prices_daily(), name="price-refresh")
        self._price_task.add_done_callback(_log_task_failure)
        logger.warning("tools registered: %s", ", ".join(sorted(t.name for t in self.tools.list_all())))

    async def _backfill_index(self) -> None:
        def progress(done: int, total: int) -> None:
            self.index_rebuild = None if done >= total else {"done": done, "total": total}

        try:
            indexed = await self.sessions.backfill_transcript_index(progress)
        finally:
            self.index_rebuild = None
        if indexed:
            logger.warning("transcript search index: %d older turns indexed", indexed)

    async def close(self) -> None:
        """Shut down keeping every active run resumable (snapshots stay in place)."""
        self.shutting_down = True
        # What a finished run still owes is a task of its own now, and a task is something a shutdown
        # can kill. It used to run inside the run, which had already ended by the time this looked at
        # it, so nothing was ever lost here. So it is given its moment before anything is cancelled:
        # the snapshot it is taking is the one an undo of that turn depends on, and the callbacks
        # behind it are an answer somebody is waiting for on another front.
        grace = self.config.ops.shutdown_grace_seconds
        housekeeping = [s.housekeeping for s in self._states.values() if s.housekeeping is not None and not s.housekeeping.done()]
        if housekeeping and grace > 0:
            _done, unfinished = await asyncio.wait(housekeeping, timeout=grace)
            if unfinished:
                logger.warning("%d run(s) were still being written down after %.0f s; the shutdown cancels them", len(unfinished), grace)
        tasks = [t for s in self._states.values() for t in (s.task, s.outage_task, s.housekeeping) if t and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        pending = [t for s in self._states.values() for t in s.persist_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)  # the last history write must land
        await self.flush_background()
        await self.hooks.aclose()
        for task in (getattr(self, "_backfill_task", None), getattr(self, "_price_task", None)):
            if task is not None and not task.done():
                task.cancel()
                # Bounded, because a cancelled task is not a finished one: the price refresh is
                # inside an HTTP client when it is cancelled, and closing a connection whose peer has
                # gone waits for a FIN that never arrives. Neither of these owes anything to disk, so
                # a shutdown that gives up on one loses nothing and a shutdown that waits for one
                # stops the process from ever exiting.
                with suppress(TimeoutError):
                    async with asyncio.timeout(BACKGROUND_SHUTDOWN_SECONDS):
                        await asyncio.gather(task, return_exceptions=True)
        await self.mcp.close()
        await self.providers.aclose()

    def budget_exceeded(self) -> str | None:
        if self.budget_flag.exists():
            return self.budget_flag.read_text(encoding="utf-8").strip()
        return None

    def provider_costs_nothing(self, provider_id: str | None) -> bool:
        """Whether dollar caps are irrelevant to this endpoint by definition."""
        if not provider_id:
            return False
        try:
            return self.providers.get(provider_id).endpoint.kind == "llamacpp"
        except KeyError:
            return False

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
        _, default = self.config.preset()  # NoModelConfigured when the table is empty
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
        self._apply_tool_visibility(state)
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

    def workspace_of(self, session_id: str, metadata: dict[str, Any], project: Project | None) -> Path:
        """Return the project root or the session's project-relative private directory."""
        if project is None:
            raise RuntimeError(f"session {session_id} has no project")
        relative = str(metadata.get("directory") or "").strip()
        if not relative:
            return project.root
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise RuntimeError(f"session {session_id} has an invalid project directory")
        target = Path(os.path.normpath(project.root / candidate))
        if project.root != target and project.root not in target.parents:
            raise RuntimeError(f"session {session_id} has a directory outside its project")
        return target

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
        project_id: str | None = None,
        own_directory: bool = False,
    ) -> SessionState:
        sid = session_id or uuid.uuid4().hex[:12]
        project = await self.projects.get(project_id) if project_id else None
        if project_id and project is None:
            raise KeyError(project_id)
        meta = dict(metadata or {})
        if project is None:
            project = (
                await self.projects.adopt_directory(title, workspace)
                if workspace is not None
                else await self.projects.create(title, settings=ProjectSettings(snapshots=True), project_id=sid)
            )
        if project_id is None and workspace is None:
            await self.db.execute("UPDATE projects SET settings = json_set(settings, '$.auto_created', json('true')) WHERE id = ?", (project.id,))
        if own_directory:
            meta["directory"] = f".agents/{sid}"
        else:
            meta.pop("directory", None)
        workspace = self.workspace_of(sid, meta, project)
        _ensure_inbox(workspace, project)
        session = Session(id=sid, tenant_id=TENANT, title=title, metadata=dict(meta))
        await self.sessions.create(session, project_id=project.id)
        state = SessionState(session=session, workspace=workspace, metadata=dict(meta), project=project)
        self._states[sid] = state
        self.register_services(state)
        return state

    def live_state(self, session_id: str) -> SessionState | None:
        """The state of a session this process already holds; ``None`` for one it would have to load."""
        return self._states.get(session_id)

    async def project_of(self, session_id: str) -> Project | None:
        """The project a session works in, for anything that makes a child of it.

        Every path that creates a session from another one — a subagent, a spawned agent, a fork, a
        scheduled task, the voice concierge's delegate — asks this and passes the answer on. A child
        that did not would carry the parent's directory without the parent's wall, which is the whole
        of the boundary gone through the commonest way of making a new worker.
        """
        state = self._states.get(session_id)
        if state is not None:
            return state.project
        return await self.projects.for_session(session_id)

    async def get_state(self, session_id: str) -> SessionState | None:
        state = self._states.get(session_id)
        if state is not None:
            return state
        try:
            session = await self.sessions.get(session_id, TENANT)
        except Exception:
            return None
        project = await self.projects.for_session(session_id)
        try:
            workspace = self.workspace_of(session_id, dict(session.metadata), project)
            _ensure_inbox(workspace, project)
        except (RuntimeError, OSError) as exc:
            # The session's stored directory no longer resolves inside the project that holds it, or
            # the folder cannot be made. Loading a session happens on every path into this process —
            # boot recovery, the scheduler restoring its in-flight runs, a service being reconciled —
            # and most of those callers already treat "no such session" as an outcome. Raising here
            # made one bad row in one session fatal to all of them; it is reported and skipped instead,
            # and ``unloadable_sessions`` is what the doctor and the operator read afterwards.
            self.unloadable_sessions[session_id] = str(exc)
            logger.warning("session %s cannot be opened: %s", session_id, exc)
            return None
        self.unloadable_sessions.pop(session_id, None)
        state = SessionState(session=session, workspace=workspace, metadata=dict(session.metadata), project=project)
        self._states[session_id] = state
        self.register_services(state)
        return state

    async def transcript(self, session_id: str, *, tail: int = 0, before: int = 0) -> list[Message]:
        """Display history: the durable transcript plus whatever the live engine has not persisted yet.

        ``tail`` and ``before`` are pushed into SQL: a page costs the page. Only a caller that
        passes neither reads the whole session, and the live engine's unpersisted messages are
        merged in only for the newest page, which is the only page they can belong to.
        """
        rows = await self.sessions.list_transcript(session_id, limit=tail, before_seq=before)
        if not rows and not before:
            # Sessions from before the transcript existed: seed it from the working history.
            lo, _ = await self.sessions.transcript_bounds(session_id)
            if not lo:
                history = list(await self.sessions.list_messages(session_id, TENANT, limit=10_000))
                if history:
                    await self.sessions.append_transcript(session_id, history)
                    rows = list(history)[-tail:] if tail > 0 else history
        if before:
            return rows
        live = await self._unpersisted(session_id, {self.sessions.transcript_key(m) for m in rows})
        if not live:
            return rows
        rows = rows + live
        return rows[-tail:] if tail > 0 else rows

    async def transcript_page(self, session_id: str, *, tail: int = 600, before: int = 0) -> list[dict[str, Any]]:
        """The page as the app draws it: stored views for what is persisted, built on the spot for what is not.

        Nothing here parses a message or runs a redaction pattern for a row that was already
        written — that was done once, when the row was appended.
        """
        views = await self.sessions.list_transcript_views(session_id, limit=tail, before_seq=before)
        if not views and not before:
            seeded = await self.transcript(session_id, tail=tail)  # legacy session: seeds the transcript from the history
            if seeded:
                views = await self.sessions.list_transcript_views(session_id, limit=tail)
        if before:
            return views
        live = await self._unpersisted(session_id, set())  # a view carries no key: the transcript is asked instead
        if not live:
            return views
        # The transcript is written by a task of its own, so during a run the newest message or two
        # are not in it yet and have no row number. A view without one reads to the app as a hole in
        # the history and costs it a re-read of the whole session on every event. So each gets the
        # number its row will have when it is written, and is marked as live: the app matches the
        # settled rows by number and takes the live tail as it comes.
        base = int(views[-1].get("seq") or 0) if views else 0
        views = views + [{**message_view(m), "seq": base + n, "live": True} for n, m in enumerate(live, start=1)]
        return views[-tail:] if tail > 0 else views

    async def _unpersisted(self, session_id: str, known_keys: set[str]) -> list[Message]:
        """What the live engine holds and the transcript does not yet, in transcript order.

        Against one page alone the dedup would re-show every live message older than it, so
        what the page does not settle is asked of the transcript by key: one indexed query,
        and the answer does not depend on how much of the session was read.
        """
        state = self._states.get(session_id)
        if state is None or state.engine is None or not state.running:
            return []
        candidates = [m for m in state.engine.history if self.sessions.transcript_key(m) not in known_keys]
        if candidates:
            persisted = await self.sessions.transcript_keys_present(session_id, [self.sessions.transcript_key(m) for m in candidates])
            candidates = [m for m in candidates if self.sessions.transcript_key(m) not in persisted]
        # A queued steer or follow-up is written to the transcript when it is submitted; the core
        # later places the same text into its history as a fresh user message with a timestamp of
        # its own, which the key-based dedup cannot recognise. Marking live user messages the host
        # did not write itself as the core's is what the persisted sync (from_history) does, and it
        # keeps the operator's words from appearing twice while the run is still going.
        return [
            m.model_copy(update={"metadata": {**m.metadata, "daedalus.origin": "core"}})
            if m.role is MessageRole.user and "daedalus.origin" not in m.metadata and not m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)
            else m
            for m in candidates
        ]

    async def list_sessions(self, limit: int = 100, *, ids: list[str] | None = None) -> list[dict[str, Any]]:
        rows = await self.sessions.list_sessions(TENANT, limit=limit, ids=ids)
        projects = await self.projects.by_session()
        out: list[dict[str, Any]] = []
        for session in rows:
            state = self._states.get(session.id)
            status = "idle"
            if state is not None:
                if state.running:
                    status = "running"
                elif state.pending is not None:
                    status = "waiting"
                elif state.compacting is not None:
                    status = "compacting"
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
                    "project_id": projects.get(session.id),
                }
            )
        return out

    async def delete_session(self, session_id: str, *, delete_workspace: bool = True) -> bool:
        """Remove a session entirely: its run, records, events and (optionally) its workspace."""
        state = await self.get_state(session_id)
        if state is None:
            return False
        for hook in self.delete_hooks:
            try:
                await hook(session_id)
            except Exception:  # noqa: BLE001
                logger.exception("delete hook failed for %s", session_id)
        if state is None:
            return False
        if state.running and state.engine is not None:
            state.engine.stop()
            if state.task is not None:
                state.task.cancel()
                await asyncio.gather(state.task, return_exceptions=True)
        # Everything the last run still owes is about to be deleted along with the session; a
        # snapshot row written after the rows are gone would belong to a session that does not exist.
        if state.housekeeping is not None and not state.housekeeping.done():
            state.housekeeping.cancel()
            await asyncio.gather(state.housekeeping, return_exceptions=True)
        self._states.pop(session_id, None)
        locator.unregister(session_id)
        for job in self._jobs.pop(session_id, {}).values():
            process = getattr(job, "process", None)
            if process is not None and process.returncode is None:
                try:
                    os.killpg(process.pid, 9)
                except (ProcessLookupError, PermissionError):
                    pass
        runs = await self.db.fetchall("SELECT id FROM runs WHERE session_id = ?", (session_id,))
        async with self.db.transaction() as conn:
            for row in runs:
                await conn.execute("DELETE FROM events WHERE run_id = ?", (row["id"],))
                await conn.execute("DELETE FROM snapshots WHERE run_id = ?", (row["id"],))
            await conn.execute("DELETE FROM runs WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
            # The index points at transcript rows by number; it goes with them, or it answers for rows that are gone.
            await conn.execute("DELETE FROM transcript_fts WHERE rowid IN (SELECT seq FROM transcript WHERE session_id = ?)", (session_id,))
            await conn.execute("DELETE FROM transcript WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM live_control WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM pending_questions WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM topics WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM checkpoint_retention WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM verifications WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM learning_records WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            project = state.project
            if project is not None and project.root != self.settings.workspaces_dir and project.root.is_relative_to(self.settings.workspaces_dir):
                # Remember ownership for older projects too, even when their creator goes first.
                if project.id == session_id:
                    await conn.execute("UPDATE projects SET settings = json_set(settings, '$.auto_created', json('true')) WHERE id = ?", (project.id,))
                await conn.execute(
                    "DELETE FROM projects WHERE id = ? AND system = '' AND json_extract(settings, '$.auto_created') = 1 "
                    "AND NOT EXISTS (SELECT 1 FROM sessions WHERE project_id = projects.id)",
                    (project.id,),
                )
        await self.projects.list()
        # A private child belongs to this session. A project root never goes with a session.
        if delete_workspace and state.metadata.get("directory") and state.workspace.exists() and state.project is not None and state.workspace.is_relative_to(state.project.root):
            if await self.workspace_users(state.workspace):
                logger.warning("session %s deleted; its workspace stays, other sessions work in it", session_id)
            else:
                shutil.rmtree(state.workspace, ignore_errors=True)
        return True

    async def workspace_users(self, workspace: Path) -> list[dict[str, str]]:
        """The sessions that work in ``workspace``: a project's, their own directory, or one they were attached to."""
        target = workspace.resolve()
        projects = {p.id: p for p in await self.projects.list()}
        out: list[dict[str, str]] = []
        for row in await self.db.fetchall("SELECT id, title, metadata, project_id FROM sessions"):
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (ValueError, AttributeError):
                metadata = {}
            project = projects.get(row["project_id"])
            if project is None:
                continue
            path = self.workspace_of(row["id"], metadata, project)
            if path.resolve() == target:
                out.append({"id": row["id"], "title": row["title"]})
        return out

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
            # A summary replaces the history of the turn whose snapshot may still be being written.
            await self._wait_until_quiet(state)
            return await self._compact_locked(state, instructions, keep_recent=keep_recent)

    async def _maybe_auto_compact(self, state: SessionState, *, required_only: bool = False) -> None:
        """When the last prompt filled ``compaction.auto_ratio`` of the window, compact the history.

        This runs after a run settles, between turns, because a compaction is a summariser call
        that takes a minute or two and nobody should be waiting on it. ``required_only`` is the
        exception on the way into a run: a history that no longer fits the window at all cannot
        be sent, so that one is compacted before the run rather than refused by the provider.
        """
        cfg = self.config.compaction
        if cfg.auto_ratio <= 0 or state.pending is not None or self.shutting_down:
            return
        ratio = 1.0 if required_only else cfg.auto_ratio

        def below(status: dict[str, Any]) -> bool:
            return not status["window"] or status["tokens"] < ratio * status["window"] or status["messages"] < cfg.min_messages

        if below(await self.context_status(state)):
            return
        async with state.lock:
            # Asked again behind the lock: two callers that both saw a full window — a manual compaction
            # racing this one, or the check on the way into a run racing the one after a run — serialise
            # here, and the second would otherwise summarise a history the first has already replaced.
            status = await self.context_status(state)
            if below(status) or (state.running and state.task is not asyncio.current_task()):
                return  # a run started while this waited: it compacts after that one instead
            try:
                started = time.monotonic()
                await self._compact_locked(state, "", keep_recent=cfg.keep_recent_messages, reason="auto", own_task_ok=True)  # own_task_ok: the required_only call runs inside the run's own task
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
        compact_provider, _compact_model = await self._compaction_rung(state)
        if exceeded and not self.provider_costs_nothing(compact_provider.endpoint.id):
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
        async with self.idle_work:
            state.compacting = {"reason": reason, "stage": "summarising", "messages": len(history), "parts_done": 0, "parts_total": 0, "started_at": datetime.now(UTC).isoformat()}
        await self._compaction_progress(state)
        try:
            return await self._compact_progressing(state, history, tail, instructions, reason, own_task_ok=own_task_ok)
        finally:
            state.compacting = None
            await self._compaction_progress(state)

    async def _compaction_progress(self, state: SessionState, **fields: Any) -> None:
        """Advance the compaction's public state and tell the session's listeners (the Mini App stream)."""
        if state.compacting is not None:
            state.compacting.update(fields)
        event = TurnEvent(
            type=EventType.STATE_CHANGED,
            run_id=state.run_id or "",
            payload={"from": "idle", "to": "compacting" if state.compacting else "idle", "reason": "compaction_progress", "compacting": state.compacting},
        )
        await self._notify_sinks(state.session.id, event)

    async def _notify_sinks(self, session_id: str, event: TurnEvent | HostEvent) -> None:
        """Hand an event to the session's listeners without booking it as a run event.

        ``_dispatch_event`` is the path for everything the loop produces: it redacts, times, and
        writes to the durable log. What the host itself raises about a session — a compaction's
        progress, a change to the steer queue — has no place in that log and takes this door.
        """
        for sink in self._sinks:
            try:
                await sink(session_id, event)
            except Exception:  # noqa: BLE001
                logger.exception("event sink failed")

    async def _compaction_rung(self, state: SessionState) -> tuple[Any, str]:
        session_id = state.session.id
        rungs, _ = self.resolve_model(await self.live.load(session_id))
        provider, model = rungs[0]  # the session's own model summarises its own history …
        if self.config.compaction.preset and self.config.compaction.preset in self.config.presets:
            try:
                provider, model = self.providers.rungs_for(self.config, self.config.compaction.preset)[0]  # … unless a cheaper one is configured for it
            except Exception:  # noqa: BLE001 — an unusable compaction preset falls back to the session's model
                logger.warning("compaction preset %r is not usable; summarising with the session's model", self.config.compaction.preset)
        return provider, model

    async def _compact_progressing(self, state: SessionState, history: list[Message], tail: list[Message], instructions: str, reason: str, *, own_task_ok: bool) -> str:
        session_id = state.session.id
        provider, model = await self._compaction_rung(state)
        language = self.config.answer_language if self.config.answer_language != "auto" else operator_language(history)
        observability = LLMObservabilityContext(tenant_id=TENANT, session_id=session_id, run_id=state.run_id, call_purpose="compaction", call_category="compaction")
        summary = await self._summarise_history(provider, model, history, language=language, instructions=instructions, observability=observability, progress=lambda **f: self._compaction_progress(state, **f))
        await self._compaction_progress(state, stage="writing")
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
        await self._reset_observed_prompt(state, before=[*history, *tail], after=rebuilt)
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
        _forget_persisted(state)
        await self.sessions.append_transcript(session_id, [message])
        return summary

    async def _reset_observed_prompt(self, state: SessionState, *, before: Sequence[Message], after: Sequence[Message]) -> None:
        """Re-estimate the prompt for the history that now exists, and retire the measurements of the one that does not.

        The trigger compares the last prompt the provider counted against the window. A
        compaction does not make a call, so that count still described the history it just
        replaced — and the next turn compacted again, summarising the kept tail for forty to
        eighty seconds and changing nothing. Nearly half of the compactions in the log were
        that. The new size is the measured one scaled by how much of the history survived.
        Scaling rather than re-counting is deliberate: the estimator's calibration cancels out
        of the ratio, so the answer errs only in the direction that costs nothing — a little
        low, which delays the next compaction by a turn at most, where a little high would pay
        for one that compacts nothing all over again.
        """
        row = await self.db.fetchone("SELECT coalesce(max(seq), 0) seq FROM usage_events WHERE session_id = ?", (state.session.id,))
        measured = int((await self.context_status(state))["tokens"])
        state.usage_floor_seq = int(row["seq"]) if row else 0
        was = history_tokens(before)
        scaled = round(measured * history_tokens(after) / was) if was else 0
        # A rewrite that did not shrink the history says nothing about the prompt getting smaller.
        state.observed_prompt_tokens = min(measured, scaled)

    async def _summarise_history(self, provider: Any, model: str, history: Sequence[Message], *, language: str, instructions: str, observability: LLMObservabilityContext, progress: Callable[..., Awaitable[None]] | None = None) -> str:
        """One structured summary of ``history``: a single call, or parallel part summaries merged when the transcript is long.

        ``progress`` hears how many parts there are and each one finishing, then the merge; a
        compaction of a long history takes minutes and the operator watches it."""

        async def report(**fields: Any) -> None:
            if progress is not None:
                await progress(**fields)

        cfg = self.config.compaction
        focus = f"\n\nThe operator asks to focus on: {instructions.strip()}" if instructions.strip() else ""
        transcript_text = transcript_for_summary(history)
        parts = split_transcript(transcript_text, cfg.chunk_tokens)
        await report(parts_total=len(parts), parts_done=0, stage="summarising")
        if len(parts) == 1:
            return await self._summary_call(provider, model, COMPACT_PROMPT.format(language=language, max_words=cfg.max_words) + focus, transcript_text, observability)
        part_words = max(300, cfg.max_words // 2)
        done = 0

        async def part_summary(index: int, part: str) -> str:
            nonlocal done
            text = await self._summary_call(provider, model, CHUNK_PROMPT.format(index=index + 1, total=len(parts), language=language, max_words=part_words) + focus, part, observability, strict=False)
            done += 1
            await report(parts_done=done)
            return text

        partials = await asyncio.gather(*(part_summary(i, part) for i, part in enumerate(parts)))
        joined = "\n\n".join(f"<part {i + 1}>\n{text}\n</part {i + 1}>" for i, text in enumerate(partials) if text)
        await report(stage="merging")
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
        """Snapshot the workspace (bounded by ``ops.checkpoint_max_gb``); returns the commit id or None.

        A project is not snapshotted unless the operator asked for it. A session workspace holds what
        one agent made; a project root is the operator's own repository, and committing it into
        ``.checkpoints`` before every turn and after every run is a cost the undo does not repay on a
        tree that already has a history of its own. Switched on in the project's settings, it behaves
        exactly as a workspace does, size cap included.
        """
        if state.project is not None and not state.project.settings.snapshots:
            return None
        limit = self.config.ops.checkpoint_max_gb
        try:
            # One walk answers both the size cap and the excludes; it used to be three.
            scan = await asyncio.to_thread(scan_workspace, state.workspace)
            if limit and scan.size > limit * 1e9:
                if not state.checkpoint_capped:
                    state.checkpoint_capped = True
                    logger.warning("session %s: workspace exceeds ops.checkpoint_max_gb=%s; no snapshots, revert restores the history only", state.session.id, limit)
                return None
            state.checkpoint_capped = False
            sha = await Checkpoints(state.workspace).snapshot(f"{kind} seq={seq} run={run_id}", scan=scan)
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

    async def list_checkpoints(self, session_id: str) -> dict[str, Any]:
        """The snapshots this session can still be put back to, and what retention took away.

        The app asks before it offers an undo: a turn whose snapshot has been dropped would revert
        the history and quietly leave the files where they are, which is not what "revert" reads as.
        """
        rows = await self.db.fetchall("SELECT seq, run_id, kind, sha, at FROM checkpoints WHERE session_id = ? ORDER BY id", (session_id,))
        cut = await self.db.fetchone("SELECT removed_before, removed, at FROM checkpoint_retention WHERE session_id = ?", (session_id,))
        ops = self.config.ops
        return {
            "checkpoints": [{"seq": r["seq"], "run_id": r["run_id"], "kind": r["kind"], "sha": r["sha"], "at": r["at"]} for r in rows],
            "total": len(rows),
            "pruned": cut is not None,
            "pruned_before": cut["removed_before"] if cut else None,
            "removed": int(cut["removed"]) if cut else 0,
            "note": "older checkpoints were removed by retention" if cut is not None else "",
            "keep_days": ops.checkpoint_keep_days,
            "keep_last": ops.checkpoint_keep_last,
        }

    async def prune_checkpoints(self) -> RetentionReport:
        """Bring the snapshot stores inside the configured bounds; returns what the pass freed."""
        return await self.checkpoint_retention.run(RetentionBounds.from_ops(self.config.ops))

    async def revert(self, session_id: str, seq: int) -> dict[str, Any]:
        """Undo everything from the operator turn at transcript ``seq`` on: history and workspace.

        The turn must still be in the working history (not compacted away); the transcript
        keeps the undone turns and a marker says where the history now ends.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        async with state.lock:
            await self._wait_until_quiet(state)
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
            await self._reset_observed_prompt(state, before=history, after=kept)
            if state.engine is not None:
                state.engine.history = kept
                state.engine.last_observed_prompt_tokens = 0
                state.engine.compaction_state = CompactionState()
            state.history_keys = [self.sessions.transcript_key(m) for m in kept]
            await self.sessions.replace_messages(session_id, TENANT, kept)
            _forget_persisted(state)
            # Input queued during the undone turns and any run snapshot that could resume them are pre-revert by definition.
            await self.live.save_queues(session_id, [], [])
            await self.steer_changed(session_id, reason="cleared")
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

    async def clear_history(self, session_id: str) -> dict[str, Any]:
        """Start the session over with an empty working history: the workspace, the brief, the model, the
        loop and every other setting stay; the transcript keeps the old turns and a marker says they are gone.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        async with state.lock:
            await self._wait_until_quiet(state)
            pending = [t for t in state.persist_tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            history = list(state.engine.history) if state.engine is not None else list(await self.sessions.list_messages(session_id, TENANT, limit=10_000))
            if history:
                backup = state.workspace / f".history-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
                try:
                    backup.write_text("\n".join(m.model_dump_json() for m in history) + "\n", encoding="utf-8")
                except OSError:
                    logger.warning("could not write the history backup %s", backup, exc_info=True)
                await self.sessions.append_transcript(session_id, history, from_history=True)
            state.persist_gen += 1
            await self._reset_observed_prompt(state, before=history, after=[])
            if state.engine is not None:
                state.engine.history = []
                state.engine.last_observed_prompt_tokens = 0
                state.engine.compaction_state = CompactionState()
            state.history_keys = []
            await self.sessions.replace_messages(session_id, TENANT, [])
            _forget_persisted(state)
            await self.live.save_queues(session_id, [], [])
            await self.steer_changed(session_id, reason="cleared")
            for run in await self.db.fetchall("SELECT id FROM runs WHERE session_id = ?", (session_id,)):
                await self.events.delete_snapshot(run["id"])
            marker = Message(
                role=MessageRole.user,
                content_blocks=[TextBlock(text=f"[history cleared: {len(history)} message(s) left the working history; the project files and the session's settings stay]")],
                metadata={"daedalus.origin": "clear", "daedalus.clear": {"dropped": len(history)}},
            )
            await self.sessions.append_transcript(session_id, [marker])
            return {"dropped": len(history)}

    async def fork_into(self, source_id: str, seq: int, target: SessionState) -> dict[str, Any]:
        """Give ``target`` the source's history before transcript ``seq`` and the right project files.

        Turns a revert undid and turns a compaction summary already stands for are left out of
        the working history; the archived originals still go into the fork's transcript, and
        the summary is re-pointed at the fork's own seqs so HistoryExpand keeps working there.

        Two sessions in the same project share one directory and nothing is copied at all. Where a
        copy does happen it is a whole directory tree, so it is measured first and refused above
        ``ops.checkpoint_max_gb`` rather than written into the state directory unbounded.
        """
        source = await self.get_state(source_id)
        if source is None:
            raise KeyError(source_id)
        async with source.lock:
            await self._wait_until_quiet(source, what="the source session")
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
            _forget_persisted(target)
            copied = False
            if target.workspace == source.workspace:
                # Both sessions are in the same project: the folder is the operator's and there is one
                # of it. Copying it would take the repository out of the folder they chose, unbounded,
                # and leave the fork working on a stale duplicate of it.
                pass
            elif source.workspace.exists():
                scan = await asyncio.to_thread(scan_workspace, source.workspace)
                limit = self.config.ops.checkpoint_max_gb
                if limit and scan.size > limit * 1e9:
                    raise RuntimeError(
                        f"{source.workspace} is {scan.size / 1e9:.1f} GB, over ops.checkpoint_max_gb={limit}; "
                        "a fork copies the whole directory, so this one is refused rather than written twice"
                    )
                await asyncio.to_thread(shutil.copytree, source.workspace, target.workspace, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".agents"))
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
        return {"messages": len(history), "workspace_copied": copied, "workspace_shared": target.workspace == source.workspace}

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
        # A system project's folder lives under the workspaces root and is not a workspace nobody
        # refers to: the concierge's agents work in it, and a sweep would take their files with it.
        for project in await self.projects.list():
            with suppress(OSError):
                known_paths.add(project.root.resolve())
        out: list[Path] = []
        if not self.settings.workspaces_dir.exists():
            return out
        for entry in sorted(self.settings.workspaces_dir.iterdir()):
            if not entry.is_dir() or entry.name in known or any(entry.resolve() == path or entry.resolve() in path.parents for path in known_paths):
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

    async def reload_project(self, project: Project | None, project_id: str) -> None:
        """Refresh the editable project value held by loaded sessions."""
        for state in self._states.values():
            if state.project is None or state.project.id != project_id:
                continue
            state.project = project
            state.workspace = self.workspace_of(state.session.id, dict(state.session.metadata), project)
            with suppress(OSError):
                _ensure_inbox(state.workspace, project)
            self.register_services(state)

    async def attach_project(self, session_id: str, project: Project, *, own_directory: bool = False) -> SessionState:
        """Move a session to a project root or to its private child in that project."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        metadata = dict(state.session.metadata)
        if own_directory:
            metadata["directory"] = f".agents/{session_id}"
        else:
            metadata.pop("directory", None)
        await self.sessions.update_metadata(session_id, metadata)
        await self.projects.attach(session_id, project.id)
        state.session.metadata.clear()  # the Session model is frozen; its dict is the thing that is kept
        state.session.metadata.update(metadata)
        state.metadata = dict(metadata)
        state.project = project
        state.workspace = self.workspace_of(session_id, metadata, project)
        with suppress(OSError):
            _ensure_inbox(state.workspace, project)
        self.register_services(state)
        return state

    @staticmethod
    def _settling(state: SessionState) -> bool:
        """The run is over but its snapshot is not on disk yet.

        Not a run — the app draws the session as idle and the next message may be typed into it —
        but a restart or a retention pass landing here would still take the snapshot away from the
        turn that just ended, so everything that asks "is anything going on in this session" is told
        yes until the files are safe.
        """
        return not state.settled.is_set()

    async def _wait_until_quiet(self, state: SessionState, *, what: str = "the session") -> None:
        """Hold until nothing else is writing this session's history or its files.

        One test for every way a session can be rewritten from underneath — undo, clear, fork,
        compaction — because they all depend on the same two things being finished: the history of
        the last turn, and the snapshot of the workspace as that turn left it. A run in flight and a
        question outstanding are refused, as they always were. The third state is the one this
        exists for: the run is over, the app draws the session as idle, and the snapshot of the very
        turn an undo would undo is still being written. That one is waited for rather than refused —
        it is a second or two, and the operator pressed the button on a session that looked idle
        because it *is* idle; what is behind it is ours to finish, not theirs to retry around.

        The wait is bounded (``ops.settle_wait_seconds``): a snapshot on a mount that never answers
        would otherwise wedge the session with nothing on the screen to say why.
        """
        if state.running or state.pending is not None:
            raise RuntimeError(f"{what} is busy; stop the run (or answer the question) first")
        if self._settling(state):
            try:
                async with asyncio.timeout(self.config.ops.settle_wait_seconds):
                    await state.settled.wait()
            except TimeoutError:
                raise RuntimeError(f"{what} is still saving the last turn; nothing may rewrite it until that finishes") from None
            # Nothing here holds ``submit``: a run may have started in the moment we waited.
            if state.running or state.pending is not None:
                raise RuntimeError(f"{what} is busy; stop the run (or answer the question) first")

    def running_run_ids(self) -> set[str]:
        return {s.run_id for s in self._states.values() if s.run_id and (s.running or s.pending is not None or self._settling(s))}

    def busy_sessions(self) -> set[str]:
        """Sessions nothing may be rewritten under: a turn in flight, a question outstanding, or a
        finished turn still being written down — the same test ``revert``, ``clear_history``,
        ``fork`` and ``compact`` wait on."""
        return {sid for sid, state in self._states.items() if state.running or state.pending is not None or self._settling(state)}

    def active_sessions(self) -> set[str]:
        """Sessions the operator would call working: a turn in flight or a question outstanding.

        Narrower than :meth:`busy_sessions` on purpose, and the difference is the point. A session
        whose run has ended and whose snapshot is still being written may not be rewritten — so the
        refusals ask the wider question — but it is not *working*, and a list that draws it as
        running contradicts its own screen, which says idle.
        """
        return {sid for sid, state in self._states.items() if state.running or state.pending is not None}

    def store_occupants(self) -> dict[Path, set[str]]:
        """Which sessions this process holds open in each snapshot store, by the store's own directory.

        Retention reads the ``checkpoints`` rows to find the stores, which is every session that has
        taken a snapshot — and not the one that has not taken its first yet. A subagent sharing its
        leader's workspace is exactly that session, and its before-turn snapshot is a ``git add`` into
        the chain a pass would otherwise consider idle. This is read from the sessions the process
        actually holds, so a store is busy from the moment one of them is loaded in it.
        """
        out: dict[Path, set[str]] = {}
        for session_id, state in self._states.items():
            out.setdefault(state.workspace / CHECKPOINT_DIR_NAME, set()).add(session_id)
        return out

    def register_services(self, state: SessionState) -> None:
        hooks = self.service_hooks
        services = SessionServices(
            session_id=state.session.id,
            workspace_dir=state.workspace,
            protected_paths=self.protected_paths(),
            tool_timeout_seconds=self.config.limits.tool_timeout_seconds,
            max_tool_output_chars=self.config.tools.exec.max_output_chars,
            send_file=_bind(hooks.get("send_file"), state.session.id),
            spawn_agent=_bind(hooks.get("spawn_agent"), state.session.id),
            schedule=hooks.get("schedule"),
            self_propose=hooks.get("self_propose"),
            self_apply=hooks.get("self_apply"),
            self_rebuild=hooks.get("self_rebuild"),
            self_rollback=hooks.get("self_rollback"),
            progress=_bind(hooks.get("progress"), state.session.id),
            writable=[q for p in (state.session.metadata.get("worktrees") or []) if str(p).startswith("/") for q in worktree_writable_paths(Path(str(p)))],
            # In a project, the workspace is also the wall: the sandbox binds it writable (it is
            # ``workspace_dir``) and ``resolve`` refuses everything outside it and the worktrees above.
            # For all but a session with a directory of its own that workspace IS the project's folder.
            project_root=state.workspace if state.project is not None else None,
            extra={"skill_store": self.skills, "manager": self, "vision": _LiveVision(self), "jobs": self._jobs.setdefault(state.session.id, {})},
        )
        state.services = services
        locator.register(services)

    async def open_writable(self, session_id: str, path: Path) -> None:
        """Let this session write to ``path`` under the sandbox from now on — a worktree it opened for its own changes."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        paths = [str(p) for p in (state.session.metadata.get("worktrees") or [])]
        if str(path) not in paths:
            paths.append(str(path))
            state.session.metadata["worktrees"] = paths
            state.metadata["worktrees"] = paths
            await self.sessions.update_metadata(session_id, state.session.metadata)
        if state.services is not None:
            for q in worktree_writable_paths(path):
                if q not in state.services.writable:
                    state.services.writable.append(q)

    # -- the steer queue, as the app sees it ----------------------------------------

    async def queued_steers(self, session_id: str) -> list[dict[str, Any]]:
        """The steers this session has taken in and not yet given to the model, oldest first.

        The store is the record: a run reloads the queue from it before every model call and writes
        back what it did not place. Between those two moments a running engine holds the only copy,
        so a listing taken exactly then can name an item the model is already reading; the change
        event that follows the write corrects it within the round.

        Each item comes back as a card: enough text to recognise it, with ``truncated`` set when
        there is more. Three long pasted messages otherwise travel whole to every open stream on
        every later change of the queue.
        """
        queued = await self.live.load(session_id)
        return _steer_cards(queued["steer"])

    async def drop_queued_steer(self, session_id: str, item_id: str) -> bool:
        """Take one steer back before the run reads it; ``False`` when it is already gone.

        The engine is asked first and the store second. A run between its reload and its next model
        call holds the queue in memory and would write that copy back over any store-only removal —
        emptying its list first is what actually stops the message, and the store write behind it
        stops a reload from bringing the item round again.
        """
        state = self._states.get(session_id)
        removed = False
        engine = state.engine if state is not None else None
        if engine is not None:
            queue = list(getattr(engine, "_steer_queue", []) or [])
            kept = [item for item in queue if str(item.get("id") or "") != item_id]
            if len(kept) != len(queue):
                engine._steer_queue = kept  # type: ignore[attr-defined]
                removed = True
        if await self.live.remove(session_id, "steer", item_id):
            removed = True
        if removed and state is not None:
            # A reload that was already waiting on the database when this ran will be handed the row
            # as it stood before the removal. Remembering the id here is what stops that answer from
            # putting the withdrawn message back into the engine's queue, where it is authoritative.
            state.steer_withdrawn.add(item_id)
        if removed:
            await self.steer_changed(session_id, reason="withdrawn")
        return removed

    async def steer_changed(self, session_id: str, *, reason: str) -> None:
        """Tell the session's listeners that its steer queue is not what they last drew."""
        state = self._states.get(session_id)
        waiting = [item for item in (await self.live.load(session_id))["steer"] if str(item.get("text") or "").strip()]
        await self._notify_sinks(
            session_id,
            HostEvent(
                type=HostEventType.STEER_CHANGED,
                run_id=(state.run_id if state is not None else "") or "",
                # ``count`` is the whole queue; ``queued`` is the first cards' worth of it.
                payload={"session_id": session_id, "reason": reason, "count": len(waiting), "queued": _steer_cards(waiting)},
            ),
        )

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
        async with state.submit_lock:
            if not self.config.has_model:
                # The one place every way in converges: chat, the API, a schedule, a subagent, the
                # voice concierge. None of them can start a run without a model, and all of them
                # already turn a RuntimeError here into a message the operator reads.
                raise NoModelConfigured
            if self.shutting_down:
                raise RuntimeError("the bot is stopping; the run starts after the restart")
            if self.recovering and not state.running:
                raise RuntimeError("the bot is starting up and first continues the runs it left behind; try again in a moment")
            if not state.workspace.is_dir():
                # One of ours is remade here: a folder under the managed tree is the installation's
                # to create, and a run refused because our own bookkeeping lost a directory is an
                # outage with nothing for the operator to do about it. A folder they pointed at is
                # theirs, so that one still refuses, with the path in the message.
                if state.project is not None:
                    await self.projects.ensure_reachable(state.project)
                    _ensure_inbox(state.workspace, state.project)
            if not state.workspace.is_dir():
                name = state.project.name if state.project is not None else state.session.title
                raise RuntimeError(f"the working directory for {name} ({state.workspace}) is not reachable; restore or mount it before starting a run")
            if not os.access(state.workspace, os.W_OK):
                raise RuntimeError(f"the working directory for {state.session.title} is not writable")
            body, image_refs = await self._ingest_attachments(state, text, attachments)
            if state.pending is not None:
                if as_answer:
                    # Free-text reply to a pending question counts as a custom answer.
                    return await self.answer(session_id, [{"custom": body}])
                await self.live.enqueue(session_id, "follow_up", {**new_queued_prompt("follow_up", body).to_dict(), "queued_at": datetime.now(UTC).isoformat()})
                await self.sessions.append_transcript(
                    session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], metadata={"daedalus.delivery": "follow_up", "daedalus.origin": origin})]
                )
                return state.run_id or ""
            provider_id: str | None = None
            if not state.running:
                try:
                    rungs, _ = self.resolve_model(await self.live.load(session_id))
                    provider_id = rungs[0][0].endpoint.id if rungs else None
                except Exception:  # noqa: BLE001 — a model problem surfaces when the run starts, not here
                    provider_id = None
                exceeded = self.budget_exceeded()
                if exceeded and not self.provider_costs_nothing(provider_id):
                    raise RuntimeError(f"daily budget exceeded ({exceeded}); runs resume tomorrow or after /budget reset")
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
                await self.live.enqueue(session_id, kind, {**new_queued_prompt(kind, body).to_dict(), "origin": origin, "queued_at": datetime.now(UTC).isoformat()})  # type: ignore[arg-type]
                # The core folds queued prompts into the model's history later (and compaction may
                # rewrite them); the transcript keeps the operator's words as sent.
                await self.sessions.append_transcript(
                    session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], metadata={"daedalus.delivery": kind, "daedalus.origin": origin})]
                )
                if kind == "steer":
                    await self.steer_changed(session_id, reason="queued")
                return state.run_id or ""
            # A new run starts. The one before it may still be tidying up behind the answer, and most
            # of that is none of this run's business — but the files are: a revert of the turn that
            # just ended restores the snapshot taken after it, so that snapshot has to exist before
            # anything is allowed to change the workspace again.
            try:
                async with asyncio.timeout(self.config.ops.settle_wait_seconds):
                    await state.settled.wait()
            except TimeoutError:
                # Bounded because this is held under ``state.submit_lock``: a snapshot that never
                # returns would otherwise wedge every later message behind a session the app draws
                # as idle, with nothing anywhere saying why. The turn starts; the cost is that an
                # undo of the previous one may restore a tree a turn older, and the log says so.
                logger.warning("session %s: the previous turn was still being written down after %.0f s; starting the next run anyway", session_id, self.config.ops.settle_wait_seconds)
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
            # Only what cannot be sent at all is compacted here — a model whose window is smaller
            # than the history was built for. The ratio-based compaction happens between runs
            # (see _drive), because it is a summariser call of a minute or two and the operator's
            # message used to queue behind it.
            await self._maybe_auto_compact(state, required_only=True)
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
        self._apply_tool_visibility(state)  # a running engine takes the mode's tool rules from the next call on
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
            if model_name:
                # The operator moved the running session onto another model. That is a change the
                # fronts should say out loud, and it is not a fallback: what the session is set to
                # moves with it, so the header notes the switch once and then reads as normal again.
                state.configured_model = self._rung_label(getattr(state.engine, "llm", None), model_name)
                state.model_change_reason = "live_override"

    # -- runs -----------------------------------------------------------------------

    async def _build_engine(self, state: SessionState, run_id: str) -> QueryEngine:
        overrides = await self.live.load(state.session.id)
        rungs, preset = self.resolve_model(overrides)
        if rungs and self.provider_costs_nothing(rungs[0][0].endpoint.id):
            # A local primary remains usable after a hosted-provider budget is exhausted. Paid
            # fallbacks do not inherit that exemption: when any applicable dollar guard is already
            # closed, keep only local rungs so a failed server cannot turn a free run into a charge.
            paid_blocked = self.budget_exceeded() is not None
            if not paid_blocked:
                for provider, _model in rungs[1:]:
                    if not self.provider_costs_nothing(provider.endpoint.id) and await self.cap_breach(state, provider.endpoint.id) is not None:
                        paid_blocked = True
                        break
            if paid_blocked:
                rungs = [(provider, model) for provider, model in rungs if self.provider_costs_nothing(provider.endpoint.id)]
        deps = EngineDeps(
            tool_registry=self.tools,
            event_stream=self.events,
            blob_store=self.blobs,
            skill_store=self.skills,
            hook_manager=self.hooks,
            bot_repo=self.settings.bot_repo_dir,
            core_repo=self.settings.core_repo_dir,
            governance_path=self.governance_path,
            github_org=self.settings.daedalus_github_org,
            ssh_config=Path.home() / ".ssh" / "config",
            policy_gate=self.policy_gate,
            selfdev_mode=self.capabilities.selfdev.mode,
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
        chain = build_chain(rungs, room=self.providers.room_for(self.config))
        # What this run was *asked* for. Everything the fallback notice says is measured against it,
        # so it is read once here, from the same rungs the chain is built on, and not re-derived later
        # from a configuration the operator may have changed while the run was going.
        state.configured_model = self._rung_label(rungs[0][0], rungs[0][1])
        state.effective_model = ""
        state.model_change_reason = ""
        state.model_stamps.clear()
        engine = build_engine(
            deps=deps,
            config=self.config,
            mode=mode,
            run_id=run_id,
            session_id=state.session.id,
            session_title=state.session.title,
            workspace=state.workspace,
            project=state.project.name if state.project is not None else "",
            rungs=rungs,
            provider_chain=chain,
            model_name=rungs[0][1],
            thinking=preset.thinking if overrides.get("thinking_enabled") is None else bool(overrides["thinking_enabled"]),
            reasoning_effort=overrides.get("reasoning_effort") or preset.reasoning_effort,
            context_window=state.context_window or preset.context_window,
            max_output_tokens=preset.max_output_tokens,
            extra_notes=self.notes_for(state),
            blocked_tools=self.blocked_tools_for(state),
            voice=self.is_voice(state),
        )
        self._attach_hooks(engine, state)
        if chain is not None:
            # The fit check reads the prompt size the engine last saw; before the first call it is zero and every rung fits.
            chain.bind_prompt_size(lambda: int(getattr(engine, "last_observed_prompt_tokens", 0) or 0))
        return engine

    def _attach_hooks(self, engine: QueryEngine, state: SessionState) -> None:
        session_id = state.session.id

        async def reload_live_control(eng: QueryEngine) -> None:
            data = await self.live.load(session_id)
            steer = [item for item in data["steer"] if str(item.get("id") or "") not in state.steer_withdrawn]
            follow_up = list(data["follow_up"])
            state.steer_seen = [str(item.get("id") or "") for item in steer]
            state.follow_up_seen = [str(item.get("id") or "") for item in follow_up]
            eng._steer_queue = steer  # type: ignore[attr-defined]
            eng._follow_up_queue = follow_up  # type: ignore[attr-defined]

        async def persist_live_control(eng: QueryEngine) -> None:
            steer = list(getattr(eng, "_steer_queue", []) or [])
            follow_up = list(getattr(eng, "_follow_up_queue", []) or [])
            # A merge, not a write: a steer that arrived after this round's reload is in the store
            # and not in the engine's list, and writing that list over the column would destroy it
            # while the app was being told the model had read it.
            gone = await self.live.replace_seen(
                session_id, steer, follow_up, seen_steer=state.steer_seen, seen_follow_up=state.follow_up_seen,
            )
            consumed = [item_id for item_id in gone if item_id not in state.steer_withdrawn]
            state.steer_seen = [str(item.get("id") or "") for item in steer]
            state.follow_up_seen = [str(item.get("id") or "") for item in follow_up]
            state.steer_withdrawn.clear()
            if consumed:
                # The round placed queued text into the history, so the cards the app draws above its
                # composer are stale. Only what this round was handed and did not hand back counts,
                # and a withdrawal announced itself when it happened and is not this.
                await self.steer_changed(session_id, reason="consumed")

        def start_persist(history: list[Message], fresh: list[Message] | None) -> None:
            """Queue one round's write behind the writes of the rounds before it.

            The hook returns as soon as the write is a task, so the rounds of a session are only in
            order if the tasks are made to be: each waits for the one queued before it. Without that,
            a write that fails could have its repair overtaken by a hand-over queued behind it, and
            the round's messages were simply never written.
            """
            previous = state.history_keys
            state.history_keys = [self.sessions.transcript_key(m) for m in history]
            prior, gen, epoch = state.persist_chain, state.persist_gen, state.persist_epoch

            async def ordered() -> None:
                if prior is not None and not prior.done():
                    await asyncio.wait([prior])  # its outcome is its own; this waits only for its turn
                await self._persist_history(state, history, previous, gen, fresh=fresh, epoch=epoch)

            task = asyncio.get_running_loop().create_task(ordered(), name=f"persist:{session_id}")
            state.persist_chain = task
            state.persist_tasks.add(task)
            task.add_done_callback(_log_task_failure)
            task.add_done_callback(_forget_if_dropped(state))
            task.add_done_callback(state.persist_tasks.discard)

        def persist_session_history(eng: QueryEngine) -> None:
            """The whole working history, for a core that cannot say what changed."""
            start_persist(list(eng.history), None)

        def persist_history_delta(eng: QueryEngine, delta: Any) -> None:
            """What the round added — or the whole history, when the sequence itself changed.

            The core remembers what it last handed over, so the store is told "these two are
            new" instead of being handed eight hundred messages to write again. A rewrite (a
            compaction, a revert, rows the host dropped) says so, and then the history becomes
            a new generation exactly as it did before.
            """
            start_persist(list(delta.history), None if delta.rewritten else list(delta.appended))

        engine.reload_live_control = reload_live_control  # type: ignore[attr-defined]
        engine.persist_live_control = persist_live_control  # type: ignore[attr-defined]
        # Both are attached: a core that knows about the delta calls the second and never the
        # first, and one that does not calls the first and never looks for the second.
        engine.persist_session_history = persist_session_history  # type: ignore[attr-defined]
        engine.persist_history_delta = persist_history_delta  # type: ignore[attr-defined]

    async def _persist_history(
        self,
        state: SessionState,
        history: list[Message],
        previous_keys: list[str],
        gen: int | None = None,
        *,
        fresh: list[Message] | None = None,
        epoch: int | None = None,
    ) -> None:
        """Persist the working history and the transcript, then label fresh summaries with what they replaced.

        A compaction summary the core just produced is tagged with the transcript seqs of the
        turns it stands for, in its metadata (for the Mini App) and in its text (so the model
        knows what HistoryExpand would return). Persists are serialised per session and a
        snapshot taken before a history rewrite is dropped, so an older picture never lands last.

        A write that did not land leaves the stored history short of a round, and the rounds after
        it would append onto messages that are not there — the working history the next load reads
        comes from ``session_messages``, so the hole is what the session would resume from, with a
        tool result missing under a tool call that is not. So a failure is recorded on the session
        before this call returns, and the next persist of that session writes the history whole
        rather than adding to the end of it, whichever hand-over gets there first. A hand-over made
        before that failure and still queued behind it is written whole too: what it holds is what
        the round added, and the rows it would be added to are the ones that went missing.
        """
        async with state.persist_lock:
            if gen is not None and gen != state.persist_gen:
                return
            if state.persist_rewrite or (epoch is not None and epoch != state.persist_epoch):
                # The stored history is missing a round. Appending is not an option, and the
                # snapshot this call was given may be older than what the session holds now — a
                # rewrite from that would drop the rounds taken since — so the engine's own history
                # is written where there is one.
                fresh = None
                live = state.engine.history if state.engine is not None else None
                if live is not None and len(live) >= len(history):
                    history = list(live)
            try:
                await self._write_history(state, history, previous_keys, fresh=fresh)
            except (Exception, asyncio.CancelledError):
                self._persist_failed(state, history)
                raise
            if fresh is None:
                state.persist_rewrite = False  # the history was written whole; there is no hole left

    def _persist_failed(self, state: SessionState, history: list[Message]) -> None:
        """A write did not land: take the promise back, and make sure somebody rewrites the history.

        The core is told at once — not from a done-callback, which fires a loop iteration later,
        by which time the hand-overs made in between have already been queued as appends. The
        rewrite is scheduled as well as marked, because the failing round may have been the last of
        the run: with no traffic after it, a mark nobody reads repairs nothing.
        """
        state.persist_rewrite = True
        state.persist_epoch += 1
        _forget_persisted(state)
        if state.persist_repair is not None and not state.persist_repair.done():
            # The failure happened inside the repair itself, or one is already on its way: a store
            # that is refusing every write is retried by the next round, not by this one forever.
            return
        task = asyncio.get_running_loop().create_task(self._repair_history(state, history), name=f"persist-repair:{state.session.id}")
        state.persist_repair = task
        state.persist_tasks.add(task)
        task.add_done_callback(_log_task_failure)
        task.add_done_callback(state.persist_tasks.discard)

    async def _repair_history(self, state: SessionState, history: list[Message]) -> None:
        """Write the history whole after a failed write, unless a hand-over has already done it."""
        await asyncio.sleep(0)  # let a hand-over queued behind the failure take it first
        if not state.persist_rewrite:
            return
        await self._persist_history(state, history, state.history_keys)

    def _stamp_model(self, state: SessionState, messages: Sequence[Message], previous_keys: Sequence[str]) -> None:
        """Name the model on every assistant turn this round added, before its row is written.

        The transcript row is written once and ignored ever after, so the stamp has to be on the
        message the first time it is offered — a turn stamped later is a turn the app never sees
        stamped. Only turns this round added are touched: a rewrite hands over the whole history,
        and stamping that with the model answering now would put today's fallback on every answer
        the session ever gave.

        The stamp itself is not read here: it was taken at ``message_stop``, while the model that
        wrote the turn was still the one answering. A persist is a fire-and-forget task, so round *n*
        is very often written after round *n+1* has already stepped the chain down — reading
        ``effective_model`` at write time is what made a turn the configured model produced claim the
        fallback wrote it, permanently, since transcript rows are written once.

        The metadata dict is annotated in place. It is the same dict the core's own history holds,
        and that is the point: the working copy and the transcript copy say the same thing, and
        neither is a message the model is ever shown.
        """
        if not state.model_stamps:
            return
        known = set(previous_keys)
        for message in messages:
            if message.role is not MessageRole.assistant:
                continue
            key = self.sessions.transcript_key(message)
            if MODEL_METADATA_KEY in message.metadata or key in known:
                continue
            record = state.model_stamps.pop(key, None)
            if record is None:
                # Nobody watched this turn end — a history the host did not stream, a turn the core
                # wrote itself. An unstamped turn says nothing; a guessed one says something false.
                continue
            message.metadata[MODEL_METADATA_KEY] = record

    async def _write_history(
        self,
        state: SessionState,
        history: list[Message],
        previous_keys: list[str],
        *,
        fresh: list[Message] | None,
    ) -> None:
        """The writes themselves, under the caller's lock: the transcript, then the working history."""
        session_id = state.session.id
        current = {self.sessions.transcript_key(m) for m in history}
        removed = [k for k in previous_keys if k not in current]
        self._stamp_model(state, fresh if fresh is not None else history, previous_keys)
        unlabelled = [i for i, m in enumerate(history) if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY) and "daedalus.archived" not in m.metadata]
        if unlabelled:
            seqs = await self.sessions.transcript_seqs(session_id, removed) if removed else []
            if removed and len(seqs) < len(removed):
                logger.warning("session %s: %d of %d archived turns were never in the transcript", session_id, len(removed) - len(seqs), len(removed))
            for index in unlabelled:
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
        await self.sessions.append_transcript(session_id, fresh if fresh is not None else history, from_history=True)
        # A round adds to the end of the history it was given; only a rewrite of the
        # sequence starts a generation, and that is not what a round does. ``fresh`` is
        # what the round added, when the caller knows; ``None`` means it does not, and
        # the stored history is brought up to this one the long way.
        if fresh is None:
            await self.sessions.sync_messages(session_id, TENANT, history)
        elif fresh:
            await self.sessions.append_messages(session_id, TENANT, fresh)

    async def _start_run(
        self, state: SessionState, message: Message | None, *, continue_turn: bool = False
    ) -> str:
        async with state.lock:
            return await self._start_run_locked(state, message, continue_turn=continue_turn)

    async def _start_run_locked(
        self, state: SessionState, message: Message | None, *, continue_turn: bool = False
    ) -> str:
        # A bounded indexing chunk yields before an agent starts; the index checks busy again
        # under this same lock, so background inference cannot overlap an active run.
        async with self.idle_work:
            return await self._start_run_ready(state, message, continue_turn=continue_turn)

    async def _start_run_ready(
        self, state: SessionState, message: Message | None, *, continue_turn: bool = False
    ) -> str:
        if state.running and not continue_turn:
            raise RuntimeError("a run is already active in this session")
        state.run_active_since = time.monotonic()
        state.last_error_kind = ""
        state.last_error_message = ""
        state.soft_stop_cause = ""
        state.soft_stop_detail = ""
        if message is not None:
            state.run_origin = str(message.metadata.get("daedalus.origin") or "operator")
        if message is not None and not continue_turn:
            message = await self._with_turn_context(state, message)
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

    async def _with_turn_context(self, state: SessionState, message: Message) -> Message:
        """The run's opening message with the turn context as its last block.

        Same message identity (role and creation time), so the transcript keeps the copy that was
        recorded when the operator sent it and the working history carries the one the model reads;
        the app and the summariser strip the block from what they show.
        """
        # The core allows a user message one content block, so the context joins the text rather
        # than following it; a message with no text (an image alone) goes as it is.
        volatile = [await self.workspace_notes(state)]
        loop_state = str(state.metadata.get("loop_state") or "").strip()
        if loop_state:
            volatile.append("\n" + loop_state)  # the loop's counter and next wake-up change every run
        context = prompts.turn_context(notes="".join(volatile))
        blocks = list(message.content_blocks)
        for i, block in enumerate(blocks):
            if isinstance(block, TextBlock):
                blocks[i] = TextBlock(text=f"{block.text.rstrip()}\n\n{context}")
                return message.model_copy(update={"content_blocks": blocks})
        return message

    async def _drive(
        self, state: SessionState, engine: QueryEngine, message: Message | None, continue_turn: bool
    ) -> None:
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
            elif await self._report_provider_refusal(state, run_id):
                status = "failed"
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
            # Everything between the last token and the end of this function is time the app spends
            # drawing a run that is over: the chip says "running" and the cursor blinks under the
            # finished answer. So only what the answer itself depends on — writing it down and saying
            # the run ended — happens here; the rest is handed to a task nobody watches.
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
            except Exception:  # noqa: BLE001
                logger.exception("run %s bookkeeping failed", run_id)
            # The announcement goes out before the task is made, and the task before this function
            # returns: a created task does not run until the loop gets the turn back, which is after
            # ``_drive`` has ended and the session reads as idle. A run-finished callback that starts
            # the next run — the voice concierge answering, a leader collecting a subagent — would
            # otherwise be told the run is over while this one is still technically going.
            # An interrupted run is not a settled one: it is parked. Its snapshot stays where it is,
            # ``resume_unfinished()`` drives it on after the restart, and nothing is announced —
            # saying "the run ended" would put every front back to idle on a turn that is about to
            # continue. The run-finished callbacks do not run for it either, and that is the change
            # this made: they used to run for every status. They hand a finished answer on (a leader
            # collecting a subagent, a schedule learning its run ended), and this answer is not
            # finished; the resumed run announces itself when it really is.
            if status != "interrupted":
                await self._announce_settled(state, run_id, status, housekeeping=True)
                state.settled.clear()
                state.housekeeping = asyncio.create_task(self._settle_run(state, run_id, status), name=f"settle:{run_id}")
                state.housekeeping.add_done_callback(_log_task_failure)

    PROVIDER_REFUSED_NOTE = "⚠️ the model provider refused this run's requests, so it was closed early — {detail}"
    """What the operator is shown when a run ends on the provider rather than on its work. It quotes the provider."""

    async def _report_provider_refusal(self, state: SessionState, run_id: str) -> bool:
        """A run the provider refused ends as an error, whatever the model wrote on the way out.

        The core winds such a run down: the tools go, the model is told the endpoint failed, and it
        writes a closing message. That message is an answer to nobody's question — the work did not
        happen — and if it is the only thing the operator sees, a provider outage reads as a polite
        non-answer and the session looks like it finished. So the failure is put on the wire as an
        error and the run is recorded as one: the app draws the error state, the Telegram front adds
        the line, the inbox gets the entry, and the outage recovery drives the turn again once the
        endpoint is back — the same path a run that failed outright already takes.

        Nothing is claimed without the provider's own words: a wind-down with no detail behind it is
        some other kind of stop and is left alone.
        """
        if state.soft_stop_cause != CAUSE_PROVIDER_ERROR or not state.soft_stop_detail:
            return False
        await self._dispatch_event(
            state,
            TurnEvent(
                type=EventType.ERROR,
                run_id=run_id,
                payload={"kind": "llm_provider_error", "message": self.PROVIDER_REFUSED_NOTE.format(detail=state.soft_stop_detail)},
            ),
        )
        return True

    async def _announce_settled(self, state: SessionState, run_id: str, status: str, *, housekeeping: bool) -> None:
        """Say on the wire that the run is over — once when the answer is written down, once when the rest is.

        The app ends the streaming turn on the model's own ``message_stop``; this is what puts the
        session back to idle beside it. ``housekeeping`` says whether anything is still being written
        behind the answer, so a front that wants to show it has something to show and something to
        take away again.
        """
        try:
            await self._dispatch_event(
                state,
                TurnEvent(type=EventType.RUN_SETTLED, run_id=run_id, payload={"status": status, "housekeeping": housekeeping, "session_id": state.session.id}),
            )
        except Exception:  # noqa: BLE001 — the run is over either way; a front that missed the event polls
            logger.warning("could not announce the end of run %s", run_id, exc_info=True)

    async def _settle_run(self, state: SessionState, run_id: str, status: str) -> None:
        """What a finished run still owes, off the path the operator is watching.

        Order is the contract. The snapshot comes first and raises ``state.settled``, because that is
        the one thing the next run may not start without: a revert of the turn that just ended has to
        find the files as that turn left them. Everything after it — the event log's own tidying, the
        run-finished callbacks (delivery to the other fronts, the learning record, a memory extraction
        that is a model call of its own), the queue that filled up while the run was settling, the
        compaction check — nobody waits for, and each is guarded so one failure does not eat the rest.
        """
        session_id = state.session.id
        try:
            if status in ("completed", "failed", "cancelled"):
                await self.checkpoint(state, kind="after", run_id=run_id)
        except Exception:  # noqa: BLE001
            logger.exception("run %s snapshot failed", run_id)
        finally:
            state.settled.set()  # the next run may start: the history is written and the files are snapshotted
        if status in ("completed", "failed", "cancelled"):
            try:
                await self.events.delete_snapshot(run_id)
                self.events.close_run(run_id)
                await self.events.trim(run_id, TENANT, max_len=self.config.ops.events_keep_per_run)
            except Exception:  # noqa: BLE001
                logger.exception("run %s event bookkeeping failed", run_id)
        for callback in self._finished:
            try:
                await callback(session_id, run_id, status)
            except Exception:  # noqa: BLE001
                logger.exception("run-finished callback failed")
        await self._announce_settled(state, run_id, status, housekeeping=False)
        if status in ("completed", "failed"):
            # A run that ended in an error still owes an answer to what arrived meanwhile.
            try:
                await self._drain_leftover_follow_ups(state)
            except Exception:  # noqa: BLE001
                logger.exception("draining the queue of session %s failed", session_id)
        if status in ("completed", "failed", "cancelled"):
            # On a task of its own, and not awaited here: a summariser call is a minute or two,
            # and while this task is unfinished the session reads as running — which blocks the
            # next message in submit() and tells the app a run is in progress that is not one.
            self._spawn_background(self._maybe_auto_compact(state), f"auto-compact:{session_id}")
        if status == "completed":
            state.overflow_streak = 0
            state.outage_streak = 0
        elif status == "failed" and state.last_error_kind == "llm_context_window_exceeded" and not state.running:
            await self._recover_from_overflow(state)
        elif status == "failed" and state.last_error_kind in PROVIDER_OUTAGE_KINDS and not state.running:
            self._schedule_outage_recovery(state)

    OUTAGE_NOTE = (
        "[The previous turn stopped because the model provider was unreachable for a while. "
        "Nothing was lost; continue the work from where it stopped.]"
    )

    def _schedule_outage_recovery(self, state: SessionState) -> None:
        """A run the provider dropped is not the end of the task either: wait, then drive the turn again.

        The core already retried in place and wound the run down; that covers a blip of seconds. An outage
        of minutes ends in a failed run, and a failed run used to stay failed until the operator wrote
        something — the turn had to be resumed by hand. The wait grows with every consecutive failure and
        the attempts are bounded, so a provider that is down for the day does not keep a session busy.
        """
        if self.shutting_down:
            return
        ops = self.config.ops
        if state.outage_streak >= ops.provider_retry_max_attempts:
            logger.warning("session %s: the provider failed %d runs in a row; not retrying", state.session.id, state.outage_streak)
            return
        state.outage_streak += 1
        delay = min(ops.provider_retry_base_seconds * 2 ** (state.outage_streak - 1), ops.provider_retry_max_seconds)
        logger.warning("session %s: provider failure %d; driving the turn again in %.0f s", state.session.id, state.outage_streak, delay)
        if state.outage_task is not None and not state.outage_task.done():
            state.outage_task.cancel()
        state.outage_task = asyncio.create_task(self._recover_from_outage(state, state.run_id, delay), name=f"outage:{state.session.id}")
        state.outage_task.add_done_callback(_log_task_failure)

    async def _recover_from_outage(self, state: SessionState, failed_run_id: str | None, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.shutting_down or self._states.get(state.session.id) is not state:
            return
        if state.running or state.pending is not None or state.run_id != failed_run_id:
            return  # something else moved the session on meanwhile (an operator message, a scheduled turn)
        try:
            await self.submit(state.session.id, self.OUTAGE_NOTE, as_answer=False, origin="core")
        except RuntimeError as exc:
            logger.warning("session %s: outage recovery did not start: %s", state.session.id, exc)

    OVERFLOW_NOTE = (
        "[The previous turn stopped because the conversation no longer fitted the model's context window. "
        "The history has been compacted into the summary above; continue the work from where it stopped.]"
    )

    async def _recover_from_overflow(self, state: SessionState) -> None:
        """A run the provider refused for size is not the end of the task: shrink the history and drive the turn again."""
        if self.shutting_down:
            return  # the interrupted run resumes after the restart; recovery would start a run into a closing process
        if state.overflow_streak >= 2:
            logger.warning("session %s: overflowed %d times in a row; not retrying", state.session.id, state.overflow_streak)
            return
        state.overflow_streak += 1
        cfg = self.config.compaction
        status = await self.context_status(state)
        if status["messages"] >= cfg.min_messages:
            async with state.lock:
                try:
                    await self._compact_locked(state, "", keep_recent=cfg.keep_recent_messages, reason="auto", own_task_ok=True)  # own_task_ok: the required_only call runs inside the run's own task
                except RuntimeError as exc:
                    logger.warning("session %s: overflow recovery could not compact: %s", state.session.id, exc)
                    return
        logger.warning("session %s: context overflow; history compacted, driving the turn again", state.session.id)
        try:
            await self.submit(state.session.id, self.OVERFLOW_NOTE, as_answer=False, origin="core")
        except RuntimeError as exc:
            logger.warning("session %s: overflow recovery did not start: %s", state.session.id, exc)

    async def _drain_leftover_follow_ups(self, state: SessionState) -> None:
        """Input that arrived while the run was settling starts the next turn instead of rotting in the queue.

        Under ``submit_lock`` because this is the second place a run is started: the settling run's
        queue and an operator message arriving at the same moment must become one run, not two driving
        one history. A message that got there first is already running — it took the queue with it.
        """
        if self.shutting_down:
            return  # the queue is in the store; the next start reads it rather than opening a run into a shutdown
        async with state.submit_lock:
            if state.running:
                return
            await self._drain_locked(state)

    async def _drain_locked(self, state: SessionState) -> None:
        queued = await self.live.load(state.session.id)
        items = [item for item in queued["follow_up"] + queued["steer"] if str(item.get("text") or "").strip()]
        if not items:
            return
        await self.live.save_queues(state.session.id, [], [])
        await self.steer_changed(state.session.id, reason="consumed")
        texts = [str(item["text"]).strip() for item in items]
        origins = {str(item.get("origin") or "operator") for item in items}
        # The transcript already holds each item as it was sent; this copy only opens the run and stays hidden.
        origin = "operator" if "operator" in origins else next(iter(origins))
        message = Message(role=MessageRole.user, content_blocks=[TextBlock(text="\n\n".join(texts))], metadata={"daedalus.origin": origin, "daedalus.delivery": "drained"})
        await self.sessions.append_transcript(state.session.id, [message])
        seqs = await self.sessions.transcript_seqs(state.session.id, [self.sessions.transcript_key(message)])
        await self.checkpoint(state, kind="before", seq=seqs[0] if seqs else None)
        await self._start_run(state, message)

    @staticmethod
    def _rung_label(provider: Any, model: str) -> str:
        """``provider:model`` — one rung of the chain, named the way the chain names it."""
        return f"{getattr(getattr(provider, 'endpoint', None), 'id', '') or '?'}:{model}"

    def model_status(self, state: SessionState) -> dict[str, Any]:
        """What is really answering this session, and whether that is what it was set to.

        ``fallback`` is present only while another model holds the run: the configured one
        answering again takes it away, which is what makes the header's note self-clearing.
        """
        configured = state.configured_model
        effective = state.effective_model or configured
        out: dict[str, Any] = {
            "configured_model": configured.split(":", 1)[-1] if configured else "",
            "effective_model": effective.split(":", 1)[-1] if effective else "",
            "effective_provider": effective.split(":", 1)[0] if ":" in effective else "",
            "fallback": None,
        }
        if configured and effective and effective != configured:
            out["fallback"] = {
                "from": configured.split(":", 1)[-1],
                "to": effective.split(":", 1)[-1],
                "reason": state.model_change_reason or "chain_step",
            }
        return out

    def _model_record(self, state: SessionState) -> dict[str, Any]:
        """The stamp an assistant turn carries: who answered, and what the session had asked for."""
        status = self.model_status(state)
        record: dict[str, Any] = {
            "provider": status["effective_provider"],
            "model": status["effective_model"],
            "configured": status["configured_model"],
        }
        if status["fallback"] is not None:
            record["fallback"] = status["fallback"]
        return record

    def _model_change(self, state: SessionState, event: TurnEvent) -> TurnEvent | None:
        """The event that says the model answering has changed — or ``None`` when it has not.

        Two kinds of event feed this, and both are needed. The core's ``model_fallback_triggered``
        is the demotion itself: it names the rung the run moved to and why, and it is the only
        notice there is, because the retry re-opens the stream inside the assistant message that
        was already started — no second ``message_start`` announces it. And ``message_start`` is
        what catches every other way the model can differ from the configured one without a
        demotion ever being announced: a chain position restored from a snapshot, a model rebound
        by a live override.
        """
        payload = event.payload
        engine = state.engine
        if engine is None:
            return None
        if event.type is EventType.STATE_CHANGED and payload.get("reason") == "model_fallback_triggered":
            # ``from``/``to`` here are the loop's own state, not models; the rung is the other field.
            return self._observe_model(
                state,
                event.run_id,
                str(payload.get("fallback_model_id") or ""),
                FALLBACK_REASONS.get(str(payload.get("error_class") or ""), "chain_step"),
            )
        if event.type is EventType.MESSAGE_START:
            return self._observe_model(
                state,
                event.run_id,
                str(payload.get("model") or getattr(engine.config, "model_name", "") or ""),
                state.model_change_reason or "chain_step",
            )
        return None

    def _observe_model(self, state: SessionState, run_id: str, model: str, reason: str) -> TurnEvent | None:
        """Record which model is answering now; say so on the wire when it is not the one before."""
        engine = state.engine
        if engine is None or not model:
            return None
        observed = self._rung_label(getattr(engine, "llm", None), model)
        previous = state.effective_model
        if observed == previous:
            return None
        state.effective_model = observed
        if not previous and observed == state.configured_model:
            # The run's first message on the model it was configured with: nothing changed, it began.
            return None
        if not previous:
            reason = "live_override" if reason == "chain_step" else reason
        state.model_change_reason = "" if observed == state.configured_model else reason
        return TurnEvent(
            type=EventType.MODEL_CHANGED,
            run_id=run_id,
            payload={
                "from": (previous or state.configured_model).split(":", 1)[-1],
                "to": model,
                "model_name": model,
                "provider": observed.split(":", 1)[0],
                "configured": state.configured_model.split(":", 1)[-1],
                "reason": reason,
                "fallback": observed != state.configured_model,
                "session_id": state.session.id,
            },
        )

    def _record_model_stamp(self, state: SessionState) -> None:
        """Name the model on the assistant turn that has just ended, while it is still the one answering.

        The turn is already in the working history by ``message_stop`` — the tool result of a tool
        round lands before it — so the last assistant message there is the one that stopped. The
        record is kept against that message's transcript key until the persist of its round picks it
        up; the map is capped so a session whose turns are never written cannot grow it without end.
        """
        engine = state.engine
        if engine is None or not state.effective_model:
            return
        message = next((m for m in reversed(list(engine.history)) if m.role is MessageRole.assistant), None)
        if message is None:
            return
        key = self.sessions.transcript_key(message)
        if key in state.model_stamps:
            return
        state.model_stamps[key] = self._model_record(state)
        while len(state.model_stamps) > MODEL_STAMPS_KEPT:
            state.model_stamps.pop(next(iter(state.model_stamps)))

    async def _dispatch_event(self, state: SessionState, event: TurnEvent) -> None:
        change = self._model_change(state, event)
        if change is not None:
            # Before the message it explains, not after it: the header says which model is speaking
            # while the first token of that model is still on its way.
            await self._dispatch_event(state, change)
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
        self._time_tool(state, event)
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
            self._record_model_stamp(state)
            await self._enforce_caps(state, event.run_id)

    def _time_tool(self, state: SessionState, event: TurnEvent) -> None:
        """One row per tool call: name, when, how long, whether it failed. The waterfall of a run is these rows."""
        p = event.payload
        call_id = str(p.get("tool_call_id") or "")
        if not call_id:
            return
        if event.type is EventType.TOOL_USE_START:
            state.tool_starts[call_id] = (time.monotonic(), str(p.get("tool_name") or ""))
        elif event.type is EventType.TOOL_RESULT:
            started = state.tool_starts.pop(call_id, None)
            if started is None:
                return
            duration_ms = int((time.monotonic() - started[0]) * 1000)
            row = (state.session.id, event.run_id, call_id, started[1], datetime.now(UTC).isoformat(), duration_ms, 0 if p.get("is_error") else 1)
            self._spawn_background(self.db.execute("INSERT INTO tool_calls(session_id, run_id, tool_call_id, name, at, duration_ms, ok) VALUES (?, ?, ?, ?, ?, ?, ?)", row), f"tool-timing:{call_id}")
            p["duration_ms"] = duration_ms

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
            state.last_error_kind = str(p.get("kind") or state.last_error_kind)
            state.last_error_message = p["message"]
            logger.warning("run error in session %s (%s): %s", getattr(getattr(state, "session", None), "id", "?"), p.get("kind") or "-", p["message"][:500])
        elif event.type is EventType.STATE_CHANGED and p.get("reason") in RECOVERY_REASONS:
            if p.get("reason") == "soft_stop_notified":
                state.soft_stop_cause = str(p.get("soft_stop_cause") or "")
                state.soft_stop_detail = self.redactor.redact(str(p.get("soft_stop_detail") or ""))
            detail = {k: v for k, v in p.items() if k not in ("from", "to", "reason")}
            logger.warning("run recovery in session %s: %s %s", getattr(getattr(state, "session", None), "id", "?"), p.get("reason"), self.redactor.redact(json.dumps(detail, ensure_ascii=False, default=str)[:400]))

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

    async def spend_with_subagents(self, session_id: str) -> float:
        """A session's priced spend plus that of every subagent it started: the cap is the leader's."""
        rows = await self.db.fetchall("SELECT id FROM sessions WHERE metadata LIKE ?", (f'%"subagent_of": "{session_id}"%',))
        ids = [session_id, *(str(r["id"]) for r in rows)]
        placeholders = ",".join("?" for _ in ids)
        row = await self.db.fetchone(f"SELECT sum(cost_usd) usd FROM usage_events WHERE session_id IN ({placeholders})", tuple(ids))
        return float(row["usd"] or 0.0) if row else 0.0

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
            # Only a call made against the history as it stands now says anything about it: a row
            # from before the last rewrite measured a history that no longer exists.
            row = await self.db.fetchone(
                "SELECT input_tokens FROM usage_events WHERE session_id = ? AND purpose = 'stream' AND seq > ? ORDER BY seq DESC LIMIT 1",
                (state.session.id, state.usage_floor_seq),
            )
            tokens = int(row["input_tokens"] or 0) if row else state.observed_prompt_tokens
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

    @staticmethod
    def is_voice(state: SessionState) -> bool:
        """Whether this is the voice session: the operator's spoken conversation with the concierge."""
        return bool(state.metadata.get("voice"))

    def blocked_tools_for(self, state: SessionState) -> set[str]:
        """Everything this session may not call right now: disabled MCP servers' tools, the operator's switches,
        and the mode's rules. One computation for the engine build and for every live update, so a toggle in
        Settings cannot disarm a mode."""
        known = {t.name for t in self.tools.list_all()}
        blocked = blocked_for(self.mcp, self.mcp_enabled(state)) | self.tools_off(state)
        # The concierge talks and hands work over; it may not read, write or run anything itself, and the
        # tools that hand work over are its alone. Neither rule goes through a mode, so editing one cannot
        # give a session being spoken to a shell, nor give a working agent a second way to spawn one.
        blocked |= (known - set(VOICE_TOOLS)) if self.is_voice(state) else (known & set(VOICE_ONLY_TOOLS))
        mode = self.mode_for(state)
        if mode is not None:
            if mode.tools_only:
                blocked |= known - {str(n) for n in mode.tools_only}
            for entry in mode.tools_off:
                name = str(entry)
                blocked |= {t for t in known if t.startswith(name[:-1])} if name.endswith("*") else {name}
        return blocked

    def _apply_tool_visibility(self, state: SessionState) -> None:
        if state.engine is None:
            return
        known = {t.name for t in self.tools.list_all()}
        blocked = self.blocked_tools_for(state)
        state.engine.config = replace(state.engine.config, tool_visibility_policy=ToolVisibilityPolicy(pinned=known - blocked, blocked=blocked))

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
        self._apply_tool_visibility(state)
        return chosen

    def notes_for(self, state: SessionState) -> str:
        """What the system prompt says about this session beyond the environment: the brief it was created with."""
        parts = [state.extra_notes.strip()] if state.extra_notes.strip() else []
        available = set(self.providers.available())
        models = [pid for pid, preset in self.config.presets.items() if preset.provider in available and preset.model]
        if models:
            parts.append("- Models SubAgent accepts (preset ids): " + ", ".join(models))
        loop_note = str(state.metadata.get("loop_note") or "").strip()
        if loop_note:
            parts.append(loop_note)
        brief = str(state.metadata.get("brief") or "").strip()
        if brief:
            origin = state.metadata.get("spawned_by") or state.metadata.get("subagent_of")
            parts.append("- Your brief" + (f" (from session {origin})" if origin else "") + ", the standing instructions for this session:\n" + brief)
        return "\n".join(parts)

    async def workspace_notes(self, state: SessionState) -> str:
        """What the workspace and the board say about the work in progress: read at every run start, so a run
        begins from the plan of record rather than from memory of it.

        ``AGENTS.md`` (or ``CLAUDE.md``) in the workspace root is the agent's own project memory; the open
        board tasks of this session are its plan. Both are volatile text: they change between runs, so
        they travel in the turn context at the end of the run's opening message, never in the system
        prompt, whose bytes must not change between runs if the provider is to serve them from cache.
        """
        parts: list[str] = []
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = state.workspace / name
            try:
                if path.is_file():
                    text = (await asyncio.to_thread(path.read_text, "utf-8", "replace")).strip()
                    if text:
                        if len(text) > WORKSPACE_NOTES_CHARS:
                            text = text[:WORKSPACE_NOTES_CHARS] + f"\n[… {name} continues; Read it for the rest]"
                        parts.append(f"- {name} in the workspace (your project memory; keep it current):\n{self.redactor.redact(text)}")
                    break
            except OSError:
                continue
        try:
            rows = await self.db.fetchall("SELECT id, title, status, priority FROM board_tasks WHERE session_id = ? AND status NOT IN ('done', 'cancelled') ORDER BY priority, updated_at DESC LIMIT 8", (state.session.id,))
        except Exception:  # noqa: BLE001 — the board is optional
            rows = []
        if rows:
            parts.append("- Your open board tasks (BoardGet for details; update them as you go):\n" + "\n".join(f"  - [{r['status']}] {r['id']}: {r['title']}" for r in rows))
        return ("\n" + "\n".join(parts)) if parts else ""

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

    # -- tool policy ---------------------------------------------------------------

    def policy(self, *, base_dir: Path | str = "") -> Policy:
        cfg = self.config.policy
        rules = [Rule(id=r.id or f"config.{i}", tool=r.tool or "*", action=r.action, note=r.note, pattern=r.pattern, source="config") for i, r in enumerate(cfg.rules, 1)]
        return Policy(
            protected_paths=self.protected_paths(),
            egress_allow=cfg.egress_allow, rules=rules, workspace_roots=(self.settings.workspaces_dir,),
            operator_checkouts=(self.settings.bot_repo_dir, self.settings.core_repo_dir),
            selfdev_mode=self.capabilities.selfdev.mode,
            native=self.settings.native,
            # The home folder is the operator's, and only a native installation is inside it.
            home_dir=Path.home() if self.settings.native else "",
            project_roots=self.projects.roots,
            worktrees_root=self.settings.worktrees_dir,
            sealed_paths=self.settings.sealed_paths,
            sealed_ports=self._sealed_ports(),
            # Where a relative path is resolved from, so that `../../daedalus-secrets/keyproxy.env`
            # is read as the file it names rather than as a word with no slash at the front.
            base_dir=base_dir,
        )

    def _sealed_ports(self) -> tuple[int, ...]:
        """The installation's own doors on the loopback interface: the app's API, and the launcher's
        action page where a launcher is holding this installation.

        The file that carries the launcher's token is sealed, but sealing a path is matched against
        the words of a command and a path a command builds for itself is not matched by it. The port
        is not built by anybody: whatever reaches the launcher reaches it there. The agent asks the
        app for a restart or an install the way the operator does, and the app is what holds the keys
        to both — so a request made to either door directly is a way round that, and is refused.
        """
        ports = [self.settings.api_port]
        launcher = launcher_bridge.read(self.settings.state_dir)
        if launcher is not None:
            ports.append(launcher.port)
        return tuple(ports)

    def protected_paths(self) -> tuple[Path, ...]:
        """What no session may read or write: the installation itself.

        It is the sealed set — the secrets, the whole state directory, the runtime, the launcher and
        the environment file the launcher owns — plus the two paths that are the installation's
        without being part of it: the governance file the operator writes the rules in, and the
        launcher's directory in the image.

        The sealed set is not copied here, it is asked for, because the two layers that judge a path
        must not be able to disagree about it: the shell rules ask ``Settings.sealed_paths`` and the
        file tools, the browser and ``SessionServices.is_protected`` ask this list. Enumerating them
        twice is how the environment file came to be refused to a command and handed to ``Read``.
        """
        return (self.governance_path, Path("/opt/launcher"), *self.settings.sealed_paths)

    def policy_gate(self, session_id: str, run_id: str) -> Any:
        """The policy bound to one session: grants are the session's, the egress log names the run."""
        state = self._states.get(session_id)
        policy = self.policy(base_dir=state.workspace if state is not None else self.workspace_for(session_id))

        def decide(tool: str, arguments: dict[str, Any]) -> Decision:
            state = self._states.get(session_id)
            now = time.time()
            grants = {k for k, until in (state.metadata.get("policy_grants") or {}).items() if float(until) > now} if state is not None else set()
            decision = policy.evaluate(tool, arguments, grants=grants)
            if decision.key and decision.action == "allow" and state is not None:
                self._consume_grant(state, decision.key)
            elif decision.key and state is not None:
                # The refusal's preimage, so an operator granting the key from chat sees what they approve.
                pending = dict(state.metadata.get("policy_pending") or {})
                pending[decision.key] = {"tool": tool, "text": self.redactor.redact(canonical(tool, arguments))[:300], "at": datetime.now(UTC).isoformat()}
                for meta in (state.metadata, state.session.metadata):
                    meta["policy_pending"] = dict(list(pending.items())[-20:])
            if decision.hosts:
                self._record_egress(session_id, run_id, tool, decision.hosts, decision.action)
            if decision.action != "allow":
                logger.warning("policy %s %s for %s in session %s: %s", decision.action, decision.rule, tool, session_id, decision.reason)
            return decision

        return PolicyAdapter(decide)

    async def flush_background(self) -> None:
        """Wait for the fire-and-forget writes (timing rows, egress rows, grant updates) to land."""
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    async def _refresh_prices_daily(self) -> None:
        """Gateways that publish no prices get them from models.dev: once at start, then once a day."""
        from daedalus.providers.modelsdev import REFRESH_SECONDS  # Lazy: the provider package imports the host's config

        while True:
            try:
                priced = await self.providers.refresh_prices(self.db)
                if priced:
                    logger.warning("model prices refreshed from models.dev: %s models", priced)
            except Exception:  # noqa: BLE001
                logger.warning("model price refresh failed", exc_info=True)
            await asyncio.sleep(REFRESH_SECONDS)

    def _spawn_background(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("%s failed: %s", name, t.exception())

        task.add_done_callback(_done)

    def _consume_grant(self, state: SessionState, key: str) -> None:
        for meta in (state.metadata, state.session.metadata):
            grants = {k: v for k, v in (meta.get("policy_grants") or {}).items() if k != key}
            meta["policy_grants"] = grants
        self._spawn_background(self.sessions.update_metadata(state.session.id, state.session.metadata), f"grant-consume:{state.session.id}")

    def _record_egress(self, session_id: str, run_id: str, tool: str, hosts: list[str], action: str) -> None:
        async def _write() -> None:
            now = datetime.now(UTC).isoformat()
            await self.db.executemany("INSERT INTO egress_log(at, session_id, run_id, tool, host, action) VALUES (?, ?, ?, ?, ?, ?)", [(now, session_id, run_id, tool, h, action) for h in hosts])

        self._spawn_background(_write(), f"egress:{run_id}")

    async def grant(self, session_id: str, key: str) -> dict[str, Any]:
        """The operator lets one refused call through: the key from the refusal, valid once, for a limited time.

        Returns what was approved (the refusal's tool and text when the host saw it) and the open grants.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        key = key.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{12}", key):
            raise ValueError("an approval key is 12 hex characters, as shown in the refusal")
        pending = dict(state.metadata.get("policy_pending") or {})
        until = time.time() + GRANT_TTL_SECONDS
        for meta in (state.metadata, state.session.metadata):
            grants = {k: v for k, v in (meta.get("policy_grants") or {}).items() if float(v) > time.time()}
            grants[key] = until
            meta["policy_grants"] = dict(list(grants.items())[-20:])
        await self.sessions.update_metadata(session_id, state.session.metadata)
        return {"key": key, "approves": pending.get(key), "grants": list(state.metadata["policy_grants"]), "expires_in_minutes": GRANT_TTL_SECONDS // 60}

    async def egress(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT at, run_id, tool, host, action FROM egress_log WHERE session_id = ? ORDER BY seq DESC LIMIT ?", (session_id, limit))
        return [dict(r) for r in rows]

    async def tool_timing(self, session_id: str) -> list[dict[str, Any]]:
        """Per tool: calls, errors, total and mean time — where a session's wall-clock goes."""
        rows = await self.db.fetchall(
            "SELECT name, count(*) calls, sum(ok = 0) errors, sum(duration_ms) total_ms, avg(duration_ms) mean_ms, max(duration_ms) max_ms FROM tool_calls WHERE session_id = ? GROUP BY name ORDER BY total_ms DESC", (session_id,)
        )
        return [dict(r) for r in rows]

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
        if self.provider_costs_nothing(provider_id):
            # Dollar caps prevent another charge. A llama.cpp call is recorded at exactly zero, so
            # refusing it would turn a hosted-provider balance into an unrelated local outage.
            return None
        limits = self.config.limits
        since = limits.total_since or None
        session_cap = self.session_cap(state)
        if session_cap is not None:
            spent = await self.spend_with_subagents(state.session.id)
            if spent >= session_cap:
                return "session_cap", f"session cap reached: ${spent:.2f} spent of ${session_cap:.2f} (subagents included); raise it in the session settings to continue"
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
        limits = self.config.limits
        if limits.max_run_tokens > 0:
            row = await self.db.fetchone("SELECT sum(input_tokens + output_tokens) t FROM usage_events WHERE run_id = ?", (run_id,))
            used = int(row["t"] or 0) if row else 0
            if used >= limits.max_run_tokens:
                kind, note = "run_tokens", f"⏱ token cap reached: {used:,} tokens in this run of {limits.max_run_tokens:,} (limits.max_run_tokens); stopping this run. Send a message to continue in a new run."
        if note is None and limits.max_run_minutes > 0 and state.run_active_since:
            minutes = (time.monotonic() - state.run_active_since) / 60
            if minutes >= limits.max_run_minutes:
                kind, note = "run_minutes", f"⏱ time cap reached: {minutes:.0f} min of active work in this run of {limits.max_run_minutes} (limits.max_run_minutes); stopping this run. Send a message to continue in a new run."
        if note is not None:
            pass
        elif (run_cap > 0 or mode_cap is not None) and spent >= run_cap:
            source = f"mode {state.metadata.get('mode')}" if mode_cap is not None else "limits.usd_per_run"
            note = f"💸 per-run cap reached: ${spent:.2f} spent of ${run_cap:.2f} ({source}); stopping this run. Send a message to continue in a new run."
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
        parked: list[str] = []
        exceeded = self.budget_exceeded()
        try:
            seen: set[str] = set()
            for entry in await self.events.unfinished_snapshots():  # newest first
                session_id = str(entry["session_id"] or entry["snapshot"].get("session_id") or "")
                if session_id in seen:
                    # An older snapshot of a session whose newer run is resumed: a leftover, not a second run.
                    await self.events.delete_snapshot(entry["run_id"])
                    continue
                seen.add(session_id)
                if exceeded:
                    state = await self.get_state(session_id)
                    try:
                        rungs, _preset = self.resolve_model(await self.live.load(session_id))
                        free = bool(rungs and self.provider_costs_nothing(rungs[0][0].endpoint.id))
                    except Exception:  # noqa: BLE001 — the normal resume path records the unusable model
                        free = False
                    if state is not None and not free:
                        parked.append(entry["run_id"])
                        continue
                await self._resume_one(entry, resumed)
            if parked:
                logger.warning("budget exceeded; %d paid-provider unfinished run(s) stay parked until the cap is lifted", len(parked))
            await self._settle_stale_runs(resumed, parked)
        finally:
            self.recovering = False
        return resumed

    async def _settle_stale_runs(self, resumed: list[str], parked: list[str] | None = None) -> None:
        """A run row still 'running' that nobody drives is a leftover of the previous process: closed as cancelled."""
        active = set(resumed) | set(parked or ()) | self.running_run_ids()
        rows = await self.db.fetchall("SELECT id FROM runs WHERE status = ?", (RunStatus.running.value,))
        self.stale_runs = []
        for row in rows:
            if row["id"] in active:
                continue
            await self.runs.update_status(row["id"], TENANT, RunStatus.cancelled)
            await self.events.delete_snapshot(row["id"])
            self.stale_runs.append(row["id"])
            logger.warning("run %s was left running by the previous process and could not be resumed; closed as cancelled", row["id"])

    async def _resume_one(self, entry: dict[str, Any], resumed: list[str]) -> None:
        session_id = entry["session_id"] or entry["snapshot"].get("session_id")
        state = await self.get_state(session_id)
        if state is None:
            await self.events.delete_snapshot(entry["run_id"])
            return
        if state.running:
            return  # something in this process already drives the session; its own run owns the snapshot
        try:
            engine = await self._build_engine(state, entry["run_id"])
            await engine.resume_from_snapshot(entry["snapshot"])
        except Exception:  # noqa: BLE001
            logger.exception("could not resume run %s", entry["run_id"])
            await self.events.delete_snapshot(entry["run_id"])
            return
        state.engine = engine
        state.run_id = entry["run_id"]
        state.run_active_since = time.monotonic()  # the time cap counts from the resume, not from the run's creation
        state.history_keys = [self.sessions.transcript_key(m) for m in engine.history]
        if engine.state is LoopState.AWAITING:
            row = await self.db.fetchone("SELECT * FROM pending_questions WHERE session_id = ?", (session_id,))
            if row is None:
                await self.events.delete_snapshot(entry["run_id"])
                return
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
            return
        if not engine.history:
            await self.events.delete_snapshot(entry["run_id"])
            return
        if engine.state is not LoopState.RUNNING:
            engine.transition_to(LoopState.RUNNING)
        async with self.idle_work:
            state.task = asyncio.create_task(self._drive(state, engine, None, True), name=f"resume:{entry['run_id']}")
        state.task.add_done_callback(_log_task_failure)
        resumed.append(entry["run_id"])


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
        text = " ".join(prompts.without_turn_context("".join(b.text for b in m.content_blocks if isinstance(b, TextBlock))).split())
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
                source = prompts.without_turn_context(b.text)
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


CHARS_PER_TOKEN = 4
"""The usual approximation, used only to say how big a history is after the host rewrote it —
until the next call comes back with the provider's own count, which is what everything else reads."""


def history_tokens(messages: Sequence[Message]) -> int:
    """Roughly what a history costs in tokens, from its searchable text."""
    return sum(len(message_text(m)) for m in messages) // CHARS_PER_TOKEN


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
        text = prompts.without_turn_context("".join(b.text for b in m.content_blocks if isinstance(b, TextBlock))).strip()
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
        prompts.without_turn_context(b.text) for m in history if m.role is MessageRole.user for b in m.content_blocks if isinstance(b, TextBlock)
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
                text = prompts.without_turn_context(block.text).strip()  # the clock and the board of a past turn are not history
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


def worktree_writable_paths(worktree: Path) -> list[Path]:
    """The worktree itself and the parts of its repository a commit there writes.

    A worktree keeps its own HEAD, index and logs under the main repository's ``.git/worktrees/<name>``, and
    shares that repository's object store and the refs of its branch. A commit writes to all of them, so a
    session that may write its worktree gets those too — the shared object store (append-only by nature),
    the ``agent/`` branch refs and their reflogs — and not the rest of the repository's state.
    """
    paths = [worktree]
    dotgit = worktree / ".git"
    try:
        text = dotgit.read_text(encoding="utf-8") if dotgit.is_file() else ""
    except OSError:
        text = ""
    if not text.startswith("gitdir:"):
        return paths
    gitdir = Path(text.split(":", 1)[1].strip())
    if gitdir.parent.name != "worktrees":
        return paths + [gitdir]
    common = gitdir.parent.parent
    for extra in (gitdir, common / "objects", common / "refs" / "heads" / "agent", common / "logs" / "refs" / "heads" / "agent"):
        try:
            extra.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        paths.append(extra)
    return paths


def _forget_persisted(state: SessionState) -> None:
    """Tell the core that what it believes the store holds is no longer there.

    The core remembers the history it last handed over and appends onto it. Rows this host
    dropped or rebuilt — a compaction, a revert, a cleared history, a fork's fresh copy — are
    not there to be appended onto, and the next round would add its messages to a generation
    that no longer contains what came before them. Forgetting makes the next hand-over a
    rewrite, which is what it is. A core that rewrote the history every round has nothing to
    forget and does not carry this.
    """
    engine = state.engine
    forget = getattr(engine, "forget_persisted_history", None) if engine is not None else None
    if callable(forget):
        forget()


def _forget_if_dropped(state: SessionState) -> Callable[[asyncio.Task[None]], None]:
    """Take back the promise if the write the hook returned on never landed.

    The hook returns as soon as the write is a task, which tells the core the round is
    recorded. When that task fails or is cancelled, it is not, and the core would go on
    appending after messages that were never written. Forgetting makes the next hand-over a
    rewrite, which writes the missing ones with everything else.
    """

    def done(task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is not None:
            _forget_persisted(state)

    return done


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
