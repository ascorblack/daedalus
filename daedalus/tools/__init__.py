"""Host tools.

Every module in this package that defines ``TOOLS`` (a list of ``Tool`` classes or
instances) is picked up by :func:`discover_tools`. Adding a tool is adding a file.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from collections.abc import Iterable
from typing import Any

from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolResult

_ARG_ERROR = re.compile(r"unexpected keyword argument '(?P<extra>\w+)'|missing \d+ required (?:positional|keyword-only) arguments?: (?P<missing>.+)$")


def _explained(exc: TypeError, tool: Tool) -> str | None:
    """A model-readable message for a call with a wrong argument name, or None for any other TypeError."""
    match = _ARG_ERROR.search(str(exc))
    if match is None:
        return None
    params = tool.definition.parameters
    names = sorted((params.properties or {}).keys()) if hasattr(params, "properties") else []
    accepted = ", ".join(names) if names else "see the tool description"
    if match.group("extra"):
        return f"unknown argument {match.group('extra')!r} for {tool.name}; accepted: {accepted}"
    return f"{tool.name} is missing {match.group('missing')}; accepted arguments: {accepted}"


def _guarded(tool: Tool) -> Tool:
    """A call with a misspelled or missing argument answers with the accepted names, not a Python traceback."""
    original = tool.invoke

    async def invoke(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            return await original(context, arguments)
        except TypeError as exc:
            # Only the call boundary is caught: an error raised deeper inside the tool has a real traceback.
            frames = inspect.trace()
            explained = _explained(exc, tool) if len(frames) <= 2 else None
            if explained is None:
                raise
            return ToolResult(tool_call_id=str(context.metadata.get("tool_call_id") or ""), content=explained, is_error=True)

    tool.invoke = invoke  # type: ignore[method-assign]
    return tool


def discover_tools() -> list[Tool]:
    """Import every submodule and collect its ``TOOLS``."""
    package = importlib.import_module(__name__)
    found: list[Tool] = []
    for module_info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        for entry in getattr(module, "TOOLS", ()):
            found.append(_guarded(entry() if isinstance(entry, type) else entry))
    return found


def tool_names(tools: Iterable[Tool]) -> list[str]:
    return sorted(t.name for t in tools)


__all__ = ["discover_tools", "tool_names"]
