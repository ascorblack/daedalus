"""``LearningReport`` — what the agent's own runs looked like lately."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


@tool(
    name="LearningReport",
    description=(
        "Summarise this installation's recent runs: outcomes, most used tools, recurring tool "
        "failures, asks that keep coming back, spend, and improvement candidates. Use it before "
        "proposing a change to yourself, so the proposal targets something that actually recurs."
    ),
)
async def learning_report(context: ToolContext, days: int = 7) -> ToolResult:
    manager = services_for(context).extra.get("manager")
    hook = manager.service_hooks.get("learning") if manager is not None else None
    if hook is None:
        return error(context, "learning records are not available")
    return ok(context, await hook("report", days=max(1, min(int(days), 90))))


TOOLS = [learning_report]

__all__ = ["TOOLS"]
