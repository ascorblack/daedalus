"""Task board: the plan lives outside the model's context.

A summary degrades; a board does not. Tasks carry acceptance criteria, a checklist,
dependencies (a task becomes ready when every dependency is done), a priority and the
session working on it. Work-in-progress is limited, a task whose session went quiet is
handed back, and the board is the same object in the Mini App, in ``/board`` and in the
agent's tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

NOTES_MAX_CHARS = 8000
STATUSES = ("todo", "doing", "review", "done", "blocked", "dropped")
TICK_SECONDS = 300


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Board:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def add(
        self,
        *,
        title: str,
        acceptance: str = "",
        depends_on: list[str] | None = None,
        priority: int = 3,
        checklist: list[str] | None = None,
        session_id: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:6]
        deps = [d for d in (depends_on or []) if d and d != task_id]
        for dep in deps:
            if await self.app.db.fetchone("SELECT id FROM board_tasks WHERE id = ?", (dep,)) is None:
                raise ValueError(f"unknown dependency {dep}")
        status = "blocked" if await self._has_open_deps(deps) else "todo"
        await self.app.db.execute(
            "INSERT INTO board_tasks(id, title, status, priority, acceptance, checklist, depends_on, session_id, origin_session_id, notes, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, title[:200], status, max(1, min(int(priority), 5)), acceptance[:2000], json.dumps([{"text": c[:200], "done": False} for c in (checklist or [])]), json.dumps(deps), session_id, session_id, notes[:4000], _now(), _now()),
        )
        await self.export_plan(session_id)
        return await self.get(task_id)

    async def get(self, task_id: str) -> dict[str, Any]:
        row = await self.app.db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        return self._view(dict(row))

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        row["checklist"] = json.loads(row.get("checklist") or "[]")
        row["depends_on"] = json.loads(row.get("depends_on") or "[]")
        return row

    async def list(self, status: str | None = None, *, include_done: bool = True) -> list[dict[str, Any]]:
        if status:
            rows = await self.app.db.fetchall("SELECT * FROM board_tasks WHERE status = ? ORDER BY priority, created_at", (status,))
        elif include_done:
            rows = await self.app.db.fetchall("SELECT * FROM board_tasks ORDER BY CASE status WHEN 'doing' THEN 0 WHEN 'review' THEN 1 WHEN 'todo' THEN 2 WHEN 'blocked' THEN 3 WHEN 'done' THEN 4 ELSE 5 END, priority, created_at")
        else:
            rows = await self.app.db.fetchall("SELECT * FROM board_tasks WHERE status NOT IN ('done', 'dropped') ORDER BY priority, created_at")
        return [self._view(dict(r)) for r in rows]

    async def _has_open_deps(self, deps: list[str]) -> bool:
        for dep in deps:
            row = await self.app.db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (dep,))
            if row is not None and row["status"] not in ("done", "dropped"):
                return True
        return False

    async def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        note: str = "",
        check: list[int] | None = None,
        uncheck: list[int] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        title: str | None = None,
        acceptance: str | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        task = await self.get(task_id)
        if status and status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        if status == "doing" and task["status"] != "doing":
            if await self._has_open_deps(task["depends_on"]):
                raise ValueError("this task still has unfinished dependencies")
            limit = self.app.config.board.wip_limit
            row = await self.app.db.fetchone("SELECT count(*) c FROM board_tasks WHERE status = 'doing'")
            if row and int(row["c"]) >= limit:
                raise ValueError(f"work-in-progress limit reached ({limit} tasks in 'doing'); finish or hand back one first")
        # Apply the caller's checklist edits before judging completeness: "I checked the last items,
        # close the task" is one call, and testing the checklist the call *arrived* to refused it.
        # Index precedence is unchanged — an index in both lists ends up unchecked.
        unchecked, checked = set(uncheck or []), set(check or [])
        checklist = [
            {**item, "done": False} if i in unchecked else {**item, "done": True} if i in checked else item
            for i, item in enumerate(task["checklist"])
        ]
        # A task may not be left finished with an item still open, whichever way this call moved:
        # closing it, or editing the checklist of a task that is already done. The gate judges the
        # checklist this call produces, and it runs before anything is written, so a refusal stores
        # nothing — the message has to say so, or the caller retries with the wrong indexes.
        if (status or task["status"]) == "done":
            still_open = [i for i, c in enumerate(checklist) if not c["done"]]
            if still_open:
                named = ", ".join(f"{i} ({str(checklist[i].get('text', '')).strip()[:40]})" for i in still_open[:5])
                more = "" if len(still_open) <= 5 else f" and {len(still_open) - 5} more"
                remedy = (
                    "Repeat it with every index you want checked (check=[...], status='done'), or leave the "
                    "task in another status."
                    if status == "done"
                    else "Reopen the task first (status='todo' or 'doing') and edit its checklist there."
                )
                raise ValueError(f"the checklist is not complete: {named}{more}. Nothing was stored. {remedy}")
        notes = task["notes"] or ""
        if note:
            notes = (notes + "\n" if notes else "") + f"[{_now()[:16].replace('T', ' ')}] {note[:1000]}"
        # The claim belongs to whoever holds the task in 'doing'; leaving 'doing' releases it.
        if status == "doing":
            owner, owner_run = session_id or task["session_id"], run_id or task["run_id"]
        elif status:
            owner, owner_run = None, None
        else:
            owner, owner_run = task["session_id"], task["run_id"]
        await self.app.db.execute(
            "UPDATE board_tasks SET status = ?, notes = ?, checklist = ?, session_id = ?, run_id = ?,"
            " title = COALESCE(?, title), acceptance = COALESCE(?, acceptance), priority = COALESCE(?, priority), updated_at = ?, heartbeat_at = ? WHERE id = ?",
            (status or task["status"], notes[-NOTES_MAX_CHARS:], json.dumps(checklist), owner, owner_run, title, acceptance, priority, _now(), _now(), task_id),
        )
        if status in ("done", "dropped"):
            await self._promote_dependents()
        elif status and task["status"] in ("done", "dropped"):
            await self._demote_dependents()
        await self.export_plan(task.get("origin_session_id") or session_id or task.get("session_id"))
        return await self.get(task_id)

    async def delete(self, task_id: str) -> bool:
        row = await self.app.db.fetchone("SELECT id FROM board_tasks WHERE id = ?", (task_id,))
        if row is None:
            return False
        owner = await self.app.db.fetchone("SELECT origin_session_id FROM board_tasks WHERE id = ?", (task_id,))
        await self.app.db.execute("DELETE FROM board_tasks WHERE id = ?", (task_id,))
        await self.export_plan(owner["origin_session_id"] if owner else None)
        for row in await self.app.db.fetchall("SELECT id, depends_on FROM board_tasks WHERE depends_on LIKE ?", (f"%{task_id}%",)):
            deps = [d for d in json.loads(row["depends_on"] or "[]") if d != task_id]
            await self.app.db.execute("UPDATE board_tasks SET depends_on = ? WHERE id = ?", (json.dumps(deps), row["id"]))
        await self._promote_dependents()
        return True

    async def _promote_dependents(self) -> list[str]:
        """A blocked task whose dependencies are all finished becomes ready."""
        promoted: list[str] = []
        for row in await self.app.db.fetchall("SELECT id, depends_on FROM board_tasks WHERE status = 'blocked'"):
            deps = json.loads(row["depends_on"] or "[]")
            if not await self._has_open_deps(deps):
                await self.app.db.execute("UPDATE board_tasks SET status = 'todo', updated_at = ? WHERE id = ?", (_now(), row["id"]))
                promoted.append(row["id"])
        return promoted

    async def _demote_dependents(self) -> list[str]:
        """A ready task whose dependency was reopened is blocked again."""
        demoted: list[str] = []
        for row in await self.app.db.fetchall("SELECT id, depends_on FROM board_tasks WHERE status = 'todo'"):
            deps = json.loads(row["depends_on"] or "[]")
            if deps and await self._has_open_deps(deps):
                await self.app.db.execute("UPDATE board_tasks SET status = 'blocked', updated_at = ? WHERE id = ?", (_now(), row["id"]))
                demoted.append(row["id"])
        return demoted

    async def recover_stale(self) -> list[str]:
        """Hand back 'doing' tasks whose session has been quiet for longer than the stale window."""
        manager = self.app.manager
        hours = self.app.config.board.stale_hours
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        handed: list[str] = []
        for row in await self.app.db.fetchall("SELECT id, title, session_id, heartbeat_at, updated_at FROM board_tasks WHERE status = 'doing'"):
            last = row["heartbeat_at"] or row["updated_at"]
            try:
                seen = datetime.fromisoformat(last)
                seen = seen if seen.tzinfo else seen.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                seen = cutoff
            if seen >= cutoff:
                continue
            state = await manager.get_state(row["session_id"]) if manager is not None and row["session_id"] else None
            if state is not None and (state.running or state.pending is not None):
                continue
            await self.app.db.execute("UPDATE board_tasks SET status = 'todo', session_id = NULL, run_id = NULL, updated_at = ?, notes = substr(notes || ?, ?) WHERE id = ?", (_now(), f"\n[{_now()[:16].replace('T', ' ')}] handed back: no activity for {hours} h", -NOTES_MAX_CHARS, row["id"]))
            handed.append(row["id"])
            inbox = self.app.extensions.get("inbox")
            if inbox is not None:
                await inbox.post("board_stale", f"Task '{row['title']}' handed back", f"No activity for {hours} h; it is 'todo' again.", severity="notice", session_id=row["session_id"])
        return handed

    async def touch(self, session_id: str) -> None:
        await self.app.db.execute("UPDATE board_tasks SET heartbeat_at = ? WHERE status = 'doing' AND session_id = ?", (_now(), session_id))

    async def loop(self) -> None:
        while True:
            try:
                await self.recover_stale()
            except Exception:  # noqa: BLE001
                logger.exception("board recovery failed")
            await asyncio.sleep(TICK_SECONDS)

    async def export_plan(self, session_id: str | None) -> None:
        """PLAN.md in the session's workspace: the board's rows for that session as a readable, diffable file.

        The board stays the plan of record (it survives compaction and restarts); the file is its
        rendering where the operator and the agent read files, regenerated on every change.
        """
        if not session_id or self.app.manager is None:
            return
        state = self.app.manager.live_state(session_id)  # a session that is not live gets no file; nothing is resurrected for it
        if state is None:
            return
        # A subagent shares its leader's workspace: the file there lists the whole family's tasks.
        leader = str(state.metadata.get("subagent_of") or session_id)
        family = [leader, *(str(r["id"]) for r in await self.app.db.fetchall("SELECT id FROM sessions WHERE metadata LIKE ?", (f'%"subagent_of": "{leader}"%',)))]
        placeholders = ",".join("?" for _ in family)
        rows = await self.app.db.fetchall(f"SELECT * FROM board_tasks WHERE origin_session_id IN ({placeholders}) ORDER BY CASE status WHEN 'doing' THEN 0 WHEN 'review' THEN 1 WHEN 'todo' THEN 2 WHEN 'blocked' THEN 3 WHEN 'done' THEN 4 ELSE 5 END, priority, created_at", tuple(family))
        tasks = [self._view(dict(r)) for r in rows]
        lines = ["# Plan", "", f"Board tasks created by session {leader}" + (" and its subagents" if len(family) > 1 else "") + "; edit them with the Board tools, this file is regenerated.", ""]
        for t in tasks:
            box = {"done": "x", "dropped": "-"}.get(t["status"], " ")
            deps = f" (after {', '.join(t['depends_on'])})" if t["depends_on"] else ""
            lines.append(f"- [{box}] **{t['id']}** {t['title']} — {t['status']}, priority {t['priority']}{deps}")
            if t.get("acceptance"):
                lines.append(f"  - done when: {t['acceptance']}")
            for item in t["checklist"]:
                lines.append(f"  - [{'x' if item['done'] else ' '}] {item['text']}")
        try:
            await asyncio.to_thread((state.workspace / "PLAN.md").write_text, "\n".join(lines) + "\n", "utf-8")
        except OSError:
            logger.warning("could not write PLAN.md for session %s", session_id, exc_info=True)

    def render(self, tasks: list[dict[str, Any]], *, limit: int = 30) -> str:
        icons = {"todo": "▫️", "doing": "🔵", "review": "🟡", "done": "✅", "blocked": "⛔", "dropped": "✖️"}
        lines = []
        for t in tasks[:limit]:
            done = sum(1 for c in t["checklist"] if c["done"])
            extra = f" [{done}/{len(t['checklist'])}]" if t["checklist"] else ""
            deps = f" ← {', '.join(t['depends_on'])}" if t["depends_on"] else ""
            lines.append(f"{icons.get(t['status'], '·')} {t['id']} p{t['priority']} {t['title']}{extra}{deps}")
        if len(tasks) > limit:
            lines.append(f"… {len(tasks) - limit} more")
        return "\n".join(lines) or "(the board is empty)"

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "add":
            return await self.add(**kwargs)
        if op == "update":
            return await self.update(kwargs.pop("task_id"), **kwargs)
        if op == "list":
            return await self.list(kwargs.get("status"), include_done=bool(kwargs.get("include_done", False)))
        if op == "get":
            return await self.get(kwargs["task_id"])
        if op == "delete":
            return await self.delete(kwargs["task_id"])
        if op == "render":
            return self.render(await self.list(kwargs.get("status"), include_done=bool(kwargs.get("include_done", False))))
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    board = Board(app)
    app.extensions["board"] = board
    assert app.manager is not None
    app.manager.service_hooks["board"] = board.service

    async def touch_on_finish(session_id: str, run_id: str, status: str) -> None:
        await board.touch(session_id)

    app.manager.on_finished(touch_on_finish)
    front = app.front
    if front is not None:

        async def cmd_board(message, command) -> None:  # type: ignore[no-untyped-def]
            arg = (command.args or "").strip().lower()
            include_done = arg == "all"
            tasks = await board.list(None, include_done=include_done)
            await message.answer(("📋 Board\n" + board.render(tasks) + "\n\n/board all shows finished tasks too")[:4000])

        front.command_hooks["board"] = cmd_board
    return [asyncio.create_task(board.loop(), name="board")]


__all__ = ["STATUSES", "Board", "install"]
