"""A project orchestrator's own tools: the brief, the folders, the journal, the team, the board, a
read-only look into the files, its two ways of speaking to the operator, and the team tools that hire,
assign, talk to, read, answer and control staff.

They exist for orchestrator sessions alone (``ORCHESTRATOR_ONLY_TOOLS``). Each goes through the
orchestrator extension, which first checks that the calling session still holds its project's
office; a replaced orchestrator is refused with the name of its successor.
"""

from __future__ import annotations

from typing import Any

from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolDefinition, ToolParameterSchema, ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for

WATCH_EVENTS = (
    "staff_finished", "staff_question", "staff_permission", "staff_crashed", "staff_silent",
    "task_moved", "terminal_output", "git_commit", "pr", "ci", "webhook",
)
"""The kinds of event a watch waits for, as the extension knows them (a tool may not import it)."""


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("orchestrator") if manager is not None else None


async def _call(tool_context: ToolContext, operation: str, /, **kwargs: Any) -> ToolResult:
    """Positional-only, because the tools' own arguments include ``op`` and ``context``."""
    hook = _hook(tool_context)
    if hook is None:
        return error(tool_context, "orchestrators are not available on this installation")
    try:
        text = await hook(operation, session_id=tool_context.session_id, **kwargs)
    except (KeyError, ValueError, RuntimeError, PermissionError) as exc:
        return error(tool_context, str(exc))
    return ok(tool_context, str(text))


@tool(
    name="Brief",
    description=(
        "Read or write the project's brief. No arguments: the whole brief. section alone: that section. section and "
        "body: write it (append=true adds to it). Sections: goals, constraints, preferences, done_when, "
        "allowed_without_operator, notes. allowed_without_operator is the operator's alone — you cannot write it; "
        "propose a change with AskOperator."
    ),
)
async def brief(context: ToolContext, section: str | None = None, body: str | None = None, append: bool = False) -> ToolResult:
    return await _call(context, "brief", section=section, body=body, append=append)


@tool(
    name="Folders",
    description=(
        "The project's folders. op='list' (default) shows them with their ids. op='add' with path (label, env, "
        "readonly optional) adds one; a host folder in a container installation goes to the operator for "
        "confirmation and the answer arrives as an event. op='update' with folder (id or label): a new label, or "
        "readonly=true to lock it (only the operator unlocks). op='remove' with folder: refused while anyone works "
        "in it; files are never deleted."
    ),
)
async def folders(
    context: ToolContext,
    op: str = "list",
    path: str | None = None,
    folder: str | None = None,
    label: str | None = None,
    env: str | None = None,
    readonly: bool | None = None,
) -> ToolResult:
    return await _call(context, "folders", op=op, path=path, folder=folder, label=label, env=env, readonly=readonly)


@tool(
    name="Journal",
    description=(
        "The project's journal, which survives compaction and restarts. op='write' (default): text, why (the reason), "
        "kind (decision, plan, answer, reassignment, note …). Record every decision the operator would want to find "
        "later. op='read': newest first, limit entries, before=<id> for older ones."
    ),
)
async def journal(context: ToolContext, op: str = "write", text: str = "", why: str = "", kind: str = "decision", before: int | None = None, limit: int = 20) -> ToolResult:
    return await _call(context, "journal", op=op, text=text, why=why, kind=kind, before=before, limit=limit)


@tool(
    name="Team",
    description=(
        "The team. No arguments: every member with status, task, what they wait for and last signal. staff (a name or "
        "id): one member in detail — sessions, branch, queued assignments, recent messages. concurrency: how many "
        "staff may work at once, within the project's cap."
    ),
)
async def team(context: ToolContext, staff: str | None = None, concurrency: int | None = None) -> ToolResult:
    return await _call(context, "team", staff=staff, concurrency=concurrency)


