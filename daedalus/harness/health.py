"""Whether the host still hears a staff member, told the same way everywhere it is shown.

The staff card, the staff view and the orchestrator's ``Team`` tool each used to have a piece of it
(a status here, a receipt there), and a member whose team tools never connected looked exactly like
one that was simply busy. This module is the one place that turns what the host last heard on each
channel into a verdict, so the operator and the orchestrator never disagree about whether a member
is reachable.

Silence is a warning, never a failure: a worker thinking hard for ten minutes and a worker whose
hooks died look the same from outside, and the screen excerpt the orchestrator is woken with is what
tells them apart.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

TeamTools = Literal["connected", "missing", "waiting", "builtin", "none"]
"""``connected``: the CLI loaded the team tools (its MCP server said hello). ``missing``: it did not
within the time allowed. ``waiting``: not yet, and the time is not up. ``builtin``: a Daedalus member,
whose ``Report`` is one of its own tools. ``none``: the CLI has no way to carry them."""

SILENT_STATUSES = frozenset({"starting", "working", "no_signal"})
"""The statuses in which a member is expected to be saying something. An idle member is quiet by
right, and counting its silence would make every finished turn look like a fault."""


class MessageLike(Protocol):
    id: str
    state: str
    updated_at: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ChannelHealth:
    team_tools: TeamTools
    last_hook_at: str | None
    last_team_call_at: str | None
    last_signal_at: str | None
    silent_s: int | None
    """Seconds since the last structured signal while the member is expected to speak; ``None`` otherwise."""
    silence_after_s: int
    silent: bool
    last_message_id: str | None
    last_message_state: str | None
    last_acknowledged_at: str | None
    problems: tuple[str, ...]
    """Codes, in the order the operator should read them: ``team_tools_missing``, ``silent``,
    ``message_failed``. The app has words for each; the orchestrator reads :meth:`line`."""

    @property
    def level(self) -> Literal["ok", "warn"]:
        return "warn" if self.problems else "ok"

    def view(self) -> dict[str, Any]:
        return {
            "team_tools": self.team_tools,
            "last_hook_at": self.last_hook_at,
            "last_team_call_at": self.last_team_call_at,
            "last_signal_at": self.last_signal_at,
            "silent_s": self.silent_s,
            "silence_after_s": self.silence_after_s,
            "silent": self.silent,
            "last_message": {"id": self.last_message_id, "state": self.last_message_state} if self.last_message_id else None,
            "last_acknowledged_at": self.last_acknowledged_at,
            "problems": list(self.problems),
            "level": self.level,
        }

    def line(self, now: datetime) -> str:
        """The same verdict in one English line, for the orchestrator's ``Team(staff)``."""
        parts = [f"team tools {self.team_tools}"]
        parts.append(f"last hook {_ago(self.last_hook_at, now)}" if self.last_hook_at else "no hook yet")
        parts.append(f"last team call {_ago(self.last_team_call_at, now)}" if self.last_team_call_at else "no team call yet")
        if self.last_message_state:
            parts.append(f"last message {self.last_message_state}")
        if self.last_acknowledged_at:
            parts.append(f"last accepted {_ago(self.last_acknowledged_at, now)}")
        if self.silent_s is not None:
            parts.append(f"silent {self.silent_s // 60} min" + (f" (past {self.silence_after_s // 60} min)" if self.silent else ""))
        return "channel: " + ", ".join(parts)


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ago(stamp: str | None, now: datetime) -> str:
    at = _parse(stamp)
    if at is None:
        return "at an unknown time"
    seconds = max(0, int((now - at).total_seconds()))
    return f"{seconds}s ago" if seconds < 120 else f"{seconds // 60} min ago"


def channel_health(
    *,
    status: str,
    team_tools: str,
    channel: Mapping[str, Any],
    last_signal_at: str | None,
    messages: Iterable[MessageLike],
    now: datetime,
    silence_after_s: int,
) -> ChannelHealth:
    """The verdict for one live session.

    ``team_tools`` is the harness's capability (``mcp``, ``extension``, ``none``) or ``builtin`` for a
    Daedalus member; ``channel`` is what the CLI runtime last heard (``CliStaffRuntime.channel``);
    ``messages`` are the session's messages, newest first.
    """
    if team_tools == "builtin":
        tools: TeamTools = "builtin"
    elif team_tools == "none":
        tools = "none"
    else:
        reported = str(channel.get("team_tools") or "waiting")
        tools = reported if reported in ("connected", "missing", "waiting") else "waiting"  # type: ignore[assignment]
    # The newest structured signal of either kind counts: a hook is a signal, and so is a team call.
    heard = [s for s in (last_signal_at, channel.get("last_hook_at"), channel.get("last_team_call_at")) if s]
    newest = max(heard, key=lambda s: _parse(s) or datetime.min.replace(tzinfo=now.tzinfo)) if heard else None
    silent_s: int | None = None
    if status in SILENT_STATUSES:
        at = _parse(newest)
        silent_s = max(0, int((now - at).total_seconds())) if at is not None else None
    silent = status == "no_signal" or (silent_s is not None and silent_s >= silence_after_s)
    rows = list(messages)
    last = rows[0] if rows else None
    acknowledged = next((m for m in rows if m.state == "acknowledged"), None)
    problems: list[str] = []
    if tools == "missing":
        problems.append("team_tools_missing")
    if silent:
        problems.append("silent")
    if last is not None and last.state == "failed":
        problems.append("message_failed")
    return ChannelHealth(
        team_tools=tools,
        last_hook_at=channel.get("last_hook_at") or None,
        last_team_call_at=channel.get("last_team_call_at") or None,
        last_signal_at=newest,
        silent_s=silent_s,
        silence_after_s=silence_after_s,
        silent=silent,
        last_message_id=last.id if last is not None else None,
        last_message_state=last.state if last is not None else None,
        last_acknowledged_at=acknowledged.updated_at if acknowledged is not None else None,
        problems=tuple(problems),
    )


__all__ = ["SILENT_STATUSES", "ChannelHealth", "TeamTools", "channel_health"]
