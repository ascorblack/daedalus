"""Per-session services that tools reach through ``ToolContext.session_id``.

Tools are registered once per process and shared by every session, so they cannot
hold session state. Instead each running session registers a :class:`SessionServices`
bundle in the process-wide :class:`ServiceLocator`, and a tool resolves it from the
``session_id`` the core stamps on every invocation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daedalus.host.filesystem import ExecBackend, LocalFS, ShellFS

ProgressFn = Callable[[str], Awaitable[None]]
SendFileFn = Callable[[Path, str | None], Awaitable[str]]
ScheduleFn = Callable[..., Awaitable[Any]]
SelfDevFn = Callable[..., Awaitable[str]]


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
    self_rebuild: SelfDevFn | None = None
    self_rollback: SelfDevFn | None = None
    spawn_agent: Callable[..., Awaitable[str]] | None = None
    """Create a standing agent session with a brief, files and settings (see the SpawnAgent tool)."""
    exec_backend: ExecBackend | None = None
    """Where Exec runs and the file tools look when the session drives another machine (a benchmark container)."""
    writable: list[Path] = field(default_factory=list)
    """Paths outside the workspace this session may write to under the sandbox: the worktrees it opened for its own changes."""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def fs(self) -> LocalFS | ShellFS:
        return ShellFS(self.exec_backend, timeout=min(self.tool_timeout_seconds, 120.0)) if self.exec_backend is not None else LocalFS()

    def resolve(self, path: str | None) -> Path:
        """Resolve a tool path: absolute stays absolute, relative is workspace-relative."""
        if not path or path == ".":
            return self.workspace_dir
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_dir / candidate
        return candidate

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

__all__ = ["ServiceLocator", "SessionServices", "locator"]
