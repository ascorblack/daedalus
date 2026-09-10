"""File tools: read, write, edit, find, search."""

from __future__ import annotations

import asyncio
import difflib
import shlex
from pathlib import Path
from typing import Any

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
    try:
        await services.fs.write_text(target, content)
    except OSError as exc:
        return error(context, f"could not write {target}: {exc}")
    return ok(context, f"wrote {len(content)} characters to {target}" + await diagnostics(services, target), path=str(target))


@tool(
    name="Edit",
    description=(
        "Replace a substring in a text file. old_string must match once unless replace_all is true; an "
        "exact match is tried first, then one ignoring trailing whitespace, then one ignoring indentation "
        "(the file's indentation is kept). On a miss the error shows the closest lines. For several "
        "changes to one file use MultiEdit."
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
    try:
        updated, count, how = apply_edit(text, old_string, new_string, replace_all=replace_all)
    except EditMiss as miss:
        return error(context, str(miss))
    try:
        await fs.write_text(target, updated)
    except OSError as exc:
        return error(context, f"could not write {target}: {exc}")
    note = f" (matched {how})" if how != "exactly" else ""
    return ok(context, f"edited {target}: {count} replacement(s){note}" + await diagnostics(services, target), path=str(target))


class EditMiss(ValueError):
    """The old text is not in the file; the message shows the nearest lines so the next attempt can be exact."""


def _match_loose(text: str, old: str) -> tuple[str, int, str]:
    """Find ``old`` in ``text`` ignoring trailing whitespace on each line, then ignoring indentation.

    Returns ``(exact_slice_in_text, count, how)`` or raises :class:`EditMiss`; the slice is the text's own
    bytes for the first match, so the replacement keeps the file's indentation and line endings.
    """
    lines = text.splitlines(keepends=True)
    old_lines = old.splitlines()
    if not old_lines:
        raise EditMiss("old_string is empty")
    for how, norm in (("ignoring trailing whitespace", lambda x: x.rstrip()), ("ignoring indentation", lambda x: x.strip())):
        wanted = [norm(line) for line in old_lines]
        hits = [i for i in range(len(lines) - len(wanted) + 1) if [norm(line.rstrip("\r\n")) for line in lines[i : i + len(wanted)]] == wanted]
        if hits:
            i = hits[0]
            block = "".join(lines[i : i + len(wanted)])
            if old.endswith("\n") is False and block.endswith("\n"):
                block = block[:-1] if not block.endswith("\r\n") else block[:-2]
            return block, len(hits), how
    raise EditMiss("old_string not found in file" + nearest_window(text, old))


def nearest_window(text: str, old: str, *, context_lines: int = 2) -> str:
    """The lines of the file that look most like ``old``, numbered, so the caller sees what is actually there."""
    lines = text.splitlines()
    size = max(1, len(old.splitlines()))
    if not lines:
        return ""
    best, best_at = 0.0, 0
    for i in range(0, max(1, len(lines) - size + 1)):
        ratio = difflib.SequenceMatcher(None, "\n".join(lines[i : i + size]), old).ratio()
        if ratio > best:
            best, best_at = ratio, i
    if best < 0.3:
        return ""
    start = max(0, best_at - context_lines)
    end = min(len(lines), best_at + size + context_lines)
    shown = "\n".join(f"{n + 1:>6}\t{lines[n][:_MAX_LINE_CHARS]}" for n in range(start, end))
    return f". The closest lines ({best:.0%} similar):\n{shown}"


def apply_edit(text: str, old: str, new: str, *, replace_all: bool = False) -> tuple[str, int, str]:
    """``(updated_text, replacements, how)``; ``how`` says which matching level found the text."""
    count = text.count(old)
    if count:
        if count > 1 and not replace_all:
            raise EditMiss(f"old_string matches {count} times; make it unique or set replace_all")
        return (text.replace(old, new) if replace_all else text.replace(old, new, 1)), (count if replace_all else 1), "exactly"
    block, count, how = _match_loose(text, old)
    if count > 1 and not replace_all:
        raise EditMiss(f"old_string matches {count} times when {how}; make it unique or set replace_all")
    # Keep the file's own indentation: re-indent the replacement to the block's first line when the match ignored it.
    if how == "ignoring indentation":
        indent = block[: len(block) - len(block.lstrip())]
        old_indent = old[: len(old) - len(old.lstrip())]
        new = "\n".join((indent + line[len(old_indent):]) if line.startswith(old_indent) and line.strip() else line for line in new.split("\n"))
    return (text.replace(block, new) if replace_all else text.replace(block, new, 1)), (count if replace_all else 1), how


@tool(
    name="MultiEdit",
    description=(
        "Apply several edits to one file atomically: edits is a list of {old_string, new_string[, replace_all]} "
        "applied in order to the same text; if any edit fails the file is left unchanged and the error names it."
    ),
)
async def multi_edit(context: ToolContext, path: str, edits: list[dict[str, Any]]) -> ToolResult:
    services = services_for(context)
    target = services.resolve(path)
    if services.is_protected(target):
        return error(context, f"{target} is protected and cannot be edited by tools")
    fs = services.fs
    if not await fs.is_file(target):
        return error(context, f"no such file: {target}")
    if not edits:
        return error(context, "edits is empty")
    text = await fs.read_text(target)
    total = 0
    for index, edit in enumerate(edits, 1):
        old, new = str(edit.get("old_string", "")), str(edit.get("new_string", ""))
        try:
            text, count, _ = apply_edit(text, old, new, replace_all=bool(edit.get("replace_all", False)))
        except EditMiss as miss:
            return error(context, f"edit {index} of {len(edits)} failed, file unchanged: {miss}")
        total += count
    try:
        await fs.write_text(target, text)
    except OSError as exc:
        return error(context, f"could not write {target}: {exc}")
    return ok(context, f"edited {target}: {total} replacement(s) in {len(edits)} edits" + await diagnostics(services, target), path=str(target))


_DIAGNOSABLE = {".py"}


async def diagnostics(services: Any, target: Path) -> str:
    """A syntax check of the file just written, appended to the tool result: the model sees a broken file at once.

    Python only for now (compile, then ruff when it is installed); nothing for other types. The check runs
    where the file lives — through the exec backend when the session drives another machine.
    """
    if target.suffix not in _DIAGNOSABLE:
        return ""
    quoted = shlex.quote(str(target))
    command = f"python3 -m py_compile {quoted} && (command -v ruff >/dev/null 2>&1 && ruff check --output-format concise {quoted} || true)"
    try:
        if services.exec_backend is not None:
            outcome = await services.exec_backend.run(command, cwd=None, env=None, timeout=60)
            code, out = outcome.exit_code, outcome.output
        else:
            proc = await asyncio.create_subprocess_exec("bash", "-lc", command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            code, out = proc.returncode or 0, raw.decode("utf-8", "replace")
    except (OSError, TimeoutError):
        return ""
    out = out.strip()
    if code != 0:
        return f"\n⚠ the file does not compile:\n{out[:1500]}"
    if out and "All checks passed" not in out:
        return f"\nlint:\n{out[:1500]}"
    return ""


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


TOOLS = [read_file, write_file, edit_file, multi_edit, find_files, search_files]

__all__ = ["TOOLS"]
