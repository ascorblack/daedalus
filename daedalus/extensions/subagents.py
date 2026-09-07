"""Subagents: helper sessions a leader spawns into its own workspace.

A subagent is a session created for one task. It shares the leader's workspace (files it
writes are the leader's files), runs on the leader's model unless another preset is named,
and reports back when its run ends: the report is delivered to the leader as a message
from ``subagent:<name>`` — queued as a follow-up while the leader is still running, or
starting a new leader turn when the leader has already finished. A leader may therefore
hand off work and end its turn; the result arrives on its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from protocore.contracts.types import MessageRole, TextBlock

from daedalus.host.prompts import split_headline

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

POLL_SECONDS = 3.0
NO_ANSWER = "(the subagent's run ended without a final reply; see its session)"

TASK_HEADER = (
    "[task from your leader session {leader} via SubAgent — you work in the leader's workspace; "
    "do the task, then put the complete result in your final reply: it is delivered to the leader "
    "verbatim, and the leader sees nothing else you wrote]\n\n"
)

BRIEF = (
    "You are a subagent of session {leader}: a helper started for one task. Work in the shared workspace "
    "(the leader reads the files you leave there), do not ask the operator questions, do not start "
    "subagents of your own, and end with a final reply that contains everything the leader needs."
)


class Subagents:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._waited: set[str] = set()

    async def models(self) -> list[str]:
        """Preset ids a subagent can run on: those whose provider is configured."""
        manager = self.app.manager
        assert manager is not None
        available = set(manager.providers.available())
        return [pid for pid, preset in self.app.config.presets.items() if preset.provider in available and preset.model]

    async def spawn(
        self,
        *,
        leader_id: str,
        task: str,
        model: str | None = None,
        name: str | None = None,
        wait: bool = False,
        timeout_minutes: int | None = None,
    ) -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        leader = await manager.get_state(leader_id)
        if leader is None:
            raise ValueError("unknown leader session")
        task = task.strip()
        if not task:
            raise ValueError("the task is empty")
        depth = int(leader.metadata.get("subagent_depth", 0)) + 1
        if depth > self.app.config.subagents.max_depth:
            raise ValueError(f"subagent chain too deep ({depth} > {self.app.config.subagents.max_depth}); do this yourself")
        active = [s for s in await self.children(leader_id) if s["running"]]
        if len(active) >= self.app.config.subagents.max_active:
            raise RuntimeError(f"{len(active)} subagents are already running for this session (limit {self.app.config.subagents.max_active}); wait for their reports")
        models = await self.models()
        if model is not None:
            model = model.strip()
            if model not in models:
                raise ValueError(f"unknown model {model!r}; choose one of: {', '.join(models)}")
        label = (name or task.splitlines()[0])[:48].strip()
        metadata: dict[str, Any] = {
            "subagent_of": leader_id,
            "subagent_name": label,
            "subagent_depth": depth,
            "workspace": str(leader.workspace),
            "brief": BRIEF.format(leader=leader_id),
            "unattended": True,
        }
        for key in ("mode", "mcp"):
            if leader.metadata.get(key) is not None:
                metadata[key] = leader.metadata[key]
        child = await manager.create_session(f"[sub] {label}", workspace=leader.workspace, metadata=metadata)
        cid = child.session.id
        if model is not None:
            await manager.set_model(cid, preset=model)
        else:
            overrides = await manager.live.load(leader_id)
            if overrides.get("preset") or (overrides.get("provider") and overrides.get("model_name")):
                await manager.live.set_model(
                    cid,
                    model_name=overrides.get("model_name"),
                    provider=overrides.get("provider"),
                    preset=overrides.get("preset"),
                    thinking_enabled=overrides.get("thinking_enabled"),
                    reasoning_effort=overrides.get("reasoning_effort"),
                )
        if wait:
            self._waited.add(cid)
        run_id = await manager.submit(cid, TASK_HEADER.format(leader=leader_id) + task, as_answer=False, origin=f"subagent-task:{leader_id}")
        result: dict[str, Any] = {"session_id": cid, "run_id": run_id, "name": label, "model": model or "(leader's model)", "answer": None}
        if not wait:
            return result
        deadline = asyncio.get_running_loop().time() + 60 * (timeout_minutes or self.app.config.subagents.wait_timeout_minutes)
        try:
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(POLL_SECONDS)
                state = await manager.get_state(cid)
                if state is None:
                    raise RuntimeError("the subagent session disappeared")
                if not state.running and state.pending is None:
                    result["answer"] = await self.answer(cid) or NO_ANSWER
                    return result
            result["note"] = "the subagent did not finish in time; its report will arrive as a message when it does"
        finally:
            self._waited.discard(cid)  # on a timeout the async report takes over
        return result

    async def children(self, leader_id: str) -> list[dict[str, Any]]:
        manager = self.app.manager
        assert manager is not None
        out = []
        for row in await manager.list_sessions(limit=500):
            if row.get("metadata", {}).get("subagent_of") != leader_id:
                continue
            state = await manager.get_state(row["id"])
            out.append({"session_id": row["id"], "name": row["metadata"].get("subagent_name"), "running": bool(state and state.running), "status": row["status"]})
        return out

    async def answer(self, session_id: str) -> str | None:
        manager = self.app.manager
        assert manager is not None
        rows = await manager.sessions.list_transcript(session_id)
        for m in reversed(rows):
            if m.role is MessageRole.assistant:
                text = "".join(b.text for b in m.content_blocks if isinstance(b, TextBlock)).strip()
                if text:
                    return split_headline(text)[0]
        return None

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        """Deliver a finished subagent's final reply to its leader."""
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(session_id)
        if state is None or not state.metadata.get("subagent_of") or status == "awaiting":
            return
        if session_id in self._waited:
            return  # the leader's tool call collects the answer itself
        leader_id = str(state.metadata["subagent_of"])
        leader = await manager.get_state(leader_id)
        if leader is None:
            logger.warning("subagent %s finished but its leader %s is gone", session_id, leader_id)
            return
        name = str(state.metadata.get("subagent_name") or session_id)
        answer = await self.answer(session_id) if status == "completed" else None
        body = answer or NO_ANSWER
        report = f"[subagent {name!r} (session {session_id}) finished: {status}. Its report follows; the files it made are in your workspace.]\n\n{body}"
        try:
            await manager.submit(leader_id, report, as_answer=False, origin=f"subagent:{name}")
        except Exception:  # noqa: BLE001
            logger.exception("could not deliver subagent %s report to %s", session_id, leader_id)

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "spawn":
            return await self.spawn(**kwargs)
        if op == "models":
            return await self.models()
        if op == "children":
            return await self.children(kwargs["leader_id"])
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    subagents = Subagents(app)
    app.extensions["subagents"] = subagents
    assert app.manager is not None
    app.manager.service_hooks["subagents"] = subagents.service
    app.manager.on_finished(subagents.on_run_finished)
    return []
