"""Scheduler tools: create, list and delete scheduled tasks."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


@tool(
    name="ScheduleCreate",
    description=(
        "Create a scheduled task. Give either cron (5-field crontab expression, UTC) for a "
        "recurring task, or run_at (ISO 8601 datetime, UTC) for a one-shot. The prompt is "
        "what the future agent session will be asked to do; files from the current "
        "workspace can be attached and are copied into the task's own persistent workspace. "
        "A recurring task keeps one workspace across runs and receives the previous run's "
        "summary. kind chooses what happens when it fires: 'agent' (default) runs the prompt "
        "as a task in a fresh session; 'message' just delivers the prompt text to the operator "
        "as a reminder, with no model call — use it for plain alarms ('take the pills', "
        "'call X at 15:00'); 'lazy' is a silent note that is shown together with the "
        "operator's next message in this session ('when I next write, remind me to…') and "
        "becomes an agent task if the operator stays away for a day. run_in chooses where an "
        "agent task runs: 'new' (default) starts a fresh task session with its own workspace each "
        "time; 'self' runs it as a turn of THIS session — same context, files, MCP servers and "
        "board claims — which is what a recurring ping to yourself wants. A fired turn may run as long "
        "as the work needs; do not put action caps or 'keep it short' into the prompt."
    ),
)
async def schedule_create(
    context: ToolContext,
    name: str,
    prompt: str,
    cron: str | None = None,
    run_at: str | None = None,
    files: list[str] | None = None,
    model: str | None = None,
    kind: str = "agent",
    run_in: str = "new",
) -> ToolResult:
    services = services_for(context)
    if services.schedule is None:
        return error(context, "scheduling is not available in this session")
    if bool(cron) == bool(run_at):
        return error(context, "give exactly one of cron or run_at")
    try:
        created = await services.schedule(
            "create",
            name=name,
            prompt=prompt,
            cron=cron,
            run_at=run_at,
            files=[str(services.resolve(f)) for f in files or []],
            model=model,
            created_by_session=context.session_id,
            kind=kind,
            run_in=run_in,
        )
    except ValueError as exc:
        return error(context, str(exc))
    where = " in this session" if created.get("run_in") == "self" else ""
    return ok(context, f"scheduled {created['id']} '{name}' ({created.get('kind', kind)}{where}), next at {created.get('next_run_at')}", schedule_id=created["id"])


@tool(name="ScheduleList", description="List scheduled tasks with their next run time.")
async def schedule_list(context: ToolContext) -> ToolResult:
    services = services_for(context)
    if services.schedule is None:
        return error(context, "scheduling is not available in this session")
    items = await services.schedule("list")
    if not items:
        return ok(context, "(no scheduled tasks)")
    lines = [
        f"- {s['id']} [{s.get('kind') or 'agent'}] '{s['name']}' {'cron ' + s['cron'] if s.get('cron') else 'once at ' + str(s.get('run_at'))}"
        f" next={s.get('next_run_at')} enabled={bool(s.get('enabled', 1))}"
        + (f" failures={s['failure_count']}" if s.get("failure_count") else "")
        for s in items
    ]
    return ok(context, "\n".join(lines))


@tool(name="ScheduleDelete", description="Delete a scheduled task by id.")
async def schedule_delete(context: ToolContext, schedule_id: str) -> ToolResult:
    services = services_for(context)
    if services.schedule is None:
        return error(context, "scheduling is not available in this session")
    removed = await services.schedule("delete", schedule_id=schedule_id)
    return ok(context, f"deleted {schedule_id}" if removed else f"no schedule {schedule_id}")


TOOLS = [schedule_create, schedule_list, schedule_delete]

__all__ = ["TOOLS"]
