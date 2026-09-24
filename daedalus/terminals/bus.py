"""What a terminal's daemon reports, retold on the host's event bus.

The daemon's events are control traffic for the host: output offsets, rows, mode flips, process
statistics. The bus carries what the rest of the host and the app act on — a title or a directory
that changed, a command that finished with its exit code, a bell, a notification, a progress bar —
in the registry's payloads, with the ids of the terminal's owner, so a filter on a session or a
project sees its terminals' events without knowing about terminals.

Left off the bus on purpose:

- ``terminal.created`` and ``terminal.exited``: the service publishes those itself, because it owns
  the row's life and a reconcile can end a terminal without the daemon saying anything;
- ``terminal.mode``, ``terminal.stats`` and anything this module does not name: state for the host
  and the browser, which would wake every subscriber many times a minute for nothing;
- the hook ingress of the command-line harnesses, when it arrives: those posts are an adapter's
  private conversation with its program, and an orchestrator's context is kept free of them.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.terminals.model import TerminalEvent

if TYPE_CHECKING:
    from daedalus.terminals.service import Terminals

logger = logging.getLogger(__name__)

TITLE_CHARS = 500
TEXT_CHARS = 2000
"""A command line or a notification body. The daemon allows 64 KiB in one escape sequence and the bus
refuses a payload past 64 KiB; what a person or an orchestrator reads in a toast is a line or two."""

MOMENT_SECONDS = 30.0
"""A bell or a progress value older than this is dropped. After the host restarts it replays the
daemon's events from its last saved cursor, and a bell from a minute ago would ring now for nothing;
a finished command, a changed title or a notification still says something and is kept."""

REMEMBERED = 4096
"""Terminals whose last title and directory are kept to tell a change from a repeat. Shells set the
title on every prompt, so without this every Enter would be an event; the bound only matters if
terminals were to end without their exit ever arriving."""

PROGRESS_STATES = {0: "remove", 1: "set", 2: "error", 3: "indeterminate", 4: "pause"}
"""OSC 9;4 states by number, as ConEmu defined them and Windows Terminal and Ghostty follow."""


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _age_seconds(at: str) -> float:
    try:
        moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment) / timedelta(seconds=1)


def translate(event: TerminalEvent) -> tuple[str, dict[str, Any]] | None:
    """The bus type and payload for one daemon event, or ``None`` for one the bus does not carry."""
    data = event.data
    kind = event.type
    if kind == "terminal.title":
        return kind, {"title": _clip(data.get("title"), TITLE_CHARS)}
    if kind == "terminal.cwd":
        cwd = str(data.get("cwd") or "")
        return (kind, {"cwd": cwd}) if cwd else None
    if kind == "terminal.command":
        exit_code = data.get("exit_code")
        payload: dict[str, Any] = {"exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None}
        if data.get("command"):
            payload["command"] = _clip(data["command"], TEXT_CHARS)
        if isinstance(data.get("seq"), int):
            payload["mark_seq"] = data["seq"]
        if isinstance(data.get("duration_ms"), int):
            payload["duration_ms"] = data["duration_ms"]
        return kind, payload
    if kind == "terminal.bell":
        return kind, {}
    if kind == "terminal.notify":
        title, body = str(data.get("title") or "").strip(), str(data.get("body") or "").strip()
        if not title:
            # OSC 9 carries one text and no title. A notification with an empty title shows as a
            # blank line on a lock screen, so the text becomes the title.
            title, body = body, ""
        if not title:
            return None
        return kind, {"title": _clip(title, TITLE_CHARS), "body": _clip(body, TEXT_CHARS)}
    if kind == "terminal.progress":
        number = data.get("state")
        state = PROGRESS_STATES.get(number) if isinstance(number, int) else None
        if state is None:
            return None
        progress: dict[str, Any] = {"state": state}
        value = data.get("value")
        if state in ("set", "error", "pause") and isinstance(value, int):
            progress["percent"] = max(0, min(100, value))
        return kind, progress
    return None


class BusBridge:
    """A subscriber of the terminals service that republishes its daemons' events on the bus.

    A title and a directory are published only when they changed; the daemon debounces them in
    time, not by value.
    """

    def __init__(self, terminals: Terminals) -> None:
        self.terminals = terminals
        self._last: OrderedDict[tuple[str, str], str] = OrderedDict()

    async def __call__(self, event: TerminalEvent) -> None:
        terminal_id = event.terminal_id
        if not terminal_id:
            return
        if event.type == "terminal.exited":
            self._forget(terminal_id)
            return
        mapped = translate(event)
        if mapped is None:
            return
        event_type, payload = mapped
        if event_type in ("terminal.bell", "terminal.progress") and _age_seconds(event.at) > MOMENT_SECONDS:
            return
        if event_type in ("terminal.title", "terminal.cwd"):
            key, value = (terminal_id, event_type), next(iter(payload.values()))
            if self._last.get(key) == value:
                return
            self._last[key] = value
            self._last.move_to_end(key)
            while len(self._last) > REMEMBERED * 2:
                self._last.popitem(last=False)
        await self.terminals.publish_event(terminal_id, event_type, payload)

    def _forget(self, terminal_id: str) -> None:
        for kind in ("terminal.title", "terminal.cwd"):
            self._last.pop((terminal_id, kind), None)


__all__ = ["BusBridge", "translate"]
