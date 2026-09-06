"""Tools that reach the chat surface: deliver files, spawn sessions."""

from __future__ import annotations

from pathlib import Path

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


@tool(
    name="SpawnAgent",
    description=(
        "Create a standing agent: a new session (its own chat topic and workspace) that keeps a brief "
        "you write in its system prompt, gets copies of the files it needs, and optionally a model "
        "preset, a mode, MCP servers and a peer name so you can AskPeer it later. Use it when another "
        "agent should own a job for good and you hold what it needs (instructions, files, settings); "
        "SpawnTask is for a one-off job. The brief is the hand-over: what the agent is for, how the work "
        "is done, where things are, what to avoid. Paths are relative to this workspace or absolute."
    ),
)
async def spawn_agent(
    context: ToolContext,
    title: str,
    brief: str,
    files: list[str] | None = None,
    first_message: str | None = None,
    preset: str | None = None,
    mode: str | None = None,
    mcp: list[str] | None = None,
    peer_name: str | None = None,
) -> ToolResult:
    services = services_for(context)
    if services.spawn_agent is None:
        return error(context, "spawning agents is not available here")
    if not brief.strip():
        return error(context, "the brief is what the new agent lives by; write it")
    paths = [str(services.resolve(f)) for f in files or []]
    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        return error(context, "these files do not exist: " + ", ".join(missing))
    try:
        session_id = await services.spawn_agent(
            title=title, brief=brief, files=paths, first_message=first_message, preset=preset, mode=mode, mcp=list(mcp or []), peer_name=peer_name
        )
    except (ValueError, KeyError) as exc:
        return error(context, str(exc))
    return ok(context, f"agent '{title}' created as session {session_id}" + (f", peer '{peer_name}'" if peer_name else "") + (f", {len(paths)} file(s) copied to its inbox" if paths else ""), session_id=session_id)


TOOLS = [send_file, spawn_task, spawn_agent]

__all__ = ["TOOLS"]
