"""Self-development tools: propose a change, rebuild, roll back."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


@tool(
    name="self_propose",
    description=(
        "Open a pull request from the current branch of one of your own repositories "
        "('bot' = this agent's code, 'core' = the agent core). Commit your work first. "
        "The owner reviews the change in chat; on approval it is merged and, if "
        "configured, the agent rebuilds itself."
    ),
)
async def self_propose(context: ToolContext, repo: str, title: str, summary: str) -> ToolResult:
    services = services_for(context)
    if services.self_propose is None:
        return error(context, "self-development is not available in this session")
    if repo not in ("bot", "core"):
        return error(context, "repo must be 'bot' or 'core'")
    result = await services.self_propose(repo=repo, title=title, summary=summary, session_id=context.session_id)
    return ok(context, result)


@tool(
    name="self_rebuild",
    description=(
        "Ask the supervisor to pull the merged main branches, rebuild if dependencies or "
        "the Dockerfile changed, run preflight checks and restart the agent. Active "
        "sessions are snapshotted and resumed afterwards. Call it after a merge."
    ),
)
async def self_rebuild(context: ToolContext, reason: str) -> ToolResult:
    services = services_for(context)
    if services.self_rebuild is None:
        return error(context, "rebuild is not available in this session")
    return ok(context, await services.self_rebuild(reason=reason, session_id=context.session_id))


@tool(
    name="self_rollback",
    description="Roll the agent back to a previous known-good revision (0 = the last one).",
)
async def self_rollback(context: ToolContext, steps_back: int = 0, reason: str = "") -> ToolResult:
    services = services_for(context)
    if services.self_rollback is None:
        return error(context, "rollback is not available in this session")
    return ok(context, await services.self_rollback(steps_back=steps_back, reason=reason, session_id=context.session_id))


TOOLS = [self_propose, self_rebuild, self_rollback]

__all__ = ["TOOLS"]
