"""``StaySilent`` — the structured way for an unattended run to end without a message."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


@tool(
    name="StaySilent",
    description=(
        "For unattended runs (heartbeat checks, scheduled monitors): declare that nothing needs "
        "the operator's attention. The run's final reply is then not sent to the chat; only a "
        "short note goes to the inbox. Call it instead of writing 'nothing new' — silence is the "
        "correct outcome of a routine check. Give a one-line note of what was checked."
    ),
)
async def stay_silent(context: ToolContext, note: str = "") -> ToolResult:
    services = services_for(context)
    manager = services.extra.get("manager")
    state = manager._states.get(context.session_id) if manager is not None else None
    metadata = state.session.metadata if state is not None else {}
    origin = (state.run_origin if state is not None else "operator").split(":")[0]
    if not (metadata.get("unattended") or metadata.get("heartbeat") or origin in ("schedule", "reminder", "heartbeat", "loop")):
        return error(context, "StaySilent is only for unattended runs (heartbeat, scheduled tasks); the operator is waiting for a reply here")
    services.extra["silent_run"] = context.run_id
    services.extra["silent_note"] = note.strip()[:500]
    return ok(context, "Noted. Finish with a brief final reply for the record; it will not be sent to the chat.")


TOOLS = [stay_silent]

__all__ = ["TOOLS"]
