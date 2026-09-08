"""``LoopNext`` / ``LoopStop`` / ``LoopPause`` / ``LoopResume`` / ``LoopStatus`` — a loop agent steers its own loop."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


def fmt_interval(seconds: int | None) -> str:
    if not seconds:
        return "dynamic"
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{suffix}"
    return f"{seconds}s"


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("loops") if manager is not None else None


def _describe(loop: dict) -> str:  # type: ignore[type-arg]
    cadence = f"every {fmt_interval(loop.get('interval_seconds'))}" if loop["mode"] == "interval" else "dynamically paced"
    nxt = f", next wake-up {str(loop['next_run_at'])[:16].replace('T', ' ')} UTC" if loop.get("next_run_at") else ""
    return f"loop {loop['status']} ({cadence}, iterations {loop['run_count']}{' of ' + str(loop['max_runs']) if loop.get('max_runs') else ''}{nxt})"


@tool(
    name="LoopNext",
    description=(
        "For a dynamically paced loop: schedule this loop's next wake-up. Call it before ending an "
        "iteration, with the delay in seconds and one short reason for it — a fast-changing target "
        "deserves a short delay, a quiet one a long one. Without LoopNext (or LoopStop) a dynamic loop ends "
        "when the iteration ends. An interval loop does not need it."
    ),
)
async def loop_next(context: ToolContext, delay_seconds: float, reason: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "loops are not available")
    try:
        result = await hook("next", session_id=context.session_id, delay_seconds=delay_seconds, reason=reason)
    except ValueError as exc:
        return error(context, str(exc))
    note = f" (clamped to {result['delay_seconds']} s)" if result.get("clamped") else ""
    return ok(context, f"next wake-up in {result['delay_seconds']} s{note}: {reason.strip()}", next_run_at=result.get("next_run_at"))


@tool(name="LoopStop", description="End this session's loop: its purpose is achieved, it became obsolete, or the operator asked. Give a short reason. The session stays; only the wake-ups stop.")
async def loop_stop(context: ToolContext, reason: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "loops are not available")
    try:
        result = await hook("stop", session_id=context.session_id, reason=reason)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, f"loop stopped: {result.get('stop_reason')}")


@tool(name="LoopPause", description="Park this session's loop until the operator resumes it, because only the operator can unblock it. Say plainly what is needed; it goes to the inbox.")
async def loop_pause(context: ToolContext, what_is_needed: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "loops are not available")
    try:
        await hook("pause", session_id=context.session_id, note=what_is_needed)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, "loop paused; the operator has been told what is needed. LoopResume starts it again.")


@tool(name="LoopResume", description="Resume this session's paused or stopped loop; the next iteration runs at once.")
async def loop_resume(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "loops are not available")
    try:
        result = await hook("resume", session_id=context.session_id)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, _describe(result))


@tool(name="LoopStatus", description="This session's loop: mode, status, iterations, next wake-up and the instruction.")
async def loop_status(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "loops are not available")
    loop = await hook("status", session_id=context.session_id)
    if loop is None:
        return ok(context, "this session has no loop")
    return ok(context, _describe(loop) + "\ninstruction: " + " ".join(loop["instruction"].split()))


TOOLS = [loop_next, loop_stop, loop_pause, loop_resume, loop_status]

__all__ = ["TOOLS"]
