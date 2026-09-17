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

from daedalus.host.filesystem import ExecBackend, LocalFS, ShellFS

ProgressFn = Callable[[str], Awaitable[None]]
SendFileFn = Callable[[Path, str | None], Awaitable[str]]
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
    """Paths outside the workspace this session may write to under the sandbox: the worktrees it opened for its own changes."""
    project_root: Path | None = None
    """The project this session works in, if any: every path it resolves stays inside this folder.

    ``None`` is a session with a directory of its own, which is every session that existed before
    projects did — those resolve paths exactly as they always have, anywhere on the filesystem."""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def fs(self) -> LocalFS | ShellFS:
        return ShellFS(self.exec_backend, timeout=min(self.tool_timeout_seconds, 120.0)) if self.exec_backend is not None else LocalFS()

    def resolve(self, path: str | None) -> Path:
        """Resolve a tool path: absolute stays absolute, relative is workspace-relative.

        In a project the answer is also contained: the resolved path must be the project root or
        inside it (or inside one of the paths the host opened for this session), and the check is
        made on the real path, so ``../..``, a symlink out of the tree and an absolute path
        elsewhere are all the same refusal. Every file tool, the file browser, the preview, the
        download and SendFile reach the filesystem through here, which is the reason the refusal
        lives at this one point rather than in each of them.

        What comes back inside a project is the real path, not the one that was asked for: the check
        judges the real path and the caller then opens what it was handed, so returning the candidate
        left a window in which a name inside the root could be turned into a link out of it between
        the two. Handing back what was judged closes it.
        """
        if not path or path == ".":
            return self.workspace_dir
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_dir / candidate
        if self.project_root is None:
            return candidate
        if not self.contains(candidate):
            raise PathOutsideProject(
                f"{candidate} is outside this project. This session works in {self.project_root} and everything it reads or writes stays there."
            )
        return Path(os.path.realpath(candidate))

    def contains(self, path: Path) -> bool:
        """Whether ``path`` is inside the project root, or inside a path the host opened for this session.

        ``os.path.realpath`` resolves the symlinks it can and leaves a not-yet-created tail alone,
        so a file about to be written is judged by where it would land.
        """
        if self.project_root is None:
            return True
        real = Path(os.path.realpath(path))
        for root in (self.project_root, *self.writable):
            base = Path(os.path.realpath(root))
            if real == base or base in real.parents:
                return True
        return False

    def is_protected(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        for protected in self.protected_paths:
            try:
                p = protected.resolve()
            except OSError:
                p = protected
            if resolved == p or p in resolved.parents:
                return True
        return False


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

__all__ = ["PathOutsideProject", "ServiceLocator", "SessionServices", "locator"]
