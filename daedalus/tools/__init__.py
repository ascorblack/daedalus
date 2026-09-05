"""Host tools.

Every module in this package that defines ``TOOLS`` (a list of ``Tool`` classes or
instances) is picked up by :func:`discover_tools`. Adding a tool is adding a file.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable

from protocore.contracts.tools import Tool


def discover_tools() -> list[Tool]:
    """Import every submodule and collect its ``TOOLS``."""
    package = importlib.import_module(__name__)
    found: list[Tool] = []
    for module_info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        for entry in getattr(module, "TOOLS", ()):
            found.append(entry() if isinstance(entry, type) else entry)
    return found


def tool_names(tools: Iterable[Tool]) -> list[str]:
    return sorted(t.name for t in tools)


__all__ = ["discover_tools", "tool_names"]
