"""The team at work: assignments, the launch queue, statuses, requests and control of staff sessions.

``app.extensions["staff"]`` is the one place a staff member is started, told something, paused,
released or answered, whichever executor runs it. The runtimes (``runtimes[<harness>]``) do what only
they can — start a session, deliver a message, stop it — and report back through :class:`Ingress`,
which writes the staff tables and publishes the bus events in one place. Daedalus staff are run here;
the command-line harnesses register their runtimes when they are installed.

Two sources could say a member is waiting: the Daedalus session's own pending question and the
request row. The row is the one the team reads, and this module is its only writer: a question is
recorded from the session's pending event, and an answer given anywhere — the app's session view,
the request list, the orchestrator — resolves the row first, by compare-and-set, before it reaches the
session. Whoever updates the row delivers; everyone else is told who was first.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.extensions.notifications import ActionConflict, ActionOutcome, ActionRefused, Draft
from daedalus.harness.capabilities import CAPABILITIES
from daedalus.harness.health import ChannelHealth, channel_health
from daedalus.host import prompts
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.launch_queue import Admission, Entry, LaunchQueue, MachineCapacity, TerminalsCapacity
from daedalus.host.staff_daedalus import DaedalusStaffRuntime
from daedalus.host.worktrees import StaffWorktrees, Worktree, WorktreeError
from daedalus.staff_runtime import (
    AskRef,
    BoardTask,
    Decision,
    LiveSession,
    OutgoingMessage,
    Receipt,
    StaffRuntime,
    StartRequest,
    UsageSnapshot,
)
from daedalus.stores.projects import Project, ProjectFolder
from daedalus.stores.staff import ACTIVE_STATUSES, HARNESS_NAMES, Ask, Staff, StaffBusy, StaffError, StaffSession
from daedalus.terminals.bridge import HostBridge

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.session_runner import SessionManager
    from daedalus.terminals.service import Terminals

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0
"""How often silence, stale requests and the machine's capacity are looked at."""
FIRST_PUMP_SECONDS = 20.0
"""After a start, the queue waits this long before launching what was assigned before the restart:
the host first continues the runs it left behind and refuses new ones meanwhile."""
NOTE_MAX = 2000
BASIS_MIN = 12
"""The least a quoted allowance may be: a line of the brief, not a word that happens to occur in it."""

OPEN_TASK = ("todo", "blocked")
FINISHED_TASK = ("done", "dropped")
ABNORMAL = ("error", "no_signal")
SENT_BACK = "sent back by the "
"""How a rejection from review is written into a task's notes (see ``review.py``)."""


