"""The voice concierge's tools: hand work to an agent, watch the agents, stop one.

Only the voice session ever sees these. Every other session is blocked from them by the host — it
has SpawnAgent and SubAgent, which do the same job with a full workspace behind them; the concierge
has no workspace and no shell, so handing work over is the only thing it can do with a request.
"""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("voice") if manager is not None else None


@tool(
    name="Delegate",
    description=(
        "Hand a task to an agent and get its session id back at once — the agent works while you keep "
        "talking. Write the task as a full hand-over: what to do, where, and what finished looks like; "
        "the agent cannot ask you what you meant. Give session_id instead to add an instruction to an "
        "agent that is already running (a correction, a change of mind, another detail) rather than "
        "starting a second one for the same job. Several things asked at once are several calls, one per "
        "task, so they run in parallel. Say out loud that you are setting it up; do not wait silently."
    ),
)
async def delegate(context: ToolContext, title: str, task: str, session_id: str | None = None) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "delegation is not available here")
    try:
        result = await hook("delegate", title=title, task=task, session_id=session_id)
    except KeyError:
        return error(context, f"no agent of yours has the id {session_id!r}; call Agents to see them")
    except ValueError as exc:
        return error(context, str(exc))
    if result["steered"]:
        return ok(context, f"the instruction reached agent {result['title']!r} ({result['session_id']}), which is already working", session_id=result["session_id"])
    return ok(context, f"agent {result['title']!r} started as session {result['session_id']}; it reports here when it finishes", session_id=result["session_id"])


@tool(
    name="Agents",
    description=(
        "The agents you started, newest first: id, title, status (idle, running, waiting for the "
        "operator, failed), when each last did something, and the beginning of what it last said. Use it "
        "when the operator asks what is going on, and to find the id of an agent you want to steer or stop."
    ),
)
async def agents(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the agent list is not available here")
    rows = await hook("agents")
    if not rows:
        return ok(context, "no agents are running; nothing has been delegated yet")
    lines = [f"- {r['title']} ({r['session_id']}): {r['status']}, last active {r['last_message_at']}" + (f" — {r['answer']}" if r["answer"] else "") for r in rows]
    return ok(context, "\n".join(lines))


@tool(
    name="AgentResult",
    description=(
        "What one agent last answered, in full, and what it is doing now. Use it when the operator asks "
        "for the detail behind a report you summarised. Summarise what comes back; never read it out whole."
    ),
)
async def agent_result(context: ToolContext, session_id: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the agent list is not available here")
    try:
        result = await hook("result", session_id=session_id)
    except KeyError:
        return error(context, f"no session {session_id!r}")
    body = result["answer"] or "(it has not answered yet)"
    return ok(context, f"{result['title']} — {result['status']}\n\n{body}")


@tool(
    name="StopAgent",
    description=(
        "Stop an agent that is running. It finishes the tool call it is in and stops; its session and its "
        "files stay. Use it when the operator says to drop something, not to tidy up after a finished agent."
    ),
)
async def stop_agent(context: ToolContext, session_id: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "stopping agents is not available here")
    stopped = await hook("stop", session_id=session_id)
    return ok(context, "it is stopping" if stopped else "it was not running")


TOOLS = [delegate, agents, agent_result, stop_agent]

__all__ = ["TOOLS"]
