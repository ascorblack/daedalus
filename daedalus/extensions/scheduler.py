"""Scheduled tasks: cron or one-shot, in three kinds.

* ``agent`` — a prompt for a fresh session with its own persistent workspace and a summary
  handoff between runs (the default).
* ``message`` — a plain reminder: the text is delivered to the operator, no model call.
* ``lazy`` — a silent note that rides along with the operator's next message in a session
  ("when I next write, remind me…"); if the operator does not write within the TTL it is
  promoted to an ``agent`` task so it is never lost.

A recurring task never overlaps itself, a task that keeps failing is switched off with an
inbox entry, an unattended run that asks a question continues on its own judgement after a
timeout, and everything a task produces lands in the inbox.
"""

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

KINDS = ("agent", "message", "lazy")
UNATTENDED_ANSWER = (
    "No operator is available for this unattended run. Continue with your best judgement, "
    "prefer the safe and reversible option, and state the assumption you made in your final reply."
)


def _now() -> datetime:
    return datetime.now(UTC)


class Scheduler:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._active: dict[str, str] = {}  # schedule id -> session id while a run is active

    @property
    def root(self) -> Path:
        return self.app.settings.workspaces_dir

    def _inbox(self):  # type: ignore[no-untyped-def]
        return self.app.extensions.get("inbox")

    async def _post(self, kind: str, title: str, body: str = "", **kw: Any) -> None:
        inbox = self._inbox()
        if inbox is not None:
            await inbox.post(kind, title, body, **kw)

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
        kind: str = "agent",
        target_session: str | None = None,
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
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
        if kind == "lazy" and not (target_session or created_by_session):
            raise ValueError("a lazy reminder needs the session it belongs to")
        workspace = self.root / f"sched-{schedule_id}"
        copied: list[str] = []
        if kind == "agent":
            (workspace / "inbox").mkdir(parents=True, exist_ok=True)
            for source in files or []:
                src = Path(source)
                if src.is_file():
                    dst = workspace / "inbox" / src.name
                    shutil.copy2(src, dst)
                    copied.append(str(dst))
        await self.app.db.execute(
            "INSERT INTO schedules(id, name, cron, run_at, prompt, files, model, recurring, enabled, workspace,"
            " next_run_at, created_by_session, created_at, kind, target_session) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (
                schedule_id, name, cron, run_at, prompt, json.dumps(copied), model, recurring,
                str(workspace), next_run.isoformat(), created_by_session, _now().isoformat(), kind,
                target_session or created_by_session,
            ),
        )
        return {"id": schedule_id, "name": name, "kind": kind, "next_run_at": next_run.isoformat(), "workspace": str(workspace)}

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
        await self.app.db.execute(
            "UPDATE schedules SET enabled = ?, failure_count = CASE WHEN ? THEN 0 ELSE failure_count END WHERE id = ?",
            (int(enabled), int(enabled), schedule_id),
        )

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
                continue  # a recurring task never runs in parallel with itself
            due = datetime.fromisoformat(row["next_run_at"])
            stale = now - due > timedelta(hours=1)
            if stale and not self.app.config.scheduler.catch_up_missed:
                await self._advance(dict(row), ran=False)
                await self._post("schedule_missed", f"Missed run of '{row['name']}' skipped", f"It was due {row['next_run_at']}; catch-up is off.", severity="notice")
                continue
            if stale:
                await self._post("schedule_missed", f"Late run of '{row['name']}'", f"It was due {row['next_run_at']} (the bot was down); running now.", severity="notice")
            await self.fire(dict(row))
        await self._promote_lazy_notes(now)
        await self._answer_stale_questions(now)

    async def fire(self, schedule: dict[str, Any]) -> str:
        kind = schedule.get("kind") or "agent"
        # The next occurrence is fixed before dispatch so a restart cannot fire the same slot twice.
        await self.app.db.execute("UPDATE schedules SET last_run_at = ? WHERE id = ?", (_now().isoformat(), schedule["id"]))
        await self._advance(schedule, ran=True)
        if kind == "message":
            return await self._fire_message(schedule)
        if kind == "lazy":
            return await self._fire_lazy(schedule)
        return await self._fire_agent(schedule)

    async def _fire_message(self, schedule: dict[str, Any]) -> str:
        """A plain reminder: deliver the text where the task was created, or to the operator channel."""
        front = self.app.front
        text = f"⏰ **{schedule['name']}**\n\n{schedule['prompt']}"
        delivered = False
        if front is not None:
            outbox = await front.outbox_for_session(schedule["target_session"]) if schedule.get("target_session") else None
            try:
                if outbox is not None:
                    await outbox.send_text(text)
                else:
                    await front.notify(text)
                delivered = True
            except Exception:  # noqa: BLE001
                logger.warning("reminder delivery failed", exc_info=True)
        await self._post("reminder", schedule["name"], schedule["prompt"], severity="notice" if delivered else "warning", session_id=schedule.get("target_session"))
        return ""

    async def _fire_lazy(self, schedule: dict[str, Any]) -> str:
        """A silent note: it rides along with the operator's next message in the target session."""
        await self.app.db.execute(
            "INSERT INTO lazy_notes(schedule_id, session_id, text, fired_at) VALUES (?, ?, ?, ?)",
            (schedule["id"], schedule["target_session"], schedule["prompt"], _now().isoformat()),
        )
        return ""

    async def pending_lazy_notes(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self.app.db.fetchall("SELECT * FROM lazy_notes WHERE session_id = ? AND delivered_at IS NULL AND promoted_at IS NULL ORDER BY id", (session_id,))
        return [dict(r) for r in rows]

    async def decorate_prompt(self, session_id: str, text: str) -> str:
        """Prepend fired lazy reminders to the operator's message; they count as delivered once the run starts."""
        notes = await self.pending_lazy_notes(session_id)
        if not notes:
            return text
        block = "\n".join(f"[Reminder fired {n['fired_at'][:16].replace('T', ' ')} UTC, id={n['id']}] {n['text']}" for n in notes)
        marks = ",".join("?" for _ in notes)
        await self.app.db.execute(f"UPDATE lazy_notes SET delivered_at = ? WHERE id IN ({marks})", (_now().isoformat(), *[n["id"] for n in notes]))
        return f"{block}\n\n{text}"

    async def _promote_lazy_notes(self, now: datetime) -> None:
        """A lazy note nobody has seen for a day becomes a task the agent runs itself."""
        cutoff = (now - timedelta(hours=self.app.config.scheduler.lazy_ttl_hours)).isoformat()
        rows = await self.app.db.fetchall("SELECT * FROM lazy_notes WHERE delivered_at IS NULL AND promoted_at IS NULL AND fired_at <= ?", (cutoff,))
        for row in rows:
            await self.app.db.execute("UPDATE lazy_notes SET promoted_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
            manager = self.app.manager
            if manager is None:
                continue
            state = await manager.get_state(row["session_id"])
            prompt = f"[Reminder fired {row['fired_at'][:16].replace('T', ' ')} UTC; the operator did not return in time, so act on it yourself] {row['text']}"
            try:
                if state is not None and not state.running and state.pending is None:
                    await manager.submit(row["session_id"], prompt, as_answer=False)
                else:
                    await self.run_task_session(f"[reminder] {row['text'][:40]}", prompt, self.root / f"lazy-{row['id']}", {"lazy_note_id": row["id"]})
                await self._post("reminder_promoted", "A lazy reminder became a task", row["text"], severity="notice", session_id=row["session_id"])
            except Exception:  # noqa: BLE001
                logger.exception("could not promote lazy note %s", row["id"])

    async def _answer_stale_questions(self, now: datetime) -> None:
        """An unattended run waiting on AskUser for too long continues on its own judgement."""
        manager = self.app.manager
        if manager is None or not self._active:
            return
        timeout = timedelta(minutes=self.app.config.scheduler.question_timeout_minutes)
        for schedule_id, session_id in list(self._active.items()):
            state = await manager.get_state(session_id)
            if state is None or state.pending is None:
                continue
            row = await self.app.db.fetchone("SELECT created_at FROM pending_questions WHERE session_id = ?", (session_id,))
            if row is None or now - datetime.fromisoformat(row["created_at"]) < timeout:
                continue
            try:
                await manager.answer(session_id, [{"custom": UNATTENDED_ANSWER}])
                await self._post("schedule_question_timeout", "An unattended run waited too long for an answer", f"Task {schedule_id}: the question was answered with 'continue on your own judgement'.", severity="warning", session_id=session_id)
            except RuntimeError:
                pass

    async def run_task_session(self, title: str, prompt: str, workspace: Path, metadata: dict[str, Any], *, preset: str | None = None) -> Any:
        """Create the session (and topic) an unattended task runs in, and start it."""
        manager = self.app.manager
        front = self.app.front
        assert manager is not None
        (workspace / "inbox").mkdir(parents=True, exist_ok=True)
        metadata = {**metadata, "workspace": str(workspace)}
        if front is not None:
            state, _ = await front.create_session_topic(title, metadata=metadata)
            if state.workspace != workspace:
                state.workspace = workspace
                manager._register_services(state)
        else:
            state = await manager.create_session(title, workspace=workspace, metadata=metadata)
        if preset:
            await manager.set_model(state.session.id, preset=preset)
        await manager.submit(state.session.id, prompt, [], as_answer=False)
        return state

    async def _fire_agent(self, schedule: dict[str, Any]) -> str:
        manager = self.app.manager
        front = self.app.front
        assert manager is not None
        workspace = Path(schedule["workspace"])
        (workspace / "inbox").mkdir(parents=True, exist_ok=True)
        title = f"[cron] {schedule['name']}"
        metadata: dict[str, Any] = {"workspace": str(workspace), "schedule_id": schedule["id"], "unattended": True}
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
        prompt += (
            "\n\nThis is an unattended scheduled run: no operator is watching. If the check finds nothing that needs "
            "attention, call StaySilent instead of writing that there is nothing new. When finished, write SUMMARY.md "
            "in the workspace root describing what was done and anything the next run should know."
        )
        self._active[schedule["id"]] = state.session.id
        await manager.submit(state.session.id, prompt, [], as_answer=False)
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
            if status == "failed":
                failures = int(row["failure_count"] or 0) + 1
                limit = self.app.config.scheduler.max_failures
                disable = bool(row["recurring"]) and failures >= limit
                await self.app.db.execute(
                    "UPDATE schedules SET failure_count = ?, last_error = ?, enabled = CASE WHEN ? THEN 0 ELSE enabled END WHERE id = ?",
                    (failures, f"run {run_id} failed", int(disable), schedule_id),
                )
                await self._post(
                    "schedule_failed",
                    f"'{row['name']}' failed ({failures}/{limit})" + (" — switched off" if disable else ""),
                    summary[:2000] or "The run ended with an error.",
                    severity="error" if disable else "warning",
                    session_id=session_id,
                    run_id=run_id,
                )
                continue
            await self.app.db.execute("UPDATE schedules SET last_summary = ?, failure_count = 0, last_error = NULL WHERE id = ?", (summary, schedule_id))
            services = self.app.manager.locator_services(session_id) if self.app.manager else None
            quiet = services is not None and services.extra.get("silent_run") == run_id
            note = str(services.extra.get("silent_note") or "") if quiet and services is not None else ""
            await self._post(
                "schedule_run",
                f"'{row['name']}': " + ("quiet, nothing to report" if quiet else status),
                note if quiet else summary[:4000],
                severity="info",
                session_id=session_id,
                run_id=run_id,
            )

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
    app.manager.prompt_hooks.append(scheduler.decorate_prompt)
    front = app.front
    if front is not None:

        async def cmd_schedules(message, command) -> None:  # type: ignore[no-untyped-def]
            items = await scheduler.list()
            if not items:
                await message.answer("No scheduled tasks. The agent creates them with ScheduleCreate.")
                return
            lines = [
                f"{'✓' if s['enabled'] else '✗'} {s['id']} [{s.get('kind') or 'agent'}] {s['name']} — {('cron ' + s['cron']) if s['cron'] else 'once'}"
                f" · next {s['next_run_at'] or '-'}" + (f" · failures {s['failure_count']}" if s.get("failure_count") else "")
                for s in items
            ]
            await message.answer("\n".join(lines) + "\n\n/schedule delete <id> · /schedule on|off <id> · /schedule run <id>")

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
                    await message.answer(f"started session {sid}" if sid else "fired")
            else:
                await message.answer("usage: /schedule delete <id> | on <id> | off <id> | run <id>")

        front.command_hooks["schedules"] = cmd_schedules
        front.command_hooks["schedule"] = cmd_schedule
    return [asyncio.create_task(scheduler.loop(), name="scheduler")]


__all__ = ["KINDS", "Scheduler", "install"]
