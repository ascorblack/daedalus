"""Tools that reach the chat surface: deliver files, spawn sessions."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


@tool(
    name="SendFile",
    description=(
        "Send a file from the workspace (or any path) to the operator in the current chat, "
        "with an optional caption. Images (png/jpg) render inline; everything else arrives as a "
        "document. Use it for reports, archives, generated code and any output longer than a "
        "screen. Chat replies themselves are Markdown and render natively (headings, tables, "
        "code blocks, quotes, <details> blocks), so short structured answers need no file."
    ),
)
async def send_file(context: ToolContext, path: str, caption: str | None = None) -> ToolResult:
    services = services_for(context)
    target = services.resolve(path)
    if not target.is_file():
        return error(context, f"no such file: {target}")
    if services.send_file is None:
        return error(context, "file delivery is not available in this session")
    result = await services.send_file(target, caption)
    return ok(context, f"sent {target.name} ({target.stat().st_size} bytes): {result}")


@tool(
    name="SpawnTask",
    description=(
        "Start a new, independent agent session (a new chat topic with its own workspace) "
        "for a separate task. Returns the new session id. Attach files by absolute path to "
        "copy them into the new workspace."
    ),
)
async def spawn_task(
    context: ToolContext, title: str, prompt: str, files: list[str] | None = None
) -> ToolResult:
    services = services_for(context)
    if services.spawn_session is None:
        return error(context, "spawning sessions is not available here")
    session_id = await services.spawn_session(title, prompt, [str(services.resolve(f)) for f in files or []])
    return ok(context, f"started session {session_id} — {title}", session_id=session_id)


TOOLS = [send_file, spawn_task]

__all__ = ["TOOLS"]
