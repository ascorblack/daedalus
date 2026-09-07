"""``SubAgent`` / ``SubAgentList`` — helpers that work in this session's workspace."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import clip, error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("subagents") if manager is not None else None


@tool(
    name="SubAgent",
    description=(
        "Start a subagent: a helper session that does one task in THIS workspace and reports back. "
        "It runs on your model unless `model` names one of the presets listed in your environment "
        "(use another model only when the operator asks for it or the task clearly suits it). "
        "wait=false (default) returns at once; the subagent's report arrives later as a message from "
        "`subagent:<name>` — even after you have finished your turn — so you may end your turn and "
        "continue when it comes. wait=true blocks until the report is in and returns it. Write the task "
        "as a complete hand-over: the subagent has none of your context."
    ),
)
async def sub_agent(
    context: ToolContext,
    task: str,
    model: str | None = None,
    name: str | None = None,
    wait: bool = False,
    timeout_minutes: int | None = None,
) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "subagents are not available")
    try:
        result = await hook("spawn", leader_id=context.session_id, task=task, model=model, name=name, wait=wait, timeout_minutes=timeout_minutes)
    except (ValueError, RuntimeError) as exc:
        return error(context, str(exc))
    if result.get("answer"):
        limit = services_for(context).max_tool_output_chars
        return ok(context, f"subagent {result['name']!r} reported:\n\n" + clip(result["answer"], limit), session_id=result["session_id"])
    note = result.get("note") or f"subagent {result['name']!r} started on {result['model']} (session {result['session_id']}); its report arrives as a message when it finishes"
    return ok(context, note, session_id=result["session_id"], run_id=result["run_id"])


@tool(name="SubAgentList", description="List this session's subagents and whether they are still running, plus the model presets SubAgent accepts.")
async def sub_agent_list(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "subagents are not available")
    children = await hook("children", leader_id=context.session_id)
    models = await hook("models")
    lines = [f"- {c['name']} → session {c['session_id']} ({'running' if c['running'] else 'finished'})" for c in children] or ["(no subagents yet)"]
    return ok(context, "\n".join(lines) + "\n\nmodels: " + ", ".join(models))


TOOLS = [sub_agent, sub_agent_list]

__all__ = ["TOOLS"]
