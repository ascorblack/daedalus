"""Board tools: the plan as durable, queryable state instead of a paragraph in the context."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("board") if manager is not None else None


@tool(
    name="BoardAdd",
    description=(
        "Add a task to the shared board. Use it when work has more than a few steps, spans sessions, "
        "or must survive compaction and restarts: title, acceptance criteria (how anyone can tell it "
        "is done), an optional checklist, dependencies (task ids that must finish first — the task "
        "stays 'blocked' until they do), priority 1 (highest) to 5. Returns the task id."
    ),
)
async def board_add(
    context: ToolContext, title: str, acceptance: str = "", checklist: list[str] | None = None, depends_on: list[str] | None = None, priority: int = 3, notes: str = ""
) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the board is not available")
    try:
        task = await hook("add", title=title, acceptance=acceptance, checklist=checklist, depends_on=depends_on, priority=priority, session_id=context.session_id, notes=notes)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, f"task {task['id']} added ({task['status']}): {task['title']}", task_id=task["id"])


@tool(
    name="BoardUpdate",
    description=(
        "Move or annotate a board task: status todo|doing|review|done|blocked|dropped, a progress note, "
        "checklist items to check/uncheck (0-based indexes). Taking a task to 'doing' claims it for this "
        "session (respecting the work-in-progress limit); 'done' requires a complete checklist and "
        "unblocks dependents."
    ),
)
async def board_update(
    context: ToolContext, task_id: str, status: str | None = None, note: str = "", check: list[int] | None = None, uncheck: list[int] | None = None, priority: int | None = None
) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the board is not available")
    try:
        task = await hook("update", task_id=task_id, status=status, note=note, check=check, uncheck=uncheck, priority=priority, session_id=context.session_id if status == "doing" else None, run_id=context.run_id if status == "doing" else None)
    except KeyError:
        return error(context, f"no task {task_id}")
    except ValueError as exc:
        return error(context, str(exc))
    done = sum(1 for c in task["checklist"] if c["done"])
    return ok(context, f"task {task['id']} is now {task['status']}" + (f", checklist {done}/{len(task['checklist'])}" if task["checklist"] else ""))


@tool(name="BoardList", description="Show the board: open tasks by status and priority (include_done=true adds finished ones). A task's acceptance, notes and checklist come with BoardGet.")
async def board_list(context: ToolContext, status: str | None = None, include_done: bool = False) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the board is not available")
    return ok(context, await hook("render", status=status, include_done=include_done))


@tool(name="BoardGet", description="Everything about one board task: acceptance criteria, checklist, dependencies, notes, who works on it.")
async def board_get(context: ToolContext, task_id: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the board is not available")
    try:
        t = await hook("get", task_id=task_id)
    except KeyError:
        return error(context, f"no task {task_id}")
    checklist = "\n".join(f"  [{'x' if c['done'] else ' '}] {i}. {c['text']}" for i, c in enumerate(t["checklist"])) or "  (none)"
    return ok(
        context,
        f"{t['id']} · {t['status']} · p{t['priority']} · {t['title']}\nacceptance: {t['acceptance'] or '(none)'}\nchecklist:\n{checklist}\n"
        f"depends on: {', '.join(t['depends_on']) or 'nothing'}\nsession: {t['session_id'] or '-'}\nnotes:\n{t['notes'] or '(none)'}",
    )


TOOLS = [board_add, board_update, board_list, board_get]

__all__ = ["TOOLS"]
