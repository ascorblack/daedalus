"""File tools: read, write, edit, find, search."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import clip, error, ok, services_for

_MAX_LINE_CHARS = 2000


@tool(
    name="Read",
    description=(
        "Read a text file. Returns numbered lines. Use offset (1-based line) and limit to page "
        "through large files. Relative paths resolve against the session workspace."
    ),
)
async def read_file(
    context: ToolContext, path: str, offset: int = 1, limit: int = 400
) -> ToolResult:
    services = services_for(context)
    fs = services.fs
    target = services.resolve(path)
    if not await fs.exists(target):
        return error(context, f"no such file: {target}")
    if await fs.is_dir(target):
        entries = await fs.listdir(target)
        return ok(context, f"{target} is a directory with {len(entries)} entries:\n" + "\n".join(entries[:500]))
    try:
        text = await fs.read_text(target)
    except UnicodeDecodeError:
        size = await fs.size(target)
        return error(context, f"{target} is binary ({size} bytes); use exec with a suitable tool")
    lines = text.splitlines()
    start = max(offset, 1) - 1
    window = lines[start : start + max(limit, 1)]
    numbered = "\n".join(
        f"{start + i + 1:>6}\t{line[:_MAX_LINE_CHARS]}" for i, line in enumerate(window)
    )
    footer = ""
    if start + len(window) < len(lines):
        footer = f"\n[{len(lines) - start - len(window)} more lines; continue with offset={start + len(window) + 1}]"
    return ok(context, clip(numbered, services.max_tool_output_chars) + footer, total_lines=len(lines))


@tool(
    name="Write",
    description="Create or overwrite a text file with the given content. Parent directories are created.",
)
async def write_file(context: ToolContext, path: str, content: str) -> ToolResult:
    services = services_for(context)
    target = services.resolve(path)
    if services.is_protected(target):
        return error(context, f"{target} is protected and cannot be written by tools")
    await services.fs.write_text(target, content)
    return ok(context, f"wrote {len(content)} characters to {target}", path=str(target))


@tool(
    name="Edit",
    description=(
        "Replace an exact substring in a text file. old_string must match exactly once unless "
        "replace_all is true. Preserve indentation precisely."
    ),
)
async def edit_file(
    context: ToolContext, path: str, old_string: str, new_string: str, replace_all: bool = False
) -> ToolResult:
    services = services_for(context)
    target = services.resolve(path)
    if services.is_protected(target):
        return error(context, f"{target} is protected and cannot be edited by tools")
    fs = services.fs
    if not await fs.is_file(target):
        return error(context, f"no such file: {target}")
    text = await fs.read_text(target)
    count = text.count(old_string)
    if count == 0:
        return error(context, "old_string not found in file")
    if count > 1 and not replace_all:
        return error(context, f"old_string matches {count} times; make it unique or set replace_all")
    updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    await fs.write_text(target, updated)
    return ok(context, f"edited {target}: {count if replace_all else 1} replacement(s)", path=str(target))


@tool(
    name="Find",
    description="Find files by glob pattern (e.g. '**/*.py') under a directory (default: workspace).",
)
async def find_files(
    context: ToolContext, pattern: str = "**/*", path: str | None = None, limit: int = 500
) -> ToolResult:
    services = services_for(context)
    root = services.resolve(path)
    if not await services.fs.is_dir(root):
        return error(context, f"not a directory: {root}")
    matches = await services.fs.find(root, pattern, limit)
    return ok(context, "\n".join(matches) or "(no matches)", count=len(matches))


@tool(
    name="Search",
    description=(
        "Search file contents with ripgrep (regex). Returns 'path:line: text' lines. "
        "Use glob to restrict file types, e.g. '*.py'."
    ),
)
async def search_files(
    context: ToolContext,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    case_insensitive: bool = False,
    limit: int = 200,
) -> ToolResult:
    services = services_for(context)
    root = services.resolve(path)
    code, text, err = await services.fs.search(root, pattern, glob=glob, case_insensitive=case_insensitive, limit=limit)
    if code == 1:
        return ok(context, "(no matches)", count=0)
    if code not in (0, 1):
        return error(context, err or f"search exited {code}")
    lines = text.splitlines()[:limit]
    rel = [line.replace(str(root) + "/", "", 1) for line in lines]
    return ok(context, clip("\n".join(rel), services.max_tool_output_chars), count=len(rel))


TOOLS = [read_file, write_file, edit_file, find_files, search_files]

__all__ = ["TOOLS"]
