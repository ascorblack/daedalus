"""Standing intents: "when an inbound event mentions X, do Y" — registered by the agent."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("intents") if manager is not None else None


@tool(
    name="IntentCreate",
    description=(
        "Register a standing intent: when an inbound event (webhook, loopback inbox message) matches "
        "the regular expression `pattern`, run `action` as a task in this session (or a fresh one when "
        "this session is busy). Bounded by a cooldown, a total fire budget and an optional expiry, so a "
        "noisy source cannot keep the agent running. Use it for 'if CI fails on main, investigate' or "
        "'when a PR mentions security, review it'."
    ),
)
async def intent_create(
    context: ToolContext, pattern: str, action: str, cooldown_minutes: int = 60, max_fires: int = 20, expires_in_hours: int | None = None
) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "standing intents are not available")
    try:
        created = await hook("create", pattern=pattern, action=action, session_id=context.session_id, cooldown_minutes=cooldown_minutes, max_fires=max_fires, expires_in_hours=expires_in_hours, created_by=context.session_id)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, f"intent {created['id']} registered: /{pattern}/ → {action[:80]}" + (f", expires {created['expires_at']}" if created.get("expires_at") else ""), intent_id=created["id"])


@tool(name="IntentList", description="List standing intents with their fire counts and state.")
async def intent_list(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "standing intents are not available")
    items = await hook("list")
    if not items:
        return ok(context, "(no standing intents)")
    return ok(context, "\n".join(f"- {i['id']} /{i['pattern']}/ → {i['action'][:80]} · enabled={bool(i['enabled'])} fired={i['fired_count']}/{i['max_fires']} cooldown={i['cooldown_minutes']}m" for i in items))


@tool(name="IntentDelete", description="Remove a standing intent by id.")
async def intent_delete(context: ToolContext, intent_id: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "standing intents are not available")
    return ok(context, f"deleted {intent_id}" if await hook("delete", intent_id=intent_id) else f"no intent {intent_id}")


TOOLS = [intent_create, intent_list, intent_delete]

__all__ = ["TOOLS"]
