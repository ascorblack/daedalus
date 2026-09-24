"""Looking into a project's folders without touching them: what an orchestrator's ``Peek`` reads through.

An orchestrator may check something itself — a file, a search, what a branch holds — but never
changes anything, and it reads only inside its project's walls. Everything here is read-only by
construction: files are opened for reading, and git runs as an argument vector with every setting
that could make a read run something (a filesystem monitor, a hook, an external diff or text
conversion) switched off, and with the optional index lock disabled so a status does not write.

A folder of this process's own environment is read directly (:class:`LocalFolderAccess`). A folder
on the other side of the container needs the terminal bridge, which ``Peek`` does not drive yet, so
it answers with the reason (:class:`UnreachableFolder`); both are a :class:`FolderAccess`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Protocol

from daedalus.host.gitrun import GitError, run_command

OUTPUT_MAX_CHARS = 12_000
"""What one Peek may return. The orchestrator pays for every character on every later turn of its run."""
READ_MAX_LINES = 400
LINE_MAX_CHARS = 2000
LIST_MAX_ENTRIES = 300
FIND_MAX = 300
SEARCH_MAX = 200
GIT_LOG_MAX = 100
GIT_TIMEOUT_SECONDS = 20.0
SKIPPED_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".agents"})

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^:+-]*$")
"""A revision or a range. It may not begin with ``-``: a ref is an argument, and one that begins
with a dash is an option (``--output=<file>`` would make a read write)."""

# Every git call starts with these. Each one is a way a read-only command can run a program or
# write a file in a repository whose configuration the orchestrator does not control.
_GIT_SAFE = (
    "git",
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
    "-c", "core.pager=cat",
    "--no-pager",
)
_GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


class PeekRefused(ValueError):
    """What Peek will not do, in words the orchestrator can act on."""


def clip(text: str, limit: int = OUTPUT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[… {len(text) - limit} more characters; narrow the path, the pattern or the range]"


def check_ref(ref: str) -> str:
    ref = ref.strip()
    if ref and not _REF_RE.match(ref):
        raise PeekRefused(f"{ref!r} is not a revision Peek accepts")
    return ref


class FolderAccess(Protocol):
    """Read-only access to one folder of a project."""

    async def read(self, path: str, *, offset: int, limit: int) -> str: ...
    async def ls(self, path: str) -> str: ...
    async def find(self, pattern: str, path: str) -> str: ...
    async def search(self, pattern: str, path: str) -> str: ...
    async def git_log(self, ref: str, path: str, *, limit: int) -> str: ...
    async def git_diff(self, ref: str, path: str) -> str: ...
    async def git_status(self) -> str: ...


@dataclass(frozen=True, slots=True)
class UnreachableFolder:
    """A folder this process cannot read; every operation says why."""

    reason: str

    async def _refuse(self) -> str:
        raise PeekRefused(self.reason)

    async def read(self, path: str, *, offset: int, limit: int) -> str:
        return await self._refuse()

    async def ls(self, path: str) -> str:
        return await self._refuse()

    async def find(self, pattern: str, path: str) -> str:
        return await self._refuse()

    async def search(self, pattern: str, path: str) -> str:
        return await self._refuse()

    async def git_log(self, ref: str, path: str, *, limit: int) -> str:
        return await self._refuse()

    async def git_diff(self, ref: str, path: str) -> str:
        return await self._refuse()

    async def git_status(self) -> str:
        return await self._refuse()


class Inside(Protocol):
    """What a session's services answer about a path: may it be read, and is it the installation's own."""

    def contains(self, path: Path, *, write: bool = False) -> bool: ...
    def is_protected(self, path: Path) -> bool: ...


class LocalFolderAccess:
    """A folder of this process's environment, read inside the reader's walls."""

    def __init__(self, root: Path, walls: Inside) -> None:
        self.root = root
        self.walls = walls

    def target(self, path: str) -> Path:
        """``path`` relative to the folder (or absolute), refused outside the walls or inside the installation."""
        candidate = Path(path or ".").expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = Path(os.path.realpath(candidate))
        if not self.walls.contains(resolved):
            raise PeekRefused(f"{candidate} is outside the project's folders")
        if self.walls.is_protected(resolved):
            raise PeekRefused(f"{candidate} is part of the installation, not of the project")
        return resolved

    async def read(self, path: str, *, offset: int = 1, limit: int = 200) -> str:
        target = self.target(path)
        if target.is_dir():
            return await self.ls(path)
        if not target.is_file():
            raise PeekRefused(f"no such file: {target}")
        start = max(1, int(offset)) - 1
        count = max(1, min(int(limit), READ_MAX_LINES))
        return await asyncio.to_thread(self._read_lines, target, start, count)

    @staticmethod
    def _read_lines(target: Path, start: int, count: int) -> str:
        # Streamed: a log of gigabytes is read up to the lines asked for, never whole.
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(8192)
            if "\x00" in head:
                raise PeekRefused(f"{target} is a binary file")
            handle.seek(0)
            window = list(islice(handle, start, start + count + 1))
        more = len(window) > count
        lines = [f"{start + i + 1:>6}\t{line.rstrip(chr(10))[:LINE_MAX_CHARS]}" for i, line in enumerate(window[:count])]
        tail = f"\n[more lines follow; continue with offset={start + count + 1}]" if more else ""
        return clip("\n".join(lines) or "(empty)") + tail

    async def ls(self, path: str) -> str:
        target = self.target(path)
        if not target.is_dir():
            raise PeekRefused(f"not a directory: {target}")

        def listing() -> str:
            entries = sorted(os.scandir(target), key=lambda e: e.name)
            shown = [e.name + ("/" if e.is_dir() else "") for e in entries[:LIST_MAX_ENTRIES]]
            extra = f"\n[… {len(entries) - LIST_MAX_ENTRIES} more entries]" if len(entries) > LIST_MAX_ENTRIES else ""
            return (f"{target}:\n" + "\n".join(shown) + extra) if shown else f"{target} is empty"

        return clip(await asyncio.to_thread(listing))

    async def find(self, pattern: str, path: str) -> str:
        root = self.target(path)
        if not root.is_dir():
            raise PeekRefused(f"not a directory: {root}")
        wanted = pattern or "*"

        def walk() -> list[str]:
            found: list[str] = []
            for directory, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
                for name in sorted(files):
                    rel = os.path.relpath(os.path.join(directory, name), root)
                    if fnmatch.fnmatch(rel, wanted) or fnmatch.fnmatch(name, wanted):
                        found.append(rel)
                        if len(found) >= FIND_MAX:
                            return found
            return found

        matches = await asyncio.to_thread(walk)
        return clip("\n".join(matches) or "(no matches)")

    async def search(self, pattern: str, path: str) -> str:
        if not pattern:
            raise PeekRefused("search needs a pattern")
        root = self.target(path)
        argv = ["rg", "-n", "--no-heading", "--color", "never", "-m", "20", "--max-columns", "300", "--glob", "!.agents", "-e", pattern, "--", str(root)]
        try:
            out = await run_command(argv, timeout=GIT_TIMEOUT_SECONDS)
        except GitError as exc:
            if exc.returncode == 1:
                return "(no matches)"
            raise PeekRefused(f"search failed: {str(exc)[-300:]}") from exc
        except FileNotFoundError as exc:
            raise PeekRefused("search needs ripgrep, which is not installed here") from exc
        lines = [line.replace(str(root) + "/", "", 1) for line in out.splitlines()[:SEARCH_MAX]]
        return clip("\n".join(lines) or "(no matches)")

    async def _git(self, args: Sequence[str]) -> str:
        try:
            return await run_command([*_GIT_SAFE, *args], cwd=self.root, env=_GIT_ENV, timeout=GIT_TIMEOUT_SECONDS)
        except GitError as exc:
            raise PeekRefused(f"git: {str(exc)[-400:]}") from exc

    def _pathspec(self, path: str) -> list[str]:
        if not path:
            return []
        return ["--", str(self.target(path))]

    async def git_log(self, ref: str, path: str, *, limit: int = 20) -> str:
        count = max(1, min(int(limit), GIT_LOG_MAX))
        revision = check_ref(ref)
        out = await self._git(["log", "--no-decorate", "--no-textconv", "--format=%h %ad %an: %s", "--date=short", f"-n{count}", *([revision] if revision else []), *self._pathspec(path)])
        return clip(out.strip() or "(no commits)")

    async def git_diff(self, ref: str, path: str) -> str:
        revision = check_ref(ref)
        common = ["diff", "--no-ext-diff", "--no-textconv", *([revision] if revision else [])]
        spec = self._pathspec(path)
        stat = await self._git([*common, "--stat", *spec])
        patch = await self._git([*common, *spec])
        if not stat.strip():
            return "(no changes)"
        return clip(stat.strip() + "\n\n" + patch)

    async def git_status(self) -> str:
        out = await self._git(["status", "--short", "--branch"])
        return clip(out.strip() or "(clean)")


__all__ = ["FolderAccess", "LocalFolderAccess", "PeekRefused", "UnreachableFolder", "check_ref", "clip"]