@tool(
    name="Tasks",
    description=(
        "The project's board. op='list' (open tasks; status narrows it), 'get' (task_id: brief, branch, notes), "
        "'create' (title and the four brief parts: objective, deliverable, boundaries, done_when; priority 1-5, "
        "depends_on, assignee to hand it over at once), 'update' (task_id and what changes, assignee='' unassigns), "
        "'move' (task_id, status: todo|doing|review|blocked|dropped). A task with an unmerged branch reaches done only "
        "through the operator's review."
    ),
)
async def tasks(
    context: ToolContext,
    op: str = "list",
    task_id: str | None = None,
    title: str | None = None,
    objective: str | None = None,
    deliverable: str | None = None,
    boundaries: str | None = None,
    done_when: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    depends_on: list[str] | None = None,
    assignee: str | None = None,
    note: str = "",
) -> ToolResult:
    return await _call(
        context, "tasks", op=op, task_id=task_id, title=title, objective=objective, deliverable=deliverable, boundaries=boundaries,
        done_when=done_when, status=status, priority=priority, depends_on=depends_on, assignee=assignee, note=note,
    )


@tool(
    name="Peek",
    description=(
        "Look into the project's files, read-only, when you must check something yourself. op: 'read' (path, offset, "
        "limit lines), 'ls' (path), 'find' (glob pattern), 'search' (regex pattern), 'git_log' (ref, path, limit), "
        "'git_diff' (ref or range such as main..agent/ira/t1, path), 'git_status'. folder: id or label (default the "
        "primary folder); paths are relative to it. Output is bounded; narrow the path to see more."
    ),
)
async def peek(context: ToolContext, op: str, path: str = "", folder: str | None = None, pattern: str = "", ref: str = "", offset: int = 1, limit: int = 200) -> ToolResult:
    return await _call(context, "peek", op=op, path=path, folder=folder, pattern=pattern, ref=ref, offset=offset, limit=limit)


QUESTION_PROPERTIES: dict[str, Any] = {
    "title": {"type": "string", "description": "A few words naming the decision, shown as the question's name in the operator's list."},
    "text": {"type": "string", "description": "The decision in one or two sentences, with what hangs on it. Markdown."},
    "options": {"type": "array", "items": {"type": "string"}, "description": "The choices, if there are some."},
    "multi": {"type": "boolean", "description": "Several options may be chosen together."},
    "context": {"type": "string", "description": "What the operator needs to decide without reading the whole conversation."},
    "task_id": {"type": "string", "description": "The task it is about, if any."},
    "urgent": {"type": "boolean", "description": "Whether work is blocked until it is answered."},
    "dispatch_id": {"type": "string", "description": "The main orchestrator's dispatch this question belongs to, if any."},
}