class AlreadyAnswered(StaffError):
    """A request someone else answered first; the message says who."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _age_seconds(at: str | None, now: datetime) -> float:
    if not at:
        return 0.0
    try:
        then = datetime.fromisoformat(at)
    except ValueError:
        return 0.0
    then = then if then.tzinfo else then.replace(tzinfo=UTC)
    return (now - then).total_seconds()


def _task(row: Any) -> BoardTask:
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except (TypeError, ValueError):
        brief = {}
    brief = brief if isinstance(brief, dict) else {}
    try:
        depends = tuple(str(d) for d in json.loads(row["depends_on"] or "[]"))
    except (TypeError, ValueError):
        depends = ()
    return BoardTask(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        priority=int(row["priority"]),
        objective=str(brief.get("objective") or ""),
        deliverable=str(brief.get("deliverable") or ""),
        boundaries=str(brief.get("boundaries") or ""),
        done_when=str(brief.get("done_when") or ""),
        project_id=row["project_id"],
        folder_id=row["folder_id"],
        assignee_staff_id=row["assignee_staff_id"],
        branch=row["branch"],
        depends_on=depends,
        sent_back=_sent_back(row),
    )


def _sent_back(row: Any) -> str:
    """The latest "sent back" note of a task the operator rejected from review, else nothing."""
    if row["merge_state"] != "rejected":
        return ""
    for line in reversed(str(row["notes"] or "").splitlines()):
        if SENT_BACK in line:
            return line.split(SENT_BACK, 1)[1].partition(": ")[2].strip()
    return ""


@dataclass(frozen=True, slots=True)
class Assigned:
    """What an assignment did: started now, or queued at a position for a stated reason."""

    admission: Admission
    task: BoardTask

    def view(self) -> dict[str, Any]:
        a = self.admission
        return {"state": a.state, "position": a.position, "reason": a.reason, "detail": a.detail, "task_id": self.task.id}


class Team:
    """The project teams of this installation at work."""

    def __init__(self, app: Application, *, capacity: Any = None) -> None:
        self.app = app
        assert app.manager is not None
        self.manager: SessionManager = app.manager
        self.runtimes: dict[str, StaffRuntime] = {"daedalus": DaedalusStaffRuntime(self.manager)}
        # A host folder in Docker is worked in through the host terminal bridge; the service is
        # looked up per call, so a bridge installed after the start is used without a restart.
        self.worktrees = StaffWorktrees(self.manager.projects.local_env, host=HostBridge(lambda: cast("Terminals | None", app.extensions.get("terminals"))))
        self.ingress = Ingress(self)
        self._capacity = capacity
        """A fixed :class:`MachineCapacity` for tests; otherwise the terminals service is asked each time."""
        self.queue = LaunchQueue(
            concurrency=self._concurrency,
            active=self._active,
            ready=self._ready,
            free=self._free,
            launch=self._launch,
            capacity=self.capacity,
            stagger=lambda: self.manager.config.staff.launch_stagger_seconds,
            on_failure=self._launch_failed,
        )
        self._pause_commits: set[asyncio.Task[None]] = set()
        self.review: Any = None
        """Review and merge of staff branches (``review.py``), set at install."""
        self.own_requests: Any = None
        """The orchestrators, once installed: a request the orchestrator itself made (a question to the
        operator, a folder it wants) has no staff session to deliver the answer to, so they take it."""

    # -- lookups -----------------------------------------------------------------------------------

    def capacity(self) -> MachineCapacity | None:
        if self._capacity is not None:
            return self._capacity  # type: ignore[no-any-return]
        terminals = self.app.extensions.get("terminals")
        return TerminalsCapacity(terminals) if terminals is not None else None

    async def project(self, project_id: str) -> Project:
        project = await self.manager.projects.get(project_id)
        if project is None:
            raise KeyError(project_id)
        return project

    async def member(self, staff_id: str) -> Staff:
        member = await self.manager.staff.get(staff_id)
        if member is None:
            raise KeyError(staff_id)
        return member

    async def live(self, staff_session_id: str) -> LiveSession | None:
        """A live session with its member; ``None`` once it has ended."""
        session = await self.manager.staff.session(staff_session_id)
        if session is None or not session.live:
            return None
        member = await self.manager.staff.get(session.staff_id)
        return LiveSession(member, session) if member is not None else None

    async def live_of(self, member: Staff) -> LiveSession | None:
        session = await self.manager.staff.live(member.id)
        return LiveSession(member, session) if session is not None else None

    async def live_for_session(self, session_id: str) -> LiveSession | None:
        """The live staff session a Daedalus session is, read from the session's own metadata so it
        works from the first event of the first run, before the start has recorded the session id."""
        state = self.manager.live_state(session_id)
        staff_session_id = str(state.metadata.get("staff_session_id") or "") if state is not None else ""
        if not staff_session_id:
            found = await self.manager.staff.by_session(session_id)
            staff_session_id = found.id if found is not None else ""
        return await self.live(staff_session_id) if staff_session_id else None

    def runtime(self, member: Staff) -> StaffRuntime:
        runtime = self.runtimes.get(member.harness)
        if runtime is None:
            raise StaffError(f"{HARNESS_NAMES.get(member.harness, member.harness)} staff cannot be started here yet: its runtime is not installed")
        return runtime

    async def health(self, live: LiveSession, now: datetime | None = None) -> ChannelHealth:
        """Whether the host still hears the member: the card, the staff view and ``Team(staff)`` all
        read this, so none of them can call a member reachable that another calls silent."""
        member = live.staff
        runtime = self.runtimes.get(member.harness)
        channel_of = getattr(runtime, "channel", None)
        channel = channel_of(live) if channel_of is not None else {}
        caps = CAPABILITIES.get(member.harness)
        config = self.manager.config
        # A command-line member is looked at by the screen reconcile after this long; a Daedalus
        # member is marked silent by the tick after its own, longer, time.
        silence = config.harness.no_signal_after_s if caps is not None else config.staff.silence_minutes * 60
        messages = [m for m in await self.manager.staff.messages(member.id, limit=10) if m.staff_session_id == live.id]
        return channel_health(
            status=live.session.status,
            team_tools=caps.team_tools if caps is not None else "builtin",
            channel=channel,
            last_signal_at=live.session.last_signal_at,
            messages=messages,
            now=now or datetime.now(UTC),
            silence_after_s=silence,
        )

    async def task(self, task_id: str) -> BoardTask | None:
        row = await self.manager.db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        return _task(row) if row is not None else None

    def folder_for(self, project: Project, member: Staff, task: BoardTask | None) -> ProjectFolder:
        for folder_id in ((task.folder_id if task else None), member.default_folder_id):
            if folder_id:
                folder = project.folder(folder_id)
                if folder is not None:
                    return folder
        if project.primary is None:
            raise StaffError(f"{project.name} has no folder to work in")
        return project.primary

    # -- publishing ----------------------------------------------------------------------------------

    async def publish(self, event_type: str, payload: dict[str, Any], *, member: Staff | None = None, project_id: str | None = None, session_id: str | None = None) -> None:
        """A bus event about the team; a bus that cannot write is logged, never the caller's failure."""
        try:
            await self.manager.bus.publish(
                event_type,
                payload,
                project_id=project_id or (member.project_id if member else None),
                staff_id=member.id if member else None,
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001 — the change is written; the event is a courtesy to its subscribers
            logger.warning("could not publish %s", event_type, exc_info=True)

    async def _move_task(self, task: BoardTask, to: str, *, actor: str, assignee: str | None = "", branch: str | None = None, folder_id: str | None = None, merge_state: str | None = None) -> BoardTask:
        """Move a task on the board and say so. ``assignee=""`` keeps the assignee; ``None`` clears it."""
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [to, _now()]
        if assignee != "":
            sets.append("assignee_staff_id = ?")
            params.append(assignee)
        if branch is not None:
            sets.append("branch = ?")
            params.append(branch)
        if folder_id is not None:
            sets.append("folder_id = ?")
            params.append(folder_id)
        if merge_state is not None:
            sets.append("merge_state = ?")
            params.append(merge_state)
        await self.manager.db.execute(f"UPDATE board_tasks SET {', '.join(sets)} WHERE id = ?", (*params, task.id))  # noqa: S608 — column names are this module's own
        moved = await self.task(task.id)
        assert moved is not None
        if to != task.status:
            payload: dict[str, Any] = {"task_id": task.id, "title": task.title, "from": task.status, "to": to, "actor": actor}
            if moved.assignee_staff_id:
                payload["assignee_staff_id"] = moved.assignee_staff_id
            await self.publish("task.moved", payload, project_id=moved.project_id)
        return moved

    async def _set_assignee(self, task: BoardTask, staff_id: str | None, *, actor: str, error: str = "") -> None:
        await self.manager.db.execute("UPDATE board_tasks SET assignee_staff_id = ?, updated_at = ? WHERE id = ?", (staff_id, _now(), task.id))
        payload: dict[str, Any] = {"task_id": task.id, "title": task.title, "assignee_staff_id": staff_id or "", "actor": actor}
        if error:
            payload["error"] = error[:500]
        await self.publish("task.assigned", payload, project_id=task.project_id)

    # -- the launch queue's questions -----------------------------------------------------------------

    async def _concurrency(self, project_id: str) -> int:
        project = await self.manager.projects.get(project_id)
        return project.settings.orchestrator.concurrency if project is not None else 0

    async def _active(self, project_id: str) -> int:
        return sum(1 for s in (await self.manager.staff.live_sessions(project_id)).values() if s.status in ACTIVE_STATUSES)

    async def _ready(self, entry: Entry) -> str | None:
        task = await self.task(entry.task_id)
        if task is None:
            return "the task is gone"
        if task.status == "blocked":
            return f"waits for {', '.join(task.depends_on) or 'its dependencies'} to finish"
        return None

    async def _free(self, entry: Entry) -> str | None:
        session = await self.manager.staff.live(entry.staff_id)
        if session is None:
            return None
        if session.status in ACTIVE_STATUSES:
            return f"{entry.staff_name} is still working" + (f" on {session.task_id}" if session.task_id else "")
        if session.pause_requested:
            return f"{entry.staff_name} is paused; a message or a new assignment resumes it"
        if session.task_id and session.task_id != entry.task_id:
            current = await self.task(session.task_id)
            if current is not None and current.status == "doing":
                return f"{entry.staff_name}'s task {current.id} is still in doing; it has to be reported done, moved or released first"
        return None

    async def _launch(self, entry: Entry) -> None:
        member = await self.member(entry.staff_id)
        task = await self.task(entry.task_id)
        if task is None:
            raise StaffError(f"task {entry.task_id} is gone")
        await self.start(member, task, by=entry.by)

    async def _launch_failed(self, entry: Entry, exc: BaseException) -> None:
        # An assignment that cannot start is taken back rather than retried by every pump: the task
        # stays on the board, unassigned, and the event says why.
        task = await self.task(entry.task_id)
        if task is not None and task.assignee_staff_id == entry.staff_id:
            await self._set_assignee(task, None, actor="system", error=f"{entry.staff_name} could not start: {exc}")

    # -- assigning and starting ---------------------------------------------------------------------

    async def assign(self, member: Staff, task: str | dict[str, Any], *, by: str = "operator") -> dict[str, Any]:
        """Give a member a task (its id, or the board's view of it): it starts now, or waits in the
        project's launch queue. Returns ``{state: started|queued, position, reason, detail, task_id}``;
        a refusal is a :class:`StaffError`, which is a ``ValueError``."""
        return (await self._assign(member, str(task["id"]) if isinstance(task, dict) else task, by=by)).view()

    async def _assign(self, member: Staff, task_id: str, *, by: str) -> Assigned:
        if not member.active:
            raise StaffError(f"{member.name} has been dismissed")
        task = await self.task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.project_id != member.project_id:
            raise StaffError(f"task {task.id} is not on the board of {member.name}'s project")
        if task.status in FINISHED_TASK or task.status == "review":
            raise StaffError(f"task {task.id} is {task.status}; reopen it before assigning it")
        missing = task.missing()
        if missing:
            raise StaffError(f"task {task.id} has no {', '.join(m.replace('_', '-') for m in missing)} yet; a task is handed over with all four parts of its brief")
        if task.status == "doing" and task.assignee_staff_id and task.assignee_staff_id != member.id:
            raise StaffBusy(f"task {task.id} is being worked on by someone else; release them first")
        runtime = self.runtime(member)
        project = await self.project(member.project_id)
        folder = self.folder_for(project, member, task)
        terminal = member.harness != "daedalus"
        if not (terminal and self.capacity() is None):
            # Without the terminals service a command-line member waits in the queue with that reason
            # rather than being refused: the service may be starting.
            available = await runtime.available(folder.env)
            if not available.ok and not terminal:
                raise StaffError(f"{member.name} cannot start: {available.reason}")
        if task.assignee_staff_id != member.id:
            await self._set_assignee(task, member.id, actor=by)
        entry = Entry(project.id, member.id, member.name, task.id, task.priority, terminal, by, env=folder.env)
        admission = await self.queue.request(entry)
        return Assigned(admission, (await self.task(task.id)) or task)

    async def start(self, member: Staff, task: BoardTask, *, by: str = "operator") -> LiveSession:
        """Start a session of ``member`` for ``task`` now; the launch queue calls this once it admits it."""
        runtime = self.runtime(member)
        project = await self.project(member.project_id)
        folder = self.folder_for(project, member, task)
        previous = await self.live_of(member)
        if previous is not None:
            if previous.session.status in ACTIVE_STATUSES:
                raise StaffBusy(f"{member.name} is still working")
            # Sessions are short and identity is long: the next task is a new session, and the idle
            # one it replaces ends here.
            await self._end(previous, "next task", stop=True)
        predecessor = next((s for s in await self.manager.staff.sessions(member.id, limit=20) if s.task_id == task.id), None)
        worktree: Worktree | None = None
        if member.isolation == "worktree" and folder.is_git:
            try:
                worktree = await self.worktrees.prepare(folder, member.name, task.id, task.title)
            except WorktreeError as exc:
                raise StaffError(f"no worktree for {member.name} in {folder.path}: {exc}") from exc
        token = secrets.token_urlsafe(32)
        session = await self.manager.staff.claim_session(
            member.id,
            kind="daedalus" if member.harness == "daedalus" else "cli",
            task_id=task.id,
            folder_id=folder.id,
            worktree_path=str(worktree.path) if worktree else None,
            branch=worktree.branch if worktree else None,
            base_ref=worktree.base_ref if worktree else None,
            predecessor_id=predecessor.id if predecessor else None,
            team_token_hash=_hash(token),
        )
        await self.publish("staff.status", {"status": "starting", "previous": None, "actor": by}, member=member)
        first = self.first_message(member, task, folder, worktree, predecessor, by)
        try:
            recorded = await self.manager.staff.add_message(member.id, first, origin=by, mode="queue", staff_session_id=session.id)
            first_id = recorded.id
        except StaffError:
            first_id = ""  # a brief longer than a message may be; it is still sent, just not receipted
        # On the board before the session starts: a quick worker's Report(done) must find the task in
        # doing, not be overtaken by this move.
        moved = await self._move_task(task, "doing", actor=by, assignee=member.id, branch=worktree.branch if worktree else None, folder_id=folder.id)
        request = StartRequest(
            staff=member,
            project=project,
            folder=folder,
            cwd=worktree.cwd if worktree else folder.path,
            worktree=worktree,
            task=moved,
            first_message=first,
            brief_text=await self.brief(member, project, folder, worktree),
            staff_session_id=session.id,
            env=folder.env,
            model=member.model,
            effort=member.effort,
            agent=member.agent,
            permission_level=self.permission_level(project),
            permission_mode=member.permission_mode,
            team_url=f"http://127.0.0.1:{self.app.settings.api_port}/api/team/{session.id}",
            team_token=token,
            predecessor=LiveSession(member, predecessor) if predecessor else None,
            origin="orchestrator" if by == "orchestrator" else "operator",
            first_message_id=first_id,
        )
        try:
            started = await runtime.start(request)
        except Exception as exc:
            await self.manager.staff.end_session(session.id, f"could not start: {exc}"[:500])
            await self.publish("staff.status", {"status": "exited", "previous": "starting", "detail": f"could not start: {exc}"[:500]}, member=member)
            await self._move_task(moved, "todo", actor="system", assignee=None)
            raise
        await self.manager.staff.started(session.id, session_id=started.session_id, terminal_id=started.terminal_id, cli_session_id=started.cli_session_id, transcript_ref=started.transcript_ref)
        refreshed = await self.manager.staff.session(session.id)
        if first_id:
            await self.ingress.message_state(first_id, "submitted")
        return LiveSession(member, refreshed or session)

    def permission_level(self, project: Project) -> str:
        """The permission level a command-line member starts with. ``full`` autonomy starts it exactly
        like ``normal``: the operator decided that full means the orchestrator answers the requests
        itself, not that the agent stops asking — a bypassed permission is one nobody sees."""
        autonomy = project.settings.orchestrator.autonomy if project.settings.orchestrator.enabled else "ask"
        return {"ask": "ask", "normal": "edits", "full": "edits"}.get(autonomy, "ask")

    async def brief(self, member: Staff, project: Project, folder: ProjectFolder, worktree: Worktree | None) -> str:
        if worktree is not None:
            where = prompts.STAFF_WORKTREE_CLAUSE.format(path=worktree.cwd, branch=worktree.branch, base=worktree.base_ref)
        elif member.isolation == "readonly":
            where = prompts.STAFF_READONLY_CLAUSE.format(path=folder.path)
        else:
            where = prompts.STAFF_SHARED_CLAUSE.format(path=folder.path)
        persona = ""
        if member.harness == "daedalus" and member.agent:
            text = self._persona(member.agent)
            if text:
                persona = f"\n\n[persona: {member.agent}]\n{text}"
        return prompts.STAFF_BRIEF.format(
            name=member.name,
            project=project.name,
            role=f" Your responsibility: {member.role}." if member.role else "",
            where=where,
            done_rule=" (a worktree with uncommitted changes is refused: commit first)" if worktree is not None else "",
            notes=f"\nYour notes from earlier sessions:\n{member.notes}\n" if member.notes else "",
            instructions=f"\nStanding instructions:\n{member.instructions}\n" if member.instructions else "",
            persona=persona,
        )

    def _persona(self, name: str) -> str | None:
        subagents = self.app.extensions.get("subagents")
        return subagents.persona(name) if subagents is not None else None

    def first_message(self, member: Staff, task: BoardTask, folder: ProjectFolder, worktree: Worktree | None, predecessor: StaffSession | None, by: str) -> str:
        before = ""
        if predecessor is not None:
            why = predecessor.end_reason or predecessor.status
            before = (
                f"\n\nThis task was worked on before, in session {predecessor.id}, which ended: {why}. "
                "Look at what is already there before you start over."
            )
        if task.sent_back:
            before += f"\n\nThe operator sent this work back from review: {task.sent_back}\nChange it on the same branch, commit, and report done again."
        return prompts.STAFF_TASK.format(
            task_id=task.id,
            by="orchestrator" if by == "orchestrator" else "operator",
            title=task.title,
            objective=task.objective,
            deliverable=task.deliverable,
            boundaries=task.boundaries,
            done_when=task.done_when,
            folder=worktree.cwd if worktree is not None else folder.path,
            branch=f"\nBranch: {worktree.branch} (from {worktree.base_ref})" if worktree is not None else "",
            predecessor=before,
        )

    # -- control ---------------------------------------------------------------------------------------

    async def tell(self, member: Staff, text: str, *, mode: str = "queue", by: str = "operator") -> dict[str, Any]:
        """Say something to a member's live session; returns the message and its receipt."""
        live = await self.live_of(member)
        if live is None:
            raise StaffError(f"{member.name} has no live session; assign a task to start one")
        if live.session.pause_requested:
            await self.manager.staff.request_pause(live.id, False)
        message = await self.manager.staff.add_message(member.id, text, origin=by, mode=mode, staff_session_id=live.id)
        outgoing = OutgoingMessage(message.id, message.text, mode, "orchestrator" if by == "orchestrator" else "operator")  # type: ignore[arg-type]
        try:
            receipt = await self.runtime(member).send(live, outgoing)
        except Exception as exc:  # noqa: BLE001 — a failed delivery is recorded on the message, not raised past it
            receipt = Receipt("failed", str(exc)[:500])
        await self.ingress.message_state(message.id, receipt.state, receipt.error)
        return {"message_id": message.id, "state": receipt.state, "error": receipt.error, "degraded_to": receipt.degraded_to}

    async def interrupt(self, member: Staff) -> None:
        live = await self.live_of(member)
        if live is None:
            raise StaffError(f"{member.name} has no live session")
        await self.runtime(member).interrupt(live)

    async def pause(self, member: Staff) -> dict[str, Any]:
        """Let the turn finish, commit what is uncommitted, and start nothing new until told or assigned."""
        live = await self.live_of(member)
        if live is None:
            raise StaffError(f"{member.name} has no live session")
        await self.manager.staff.request_pause(live.id)
        if live.session.status not in ACTIVE_STATUSES:
            return {"paused": True, "commit": await self._settle_pause(live)}
        return {"paused": False, "note": f"{member.name} pauses when the current turn ends"}

    async def _settle_pause(self, live: LiveSession) -> str | None:
        commit = None
        worktree = await self.worktree_of(live.session)
        if worktree is not None:
            title = live.session.task_id or "task"
            try:
                commit = await self.worktrees.commit_wip(worktree, f"wip: {title} (paused)")
            except WorktreeError as exc:
                logger.warning("could not commit the paused work of %s: %s", live.staff.name, exc)
        await self.ingress.status(live, "idle", detail="paused" + (f"; work committed as {commit[:10]}" if commit else ""))
        return commit

    async def worktree_of(self, session: StaffSession) -> Worktree | None:
        """The worktree a session works in, rebuilt from its row: the folder it was made in is three
        levels up (``<folder>/.agents/worktrees/<name>``), and the folder's environment is the git's."""
        if not session.worktree_path or not session.branch:
            return None
        path = Path(session.worktree_path)
        env = self.manager.projects.local_env
        if session.folder_id:
            row = await self.manager.db.fetchone("SELECT env FROM project_folders WHERE id = ?", (session.folder_id,))
            env = str(row["env"]) if row is not None else env
        return Worktree(path=path, branch=session.branch, base_ref=session.base_ref or "HEAD", folder=path.parent.parent.parent, env=env)

    async def release(self, member: Staff, *, keep_worktree: bool = True, reason: str = "released", by: str = "operator") -> bool:
        """End the member's live session: its runtime stops it, the row ends, the task goes back to todo.

        The worktree stays unless asked otherwise, and even then an unmerged branch is kept: it is the
        only copy of the work. Returns whether there was a session to end.
        """
        live = await self.live_of(member)
        if live is None:
            return False
        await self._end(live, reason, stop=True, by=by)
        if live.session.task_id:
            task = await self.task(live.session.task_id)
            if task is not None and task.status == "doing":
                # Named by who released, so the orchestrator is not woken by its own release.
                await self._move_task(task, "todo", actor=by, assignee=None)
        if not keep_worktree:
            worktree = await self.worktree_of(live.session)
            if worktree is not None:
                try:
                    await self.worktrees.remove(worktree, delete_branch_if_merged=True)
                except WorktreeError as exc:
                    logger.warning("kept the worktree of %s: %s", member.name, exc)
        self.queue.pump_soon(member.project_id)
        return True

    async def _end(self, live: LiveSession, reason: str, *, stop: bool, by: str | None = None) -> None:
        if stop:
            try:
                await self.runtime(live.staff).stop(live)
            except Exception:  # noqa: BLE001 — the row ends whatever the process did; reconcile finds a survivor
                logger.exception("stopping %s's session failed", live.staff.name)
        ended = await self.manager.staff.end_session(live.id, reason)
        if ended is not None:
            payload: dict[str, Any] = {"status": "exited", "previous": live.session.status, "detail": reason[:500]}
            if by:
                payload["actor"] = by
            await self.publish("staff.status", payload, member=live.staff, session_id=live.session_id)
            for ask in await self._open_asks(live.id):
                if await self.manager.asks.resolve(ask.id, "system", {"closed": f"the session ended: {reason}"}):
                    await self._withdrawn(ask)

    async def _open_asks(self, staff_session_id: str) -> list[Ask]:
        rows = await self.manager.db.fetchall("SELECT id FROM asks WHERE staff_session_id = ? AND resolved_at IS NULL ORDER BY created_at", (staff_session_id,))
        out = []
        for row in rows:
            ask = await self.manager.asks.get(row["id"])
            if ask is not None:
                out.append(ask)
        return out

    async def seen(self, live: LiveSession) -> None:
        """Someone read the finished turn: it is no longer news."""
        if live.session.status == "turn_done_unseen":
            await self.ingress.status(live, "idle")

    # -- requests ----------------------------------------------------------------------------------------

    def route(self, project: Project, kind: str) -> str:
        """Who a staff request goes to first, by the project's autonomy (see ``answer``)."""
        orchestrator = project.settings.orchestrator
        if not orchestrator.enabled:
            return "operator"
        if kind == "permission" and orchestrator.autonomy == "ask":
            return "operator"
        return "orchestrator"

    async def answer(self, ref: str, *, allow: bool | None = None, always: bool = False, text: str | None = None, selected: list[str] | None = None, by: str = "operator", basis: str = "", via: str | None = None) -> dict[str, Any]:
        """Answer a request, once. The first answer to update the row delivers; a later one is refused.

        The orchestrator answers within the project's autonomy. At ``ask`` its answer to a question
        becomes a suggestion the operator confirms, and a permission is the operator's. At ``normal``
        it may grant only by quoting, as ``basis``, a line of the brief's allowances — the section only
        the operator writes — so a grant is never consent read into a chat message. At ``full`` any
        stated reason will do. Denying is always allowed, and every grant is written to the journal.
        """
        ask = await self.manager.asks.get(ref)
        if ask is None:
            raise KeyError(ref)
        if not ask.open:
            raise AlreadyAnswered(f"request {ask.short_id} was already answered by the {ask.resolved_by}")
        project = await self.project(ask.project_id)
        if by == "orchestrator":
            outcome = await self._orchestrator_may(ask, project, allow=allow, text=text, selected=selected, basis=basis)
            if outcome is not None:
                return outcome
        resolution: dict[str, Any] = {"allow": allow, "text": text or "", "selected": list(selected or []), "via": via or ("orchestrator" if by == "orchestrator" else "app")}
        if basis:
            resolution["basis"] = basis
        # "Always" is the operator's alone: a standing grant is a change to what the member may do,
        # which the brief's allowances give the orchestrator no say over.
        always = bool(always and allow and ask.kind == "permission" and by == "operator")
        if always:
            resolution["always"] = True
        if not await self.manager.asks.resolve(ask.id, by, resolution):
            current = await self.manager.asks.get(ask.id)
            raise AlreadyAnswered(f"request {ask.short_id} was already answered by the {current.resolved_by if current else 'someone else'}")
        delivered, error = await self._deliver(ask, allow=allow, always=always, text=text, selected=selected, by=by)
        if ask.kind == "permission" and allow:
            who = "The orchestrator" if by == "orchestrator" else "The operator"
            member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
            await self.manager.projects.record(
                project.id, "system", "grant",
                f"{who} granted {member.name if member else 'a staff member'}: {ask.text[:300]}" + (f" (basis: {basis})" if basis else ""),
                {"ask_id": ask.id, "staff_id": ask.staff_id or ""},
            )
        await self._announce_resolved(ask, allow=allow, by=by, via=resolution["via"])
        answered = await self.manager.asks.get(ask.id)
        return {"state": "answered", "delivered": delivered, "error": error, "ask": answered.view() if answered else ask.view()}

    async def _orchestrator_may(self, ask: Ask, project: Project, *, allow: bool | None, text: str | None, selected: list[str] | None, basis: str) -> dict[str, Any] | None:
        """The orchestrator's limits; a dict when the answer ends here (a suggestion), ``None`` to go on."""
        orchestrator = project.settings.orchestrator
        if not orchestrator.enabled:
            raise StaffError(f"{project.name} has no orchestrator")
        if ask.routed_to != "orchestrator":
            raise StaffError(f"request {ask.short_id} is the operator's to answer")
        if ask.kind == "question" and orchestrator.autonomy == "ask":
            suggestion = (text or "").strip() or ", ".join(selected or [])
            await self.manager.asks.route(ask.id, "operator", suggestion)
            await self._release_hold(ask)
            routed = await self.manager.asks.get(ask.id)
            return {"state": "suggested", "delivered": False, "error": "", "ask": routed.view() if routed else ask.view()}
        if ask.kind == "permission" and allow:
            if orchestrator.autonomy == "ask":
                raise StaffError(f"in {project.name} the operator decides permissions")
            if orchestrator.autonomy == "normal":
                quoted = " ".join(basis.split())
                allowances = (await self.manager.projects.brief(project.id))["allowed_without_operator"].body
                lines = [" ".join(line.split()) for line in allowances.splitlines()]
                if len(quoted) < BASIS_MIN or not any(quoted in line for line in lines if line):
                    raise StaffError(
                        "a grant needs, as its basis, a line quoted verbatim from the brief's 'allowed without the operator' "
                        f"(at least {BASIS_MIN} characters); otherwise escalate the request to the operator"
                    )
            elif not (basis.strip() or (text or "").strip()):
                raise StaffError("a grant states its reason")
        return None

    async def _deliver(self, ask: Ask, *, allow: bool | None, text: str | None, selected: list[str] | None, by: str, always: bool = False) -> tuple[bool, str]:
        if ask.origin == "orchestrator":
            if self.own_requests is None:
                return False, "no orchestrator is installed to take the answer"
            delivered: tuple[bool, str] = await self.own_requests.deliver_own(ask, allow=allow, text=text, selected=selected)
            return delivered
        live = await self.live(ask.staff_session_id) if ask.staff_session_id else None
        if live is None:
            return False, "the session that asked has ended"
        decision = Decision(allow=allow, text=text, selected=list(selected or []), by="orchestrator" if by == "orchestrator" else "operator", always=always)
        try:
            await self.runtime(live.staff).answer(live, AskRef(ask.id, ask.kind, ask.request_ref), decision)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 — the answer is recorded; the failure to deliver it is reported beside it
            logger.warning("could not deliver the answer to %s: %s", ask.short_id, exc)
            return False, str(exc)[:500]
        await self._after_answer(live)
        return True, ""

    async def _after_answer(self, live: LiveSession) -> None:
        """The status once a request is answered: working again, unless another request of the
        session is still open — then it waits on that one, a permission before a question, since a
        permission holds the process itself."""
        remaining = await self._open_asks(live.id)
        current = await self.live(live.id) or live
        if not remaining:
            await self.ingress.status(current, "working")
            return
        ask = next((a for a in remaining if a.kind == "permission"), remaining[0])
        if ask.kind == "permission":
            words = f"permission [{ask.short_id}]: {ask.detail.get('tool') or ask.text}"
        else:
            words = f"question [{ask.short_id}]: {ask.text}"
        await self.ingress.status(current, ask.kind, words)

    async def _announce_resolved(self, ask: Ask, *, allow: bool | None, by: str, via: str) -> None:
        """The pending events of a command-line member's request are this module's, so their answers are too,
        and so are those of the orchestrator's own requests. A Daedalus member's are the session's own,
        published when the session is answered or granted. The notification router closes the operator's
        copy from these events, and the orchestrator is woken by the answer to what it asked."""
        ref = str(ask.detail.get("event_ref") or "")
        if ref.startswith("orchestrator:"):
            await self.publish("ask.answered", {"request_id": ask.id, "request_ref": ref, "via": via, "by": by}, project_id=ask.project_id)
        elif ref.startswith("staff:"):
            member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
            if ask.kind == "permission":
                # Answered on the agent's own screen, the decision is the CLI's to know, not ours.
                decision = "terminal" if allow is None and via == "terminal" else "allow" if allow else "deny"
                await self.publish("permission.resolved", {"request_id": ask.id, "request_ref": ref, "decision": decision, "via": via, "by": by}, member=member, project_id=ask.project_id)
            else:
                await self.publish("ask.answered", {"request_id": ask.id, "request_ref": ref, "via": via}, member=member, project_id=ask.project_id)

    async def _withdrawn(self, ask: Ask) -> None:
        """A request nobody will answer any more, because its session ended: the operator's copy closes."""
        ref = str(ask.detail.get("event_ref") or "")
        resolve = getattr(self.app.notifications, "resolve", None)
        if resolve is not None and ref:
            try:
                await resolve(ref, "withdrawn", via="system")
            except Exception:  # noqa: BLE001
                logger.warning("could not close the notification of %s", ask.short_id, exc_info=True)

    async def resolve_action(self, req: Any) -> ActionOutcome:
        """An answer to a command-line member's request taken from a notification (``staff:<session>:<ask>``)."""
        ask = await self.manager.asks.get(req.target)
        if ask is None:
            raise ActionConflict("withdrawn")
        allow: bool | None = None
        text: str | None = None
        selected: list[str] = []
        if ask.kind == "permission":
            if req.action not in ("allow", "deny"):
                raise ActionRefused(f"a permission is not answered with {req.action!r}")
            allow = req.action == "allow"
        elif req.action.startswith("answer:"):
            options = list(ask.detail.get("options") or [])
            try:
                selected = [str(options[int(req.action.split(":", 1)[1])])]
            except (ValueError, IndexError) as exc:
                raise ActionRefused(f"no option {req.action!r}") from exc
        elif req.action == "answer" and (req.value or "").strip():
            text = req.value.strip()
        else:
            raise ActionRefused(f"a question is not answered with {req.action!r}")
        try:
            await self.answer(ask.id, allow=allow, text=text, selected=selected, by="operator", via=req.via)
        except AlreadyAnswered as exc:
            raise ActionConflict("answered") from exc
        return ActionOutcome("allow" if allow else "deny" if ask.kind == "permission" else "answered")

    async def _release_hold(self, ask: Ask) -> None:
        """The operator hears of a request now: the router's hold ends early, or, without a router, an
        urgent notification says it waits for them."""
        ref = str(ask.detail.get("event_ref") or "")
        notifications = self.app.notifications
        release = getattr(notifications, "release", None)
        if release is not None and ref:
            try:
                await release(ref)
                return
            except Exception:  # noqa: BLE001
                logger.warning("could not release the hold on %s", ask.short_id, exc_info=True)
        if notifications is not None:
            member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
            who = member.name if member else "A staff member"
            await notifications.post(Draft(
                "permission" if ask.kind == "permission" else "question",
                f"{who} is waiting for you [{ask.short_id}]",
                ask.text[:1000] + (f"\n\nThe orchestrator suggests: {ask.suggestion}" if ask.suggestion else ""),
                kind=f"staff_{ask.kind}",
                level="urgent",
                project_id=ask.project_id,
                staff_id=ask.staff_id,
                dedupe_key=f"staff-ask:{ask.id}",
                source="staff",
            ))

    async def escalate(self, ask: Ask, *, why: str = "", suggestion: str = "") -> bool:
        """Hand a request the orchestrator has not answered to the operator, with what it would have
        answered when it has a view. True when it moved."""
        if not ask.open or ask.routed_to == "operator":
            return False
        if not await self.manager.asks.route(ask.id, "operator", suggestion):
            return False
        await self.manager.projects.record(ask.project_id, "system", "escalation", f"Request {ask.short_id} went to the operator{': ' + why if why else ''}", {"ask_id": ask.id})
        routed = await self.manager.asks.get(ask.id)
        await self._release_hold(routed or ask)
        return True

    async def claim_answer(self, session_id: str, tool_call_id: str, via: str) -> str | None:
        """An answer typed into a Daedalus member's session is the operator's answer to the request."""
        live = await self.live_for_session(session_id)
        if live is None:
            return None
        row = await self.manager.db.fetchone(
            "SELECT id FROM asks WHERE staff_session_id = ? AND request_ref = ? AND kind = 'question' ORDER BY created_at DESC LIMIT 1", (live.id, tool_call_id)
        )
        if row is None:
            return None
        ask = await self.manager.asks.get(row["id"])
        if ask is None:
            return None
        if ask.open and await self.manager.asks.resolve(ask.id, "operator", {"via": via, "in_session": True}):
            await self._after_answer(live)
            return None
        current = await self.manager.asks.get(ask.id)
        return f"this question was already answered by the {current.resolved_by if current else 'someone else'}"

    # -- following the sessions --------------------------------------------------------------------------

    async def on_turn_event(self, session_id: str, event: TurnEvent | Any) -> None:
        """Every event of a staff session is a sign of life; a few say more."""
        if not isinstance(event, TurnEvent):
            return
        state = self.manager.live_state(session_id)
        if state is None or not state.metadata.get("staff_session_id"):
            return
        live = await self.live(str(state.metadata["staff_session_id"]))
        if live is None:
            return
        if live.session.status == "no_signal":
            await self.ingress.status(live, "working", detail="signal again")
        else:
            await self.manager.staff.touch(live.id)
        if event.type is EventType.TOOL_CALL_PENDING and event.payload.get("kind") == "ask_user" and event.payload.get("tool_name") == "AskOrchestrator":
            questions = (event.payload.get("ask_user_payload") or {}).get("questions") or [{}]
            first = questions[0] if isinstance(questions[0], dict) else {}
            options = [str(o.get("label") or "") for o in first.get("options") or [] if isinstance(o, dict)]
            call_id = str(event.payload.get("tool_call_id") or "")
            await self.ingress.question(live, call_id, str(first.get("question") or ""), options, event_ref=f"ask:{session_id}:{call_id}")
        elif event.type is EventType.QUEUE_UPDATE and event.payload.get("placed"):
            # A queued message the model has now received: that is the acknowledgement.
            for message_id in event.payload["placed"]:
                if str(message_id).startswith("sm-"):
                    await self.ingress.message_state(str(message_id), "acknowledged")

    async def on_run_started(self, session_id: str, run_id: str) -> None:
        live = await self.live_for_session(session_id)
        if live is not None and live.session.status != "working":
            await self.ingress.status(live, "working")

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        live = await self.live_for_session(session_id)
        if live is None or status == "awaiting":
            return
        if live.session.pause_requested and status in ("completed", "cancelled"):
            await self._settle_pause(live)
        elif await self._open_asks(live.id):
            pass  # still waiting on a question or a permission; the request says so
        elif status == "completed":
            await self.ingress.status(live, "turn_done_unseen")
        elif status == "failed":
            state = self.manager.live_state(session_id)
            await self.ingress.status(live, "error", detail=str(getattr(state, "last_error_message", "") or "the run failed")[:500])
        else:
            await self.ingress.status(live, "idle", detail="interrupted")
        usage = await self.runtime(live.staff).usage(live)
        if usage is not None:
            await self.ingress.usage(live, usage)

    async def on_session_deleted(self, session_id: str) -> None:
        live = await self.live_for_session(session_id)
        if live is not None:
            await self._end(live, "the session was deleted", stop=False)
            self.queue.pump_soon(live.staff.project_id)

    async def on_bus(self, event: AppEvent) -> None:
        """The session-level events of staff sessions, and the board's moves."""
        if event.type == "permission.pending" and event.session_id:
            live = await self.live_for_session(event.session_id)
            if live is not None:
                p = event.payload
                await self.ingress.permission(live, str(p.get("request_id") or ""), str(p.get("tool") or ""), str(p.get("text") or ""), event_ref=str(p.get("request_ref") or ""))
        elif event.type == "permission.resolved" and event.session_id and event.payload.get("via") != "orchestrator":
            live = await self.live_for_session(event.session_id)
            if live is not None:
                row = await self.manager.db.fetchone(
                    "SELECT id FROM asks WHERE staff_session_id = ? AND request_ref = ? AND kind = 'permission' AND resolved_at IS NULL", (live.id, str(event.payload.get("request_id") or ""))
                )
                if row is not None and await self.manager.asks.resolve(row["id"], "operator", {"allow": event.payload.get("decision") == "allow", "via": event.payload.get("via"), "in_session": True}):
                    await self._after_answer(live)
        elif event.type == "presence":
            for session_id in (event.payload.get("newly_attended") or {}).get("sessions") or ():
                live = await self.live_for_session(str(session_id))
                if live is not None:
                    await self.seen(live)
        elif event.type == "task.moved" and event.payload.get("to") in FINISHED_TASK:
            await self._task_finished(str(event.payload.get("task_id") or ""))
        elif event.type == "task.moved" and event.payload.get("to") == "todo" and event.project_id:
            self.queue.pump_soon(event.project_id)  # a dependency finished: a waiting task may go
        elif event.type in ("staff.status", "terminal.exited"):
            if event.type == "terminal.exited" or event.payload.get("status") in ("exited", "idle", "turn_done_unseen", "error"):
                self.queue.pump_soon(event.project_id if event.type == "staff.status" else None)

    async def _task_finished(self, task_id: str) -> None:
        """A task done or dropped: nobody waits to start it, and a one-off helper goes with it."""
        task = await self.task(task_id)
        if task is None or task.project_id is None:
            return
        self.queue.withdraw(task.project_id, task_id=task.id)
        if not task.assignee_staff_id:
            return
        member = await self.manager.staff.get(task.assignee_staff_id)
        if member is None or not member.one_off or not member.active:
            self.queue.pump_soon(task.project_id)
            return
        live = await self.live_of(member)
        if live is not None and live.session.task_id == task.id:
            await self._end(live, f"its task was {task.status}", stop=True)
        try:
            await self.manager.staff.archive(member.id, by="system")
            await self.publish("project.changed", {"change": "staff.dismissed", "actor": "system"}, member=member)
        except StaffBusy:
            logger.info("one-off %s still has a session; left on the team", member.name)
        self.queue.pump_soon(task.project_id)

    # -- the clock -----------------------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> None:
        """Silence and stale requests: a working session that says nothing is shown silent, and a
        request the orchestrator has left too long goes to the operator."""
        now = now or datetime.now(UTC)
        config = self.manager.config.staff
        for session in await self.manager.staff.all_live():
            if session.status == "working" and _age_seconds(session.last_signal_at, now) > config.silence_minutes * 60:
                live = await self.live(session.id)
                if live is not None:
                    await self.ingress.status(live, "no_signal", detail=f"no signal for {config.silence_minutes} minutes")
        cutoff = (now - timedelta(minutes=config.ask_escalate_minutes)).isoformat()
        rows = await self.manager.db.fetchall("SELECT id FROM asks WHERE resolved_at IS NULL AND routed_to = 'orchestrator' AND routed_at < ?", (cutoff,))
        for row in rows:
            ask = await self.manager.asks.get(row["id"])
            if ask is not None:
                await self.escalate(ask, why=f"the orchestrator left it unanswered for {config.ask_escalate_minutes} minutes")
        await self.queue.pump()

    async def loop(self) -> None:
        await asyncio.sleep(FIRST_PUMP_SECONDS)
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("staff tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def rebuild(self) -> int:
        """Offer every assigned task that has not started to the queue again; the board is what survives a restart."""
        rows = await self.manager.db.fetchall(
            "SELECT t.id AS task_id, t.priority, t.project_id, t.folder_id, m.id AS staff_id FROM board_tasks t JOIN staff m ON m.id = t.assignee_staff_id "
            "WHERE t.status IN ('todo', 'blocked') AND m.archived_at IS NULL ORDER BY t.priority, t.created_at"
        )
        count = 0
        for row in rows:
            member = await self.manager.staff.get(row["staff_id"])
            project = await self.manager.projects.get(row["project_id"]) if row["project_id"] else None
            if member is None or project is None:
                continue
            task = await self.task(row["task_id"])
            folder = self.folder_for(project, member, task)
            self.queue.add(Entry(project.id, member.id, member.name, row["task_id"], int(row["priority"]), member.harness != "daedalus", "operator", env=folder.env))
            count += 1
        return count

    # -- the team server of command-line staff --------------------------------------------------------

    async def authenticate(self, staff_session_id: str, token: str) -> LiveSession:
        """The live session a team token was minted for; ``PermissionError`` for anything else."""
        expected = await self.manager.staff.team_token_hash(staff_session_id)
        if not token or not expected or not secrets.compare_digest(_hash(token), expected):
            raise PermissionError("the team token does not match this session")
        live = await self.live(staff_session_id)
        if live is None:
            raise PermissionError("this session has ended")
        return live

    def attach(self) -> asyncio.Task[None]:
        """Follow the sessions: every hook the team needs from the session manager and the bus."""
        manager = self.manager
        manager.service_hooks["staff"] = self.service
        manager.add_sink(self.on_turn_event)
        manager.run_started_hooks.append(self.on_run_started)
        manager.on_finished(self.on_run_finished)
        manager.delete_hooks.append(self.on_session_deleted)
        manager.answer_claims.append(self.claim_answer)
        notifications = self.app.notifications
        if notifications is not None and hasattr(notifications, "register_resolver"):
            notifications.register_resolver("staff", self.resolve_action)
        return manager.bus.on(
            EventFilter(types=("permission.pending", "permission.resolved", "presence", "task.moved", "staff.status", "terminal.exited")),
            self.on_bus,
            name="staff",
        )

    # -- the tools' hook -------------------------------------------------------------------------------

    async def service(self, op: str, **kwargs: Any) -> Any:
        session_id = str(kwargs.get("session_id") or "")
        live = await self.live_for_session(session_id)
        if live is None:
            raise RuntimeError("this session is not a staff member's live session")
        if op == "can_ask":
            return True
        if op == "report":
            return await self.ingress.report(live, str(kwargs.get("kind") or ""), str(kwargs.get("note") or ""), kwargs.get("artifacts"), kwargs.get("remember"))
        raise ValueError(op)


class Ingress:
    """:class:`daedalus.staff_runtime.TeamIngress`: every change of a staff session's state, written and announced."""

    def __init__(self, team: Team) -> None:
        self.team = team

    @property
    def manager(self) -> SessionManager:
        return self.team.manager

    async def status(self, live: LiveSession, status: str, waiting_for: str = "", *, detail: str = "", actor: str = "") -> None:
        # Compared with the row as it was, not with the caller's copy of it: two paths report the same
        # change — a command-line runtime sets the session working when it delivers an answer, and the
        # team does after it — and a stale copy would announce the second as a change of its own.
        before = await self.manager.staff.session(live.id)
        # One line: it is a status, and the whole question is on the request.
        changed = await self.manager.staff.set_status(live.id, status, " ".join(waiting_for.split())[:200])
        if changed is None:
            return
        previous, session = changed
        if previous == status and session.waiting_for == (before.waiting_for if before is not None else live.session.waiting_for):
            return
        payload: dict[str, Any] = {"status": status, "previous": previous}
        if session.waiting_for:
            payload["waiting_for"] = session.waiting_for
        if detail:
            payload["detail"] = detail[:500]
        if actor:
            payload["actor"] = actor
        await self.team.publish("staff.status", payload, member=live.staff, session_id=live.session_id)
        if status == "turn_done_unseen" and session.pause_requested and session.kind == "cli":
            # A command-line member's turn ends here, not in a run of this host: this is where a
            # pause asked for during the turn takes effect (a Daedalus member's in on_run_finished).
            await self.team._settle_pause(LiveSession(live.staff, session))

    async def _open(self, live: LiveSession, kind: str, request_ref: str, text: str, detail: dict[str, Any], event_ref: str | None) -> Ask:
        if request_ref:
            # The same request seen again — a hook the daemon replayed after the host restarted — is
            # the request already open, not a second one for the orchestrator to answer twice.
            row = await self.manager.db.fetchone(
                "SELECT id FROM asks WHERE staff_session_id = ? AND request_ref = ? AND kind = ? AND resolved_at IS NULL LIMIT 1", (live.id, request_ref, kind)
            )
            existing = await self.manager.asks.get(row["id"]) if row is not None else None
            if existing is not None:
                return existing
        project = await self.team.project(live.staff.project_id)
        routed = self.team.route(project, kind)
        ask = await self.manager.asks.open(
            project.id,
            origin="staff",
            kind=kind,
            text=text.strip()[:8000] or f"{live.staff.name} asks",
            routed_to=routed,
            staff_id=live.staff.id,
            staff_session_id=live.id,
            task_id=live.session.task_id,
            request_ref=request_ref,
            detail={**detail, "event_ref": event_ref or ""},
        )
        if event_ref is None:
            # A command-line member's request has no session to announce it, so the ingress does,
            # under a reference of its own that answering it resolves.
            ref = f"staff:{live.id}:{ask.id}"
            await self.manager.db.execute("UPDATE asks SET detail_json = json_set(detail_json, '$.event_ref', ?) WHERE id = ?", (ref, ask.id))
            ask = (await self.manager.asks.get(ask.id)) or ask
            common = {"request_id": ask.id, "request_ref": ref, "title": live.staff.name, "telegram": False, "routed_to": routed, "short_id": ask.short_id}
            if kind == "permission":
                await self.team.publish("permission.pending", {**common, "kind": "staff", "tool": str(detail.get("tool") or ""), "text": text[:300], "risk": "routine", "quick": routed == "operator"}, member=live.staff)
            else:
                options = [{"label": o, "description": ""} for o in detail.get("options") or []]
                await self.team.publish("ask.pending", {**common, "run_id": "", "questions": [{"question": text[:2000], "options": options, "multi": False, "custom": True}], "operator_facing": routed == "operator"}, member=live.staff)
        elif routed == "operator":
            await self.team._release_hold(ask)
        return ask

    async def permission(self, live: LiveSession, request_ref: str, tool: str, summary: str, *, event_ref: str | None = None) -> str:
        ask = await self._open(live, "permission", request_ref, f"{tool}: {summary}" if tool else summary, {"tool": tool}, event_ref)
        await self.status(live, "permission", f"permission [{ask.short_id}]: {tool or summary}")
        return ask.id

    async def question(self, live: LiveSession, request_ref: str, text: str, options: list[str], *, event_ref: str | None = None) -> str:
        ask = await self._open(live, "question", request_ref, text, {"options": list(options)}, event_ref)
        await self.status(live, "question", f"question [{ask.short_id}]: {text}")
        return ask.id

    async def message_state(self, message_id: str, state: str, error: str = "") -> None:
        before = await self.manager.staff.message(message_id)
        after = await self.manager.staff.set_message_state(message_id, state, error)
        if after is None or before is None or after.state == before.state:
            return
        member = await self.manager.staff.get(after.staff_id)
        payload: dict[str, Any] = {"message_id": message_id, "state": after.state}
        if after.error:
            payload["error"] = after.error[:500]
        await self.team.publish("staff.message", payload, member=member)

    async def report(self, live: LiveSession, kind: str, note: str, artifacts: list[str] | None = None, remember: str | None = None, *, call_id: str | None = None) -> str:
        if call_id and await self._reported(live, call_id):
            return f"reported {kind}"
        if kind not in ("checkpoint", "needs_input", "stuck", "done"):
            raise ValueError("kind is checkpoint, needs_input, stuck or done")
        note = (note or "").strip()
        if not note:
            raise ValueError("a report needs a note")
        task = await self.team.task(live.session.task_id) if live.session.task_id else None
        told = f"reported {kind}"
        if kind == "done":
            worktree = await self.team.worktree_of(live.session)
            if worktree is not None:
                try:
                    status = await self.team.worktrees.status(worktree)
                except WorktreeError as exc:
                    raise RuntimeError(f"the worktree could not be checked: {exc}") from exc
                if status.dirty:
                    raise ValueError(f"{worktree.path} has uncommitted changes; commit them on {worktree.branch} and report again")
            if task is not None and task.status in ("doing", "todo", "blocked"):
                await self.team._move_task(task, "review", actor="staff", merge_state="proposed" if worktree is not None else "")
                told += f"; task {task.id} is in review"
        if remember and remember.strip():
            await self.manager.staff.append_notes(live.staff.id, remember.strip())
            told += "; noted for your next sessions"
        payload: dict[str, Any] = {"kind": kind, "text": note[:NOTE_MAX], "actor": "staff"}
        refs = [str(a)[:300] for a in (artifacts or []) if str(a).strip()][:20]
        if refs:
            payload["refs"] = refs
        if task is not None:
            payload["task_id"] = task.id
        if call_id:
            payload["call_id"] = call_id
        await self.team.publish("staff.report", payload, member=live.staff, session_id=live.session_id)
        return told

    async def _reported(self, live: LiveSession, call_id: str) -> bool:
        """Whether a report with this call id was already published: the host restarted after acting
        on the post and before the daemon heard it was taken, and the daemon replayed it."""
        row = await self.manager.db.fetchone(
            "SELECT 1 FROM app_events WHERE project_id = ? AND type = 'staff.report' AND staff_id = ? AND json_extract(payload_json, '$.call_id') = ? LIMIT 1",
            (live.staff.project_id, live.staff.id, call_id),
        )
        return row is not None

    async def implicit_report(self, live: LiveSession, kind: str, text: str) -> None:
        task = await self.team.task(live.session.task_id) if live.session.task_id else None
        payload: dict[str, Any] = {"kind": kind, "text": text[:NOTE_MAX], "actor": "system", "implicit": True}
        if task is not None:
            payload["task_id"] = task.id
        await self.team.publish("staff.report", payload, member=live.staff, session_id=live.session_id)

    async def channel(self, live: LiveSession, team_tools: str, detail: str = "") -> None:
        payload: dict[str, Any] = {"team_tools": team_tools}
        if detail:
            payload["detail"] = detail[:500]
        await self.team.publish("staff.channel", payload, member=live.staff, session_id=live.session_id)

    async def messages_of(self, live: LiveSession) -> list[Any]:
        messages = await self.manager.staff.messages(live.staff.id, limit=500)
        return [m for m in reversed(list(messages)) if m.staff_session_id == live.id]

    async def ask(self, live: LiveSession, question: str, options: list[str] | None = None, context: str = "") -> str:
        text = question.strip() + (f"\n\nContext: {context.strip()}" if context.strip() else "")
        return await self.question(live, f"team:{uuid.uuid4().hex[:12]}", text, list(options or []))

    async def usage(self, live: LiveSession, snapshot: UsageSnapshot) -> None:
        await self.manager.staff.record_usage(live.id, snapshot.view())

    async def signal(self, live: LiveSession) -> None:
        await self.manager.staff.touch(live.id)

    async def resolved(self, live: LiveSession, request_ref: str, *, by: str = "operator", via: str = "terminal") -> bool:
        row = await self.manager.db.fetchone(
            "SELECT id FROM asks WHERE staff_session_id = ? AND request_ref = ? AND resolved_at IS NULL ORDER BY created_at DESC LIMIT 1", (live.id, request_ref)
        )
        ask = await self.manager.asks.get(row["id"]) if row is not None else None
        if ask is None or not await self.manager.asks.resolve(ask.id, by, {"via": via, "in_session": True}):
            return False
        if via == "withdrawn":
            await self.team._withdrawn(ask)
        else:
            await self.team._announce_resolved(ask, allow=None, by=by, via=via)
        return True

    async def located(self, live: LiveSession, *, cli_session_id: str | None = None, transcript_ref: str | None = None) -> None:
        await self.manager.staff.started(live.id, cli_session_id=cli_session_id, transcript_ref=transcript_ref)

    async def ended(self, live: LiveSession, reason: str) -> None:
        await self.team._end(live, reason, stop=False)
        # A place under the project's concurrency came free.
        self.team.queue.pump_soon(live.staff.project_id)


async def install(app: Application) -> list[asyncio.Task[None]]:
    from daedalus.extensions.review import Review  # Lazy: review.py imports this module for its names

    team = Team(app)
    team.review = Review(app, team)
    app.extensions["staff"] = team
    handler = team.attach()
    await team.rebuild()
    return [handler, asyncio.create_task(team.loop(), name="staff")]


__all__ = ["AlreadyAnswered", "Assigned", "Ingress", "Team", "install"]
