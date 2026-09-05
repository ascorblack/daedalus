"""Scheduled tasks: cron or one-shot, each with a persistent workspace and a summary handoff."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from croniter import croniter
from protocore.contracts.types import MessageRole, TextBlock

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class Scheduler:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._active: dict[str, str] = {}  # schedule id -> session id while a run is active

    @property
    def root(self) -> Path:
        return self.app.settings.workspaces_dir

    # -- CRUD -----------------------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        prompt: str,
        cron: str | None,
        run_at: str | None,
        files: list[str] | None = None,
        model: str | None = None,
        created_by_session: str | None = None,
    ) -> dict[str, Any]:
        schedule_id = uuid.uuid4().hex[:8]
        if cron:
            if not croniter.is_valid(cron):
                raise ValueError(f"invalid cron expression: {cron!r}")
            next_run = croniter(cron, _now()).get_next(datetime)
            recurring = 1
        else:
            if not run_at:
                raise ValueError("give either cron or run_at")
            try:
                next_run = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"invalid run_at: {run_at!r} (use ISO 8601)") from exc
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=UTC)
            recurring = 0
        workspace = self.root / f"sched-{schedule_id}"
        (workspace / "inbox").mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for source in files or []:
            src = Path(source)
            if src.is_file():
                dst = workspace / "inbox" / src.name
                shutil.copy2(src, dst)
                copied.append(str(dst))
        await self.app.db.execute(
            "INSERT INTO schedules(id, name, cron, run_at, prompt, files, model, recurring, enabled, workspace,"
            " next_run_at, created_by_session, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                schedule_id, name, cron, run_at, prompt, json.dumps(copied), model, recurring,
                str(workspace), next_run.isoformat(), created_by_session, _now().isoformat(),
            ),
        )
        return {"id": schedule_id, "name": name, "next_run_at": next_run.isoformat(), "workspace": str(workspace)}

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.app.db.fetchall("SELECT * FROM schedules ORDER BY next_run_at")
        return [dict(r) for r in rows]

    async def delete(self, schedule_id: str) -> bool:
        row = await self.app.db.fetchone("SELECT id FROM schedules WHERE id = ?", (schedule_id,))
        if row is None:
            return False
        await self.app.db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        return True

    async def set_enabled(self, schedule_id: str, enabled: bool) -> None:
        await self.app.db.execute("UPDATE schedules SET enabled = ? WHERE id = ?", (int(enabled), schedule_id))

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "create":
            return await self.create(**kwargs)
        if op == "list":
            return await self.list()
        if op == "delete":
            return await self.delete(kwargs["schedule_id"])
        raise ValueError(op)

    # -- loop -----------------------------------------------------------------------

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler tick failed")
            await asyncio.sleep(30)

    async def tick(self) -> None:
        now = _now()
        rows = await self.app.db.fetchall(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (now.isoformat(),),
        )
        for row in rows:
            if row["id"] in self._active:
                continue
            due = datetime.fromisoformat(row["next_run_at"])
            stale = now - due > timedelta(hours=1)
            if stale and not self.app.config.scheduler.catch_up_missed:
                await self._advance(dict(row), ran=False)
                continue
            await self.fire(dict(row))

    async def fire(self, schedule: dict[str, Any]) -> str:
        manager = self.app.manager
        front = self.app.front
        assert manager is not None
        workspace = Path(schedule["workspace"])
        (workspace / "inbox").mkdir(parents=True, exist_ok=True)
        title = f"[cron] {schedule['name']}"
        metadata: dict[str, Any] = {"workspace": str(workspace), "schedule_id": schedule["id"]}
        if schedule.get("model"):
            metadata["model"] = schedule["model"]
        per_task = self.app.config.scheduler.topic_mode == "per_task"
        if front is not None and per_task and schedule.get("topic_thread_id") and self.app.config.telegram.forum_chat_id:
            state = await manager.create_session(title, workspace=workspace, metadata=metadata)
            await front._bind(self.app.config.telegram.forum_chat_id, int(schedule["topic_thread_id"]), state.session.id, title)
        elif front is not None:
            state, binding = await front.create_session_topic(title, metadata=metadata)
            if state.workspace != workspace:
                state.workspace = workspace
                manager._register_services(state)
            if per_task:
                await self.app.db.execute("UPDATE schedules SET topic_thread_id = ? WHERE id = ?", (binding.thread_id, schedule["id"]))
        else:
            state = await manager.create_session(title, workspace=workspace, metadata=metadata)
        prompt = schedule["prompt"]
        files = json.loads(schedule.get("files") or "[]")
        if files:
            prompt += "\n\nFiles attached to this task:\n" + "\n".join(f"- {f}" for f in files)
        if schedule.get("last_summary"):
            prompt += f"\n\nSummary of the previous run ({schedule.get('last_run_at')}):\n{schedule['last_summary'][:6000]}"
        prompt += "\n\nWhen finished, write SUMMARY.md in the workspace root describing what was done and anything the next run should know."
        self._active[schedule["id"]] = state.session.id
        await self.app.db.execute("UPDATE schedules SET last_run_at = ? WHERE id = ?", (_now().isoformat(), schedule["id"]))
        # The next occurrence is fixed before dispatch so a restart cannot fire the same slot twice.
        await self._advance(schedule, ran=True)
        # Attached files already live in the task workspace inbox; the prompt lists their paths.
        await manager.submit(state.session.id, prompt, [])
        return state.session.id

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        for schedule_id, sid in list(self._active.items()):
            if sid != session_id:
                continue
            if status == "awaiting":
                continue  # the operator still has to answer; the summary is collected when the run ends
            self._active.pop(schedule_id, None)
            row = await self.app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
            if row is None:
                continue
            summary = await self._collect_summary(dict(row), session_id)
            await self.app.db.execute("UPDATE schedules SET last_summary = ? WHERE id = ?", (summary, schedule_id))

    async def _collect_summary(self, schedule: dict[str, Any], session_id: str) -> str:
        summary_file = Path(schedule["workspace"]) / "SUMMARY.md"
        if summary_file.is_file():
            return summary_file.read_text(encoding="utf-8")[-8000:]
        manager = self.app.manager
        assert manager is not None
        messages = await manager.sessions.list_messages(session_id, "daedalus", limit=10_000)
        for message in reversed(messages):
            if message.role is MessageRole.assistant:
                text = "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock)).strip()
                if text:
                    return text[-8000:]
        return ""

    async def _advance(self, schedule: dict[str, Any], *, ran: bool) -> None:
        if schedule.get("cron"):
            next_run = croniter(schedule["cron"], _now()).get_next(datetime)
            await self.app.db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (next_run.isoformat(), schedule["id"]))
        else:
            await self.app.db.execute("UPDATE schedules SET enabled = 0, next_run_at = NULL WHERE id = ?", (schedule["id"],))


async def install(app: Application) -> list[asyncio.Task[None]]:
    scheduler = Scheduler(app)
    app.extensions["scheduler"] = scheduler
    assert app.manager is not None
    app.manager.service_hooks["schedule"] = scheduler.service
    app.manager.on_finished(scheduler.on_run_finished)
    front = app.front
    if front is not None:

        async def cmd_schedules(message, command) -> None:  # type: ignore[no-untyped-def]
            items = await scheduler.list()
            if not items:
                await message.answer("No scheduled tasks. The agent creates them with schedule_create.")
                return
            lines = [
                f"{'✓' if s['enabled'] else '✗'} {s['id']} {s['name']} — {('cron ' + s['cron']) if s['cron'] else 'once'} · next {s['next_run_at'] or '-'}"
                for s in items
            ]
            await message.answer("\n".join(lines) + "\n\n/schedule delete <id> · /schedule on|off <id>")

        async def cmd_schedule(message, command) -> None:  # type: ignore[no-untyped-def]
            parts = (command.args or "").split()
            if len(parts) == 2 and parts[0] == "delete":
                await message.answer("deleted" if await scheduler.delete(parts[1]) else "no such schedule")
            elif len(parts) == 2 and parts[0] in ("on", "off"):
                await scheduler.set_enabled(parts[1], parts[0] == "on")
                await message.answer(f"{parts[1]}: {parts[0]}")
            elif len(parts) == 2 and parts[0] == "run":
                row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (parts[1],))
                if row is None:
                    await message.answer("no such schedule")
                else:
                    sid = await scheduler.fire(dict(row))
                    await message.answer(f"started session {sid}")
            else:
                await message.answer("usage: /schedule delete <id> | on <id> | off <id> | run <id>")

        front.command_hooks["schedules"] = cmd_schedules
        front.command_hooks["schedule"] = cmd_schedule
    return [asyncio.create_task(scheduler.loop(), name="scheduler")]


__all__ = ["Scheduler", "install"]