class AskOperator(Tool):
    """Written out rather than decorated: its questions carry an argument called ``context``, which the
    decorator keeps for the tool context."""

    @property
    def name(self) -> str:
        return "AskOperator"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Put decisions only the operator can make to them. Ask everything you need at once: questions=[{title, "
                "text, options?, multi?, urgent?, dispatch_id?, context?}, …] (up to 12), or one question with title and "
                "text. The operator sees them as a list, answers any of them and sends the answers together. It returns "
                "at once with each question's short id; do not wait — the answers arrive as events in a later wake-up, "
                "in one wake-up when they were sent together. WithdrawQuestions takes back those that no longer matter."
            ),
            parameters=ToolParameterSchema(
                properties={
                    "questions": {
                        "type": "array",
                        "description": "Several questions at once, each with its own title and text.",
                        "items": {"type": "object", "properties": QUESTION_PROPERTIES, "required": ["title", "text"]},
                    },
                    **QUESTION_PROPERTIES,
                },
                required=[],
            ),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        questions = arguments.get("questions")
        if questions is not None and not isinstance(questions, list):
            return error(context, "questions is a list of objects, each with a title and a text")
        options = arguments.get("options") or []
        if not isinstance(options, list):
            return error(context, "options is a list of strings")
        return await _call(
            context,
            "ask_operator",
            questions=questions or None,
            title=str(arguments.get("title") or ""),
            text=str(arguments.get("text") or ""),
            options=[str(o) for o in options],
            multi=bool(arguments.get("multi")),
            context=str(arguments.get("context") or ""),
            task_id=str(arguments["task_id"]) if arguments.get("task_id") else None,
            urgent=bool(arguments.get("urgent")),
            dispatch_id=str(arguments["dispatch_id"]) if arguments.get("dispatch_id") else None,
        )


@tool(
    name="WithdrawQuestions",
    description=(
        "Take back questions of yours that still wait for the operator, one or many, when the answer no longer "
        "matters — the work changed, you found the answer yourself, a newer question replaces it. ids: their short "
        "ids. reason: a few words the operator sees where each question was. A question already answered is reported "
        "with its answer instead."
    ),
)
async def withdraw_questions(context: ToolContext, ids: list[str], reason: str) -> ToolResult:
    return await _call(context, "withdraw_questions", ids=ids, reason=reason)


@tool(
    name="ProjectReport",
    description=(
        "Tell the operator something that matters — a task done, a decision, a blocker — as a notification on their "
        "phone and an entry in the journal. kind: progress, done, blocked or decision. Not a running commentary: "
        "routine progress goes in the journal. dispatch_id: the main orchestrator's dispatch it answers, if any."
    ),
)
async def project_report(context: ToolContext, text: str, title: str = "", kind: str = "progress", task_id: str | None = None, dispatch_id: str | None = None) -> ToolResult:
    return await _call(context, "project_report", text=text, title=title, kind=kind, task_id=task_id, dispatch_id=dispatch_id)


@tool(
    name="Hire",
    description=(
        "Add a member to the team. name (unique, it names their branch), role (their lasting area of work), harness "
        "(daedalus, or a command-line agent: claude, codex, grok, opencode, pi — Harnesses shows what is installed and "
        "what each offers), agent (a persona for daedalus, the CLI's agent otherwise), model (a preset for daedalus, "
        "the CLI's model otherwise), effort, permission_mode (CLI only), env (container or host), folder (their "
        "default folder), isolation (worktree: their own branch, the default in a git folder; shared; readonly), "
        "instructions (standing guidance), one_off=true for a helper dismissed when their task is done. Hire for a "
        "lasting need; a team of a few well-briefed members beats a crowd."
    ),
)
async def hire(
    context: ToolContext,
    name: str,
    role: str,
    harness: str = "daedalus",
    agent: str = "",
    model: str = "",
    effort: str = "",
    permission_mode: str = "",
    env: str = "",
    folder: str | None = None,
    isolation: str | None = None,
    instructions: str = "",
    one_off: bool = False,
) -> ToolResult:
    return await _call(
        context, "hire", name=name, role=role, harness=harness, agent=agent, model=model, effort=effort, permission_mode=permission_mode,
        env=env, folder=folder, isolation=isolation, instructions=instructions, one_off=one_off,
    )


@tool(
    name="StaffEdit",
    description=(
        "Change a member (staff: name or id): role, agent, model, effort, permission_mode, env, folder, isolation, "
        "instructions, notes (what they carry between sessions; this replaces it). Name and harness cannot change — "
        "hire someone else for that. It takes effect from their next session."
    ),
)
async def staff_edit(
    context: ToolContext,
    staff: str,
    role: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    env: str | None = None,
    folder: str | None = None,
    isolation: str | None = None,
    instructions: str | None = None,
    notes: str | None = None,
) -> ToolResult:
    return await _call(
        context, "staff_edit", staff=staff, role=role, agent=agent, model=model, effort=effort, permission_mode=permission_mode,
        env=env, folder=folder, isolation=isolation, instructions=instructions, notes=notes,
    )


@tool(
    name="Dismiss",
    description=(
        "Take a member off the team. Refused while they have a live session unless release=true, which ends it first "
        "(their unfinished task goes back to todo). keep_worktree=false removes a clean worktree; an unmerged branch "
        "is always kept."
    ),
)
async def dismiss(context: ToolContext, staff: str, release: bool = False, keep_worktree: bool = True) -> ToolResult:
    return await _call(context, "dismiss", staff=staff, release=release, keep_worktree=keep_worktree)


@tool(
    name="Assign",
    description=(
        "Hand a member a task: task_id of a task on the board, or title plus the brief for a new one. The brief has "
        "four parts, each a real sentence, on the task or given here: objective (what and why), deliverable (what "
        "exists when done), boundaries (where to work, what not to touch), done_when (a check anyone can run). "
        "folder, priority (1 first … 5) and depends_on are optional. It starts now or waits in the project's queue; "
        "the answer says which and why."
    ),
)
async def assign(
    context: ToolContext,
    staff: str,
    task_id: str | None = None,
    title: str | None = None,
    objective: str | None = None,
    deliverable: str | None = None,
    boundaries: str | None = None,
    done_when: str | None = None,
    folder: str | None = None,
    priority: int | None = None,
    depends_on: list[str] | None = None,
) -> ToolResult:
    return await _call(
        context, "assign", staff=staff, task_id=task_id, title=title, objective=objective, deliverable=deliverable, boundaries=boundaries,
        done_when=done_when, folder=folder, priority=priority, depends_on=depends_on,
    )


@tool(
    name="Tell",
    description=(
        "Say something to a member's live session. mode: queue (default; read when the current turn ends), steer "
        "(joins the turn now), interrupt (stops the turn and sends it). Returns the delivery receipt: queued, written, "
        "submitted, acknowledged or failed."
    ),
)
async def tell(context: ToolContext, staff: str, text: str, mode: str = "queue") -> ToolResult:
    return await _call(context, "tell", staff=staff, text=text, mode=mode)


@tool(
    name="ReadStaff",
    description=(
        "Read what a member did, in a bounded page. what: last (their last reply, the default), turns (the last "
        "turns, one line per tool call), screen (a command-line agent's terminal), diff (their changes against the "
        "base). cursor from an earlier read shows only what came after it; max_chars widens the page up to a limit. "
        "Reading a finished turn marks it seen."
    ),
)
async def read_staff(context: ToolContext, staff: str, what: str = "last", turns: int = 1, cursor: str | None = None, max_chars: int | None = None) -> ToolResult:
    return await _call(context, "read_staff", staff=staff, what=what, turns=turns, cursor=cursor, max_chars=max_chars)


@tool(
    name="Answer",
    description=(
        "Answer a staff request by its id ([q…] in the events and the state block). A question: text, or selected "
        "options. A permission: allow=true or false; a grant needs basis — under normal autonomy the exact line of "
        "the brief's 'allowed without the operator' that covers it, under full your reason. Denying is always "
        "allowed. escalate=true hands it to the operator instead (text becomes your suggestion, basis the reason)."
    ),
)
async def answer(
    context: ToolContext,
    request_id: str,
    allow: bool | None = None,
    text: str | None = None,
    selected: list[str] | None = None,
    basis: str = "",
    escalate: bool = False,
) -> ToolResult:
    return await _call(context, "answer", request_id=request_id, allow=allow, text=text, selected=selected, basis=basis, escalate=escalate)


@tool(name="Interrupt", description="Stop a member's current turn (Esc for a command-line agent). The session stays; Tell says what next.")
async def interrupt(context: ToolContext, staff: str) -> ToolResult:
    return await _call(context, "interrupt", staff=staff)


@tool(
    name="Pause",
    description="Let a member finish the current turn, commit their work in progress on their branch, and start nothing new until Tell or Assign.",
)
async def pause(context: ToolContext, staff: str) -> ToolResult:
    return await _call(context, "pause", staff=staff)


@tool(
    name="Release",
    description=(
        "End a member's live session. Their unfinished task goes back to todo, unassigned; their branch stays. "
        "keep_worktree=false also removes a clean worktree. Look (ReadStaff) before releasing someone who went silent."
    ),
)
async def release(context: ToolContext, staff: str, keep_worktree: bool = True) -> ToolResult:
    return await _call(context, "release", staff=staff, keep_worktree=keep_worktree)


@tool(
    name="Harnesses",
    description=(
        "The executors staff can run on. No arguments: each one per environment — installed, version, signed in, "
        "whether it can run staff here. harness: what it offers (models, agents, permission modes, efforts); folder "
        "adds the agents that folder defines; env narrows to container or host. Check here before Hire."
    ),
)
async def harnesses(context: ToolContext, harness: str | None = None, env: str | None = None, folder: str | None = None) -> ToolResult:
    return await _call(context, "harnesses", harness=harness, env=env, folder=folder)


@tool(
    name="WakeMe",
    description=(
        "Set an alarm for yourself: when it fires you are woken with the note, even in the middle of a turn. Give "
        "exactly one of in_minutes (at least 1), at (ISO 8601; without an offset it is the operator's time) or cron "
        "(minute hour day month weekday, in UTC; at most every 10 minutes by default). The note says what to look at "
        "— write it for yourself without this conversation. The scheduler looks every 30 seconds, so a wake-up can "
        "come up to half a minute late. Unwatch(id) cancels it; the state block lists yours."
    ),
)
async def wake_me(context: ToolContext, note: str, at: str | None = None, in_minutes: int | None = None, cron: str | None = None) -> ToolResult:
    return await _call(context, "wake_me", note=note, at=at, in_minutes=in_minutes, cron=cron)


class Watch(Tool):
    """Written out rather than decorated: ``when`` and ``then`` are objects whose shape the model has to
    be shown, and the decorator describes a dict as nothing more than an object."""

    @property
    def name(self) -> str:
        return "Watch"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "When something happens in the project, do something — without polling. when.event is one of: "
                "staff_finished, staff_question, staff_permission, staff_crashed (each with an optional staff); "
                "staff_silent (staff, minutes: silent that long while working); task_moved (task, to — both optional); "
                "terminal_output (terminal id or title, or a command-line staff member; regex, at most 200 characters, "
                "no lookarounds); git_commit (folder, branch — optional: a new commit on a branch of that folder); pr "
                "(provider, repo, conclusion such as opened or merged); ci (provider, repo, conclusion such as failure); "
                "webhook (provider, regex over the payload). then.action is wake (you are woken with the note), tell "
                "(staff, text, mode) or notify (title, text, level: quiet, normal or urgent). A watch fires at most once "
                "per cooldown, once=true removes it after the first fire, and one that fires twelve times in an hour "
                "switches itself off. Nothing you do yourself fires a watch. Unwatch(id) removes it."
            ),
            parameters=ToolParameterSchema(
                properties={
                    "when": {
                        "type": "object",
                        "description": "What to wait for, e.g. {\"event\": \"staff_finished\", \"staff\": \"Max\"} or {\"event\": \"ci\", \"provider\": \"github\", \"conclusion\": \"failure\"}.",
                        "properties": {
                            "event": {"type": "string", "enum": list(WATCH_EVENTS)},
                            "staff": {"type": "string"}, "minutes": {"type": "integer"}, "task": {"type": "string"}, "to": {"type": "string"},
                            "terminal": {"type": "string"}, "regex": {"type": "string"}, "folder": {"type": "string"}, "branch": {"type": "string"},
                            "provider": {"type": "string"}, "repo": {"type": "string"}, "conclusion": {"type": "string"},
                        },
                        "required": ["event"],
                    },
                    "then": {
                        "type": "object",
                        "description": "What to do, e.g. {\"action\": \"wake\"} or {\"action\": \"tell\", \"staff\": \"Max\", \"text\": \"…\"}.",
                        "properties": {
                            "action": {"type": "string", "enum": ["wake", "tell", "notify"]},
                            "note": {"type": "string"}, "staff": {"type": "string"}, "text": {"type": "string"},
                            "mode": {"type": "string", "enum": ["queue", "steer", "interrupt"]}, "title": {"type": "string"},
                            "level": {"type": "string", "enum": ["quiet", "normal", "urgent"]},
                        },
                        "required": ["action"],
                    },
                    "cooldown_minutes": {"type": "number", "description": "Least time between two fires; at least 1, default 10."},
                    "once": {"type": "boolean", "description": "Remove the watch after it fires once."},
                    "note": {"type": "string", "description": "Why you set it, for you and the operator."},
                },
                required=["when", "then"],
            ),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        return await _call(
            context,
            "watch",
            when=arguments.get("when"),
            then=arguments.get("then"),
            cooldown_minutes=arguments.get("cooldown_minutes", 10),
            once=bool(arguments.get("once")),
            note=str(arguments.get("note") or ""),
        )


@tool(
    name="Unwatch",
    description="Cancel a wake-up or remove a watch, by the id the state block or WakeMe/Watch gave you.",
)
async def unwatch(context: ToolContext, id: str) -> ToolResult:
    return await _call(context, "unwatch", id=id)


TOOLS = [
    brief, folders, journal, team, tasks, peek, AskOperator, withdraw_questions, project_report,
    hire, staff_edit, dismiss, assign, tell, read_staff, answer, interrupt, pause, release, harnesses,
    wake_me, Watch, unwatch,
]

__all__ = ["TOOLS"]
