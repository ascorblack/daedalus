"""Which modules of the host are reachable from its entry points.

A change to the agent's own code is only a change if the running agent executes it. This module
answers, from the source tree alone, whether a module is on any execution path: it parses every
``import`` in the ``daedalus`` package and walks the graph from the entry points the process
actually starts from. Tests are not importers: a module that only its tests import is dead.

The walk is static (``ast``), so it works on a worktree that is not importable in this process,
and it is deliberately generous: a dynamic import by string (``importlib.import_module(name)``)
counts when the string is a literal and the module it names is resolvable from the source — an
absolute name, or a relative one resolved against the package the source names: a literal
``package=`` (keyword or positional), the file's own ``__package__``, or its bare ``__name__``. When
the call names no package at all, the file's own package is used. When the package is an expression
the source does not name — a variable, another module's attribute — the literal is kept and no edge
is drawn: a module reached only that way needs a path the walk can see. A relative name that would
leave the package root keeps its literal form too, since there is no module it could name.
Package-level discovery (``pkgutil.iter_modules``) counts every submodule of the package that calls
it, and a relative ``from`` import resolves against the file's package. Generosity is the right
error: the gate exists to catch modules nothing mentions, not to argue about how they are mentioned.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

PACKAGE = "daedalus"
ENTRY_POINTS = ("daedalus.__main__", "daedalus.app", "daedalus.bench.harbor")
"""Where a process starts: the CLI, the application, and the Harbor benchmark adapter (loaded by
``harbor run -a daedalus.bench.harbor:DaedalusAgent``); everything else is reached from them."""

DISCOVERED_PACKAGES = ("daedalus.tools",)
"""Packages whose submodules are loaded by ``pkgutil`` discovery: every module directly inside them is
reachable once the package is. Extensions are not discovered: they are named one by one in the
``EXTENSIONS`` tuple, which the walk reads as a list of literal imports."""


def module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(module: str, is_package: bool, level: int, target: str | None) -> str | None:
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    if level > len(module.split(".")):
        return None
    return ".".join(base + ([target] if target else []))


def _package_of(module: str, is_package: bool) -> str:
    """What ``__package__`` holds inside this file: the file's own package, not its module name."""
    return module if is_package else module.rsplit(".", 1)[0]


def _resolve_from_package(package: str, dotted: str) -> str | None:
    """Resolve a relative name the way ``importlib.import_module(name, package=...)`` does:
    ``.mod`` against the package itself, ``..mod`` one level up. ``None`` when it leaves the root."""
    level = len(dotted) - len(dotted.lstrip("."))
    rest = dotted[level:]
    parts = package.split(".")
    if level > 1:
        if len(parts) < level - 1:
            return None
        parts = parts[: len(parts) - (level - 1)]
    base = ".".join(parts)
    if not base:
        return None
    return f"{base}.{rest}" if rest else base


def _static_package(args: list[ast.expr], keywords: list[ast.keyword], module: str, is_package: bool) -> str | None:
    """The package a dynamic relative import resolves against, when the source says it.

    ``package=`` may be given by keyword or positionally (``import_module(".x", "pkg")`` is legal).
    It is knowable when it is a string literal, the file's own ``__package__``, or the bare
    ``__name__``. Anything else — a variable, a call, another module's attribute — is *not* knowable,
    and ``None`` is returned so the caller keeps the literal: guessing here is how a live module ends
    up declared unreached, and the one thing this walk must not do is draw an edge to a module the
    source never names."""
    if len(args) > 1 and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
        return args[1].value
    for kw in keywords:
        if kw.arg != "package":
            continue
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
        if isinstance(kw.value, ast.Name):
            if kw.value.id == "__package__":
                return _package_of(module, is_package)
            if kw.value.id == "__name__":
                return module
        if isinstance(kw.value, ast.Attribute):  # bare ``__package__``/``__name__``, not ``other.__name__``
            if isinstance(kw.value.value, ast.Name) and kw.value.value.id in ("__package__", "__name__"):
                return _package_of(module, is_package) if kw.value.attr == "__package__" else module
    return None


