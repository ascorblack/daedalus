"""Shared helpers for host tools."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult

from daedalus.host.services import SessionServices, locator


def call_id(context: ToolContext) -> str:
    return str(context.metadata.get("tool_call_id") or "")


def services_for(context: ToolContext) -> SessionServices:
    return locator.get(context.session_id)


def tool_config(context: ToolContext):  # type: ignore[no-untyped-def]
    """The live ``[tools]`` section (read from the running manager, so edits apply at once)."""
    from daedalus.config import ToolsConfig

    manager = services_for(context).extra.get("manager")
    config = getattr(manager, "config", None)
    return getattr(config, "tools", None) or ToolsConfig()


def ok(context: ToolContext, content: str, **metadata: object) -> ToolResult:
    return ToolResult(tool_call_id=call_id(context), content=content, metadata=dict(metadata))


def error(context: ToolContext, content: str, **metadata: object) -> ToolResult:
    return ToolResult(
        tool_call_id=call_id(context), content=content, is_error=True, metadata=dict(metadata)
    )


def clip(text: str, limit: int, *, note: str = "") -> str:
    """Keep the head and the tail of an over-long output."""
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25) :]
    dropped = len(text) - len(head) - len(tail)
    marker = f"\n\n[... {dropped} characters omitted{(' — ' + note) if note else ''} ...]\n\n"
    return head + marker + tail


__all__ = ["call_id", "clip", "error", "ok", "services_for", "tool_config"]
