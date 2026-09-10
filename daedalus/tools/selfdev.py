"""Self-development tools: worktree, propose a change, rebuild, roll back."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


@tool(
    name="SelfWorkspace",
    description=(
        "Create a git worktree for editing one of your own repositories ('bot' = this "
        "agent's host code, 'core' = the agent core library). Returns the directory to "
        "work in, on a fresh branch agent/<branch> based on origin/main. Edit, test and "
        "commit there; never edit the running checkout directly."
    ),
)
async def self_workspace(context: ToolContext, repo: str, branch: str) -> ToolResult:
    services = services_for(context)
    hook = services.extra.get("manager").service_hooks.get("self_workspace") if services.extra.get("manager") else None
    if hook is None:
        return error(context, "self-development is not available in this session")
    if repo not in ("bot", "core"):
        return error(context, "repo must be 'bot' or 'core'")
    try:
        return ok(context, await hook(repo=repo, branch=branch))
    except Exception as exc:  # noqa: BLE001
        return error(context, f"could not create the worktree: {exc}")


@tool(
    name="SelfPropose",
    description=(
        "Open a pull request from a worktree branch of one of your repositories ('bot' or "
        "'core'). Commit your work first and run the tests with Verify so the receipts are on "
        "the card. The owner reviews the change in chat; on approval it is merged and, if "
        "configured, the agent rebuilds itself. branch defaults to the most recently used "
        "worktree of that repo. The summary describes the change and what you checked — no "
        "session ids, no operator details, nothing about the machine it runs on. execution_path names "
        "the code that runs the change ('pkg.module' or 'pkg.module:symbol' — a tool, a hook, an "
        "extension's install, a startup step); a host-module change without one is refused, as is one "
        "with no passing Verify receipt that exercises the changed code, or a large change whose summary "
        "does not say what it replaces."
    ),
)
async def self_propose(
    context: ToolContext, repo: str, title: str, summary: str, branch: str | None = None, execution_path: str | None = None
) -> ToolResult:
    services = services_for(context)
    if services.self_propose is None:
        return error(context, "self-development is not available in this session")
    if repo not in ("bot", "core"):
        return error(context, "repo must be 'bot' or 'core'")
    try:
        result = await services.self_propose(
            repo=repo, title=title, summary=summary, session_id=context.session_id, branch=branch, execution_path=execution_path
        )
    except RuntimeError as exc:
        hint = " The token cannot push: the operator must grant the GitHub token write access to this repository (Contents: read and write)." if "denied" in str(exc) or "403" in str(exc) else ""
        return error(context, f"{exc}{hint}")
    return ok(context, result)


@tool(
    name="SelfRebuild",
    description=(
        "Ask the supervisor to pull the merged main branches, rebuild if the Dockerfile "
        "changed, run preflight checks and restart the agent. Active sessions are "
        "snapshotted and resumed afterwards. Call it after a merge."
    ),
)
async def self_rebuild(context: ToolContext, reason: str) -> ToolResult:
    services = services_for(context)
    if services.self_rebuild is None:
        return error(context, "rebuild is not available in this session")
    return ok(context, await services.self_rebuild(reason=reason, session_id=context.session_id))


@tool(
    name="SelfRollback",
    description="Roll the agent back to a previous known-good revision (0 = the last one).",
)
async def self_rollback(context: ToolContext, steps_back: int = 0, reason: str = "") -> ToolResult:
    services = services_for(context)
    if services.self_rollback is None:
        return error(context, "rollback is not available in this session")
    return ok(
        context,
        await services.self_rollback(steps_back=steps_back, reason=reason, session_id=context.session_id),
    )


TOOLS = [self_workspace, self_propose, self_rebuild, self_rollback]

__all__ = ["TOOLS"]