def imports_of(path: Path, module: str, *, is_package: bool) -> set[str]:
    """Module names ``path`` mentions: static imports, ``from x import y`` (both ``x`` and ``x.y``,
    since ``y`` may be a submodule) and literal strings handed to ``importlib.import_module``.

    A *relative* literal (``import_module(".feature", package=__package__)``) is resolved against the
    package the source names: a literal ``package=``, the file's own ``__package__`` or its ``__name__``.
    When the call names no package at all the file's own package is used — a relative name in this file
    means something inside this tree. When the package is an expression the source does not name (a
    variable, another module's attribute), the literal is kept as written and no edge is drawn: the walk
    cannot tell what it loads, it does not guess, and a module reached only that way needs a path the
    walk can see. A relative name that would leave the package root is kept as written too — there is no
    module it could name."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(module, is_package, node.level, node.module) if node.level else node.module
            if base:
                found.add(base)
                for alias in node.names:
                    found.add(f"{base}.{alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "import_module" and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                literal = node.args[0].value
                if literal.startswith("."):
                    given = _static_package(node.args, node.keywords, module, is_package)
                    if given is None and not node.keywords and len(node.args) < 2:
                        given = _package_of(module, is_package)
                    resolved = _resolve_from_package(given, literal) if given else None
                    found.add(resolved if resolved else literal)
                else:
                    found.add(literal)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "EXTENSIONS" for t in node.targets):
            # The extension list: a tuple of dotted names handed to import_module one by one at start-up.
            if isinstance(node.value, (ast.Tuple, ast.List)):
                found.update(e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return found


def import_graph(root: Path) -> dict[str, set[str]]:
    """``module -> modules it imports`` for every module of the package under ``root``."""
    package_dir = root / PACKAGE
    graph: dict[str, set[str]] = {}
    for path in sorted(package_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        name = module_name(root, path)
        graph[name] = imports_of(path, name, is_package=path.name == "__init__.py")
    return graph


def reachable(graph: dict[str, set[str]], entry_points: Iterable[str] = ENTRY_POINTS) -> set[str]:
    """Every module reachable from the entry points through the graph, plus the parents of each
    reached module (importing ``a.b.c`` runs ``a`` and ``a.b``) and the discovered packages' children."""
    known = set(graph)
    seen: set[str] = set()
    stack = [e for e in entry_points if e in known]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        parts = current.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent in known and parent not in seen:
                stack.append(parent)
        if current in DISCOVERED_PACKAGES:
            prefix = current + "."
            stack.extend(m for m in known if m.startswith(prefix) and "." not in m[len(prefix):])
        for target in graph.get(current, ()):
            if target in known:
                stack.append(target)
            else:
                # ``from a.b import name`` where ``name`` is a symbol, not a module: reach ``a.b``.
                head = target.rsplit(".", 1)[0]
                if head in known and head not in seen:
                    stack.append(head)
    return seen


def unreachable_modules(root: Path) -> list[str]:
    graph = import_graph(root)
    return sorted(set(graph) - reachable(graph))


def modules_for_files(root: Path, files: Iterable[str]) -> list[str]:
    """The package modules among ``files`` (repo-relative paths); tests, docs and assets are not modules."""
    out = []
    for rel in files:
        path = Path(rel)
        if path.suffix == ".py" and path.parts and path.parts[0] == PACKAGE:
            out.append(module_name(root, root / path))
    return out


def path_reaches(root: Path, execution_path: str, targets: Iterable[str]) -> tuple[bool, list[str]]:
    """Whether the module named by ``execution_path`` (``pkg.mod`` or ``pkg.mod:symbol``) exists and
    reaches every target module; returns the targets it does not reach."""
    graph = import_graph(root)
    start = execution_path.split(":", 1)[0].strip()
    if start not in graph:
        return False, [f"no module {start!r}"]
    reached = reachable(graph, (start,)) | {start}
    missing = [t for t in targets if t not in reached]
    return not missing, missing


__all__ = ["DISCOVERED_PACKAGES", "ENTRY_POINTS", "import_graph", "modules_for_files", "path_reaches", "reachable", "unreachable_modules"]
