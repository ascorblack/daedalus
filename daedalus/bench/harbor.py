"""The agent as a Harbor ``BaseAgent``: the loop runs here, every command runs in the task container.

Harbor (``pip install harbor``) drives Terminal-Bench, SWE-bench, Aider Polyglot and the other
adapters it ships; it gives the agent a task instruction and an environment with ``exec``. This
adapter keeps compaction, resume, skills and subagents host-side and points Exec and the file
tools at the container through :class:`HarborExecBackend`. Run it as

    harbor run -d terminal-bench/terminal-bench@4.0.0 -a daedalus.bench.harbor:DaedalusAgent -m <model>

with ``BENCH_STATE_DIR`` naming a state directory that holds a ``config.toml`` with the providers
to use (never the bot's own state directory: benchmarks stay out of its database).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from daedalus.bench.manifest import DEFAULT_TOOLS_OFF
from daedalus.bench.runner import count_turns, trajectory
from daedalus.config import RuntimeConfig, Settings
from daedalus.host.filesystem import ExecOutcome
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

try:  # Harbor is optional: the adapter only loads where a benchmark runs.
    from harbor.agents.base import BaseAgent
except ImportError:  # pragma: no cover - exercised only without harbor installed
    BaseAgent = object  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

CONTAINER_WORKSPACE = "/app"
"""Where Harbor tasks keep their files; the session's workspace path inside the container."""

TRIAL_TIMEOUT_SECONDS = 8 * 3600
"""The longest a trial may run before the adapter stops the session itself (Terminal-Bench's own cap is eight hours)."""


class HarborExecBackend:
    """Exec inside the task container through ``environment.exec``."""

    def __init__(self, environment: Any) -> None:
        self.environment = environment

    async def run(self, command: str, *, cwd: str | None, env: dict[str, str] | None, timeout: float) -> ExecOutcome:
        try:
            result = await asyncio.wait_for(self.environment.exec(command, cwd=cwd or CONTAINER_WORKSPACE, env=env, timeout_sec=int(timeout)), timeout=timeout + 15)
        except TimeoutError:
            return ExecOutcome(exit_code=124, output=f"timed out after {timeout:.0f}s", timed_out=True)
        output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
        code = int(result.return_code) if result.return_code is not None else 0
        return ExecOutcome(exit_code=code, output=output)


class _SharedManager:
    """One SessionManager per process, shared by the trials Harbor runs concurrently."""

    def __init__(self) -> None:
        self.manager: SessionManager | None = None
        self.db: Database | None = None
        self.settings: Settings | None = None
        self.lock = asyncio.Lock()
        self.status: dict[str, str] = {}
        self.done: dict[str, asyncio.Event] = {}

    async def get(self) -> SessionManager:
        async with self.lock:
            if self.manager is None:
                settings = Settings()
                state_dir = settings.bench_state_dir or Path.cwd() / "bench-state"
                settings.state_dir = state_dir
                settings.workspaces_dir = state_dir / "workspaces"
                settings.state_dir.mkdir(parents=True, exist_ok=True)
                settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
                config = RuntimeConfig.load(settings.config_path)
                self.db = Database(settings.db_path)
                await self.db.open()
                self.manager = SessionManager(settings, config, db=self.db)
                await self.manager.start(recovering=False)
                self.manager.on_finished(self._finished)
                self.settings = settings
            return self.manager

    async def _finished(self, session_id: str, run_id: str, status: str) -> None:
        self.status[session_id] = status
        event = self.done.get(session_id)
        if event is not None:
            event.set()


_shared = _SharedManager()


class DaedalusAgent(BaseAgent):  # type: ignore[misc]
    """Harbor entry point. ``-m`` selects a preset id from the bench state's config.toml."""

    SUPPORTS_ATIF = False

    @staticmethod
    def name() -> str:
        return "daedalus"

    @staticmethod
    def version() -> str:
        return "1"

    async def setup(self, environment: Any) -> None:
        return None

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        manager = await _shared.get()
        assert _shared.db is not None
        backend = HarborExecBackend(environment)
        clock = time.monotonic()
        # The session's own workspace stays on this machine (host-side artefacts); the tools work in the container.
        state = await manager.create_session("[harbor] task", metadata={"unattended": True, "bench": True})
        sid = state.session.id
        _shared.done[sid] = asyncio.Event()
        try:
            if self.model_name:
                if self.model_name not in manager.config.presets:
                    raise ValueError(f"-m must name a preset id from the bench config.toml; known: {', '.join(manager.config.presets)}")
                await manager.set_model(sid, preset=self.model_name)
            await manager.set_tools_off(sid, list(DEFAULT_TOOLS_OFF))
            services = manager.locator_services(sid)
            assert services is not None
            services.exec_backend = backend
            services.workspace_dir = Path(CONTAINER_WORKSPACE)
            await manager.submit(sid, instruction, origin="bench")
            try:
                await asyncio.wait_for(_shared.done[sid].wait(), timeout=TRIAL_TIMEOUT_SECONDS)
                status = _shared.status.get(sid, "error")
            except TimeoutError:
                status = "timeout"
                await manager.stop(sid)
            messages = await manager.transcript(sid)
            turns, calls = count_turns(messages)
            usage = await _shared.db.fetchone("SELECT sum(input_tokens) i, sum(cache_read_tokens) ch, sum(output_tokens) o, sum(cost_usd) usd, sum(cost_usd IS NULL) unmetered FROM usage_events WHERE session_id = ?", (sid,))
            if usage is not None:
                context.n_input_tokens = int(usage["i"] or 0)
                context.n_cache_tokens = int(usage["ch"] or 0)
                context.n_output_tokens = int(usage["o"] or 0)
                context.cost_usd = None if (usage["unmetered"] or 0) else (float(usage["usd"]) if usage["usd"] is not None else None)
            context.metadata = {"status": status, "turns": turns, "tool_calls": calls, "wall_seconds": round(time.monotonic() - clock, 1), "session_id": sid}
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            (self.logs_dir / "trajectory.json").write_text(json.dumps({"session": sid, "status": status, "steps": trajectory(messages)}, ensure_ascii=False, indent=1), encoding="utf-8")
        finally:
            _shared.done.pop(sid, None)
            try:
                await manager.delete_session(sid, delete_workspace=False)
            except Exception:  # noqa: BLE001
                logger.exception("could not delete harbor session %s", sid)


__all__ = ["CONTAINER_WORKSPACE", "DaedalusAgent", "HarborExecBackend"]
