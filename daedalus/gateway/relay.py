"""The relay: a WebSocket from the app on one side, a daemon's channel on the other.

The host reads the first byte of what the app sends for three reasons only — to drop input on a
read-only socket, to count what was sent for the audit, and to refuse frames of the wrong size — and
never looks at what the daemon sends. Which frames are which is the target's business: a terminal
and a browser each hand the relay a ``Check``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from daedalus.terminals.client import Channel, Unavailable

logger = logging.getLogger(__name__)

# Close codes the app acts on (``miniapp/src/terminal/connection.ts`` and the browser viewer's). The
# 4xxx ones are final or retried as the app decides; 1012 means "the service is restarting, come back".
CLOSE_NORMAL = 1000
CLOSE_UNSUPPORTED = 1003
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_INTERNAL = 1011
CLOSE_SERVICE_RESTART = 1012
CLOSE_TICKET = 4401
CLOSE_ORIGIN = 4403
CLOSE_UNKNOWN = 4404
CLOSE_UNAVAILABLE = 4409


class SocketGone(Exception):
    """The app's socket cannot be written to any more."""


class FrameSocket(Protocol):
    """A WebSocket as the relay needs it; the API extension adapts the framework's to this."""

    async def receive(self) -> bytes | str | None:
        """The next message: bytes, text (which these protocols never use), or ``None`` once closed."""

    async def send(self, frame: bytes) -> None:
        """Raises ``SocketGone`` when the app is no longer there."""

    async def close(self, code: int, reason: str = "") -> None:
        """Close with a code; never raises."""


@dataclass(slots=True)
class RelayResult:
    """How a socket ended and what went through it: what the audit's detach row records."""

    code: int = CLOSE_NORMAL
    reason: str = ""
    ended_by: str = "browser"
    """``browser`` (the app's socket) · the target's word (the daemon closed the channel: ``terminal``,
    ``view``) · ``service`` (the daemon's connection went) · ``protocol`` (the app sent something the
    protocol does not have)"""
    bytes_typed: int = 0
    """A terminal's INPUT bytes passed on. How many, never which: a host terminal's typing includes
    the password given to every ``sudo``."""
    input_dropped: int = 0
    """Input bytes (a terminal) or input frames (a browser) dropped because the socket is read-only."""
    counts: dict[str, int] = field(default_factory=dict)
    """What a browser's person did, by kind (``mouse``, ``key``, ``text``, …): counted, never kept."""
    frames_in: int = 0
    frames_out: int = 0
    bytes_out: int = 0


class Refused(Exception):
    """A frame the app must not send; the socket is closed with ``code``."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


Check = Callable[[bytes, RelayResult], "bytes | None"]
"""The target's judgement of one frame from the app: the frame to forward (maybe rewritten), ``None``
to drop it, or ``Refused``. It counts what it passes or drops into the result itself."""


def close_reason(text: str) -> str:
    """A close reason fits in 123 bytes of UTF-8, or the close frame is invalid."""
    data = text.encode()
    return text if len(data) <= 123 else data[:123].decode("utf-8", "ignore")


async def relay(
    sock: FrameSocket,
    channel: Channel,
    *,
    check: Check,
    service_alive: Callable[[], bool],
    target: str = "terminal",
    closed_text: str = "the terminal closed this attachment",
    gone_text: str = "the terminal service went away",
) -> RelayResult:
    """Carry frames both ways until one side ends, then end the other; returns how it went.

    Two pumps, and neither buffers: the daemon's flow control (the app acknowledges what it drew or
    parsed, the daemon waits for it) runs end to end, so what waits in the host is at most one window.
    The socket is closed here with the code the app acts on, and the channel with its empty frame.
    """
    result = RelayResult()

    def channel_ended() -> tuple[int, str, str]:
        if service_alive():
            return CLOSE_NORMAL, closed_text, target
        return CLOSE_SERVICE_RESTART, gone_text, "service"

    async def app_to_daemon() -> tuple[int, str, str]:
        while True:
            message = await sock.receive()
            if message is None:
                return CLOSE_NORMAL, "", "browser"
            if isinstance(message, str):
                return CLOSE_UNSUPPORTED, "frames are binary", "protocol"
            result.frames_in += 1
            if not message:
                return CLOSE_POLICY, "an empty frame", "protocol"
            try:
                forward = check(message, result)
            except Refused as refused:
                return refused.code, refused.reason, "protocol"
            if forward is None:
                continue
            try:
                await channel.send(forward)
            except Unavailable:
                return channel_ended()

    async def daemon_to_app() -> tuple[int, str, str]:
        while (payload := await channel.recv()) is not None:
            try:
                await sock.send(payload)
            except SocketGone:
                return CLOSE_NORMAL, "", "browser"
            result.frames_out += 1
            result.bytes_out += len(payload)
        return channel_ended()

    up = asyncio.create_task(app_to_daemon(), name=f"{target}-relay-up")
    down = asyncio.create_task(daemon_to_app(), name=f"{target}-relay-down")
    try:
        done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        # When both finished in the same turn, the daemon's side is the truer account of why.
        first = down if down in done else up
        try:
            result.code, result.reason, result.ended_by = first.result()
        except Exception:  # noqa: BLE001 — a fault in one pump still closes both ends cleanly
            logger.exception("%s relay failed", target)
            result.code, result.reason, result.ended_by = CLOSE_INTERNAL, "the relay failed", "service"
    finally:
        for task in (up, down):
            task.cancel()
        await asyncio.gather(up, down, return_exceptions=True)
        await channel.close()
        if result.ended_by != "browser":
            await sock.close(result.code, close_reason(result.reason))
    return result


__all__ = [
    "CLOSE_INTERNAL",
    "CLOSE_NORMAL",
    "CLOSE_ORIGIN",
    "CLOSE_POLICY",
    "CLOSE_SERVICE_RESTART",
    "CLOSE_TICKET",
    "CLOSE_TOO_BIG",
    "CLOSE_UNAVAILABLE",
    "CLOSE_UNKNOWN",
    "CLOSE_UNSUPPORTED",
    "Check",
    "FrameSocket",
    "Refused",
    "RelayResult",
    "SocketGone",
    "close_reason",
    "relay",
]
