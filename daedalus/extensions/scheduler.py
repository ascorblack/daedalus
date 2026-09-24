"""Scheduled tasks: cron or one-shot, in three kinds.

* ``agent`` — a prompt for a fresh session with its own persistent workspace and a summary
  handoff between runs (the default).
* ``message`` — a plain reminder: the text is delivered to the operator, no model call.
* ``lazy`` — a silent note that rides along with the operator's next message in a session
  ("when I next write, remind me…"); if the operator does not write within the TTL it is
  promoted to an ``agent`` task so it is never lost.

A recurring task never overlaps itself (the in-flight run is recorded on the row, so a
restart remembers it), a task that keeps failing is switched off with an inbox entry, an
unattended run that asks a question continues on its own judgement after a timeout, and
everything a task produces lands in the inbox. Side effects of a reminder (marking a lazy
note delivered, a slot consumed) are committed only once the run actually exists.
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

from daedalus.config import NO_MODEL_MESSAGE, NoModelConfigured
from daedalus.extensions.notifications import Category, Draft, Level, Tone

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

RUN_IN = ("new", "self")
"""Where an agent task runs when it fires: a fresh task session, or the session that created it."""
KINDS = ("agent", "message", "lazy")
UNATTENDED_ANSWER = (
    "No operator is available for this unattended run. Continue with your best judgement, "
    "prefer the safe and reversible option, and state the assumption you made in your final reply."
)
PROMOTE_MAX_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(UTC)


class Scheduler:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._active: dict[str, str] = {}  # schedule id -> session id while a run is active
        self._active_runs: dict[str, str] = {}  # schedule id -> run id, so a session's other runs are not mistaken for the task's
        self._delivering: dict[str, list[int]] = {}  # session id -> lazy note ids folded into a message not yet started
        self._maintained_at: datetime | None = None  # last database housekeeping pass
        self._retention: asyncio.Task[None] | None = None  # the checkpoint pass, which runs off the tick

    @property
    def root(self) -> Path:
        return self.app.settings.workspaces_dir

    async def _project_of(self, session_id: str | None):  # type: ignore[no-untyped-def]
        """The project the session that owns a schedule works in, or None.

        A task created from a project session fires in that project: same folder, same wall. It is
        read at both ends — when the schedule is stored and when it fires — rather than copied into
        the row, so a project the operator moves afterwards moves its tasks with it.
        """
        manager = self.app.manager
        if manager is None or not session_id:
            return None
        return await manager.project_of(session_id)

    async def _post(
        self,
        kind: str,
        title: str,
        body: str = "",
        *,
        tone: Tone = "info",
        level: Level | None = None,
        category: Category = "system",
        session_id: str | None = None,
        run_id: str | None = None,
        handled: frozenset[str] = frozenset(),
    ) -> None:
        notifications = self.app.notifications
        if notifications is not None:
            await notifications.post(Draft(category, title, body, kind=kind, tone=tone, level=level, session_id=session_id, run_id=run_id, handled=handled, source="scheduler"))

    async def restore(self) -> None:
        """Rebuild the in-flight map from the rows after a restart; settle runs that are already over."""
        manager = self.app.manager
        if manager is None:
            return
        rows = await self.app.db.fetchall("SELECT * FROM schedules WHERE active_session_id IS NOT NULL")
        for row in rows:
            state = await manager.get_state(row["active_session_id"])
            if state is not None and (state.running or state.pending is not None):
                self._active[row["id"]] = row["active_session_id"]
                if row["active_run_id"]:
                    self._active_runs[row["id"]] = row["active_run_id"]
            else:
                self._active[row["id"]] = row["active_session_id"]
                run_status = "completed"
                if row["active_run_id"]:
                    run = await self.app.db.fetchone("SELECT status FROM runs WHERE id = ?", (row["active_run_id"],))
                    run_status = "failed" if run and run["status"] == "error" else "completed"
                await self.on_run_finished(row["active_session_id"], row["active_run_id"] or "", run_status)

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
        run_in: str = "new",
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if run_in not in RUN_IN:
            raise ValueError(f"run_in must be one of {', '.join(RUN_IN)}")
        if run_in == "self" and kind != "agent":
            raise ValueError("run_in='self' applies to agent tasks only")
        if run_in == "self" and not (target_session or created_by_session):
            raise ValueError("a task that runs in its own session needs that session")
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
        project = await self._project_of(target_session or created_by_session)
        if kind == "agent" and run_in == "self":
            manager = self.app.manager
            owner = await manager.get_state(target_session or created_by_session or "") if manager is not None else None
            if owner is None:
                raise ValueError("the session this task should run in does not exist")
            workspace = owner.workspace
            copied = [str(Path(f)) for f in files or [] if Path(f).is_file()]
        elif kind == "agent" and project is not None:
            # The files are already in the folder the task will run in; copying them into a directory
            # of the task's own would take the operator's files out of the project they chose.
            workspace = project.root
            copied = [str(Path(f)) for f in files or [] if Path(f).is_file()]
        elif kind == "agent":
            (workspace / "inbox").mkdir(parents=True, exist_ok=True)
            for source in files or []:
                src = Path(source)
                if src.is_file():
                    dst = workspace / "inbox" / src.name
                    shutil.copy2(src, dst)
                    copied.append(str(dst))
        await self.app.db.execute(
            "INSERT INTO schedules(id, name, cron, run_at, prompt, files, model, recurring, enabled, workspace,"
            " next_run_at, created_by_session, created_at, kind, target_session, run_in) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (
                schedule_id, name, cron, run_at, prompt, json.dumps(copied), model, recurring,
                str(workspace), next_run.isoformat(), created_by_session, _now().isoformat(), kind,
                target_session or created_by_session, run_in,
            ),
        )
        return {"id": schedule_id, "name": name, "kind": kind, "run_in": run_in, "next_run_at": next_run.isoformat(), "workspace": str(workspace)}

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.app.db.fetchall("SELECT * FROM schedules ORDER BY next_run_at")
        return [dict(r) for r in rows]

    async def delete(self, schedule_id: str) -> bool:
        row = await self.app.db.fetchone("SELECT id FROM schedules WHERE id = ?", (schedule_id,))
        if row is None:
            return False
        async with self.app.db.transaction() as conn:
            await conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            await conn.execute("DELETE FROM lazy_notes WHERE schedule_id = ? AND delivered_at IS NULL AND promoted_at IS NULL", (schedule_id,))
        self._active.pop(schedule_id, None)
        self._active_runs.pop(schedule_id, None)
        return True

    async def update(self, schedule_id: str, **fields: Any) -> dict[str, Any]:
        """Change a schedule in place: its name, prompt, cadence or moment; a new cadence moves the next run."""
        row = await self.app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if row is None:
            raise KeyError(schedule_id)
        current = dict(row)
        sets: list[str] = []
        values: list[Any] = []
        if "name" in fields and fields["name"] is not None:
            name = str(fields["name"]).strip()
            if not name:
                raise ValueError("the name cannot be empty")
            sets.append("name = ?")
            values.append(name)
        if "prompt" in fields and fields["prompt"] is not None:
            prompt = str(fields["prompt"]).strip()
            if not prompt:
                raise ValueError("the prompt cannot be empty")
            sets.append("prompt = ?")
            values.append(prompt)
        # A new moment without a cadence turns a recurring schedule into a one-off; a cadence wins when both are given.
        cron = fields["cron"] if "cron" in fields else (None if fields.get("run_at") else current["cron"])
        run_at = fields["run_at"] if "run_at" in fields else current["run_at"]
        if "cron" in fields or "run_at" in fields:
            if cron:
                if not croniter.is_valid(cron):
                    raise ValueError(f"invalid cron expression: {cron!r}")
                next_run = croniter(cron, _now()).get_next(datetime)
                run_at = None
                recurring = 1
            else:
                if not run_at:
                    raise ValueError("give either cron or run_at")
                try:
                    next_run = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(f"invalid run_at: {run_at!r} (use ISO 8601)") from exc
                if next_run.tzinfo is None:
                    next_run = next_run.replace(tzinfo=UTC)
                recurring = 0
            sets += ["cron = ?", "run_at = ?", "recurring = ?", "next_run_at = ?"]
            values += [cron, run_at, recurring, next_run.isoformat()]
        if sets:
            values.append(schedule_id)
            await self.app.db.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", tuple(values))
        if "enabled" in fields and fields["enabled"] is not None:
            await self.set_enabled(schedule_id, bool(fields["enabled"]))
        fresh = await self.app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        return dict(fresh) if fresh is not None else current

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
        try:
            while True:
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001
                    logger.exception("scheduler tick failed")
                await asyncio.sleep(30)
        finally:
            # The maintenance the tick started runs on its own; it goes down with the loop that
            # started it rather than being left behind for the interpreter to complain about.
            if self._retention is not None and not self._retention.done():
                self._retention.cancel()

    async def tick(self) -> None:
        now = _now()
        rows = await self.app.db.fetchall(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (now.isoformat(),),
        )
        for row in rows:
            if row["id"] in self._active:
                continue  # a recurring task never runs in parallel with itself
            schedule = dict(row)
            try:
                due = datetime.fromisoformat(row["next_run_at"])
                stale = now - due > timedelta(hours=1)
                if stale and not self.app.config.scheduler.catch_up_missed:
                    await self._advance(schedule, ran=False)
                    await self._post("schedule_missed", f"Missed run of '{row['name']}' skipped", f"It was due {row['next_run_at']}; catch-up is off.")
                    continue
                if stale:
                    await self._post("schedule_missed", f"Late run of '{row['name']}'", f"It was due {row['next_run_at']} (the bot was down); running now.")
                await self.fire(schedule)
            except NoModelConfigured:
                # Not this schedule's failure and not something it can recover from by being counted
                # against: the slot is skipped, the operator is told once, and the schedule is still
                # there when a model is added. Counted as a failure, a fresh install would switch off
                # every schedule it ships before anyone had configured a model.
                await self._advance(schedule, ran=False)
                await self._post("schedule_no_model", f"'{row['name']}' was skipped", NO_MODEL_MESSAGE)
            except Exception as exc:  # noqa: BLE001 — one bad task must not skip the rest of the tick
                logger.exception("schedule %s could not fire", row["id"])
                await self._record_start_failure(schedule, f"{type(exc).__name__}: {exc}")
        await self._promote_lazy_notes(now)
        await self._answer_stale_questions(now)
        if self.app.notifications is not None:
            await self.app.notifications.prune(self.app.config.notifications.keep_days)
        await self._maintain_database(now)

    async def _maintain_database(self, now: datetime) -> None:
        """The database's own housekeeping, on the tick that already runs: sweep old events, reclaim
        pages, and bring the workspace snapshot stores back inside their bounds.

        Per-run trimming bounds a run and nothing bounded the table; and with auto_vacuum
        incremental, freed pages are handed back only when something asks for them. The checkpoint
        stores are on the same tick and for the same reason: they are written before every turn and
        nothing but deleting the session ever took one away.
        """
        ops = self.app.config.ops
        due = self._maintained_at is None or now - self._maintained_at >= timedelta(minutes=ops.db_maintenance_minutes)
        if not due or self.app.manager is None:
            return
        self._maintained_at = now
        try:
            dropped = await self.app.manager.events.prune(keep_days=ops.events_keep_days, max_rows=ops.events_max_rows)
            dropped += await self.app.manager.bus.prune(keep_days=ops.app_events_keep_days, max_rows=ops.app_events_max_rows)
            abandoned_media = await self.app.manager.media.prune_staged()
            pages = await self.app.db.reclaim()
            if dropped or abandoned_media or pages:
                logger.warning("database maintenance: %d event rows and %d abandoned media dropped, %d pages reclaimed", dropped, abandoned_media, pages)
            self._start_retention()
        except Exception:  # noqa: BLE001 — housekeeping must never take the tick down
            logger.exception("database maintenance failed")

    def _start_retention(self) -> None:
        """Start the checkpoint retention pass, unless the one from an earlier tick is still going.

        It does not run *on* the tick. The size pass cuts a batch of snapshots, runs a full
        ``reflog expire`` and ``git gc --prune=now``, measures again and goes round; each of those
        subprocesses may take minutes, and a store on a long-running installation holds hundreds of
        snapshots. Everything else the tick does — firing cron tasks, heartbeats, the inbox — would
        wait behind it. As its own task it takes as long as it takes, and the next tick finds it
        still running and leaves it alone.
        """
        if self._retention is not None and not self._retention.done():
            return
        self._retention = asyncio.create_task(self._prune_checkpoints(), name="checkpoint-retention")

    async def _prune_checkpoints(self) -> None:
        try:
            report = await self.app.manager.prune_checkpoints()
        except Exception:  # noqa: BLE001 — housekeeping must never take anything else down
            logger.exception("checkpoint retention failed")
            return
        if report.dropped or report.freed:
            logger.warning("checkpoint retention: %s", report.line())

    async def drain_retention(self) -> None:
        """Wait for the retention pass to finish — what a shutdown and the tests wait on."""
        task = self._retention
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

    async def _record_start_failure(self, schedule: dict[str, Any], error: str) -> None:
        """A run that could not even start counts as a failure and is reported; the slot was consumed."""
        failures = int(schedule.get("failure_count") or 0) + 1
        limit = self.app.config.scheduler.max_failures
        disable = bool(schedule.get("recurring")) and failures >= limit
        await self.app.db.execute(
            "UPDATE schedules SET failure_count = ?, last_error = ?, enabled = CASE WHEN ? THEN 0 ELSE enabled END WHERE id = ?",
            (failures, error[:500], int(disable), schedule["id"]),
        )
        await self._post(
            "schedule_failed",
            f"'{schedule['name']}' could not start ({failures}/{limit})" + (" — switched off" if disable else ""),
            error[:2000],
            tone="error" if disable else "warning",
        )

    async def fire(self, schedule: dict[str, Any], *, advance: bool = True) -> str:
        """Run a schedule now. ``advance=False`` (a manual run) leaves the next occurrence untouched."""
        if schedule["id"] in self._active:
            raise RuntimeError(f"schedule {schedule['id']} already has a run in flight")
        kind = schedule.get("kind") or "agent"
        await self.app.db.execute("UPDATE schedules SET last_run_at = ? WHERE id = ?", (_now().isoformat(), schedule["id"]))
        if self.app.manager is not None:
            await self.app.manager.bus.publish("schedule.fired", {"schedule_id": str(schedule["id"]), "name": str(schedule["name"]), "kind": kind})
        if advance:
            # The next occurrence is fixed before dispatch so a restart cannot fire the same slot twice.
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
        await self._post(
            "reminder", schedule["name"], schedule["prompt"], category="reminder", tone="info" if delivered or front is None else "warning",
            session_id=schedule.get("target_session"), handled=frozenset({"telegram"}) if delivered else frozenset(),
        )
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
        """Prepend fired lazy reminders to a message that starts a run; they are marked delivered once the run exists."""
        notes = await self.pending_lazy_notes(session_id)
        if not notes:
            return text
        block = "\n".join(f"[Reminder fired {n['fired_at'][:16].replace('T', ' ')} UTC, id={n['id']}] {n['text']}" for n in notes)
        self._delivering[session_id] = [int(n["id"]) for n in notes]
        return f"{block}\n\n{text}"

    async def on_run_started(self, session_id: str, run_id: str) -> None:
        ids = self._delivering.pop(session_id, [])
        if ids:
            marks = ",".join("?" for _ in ids)
            await self.app.db.execute(f"UPDATE lazy_notes SET delivered_at = ? WHERE id IN ({marks})", (_now().isoformat(), *ids))

    async def _promote_lazy_notes(self, now: datetime) -> None:
        """A lazy note nobody has seen for a day becomes a task the agent runs itself."""
        cutoff = (now - timedelta(hours=self.app.config.scheduler.lazy_ttl_hours)).isoformat()
        rows = await self.app.db.fetchall(
            "SELECT * FROM lazy_notes WHERE delivered_at IS NULL AND promoted_at IS NULL AND fired_at <= ? AND promote_attempts < ?",
            (cutoff, PROMOTE_MAX_ATTEMPTS),
        )
        manager = self.app.manager
        for row in rows:
            if manager is None:
                return
            prompt = f"[Reminder fired {row['fired_at'][:16].replace('T', ' ')} UTC; the operator did not return in time, so act on it yourself] {row['text']}"
            try:
                state = await manager.get_state(row["session_id"])
                if state is not None and not state.running and state.pending is None:
                    self._delivering.pop(row["session_id"], None)
                    await manager.submit(row["session_id"], prompt, as_answer=False, origin="reminder")
                else:
                    note_project = await self._project_of(row["session_id"])
                    await self.run_task_session(
                        f"[reminder] {row['text'][:40]}",
                        prompt,
                        self.root / f"lazy-{row['id']}",
                        {"lazy_note_id": row["id"], "unattended": True},
                        origin="reminder",
                        project_id=note_project.id if note_project is not None and note_project.reachable else None,
                    )
            except Exception as exc:  # noqa: BLE001
                attempts = int(row["promote_attempts"] or 0) + 1
                await self.app.db.execute("UPDATE lazy_notes SET promote_attempts = ? WHERE id = ?", (attempts, row["id"]))
                logger.warning("could not promote lazy note %s (attempt %d): %s", row["id"], attempts, exc)
                if attempts >= PROMOTE_MAX_ATTEMPTS:
                    await self._post("reminder_lost", "A lazy reminder could not be turned into a task", f"{row['text']}\n\n{type(exc).__name__}: {exc}", tone="error", session_id=row["session_id"])
                continue
            await self.app.db.execute("UPDATE lazy_notes SET promoted_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
            await self._post("reminder_promoted", "A lazy reminder became a task", row["text"], session_id=row["session_id"])

    async def _answer_stale_questions(self, now: datetime) -> None:
        """Any unattended run waiting on AskUser for too long continues on its own judgement."""
        manager = self.app.manager
        if manager is None:
            return
        timeout = timedelta(minutes=self.app.config.scheduler.question_timeout_minutes)
        rows = await self.app.db.fetchall("SELECT session_id, created_at FROM pending_questions")
        for row in rows:
            if now - datetime.fromisoformat(row["created_at"]) < timeout:
                continue
            state = await manager.get_state(row["session_id"])
            if state is None or state.pending is None:
                continue
            meta = state.session.metadata
            if not (meta.get("unattended") or meta.get("heartbeat")):
                continue
            front = self.app.front
            try:
                if front is not None:
                    await front.close_question(row["session_id"], f"⏳ No answer for {self.app.config.scheduler.question_timeout_minutes} min: the unattended run continues on its own judgement.")
                await manager.answer(row["session_id"], [{"custom": UNATTENDED_ANSWER}])
                await self._post("schedule_question_timeout", "An unattended run waited too long for an answer", f"Session '{state.session.title}': the question was answered with 'continue on your own judgement'.", tone="warning", session_id=row["session_id"])
            except RuntimeError:
                pass

    async def run_task_session(self, title: str, prompt: str, workspace: Path, metadata: dict[str, Any], *, preset: str | None = None, origin: str = "schedule", project_id: str | None = None) -> Any:
        """Create the session (and topic) an unattended task runs in, and start it.

        With a project the folder comes from it, and so does the wall: a task raised out of a project
        session must not be the way a fresh uncontained session appears in the operator's folder.
        """
        manager = self.app.manager
        assert manager is not None
        if project_id is None:
            project_id = (await manager.projects.adopt_directory(title, workspace)).id
        state = await self.app.create_session(title, metadata=metadata, project_id=project_id)
        if preset:
            await manager.set_model(state.session.id, preset=preset)
        await manager.submit(state.session.id, prompt, [], as_answer=False, origin=origin)
        return state

    async def _fire_agent(self, schedule: dict[str, Any]) -> str:
        manager = self.app.manager
        front = self.app.front
        assert manager is not None
        if (schedule.get("run_in") or "new") == "self":
            fired = await self._fire_in_own_session(schedule)
            if fired is not None:
                return fired
        project = await self._project_of(schedule.get("target_session") or schedule.get("created_by_session"))
        if project is not None and not await manager.projects.ensure_reachable(project):
            raise RuntimeError(f"the folder of the project {project.name} ({project.root}) is not reachable; the task cannot run in it")
        workspace = project.root if project is not None else Path(schedule["workspace"])
        if project is None and not workspace.is_dir():
            # The stored folder of a schedule whose project is gone. ``mkdir(parents=True)`` would make
            # the whole path and adopt the empty result as a project, so a task that used to run over the
            # operator's files would quietly start running over nothing. The tick records this as a start
            # failure and tells the operator, which is the only honest answer.
            raise RuntimeError(f"the folder this task runs in ({workspace}) is not there; re-create it or point the task at a project")
        (workspace / "inbox").mkdir(parents=True, exist_ok=True)
        title = f"[cron] {schedule['name']}"
        if project is None:
            project = await manager.projects.adopt_directory(schedule["name"], workspace)
        project_id = project.id
        metadata: dict[str, Any] = {"schedule_id": schedule["id"], "unattended": True}
        if schedule.get("model"):
            metadata["model"] = schedule["model"]
        per_task = self.app.config.scheduler.topic_mode == "per_task"
        if front is not None and per_task and schedule.get("topic_thread_id") and self.app.config.telegram.forum_chat_id:
            state = await manager.create_session(title, workspace=workspace, metadata=metadata, project_id=project_id)
            await front.bind_topic(self.app.config.telegram.forum_chat_id, int(schedule["topic_thread_id"]), state.session.id, title)
        elif front is not None:
            state, binding = await front.create_session_topic(title, metadata=metadata, project_id=project_id)
            if per_task:
                await self.app.db.execute("UPDATE schedules SET topic_thread_id = ? WHERE id = ?", (binding.thread_id, schedule["id"]))
        else:
            state = await self.app.create_session(title, metadata=metadata, workspace=workspace, project_id=project_id)
        prompt = schedule["prompt"]
        files = json.loads(schedule.get("files") or "[]")
        if files:
            prompt += "\n\nFiles attached to this task:\n" + "\n".join(f"- {f}" for f in files)
        if schedule.get("last_summary"):
            prompt += f"\n\nSummary of the previous run ({schedule.get('last_run_at')}):\n{schedule['last_summary'][:6000]}"
        prompt += (
            "\n\nThis is an unattended scheduled run: no operator is watching. If the check finds nothing that needs "
            "attention, call StaySilent instead of writing that there is nothing new. When finished, write SUMMARY.md "
            "in the project directory describing what was done and anything the next run should know."
        )
        run_id = await manager.submit(state.session.id, prompt, [], as_answer=False, origin="schedule")
        await self._mark_in_flight(schedule["id"], state.session.id, run_id)
        return state.session.id

    async def _fire_in_own_session(self, schedule: dict[str, Any]) -> str | None:
        """Run the task as a turn of the session that owns it; None when that session cannot take it now."""
        manager = self.app.manager
        assert manager is not None
        target = schedule.get("target_session") or schedule.get("created_by_session")
        state = await manager.get_state(target) if target else None
        if state is None:
            await self._post("schedule_orphaned", f"'{schedule['name']}' lost its session", "The session it was meant to run in no longer exists; this run starts a task session instead.", tone="warning")
            return None
        if state.pending is not None:
            await self._post("schedule_skipped", f"'{schedule['name']}' skipped", "Its session is waiting for the operator's answer; the next occurrence will try again.", session_id=state.session.id)
            return state.session.id
        if state.running:
            # A wake-up call for a session that is already working is noise: it would land mid-task as a steer.
            logger.warning("schedule %s skipped: session %s is busy", schedule["id"], state.session.id)
            return state.session.id
        prompt = schedule["prompt"]
        files = json.loads(schedule.get("files") or "[]")
        if files:
            prompt += "\n\nFiles attached to this task:\n" + "\n".join(f"- {f}" for f in files)
        if schedule.get("last_summary"):
            prompt += f"\n\nYour note from the previous run ({schedule.get('last_run_at')}):\n{schedule['last_summary'][:6000]}"
        prompt += (
            f"\n\n[scheduled run '{schedule['name']}' in this session; no operator message accompanies it. "
            "Take the work as far as it goes now — the next occurrence is skipped while this turn runs. "
            "If nothing needs attention, call StaySilent with a one-line note of what you checked. End with a short note for the next run.]"
        )
        run_id = await manager.submit(state.session.id, prompt, [], as_answer=False, origin="schedule")
        await self._mark_in_flight(schedule["id"], state.session.id, run_id)
        return state.session.id

    async def _mark_in_flight(self, schedule_id: str, session_id: str, run_id: str) -> None:
        # Only a run that exists is in flight — and the row remembers it across a restart.
        self._active[schedule_id] = session_id
        self._active_runs[schedule_id] = run_id
        await self.app.db.execute("UPDATE schedules SET active_session_id = ?, active_run_id = ? WHERE id = ?", (session_id, run_id, schedule_id))

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        for schedule_id, sid in list(self._active.items()):
            if sid != session_id:
                continue
            expected = self._active_runs.get(schedule_id)
            if expected and run_id and run_id != expected:
                continue  # another turn of the same session ended, not the scheduled one
            if status == "awaiting":
                continue  # the operator still has to answer; the summary is collected when the run ends
            self._active.pop(schedule_id, None)
            self._active_runs.pop(schedule_id, None)
            row = await self.app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
            if row is None:
                continue
            await self.app.db.execute("UPDATE schedules SET active_session_id = NULL, active_run_id = NULL WHERE id = ?", (schedule_id,))
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
                    tone="error" if disable else "warning",
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
                category="reminder",
                level="quiet" if quiet else "normal",
                session_id=session_id,
                run_id=run_id,
            )

    async def _collect_summary(self, schedule: dict[str, Any], session_id: str) -> str:
        summary_file = Path(schedule["workspace"]) / "SUMMARY.md"
        if (schedule.get("run_in") or "new") != "self" and summary_file.is_file():
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
    app.manager.run_started_hooks.append(scheduler.on_run_started)
    await scheduler.restore()
    front = app.front
    if front is not None:

        async def cmd_schedules(message, command) -> None:  # type: ignore[no-untyped-def]
            items = await scheduler.list()
            if not items:
                await message.answer("No scheduled tasks. The agent creates them with ScheduleCreate.")
                return
            lines = [
                f"{'✓' if s['enabled'] else '✗'} {s['id']} [{s.get('kind') or 'agent'}] {s['name']} — {('cron ' + s['cron']) if s['cron'] else 'once'}"
                f" · next {s['next_run_at'] or '-'}" + (f" · failures {s['failure_count']}" if s.get("failure_count") else "") + (" · running" if s["id"] in scheduler._active else "")
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
                    return
                try:
                    sid = await scheduler.fire(dict(row), advance=False)
                except RuntimeError as exc:
                    await message.answer(str(exc))
                    return
                await message.answer(f"started session {sid}" if sid else "fired")
            else:
                await message.answer("usage: /schedule delete <id> | on <id> | off <id> | run <id>")

        front.command_hooks["schedules"] = cmd_schedules
        front.command_hooks["schedule"] = cmd_schedule
    return [asyncio.create_task(scheduler.loop(), name="scheduler")]


__all__ = ["KINDS", "Scheduler", "install"]
