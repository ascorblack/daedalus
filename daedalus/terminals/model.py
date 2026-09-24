"""The values the terminals service takes and gives: owners, specifications, views and errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ENVS = ("container", "host")
OWNER_KINDS = ("session", "staff", "project", "free")
STATUSES = ("running", "exited", "lost")


@dataclass(frozen=True, slots=True)
class Owner:
    kind: str
    """``session`` · ``staff`` · ``project`` · ``free``"""
    id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in OWNER_KINDS:
            raise InvalidRequest(f"an owner is one of {', '.join(OWNER_KINDS)}, not {self.kind!r}")
        if (self.kind == "free") != (not self.id):
            raise InvalidRequest("a free terminal has no owner id, and every other owner has one")


@dataclass(frozen=True, slots=True)
class Origin:
    """Who writes into a terminal: always an agent, named, because the audit and the daemon's journal
    must say whose words they were. Human input never comes this way; it arrives on an attachment."""

    actor: str
    launch_id: str = ""
    note: str = ""


@dataclass(slots=True)
class TerminalSpec:
    env: str
    owner: Owner
    project_id: str | None = None
    cwd: str | None = None
    """Absolute, in the environment's filesystem. ``None`` = the owner's default place."""
    argv: list[str] | None = None
    """Run exactly, never typed into a shell. ``None`` = the environment's login shell."""
    env_vars: dict[str, str] = field(default_factory=dict)
    strip_env: list[str] = field(default_factory=list)
    title: str = ""
    sandbox: bool = False
    cols: int = 80
    rows: int = 24
    profile: str = "shell"
    """``shell`` or ``harness:<name>``: what kind of cost this terminal is, for the load estimate."""
    launch_id: str = ""
    log_to_disk: bool = False
    created_by: str = "operator"
    """``operator`` or ``agent:<actor>``. An agent's launch waits for a place under the cap; the
    operator's goes past it once confirmed."""


@dataclass(frozen=True, slots=True)
class EnvStatus:
    env: str
    available: bool
    reason: str = ""
    """A code: ``not_configured``, ``connecting``, ``not_installed``, ``not_running``, ``refused``,
    ``unreachable``, ``protocol_mismatch``; empty when available."""
    detail: str = ""
    version: str = ""
    sandbox: bool = False
    shell: str = ""
    home: str = ""
    port_range: str = ""
    public_host: str = ""
    preview_poll_ms: int = 3000
    running: int = 0

    def view(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "available": self.available,
            "reason": self.reason,
            "detail": self.detail,
            "version": self.version,
            "sandbox": self.sandbox,
            "shell": self.shell,
            "home": self.home,
            "port_range": self.port_range,
            "public_host": self.public_host,
            "preview_poll_ms": self.preview_poll_ms,
            "running": self.running,
        }


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    """The bytes reached the terminal. Never that the program read them, let alone acted on them."""

    bytes: int
    seq_before: int
    queued_ms: int
    delivered_at: str


@dataclass(frozen=True, slots=True)
class OutputChunk:
    from_seq: int
    to_seq: int
    head_seq: int
    gap: bool
    data: str | bytes


@dataclass(frozen=True, slots=True)
class TerminalEvent:
    """An event of one environment's daemon, in the daemon's own total order (``seq``)."""

    env: str
    seq: int
    type: str
    terminal_id: str | None
    at: str
    data: dict[str, Any]


class TerminalError(Exception):
    """Base of what the service refuses; the API maps each kind to a status."""

    status = 500
    code = "error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(TerminalError):
    status, code = 404, "not_found"


class InvalidRequest(TerminalError):
    status, code = 400, "invalid"


class Conflict(TerminalError):
    status, code = 409, "conflict"


class OverCap(TerminalError):
    """The machine already runs as many terminals as the cap allows."""

    status, code = 409, "over_cap"


class EnvUnavailable(TerminalError):
    status, code = 503, "unavailable"


class Unsupported(TerminalError):
    status, code = 501, "unsupported"


class TimedOut(TerminalError):
    status, code = 504, "timeout"


__all__ = [
    "ENVS",
    "OWNER_KINDS",
    "STATUSES",
    "Conflict",
    "EnvStatus",
    "EnvUnavailable",
    "InvalidRequest",
    "NotFound",
    "Origin",
    "OutputChunk",
    "OverCap",
    "Owner",
    "TerminalError",
    "TerminalEvent",
    "TerminalSpec",
    "TimedOut",
    "Unsupported",
    "WriteReceipt",
]
