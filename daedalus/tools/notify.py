"""``Notify`` — the agent tells the operator something outside its chat, within a budget."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("notify") if manager is not None else None


@tool(
    name="Notify",
    description=(
        "Tell the operator something outside this chat: a toast in the app, their phone, the desktop — as their "
        "notification settings decide. Only for what they would want to know now while not watching: a long job "
        "finished or failed, you are blocked on something that is not a question to them, a service they rely on "
        "is down. Not for progress and not for your answer. title: one line (at most 120 characters); body: the "
        "detail (at most 1000); level: quiet (recorded, no sound), normal, or urgent (breaks through quiet hours — "
        "only for what cannot wait until morning); link: empty for this session, an app path under /app/, or an "
        "https:// URL; key: pass the same key to update an earlier notification instead of sending another. "
        "Limited per session; the result says what was delivered, or when the next one is possible."
    ),
)
async def notify(context: ToolContext, title: str, body: str = "", level: str = "normal", link: str = "", key: str = "") -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "notifications are not available here")
    try:
        outcome = await hook(session_id=context.session_id, title=title, body=body, level=level, link=link, key=key)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, outcome)


TOOLS = [notify]

__all__ = ["TOOLS"]
