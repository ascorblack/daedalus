"""A project orchestrator's own tools: the brief, the folders, the journal, the team, the board, a
read-only look into the files, and its two ways of speaking to the operator.

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


class AskOperator(Tool):
    """Written out rather than decorated: its third argument is called ``context``, which the decorator keeps
    for the tool context."""

    @property
    def name(self) -> str:
        return "AskOperator"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Put a decision only the operator can make to them, with the options you see. It returns at once with the "
                "request's short id; do not wait for the answer — it arrives as an event in a later wake-up. Link it to a "
                "task with task_id when it is about one."
            ),
            parameters=ToolParameterSchema(
                properties={
                    "question": {"type": "string", "description": "The decision, in one or two sentences."},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "The choices, if there are some."},
                    "context": {"type": "string", "description": "What the operator needs to decide without reading the whole conversation."},
                    "task_id": {"type": "string", "description": "The task it is about, if any."},
                    "urgent": {"type": "boolean", "description": "Whether work is blocked until it is answered."},
                },
                required=["question"],
            ),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        options = arguments.get("options") or []
        if not isinstance(options, list):
            return error(context, "options is a list of strings")
        return await _call(
            context,
            "ask_operator",
            question=str(arguments.get("question") or ""),
            options=[str(o) for o in options],
            context=str(arguments.get("context") or ""),
            task_id=str(arguments["task_id"]) if arguments.get("task_id") else None,
            urgent=bool(arguments.get("urgent")),
        )


@tool(
    name="ProjectReport",
    description=(
        "Tell the operator something that matters — a task done, a decision, a blocker — as a notification on their "
        "phone and an entry in the journal. kind: progress, done, blocked or decision. Not a running commentary: "
        "routine progress goes in the journal."
    ),
)
async def project_report(context: ToolContext, text: str, title: str = "", kind: str = "progress", task_id: str | None = None) -> ToolResult:
    return await _call(context, "project_report", text=text, title=title, kind=kind, task_id=task_id)


TOOLS = [brief, folders, journal, team, tasks, peek, AskOperator, project_report]

__all__ = ["TOOLS"]
