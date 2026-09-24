"""Per-session services that tools reach through ``ToolContext.session_id``.

Tools are registered once per process and shared by every session, so they cannot
hold session state. Instead each running session registers a :class:`SessionServices`
bundle in the process-wide :class:`ServiceLocator`, and a tool resolves it from the
``session_id`` the core stamps on every invocation.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daedalus.host.containment import Walls
from daedalus.host.filesystem import ExecBackend, LocalFS, ShellFS
from daedalus.host.policy import sealed_root

ProgressFn = Callable[[str], Awaitable[None]]
SendFileFn = Callable[[Path, str | None], Awaitable[str]]
AttachMediaFn = Callable[[list[dict[str, str]], str], Awaitable[dict[str, Any]]]
ScheduleFn = Callable[..., Awaitable[Any]]
SelfDevFn = Callable[..., Awaitable[str]]


class PathOutsideProject(PermissionError):
    """A path a session in a project may not touch. Raised where the path is resolved, so no tool can forget the check."""


@dataclass(slots=True)
class SessionServices:
    session_id: str
    workspace_dir: Path
    protected_paths: tuple[Path, ...] = ()
    tool_timeout_seconds: float = 900.0
    max_tool_output_chars: int = 60_000
    progress: ProgressFn | None = None
    send_file: SendFileFn | None = None
    attach_media: AttachMediaFn | None = None
    schedule: ScheduleFn | None = None
    self_propose: SelfDevFn | None = None
    self_apply: SelfDevFn | None = None
    """Commit a worktree branch into the running checkout, where there is no remote to propose it to."""
    self_rebuild: SelfDevFn | None = None
    self_rollback: SelfDevFn | None = None
    spawn_agent: Callable[..., Awaitable[str]] | None = None
    """Create a standing agent session with a brief, files and settings (see the SpawnAgent tool)."""
    exec_backend: ExecBackend | None = None
    """Where Exec runs and the file tools look when the session drives another machine (a benchmark container)."""
    writable: list[Path] = field(default_factory=list)
    """Paths outside the walls this session may write to under the sandbox: the worktrees it opened for its own changes."""
    walls: Walls | None = None
    """The folders of its project this session may read and the ones it may write (:func:`walls_for`).

    ``None`` is a session with no project walls — a session driving another machine, whose tools
    work somewhere this process's folders mean nothing — which resolves paths anywhere, as every
    session did before projects existed."""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def fs(self) -> LocalFS | ShellFS:
        return ShellFS(self.exec_backend, timeout=min(self.tool_timeout_seconds, 120.0)) if self.exec_backend is not None else LocalFS()

    def resolve(self, path: str | None, *, write: bool = False) -> Path:
        """Resolve a tool path: absolute stays absolute, relative is workspace-relative.

        In a project the answer is also contained: the resolved path must be inside one of the
        session's walls — a folder it may read, or with ``write`` a folder it may write — or inside
        one of the paths the host opened for this session. The check is made on the real path, so
        ``../..``, a symlink out of the tree and an absolute path elsewhere are all the same refusal.
        Every file tool, the file browser, the preview, the download and SendFile reach the
        filesystem through here, which is the reason the refusal lives at this one point rather than
        in each of them.

        What comes back inside a project is the real path, not the one that was asked for: the check
        judges the real path and the caller then opens what it was handed, so returning the candidate
        left a window in which a name inside a folder could be turned into a link out of it between
        the two. Handing back what was judged closes it.
        """
        if not path or path == ".":
            candidate = self.workspace_dir
        else:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = self.workspace_dir / candidate
        if self.walls is None:
            return candidate
        if not self.contains(candidate, write=write):
            raise PathOutsideProject(self.refusal(candidate, write=write))
        return Path(os.path.realpath(candidate))

    def contains(self, path: Path, *, write: bool = False) -> bool:
        """Whether this session may read ``path`` (or write it, with ``write``): inside its walls, or inside a path the host opened for it."""
        if self.walls is None:
            return True
        if self.walls.root_of(path, write=write) is not None:
            return True
        return Walls(readable=tuple(self.writable), writable=tuple(self.writable)).root_of(path, write=write) is not None

    def refusal(self, path: Path, *, write: bool = False) -> str:
        """Why ``path`` is refused, in words that name the folder and the way the session may use it."""
        if write and self.walls is not None and (root := self.walls.root_of(path)) is not None:
            return f"{path} is in {root}, a folder this session may read but not write: it is read-only here."
        return f"{path} is outside this project. This session works in {self.where()} and everything it reads or writes stays there."

    def where(self) -> str:
        """The folders this session may read, as a sentence names them."""
        if self.walls is None or not self.walls.readable:
            return str(self.workspace_dir)
        return ", ".join(str(p) for p in self.walls.readable)

    @property
    def workspace_writable(self) -> bool:
        """Whether the walls let this session write the folder it works in; a read-only folder does not."""
        return self.contains(self.workspace_dir, write=True)

    def sandbox_writable(self) -> list[Path]:
        """What the sandbox binds writable: the writable walls and the paths the host opened.

        Nothing else. The sandbox used to bind the workspace implicitly, which is exactly what would
        make a read-only folder writable under ``Exec`` while every file tool refused it.
        """
        base = list(self.walls.writable) if self.walls is not None else [self.workspace_dir]
        return [*base, *(p for p in self.writable if p not in base)]

    def is_protected(self, path: Path) -> bool:
        """Whether ``path`` is part of the installation rather than of its work.

        The same function the shell rules ask, over the same list, so that a path Exec is refused is
        not one a file tool opens: it resolves the symlinks and the ``..`` first, and it resolves a
        relative path against this session's workspace, which is where the tools resolve theirs.
        """
        return sealed_root(str(path), [str(p) for p in self.protected_paths], base=str(self.workspace_dir)) is not None


class ServiceLocator:
    def __init__(self) -> None:
        self._by_session: dict[str, SessionServices] = {}
        self.default: SessionServices | None = None

    def register(self, services: SessionServices) -> None:
        self._by_session[services.session_id] = services

    def unregister(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)

    def get(self, session_id: str) -> SessionServices:
        services = self._by_session.get(session_id) or self.default
        if services is None:
            raise RuntimeError(f"no services registered for session {session_id!r}")
        return services


locator = ServiceLocator()

__all__ = ["PathOutsideProject", "ServiceLocator", "SessionServices", "Walls", "locator"]
