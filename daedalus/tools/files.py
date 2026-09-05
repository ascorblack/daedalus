"""File tools: read, write, edit, find, search."""

from __future__ import annotations

import asyncio
import fnmatch
import os
from pathlib import Path

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
    target = services.resolve(path)
    if not target.exists():
        return error(context, f"no such file: {target}")
    if target.is_dir():
        entries = sorted(os.listdir(target))
        return ok(context, f"{target} is a directory with {len(entries)} entries:\n" + "\n".join(entries[:500]))
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        size = target.stat().st_size
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
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
    if not target.is_file():
        return error(context, f"no such file: {target}")
    text = target.read_text(encoding="utf-8")
    count = text.count(old_string)
    if count == 0:
        return error(context, "old_string not found in file")
    if count > 1 and not replace_all:
        return error(context, f"old_string matches {count} times; make it unique or set replace_all")
    updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    target.write_text(updated, encoding="utf-8")
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
    if not root.is_dir():
        return error(context, f"not a directory: {root}")
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".venv", "__pycache__"}]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                matches.append(rel)
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break
    return ok(context, "\n".join(sorted(matches)) or "(no matches)", count=len(matches))


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
    args = ["rg", "-n", "--no-heading", "--color", "never", "-m", str(limit)]
    if case_insensitive:
        args.append("-i")
    if glob:
        args += ["-g", glob]
    args += ["-e", pattern, str(root)]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode == 1:
        return ok(context, "(no matches)", count=0)
    if proc.returncode not in (0, 1):
        return error(context, err.decode("utf-8", "replace") or f"rg exited {proc.returncode}")
    text = out.decode("utf-8", "replace")
    lines = text.splitlines()[:limit]
    rel = [line.replace(str(root) + "/", "", 1) for line in lines]
    return ok(context, clip("\n".join(rel), services.max_tool_output_chars), count=len(rel))


def _unused(_: Path) -> None:
    return None


TOOLS = [read_file, write_file, edit_file, find_files, search_files]

__all__ = ["TOOLS"]
