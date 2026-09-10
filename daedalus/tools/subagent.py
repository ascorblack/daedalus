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
        "as a complete hand-over: the subagent has none of your context. A subagent is removed once it "
        "has reported (its files stay in the workspace); pass keep=true when you will talk to it again "
        "with SubAgentSend — it then keeps its context until you delete it. Make the contract explicit: "
        "`expects` says what the report must contain (the subagent is told, and the report is marked when "
        "it does not), `deliverable` names a workspace-relative file that must exist when it reports "
        "(checked by the host, not by the subagent's word)."
    ),
)
async def sub_agent(
    context: ToolContext,
    task: str,
    model: str | None = None,
    name: str | None = None,
    wait: bool = False,
    timeout_minutes: int | None = None,
    keep: bool = False,
    expects: str | None = None,
    deliverable: str | None = None,
) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "subagents are not available")
    try:
        result = await hook("spawn", leader_id=context.session_id, task=task, model=model, name=name, wait=wait, timeout_minutes=timeout_minutes, keep=keep, expects=expects, deliverable=deliverable)
    except (ValueError, RuntimeError) as exc:
        return error(context, str(exc))
    return _report(context, result, started=True)


def _report(context: ToolContext, result: dict, *, started: bool) -> ToolResult:  # type: ignore[type-arg]
    fate = f"it stays for follow-ups (SubAgentSend {result['name']!r})" if result.get("kept") else "it has been removed; its files are in the workspace"
    if result.get("answer"):
        limit = services_for(context).max_tool_output_chars
        return ok(context, f"subagent {result['name']!r} reported ({fate}):\n\n" + clip(result["answer"], limit), session_id=result["session_id"])
    if result.get("note"):
        return ok(context, result["note"], session_id=result["session_id"])
    if result.get("delivered") == "steer":
        return ok(context, f"delivered to subagent {result['name']!r} while it works; it will act on it before its next step", session_id=result["session_id"])
    what = f"subagent {result['name']!r} started on {result.get('model', 'its model')}" if started else f"subagent {result['name']!r} took the message and started a run"
    return ok(context, f"{what} (session {result['session_id']}); its report arrives as a message when it finishes", session_id=result["session_id"], run_id=result.get("run_id"))


@tool(
    name="SubAgentSend",
    description=(
        "Send a message to one of your subagents by name. While it is still working the message is a steer "
        "it sees before its next step; once it has finished (and was started with keep=true, so it still "
        "exists with its context) the message starts a new run for it, whose report comes back like the "
        "first one — as a message from subagent:<name>, or inline with wait=true."
    ),
)
async def sub_agent_send(context: ToolContext, name: str, text: str, wait: bool = False, timeout_minutes: int | None = None) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "subagents are not available")
    try:
        result = await hook("send", leader_id=context.session_id, name=name, text=text, wait=wait, timeout_minutes=timeout_minutes)
    except (ValueError, RuntimeError) as exc:
        return error(context, str(exc))
    return _report(context, result, started=False)


@tool(name="SubAgentList", description="List this session's subagents and whether they are still running, plus the model presets SubAgent accepts.")
async def sub_agent_list(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "subagents are not available")
    children = await hook("children", leader_id=context.session_id)
    models = await hook("models")
    lines = [f"- {c['name']} → session {c['session_id']} ({'running' if c['running'] else 'finished'}{', kept' if c.get('kept') else ''})" for c in children] or ["(no subagents yet)"]
    return ok(context, "\n".join(lines) + "\n\nmodels: " + ", ".join(models))


TOOLS = [sub_agent, sub_agent_list, sub_agent_send]

__all__ = ["TOOLS"]
