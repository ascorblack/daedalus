"""The browser's way to a terminal: one-use tickets, the Origin rule, and the frame relay.

A browser cannot put a header on a WebSocket, so it first asks, authenticated the usual way, for a
ticket naming one terminal, and spends it on the socket within seconds. The socket then carries the
browser frames of ``docs/architecture/terminals.md`` to the daemon's attachment channel and back,
unchanged: the host is a relay, not a terminal. It reads the first byte of what the browser sends for
three reasons only — to drop typing on a read-only attachment, to count what was typed for the audit,
and to refuse frames of the wrong size — and never looks at what the daemon sends.

Nothing here imports the web framework (only the API extension may); the routes hand this module a
``FrameSocket`` and it does the rest.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from daedalus.terminals import wire
from daedalus.terminals.client import Channel, Unavailable
from daedalus.terminals.model import EnvUnavailable, NotFound, TerminalError

if TYPE_CHECKING:
    from daedalus.terminals.service import Attachment, Terminals

logger = logging.getLogger(__name__)

TERMINAL_WS_MAX_BYTES = 1 << 20
"""The largest WebSocket message the server takes. The largest frame a browser legitimately sends is
32 KiB of typing; the daemon's frames to the browser are bounded by its own 1 MiB framing."""

MAX_INPUT_BYTES = 32 << 10
MAX_ATTACH_JSON_BYTES = 4 << 10
FIXED_SIZES = {wire.RESIZE: 9, wire.ACK: 9}
"""Frames whose length is their definition, type byte included."""

TICKETS_HELD = 256
AUDITED_ENVS = frozenset({"host"})
"""Where attaching and detaching are written to the audit: a terminal on the operator's own machine,
which is always marked and always accounted for. A container terminal's attachments are ordinary
use of a sandboxed shell and would only bury the host's entries."""

# Close codes the app acts on (``miniapp/src/terminal/connection.ts``). The 4xxx ones are final or
# retried as the app decides; 1012 means "the terminal service is restarting, come back".
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


# -- tickets -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ticket:
    terminal_id: str
    read_only: bool
    who: dict[str, Any]
    """How the person who asked for it signed in, from which address, with which browser: the
    audit's "who", captured where the authentication happened."""
    expires: float


class TicketBook:
    """Tickets in memory: each opens one socket to one terminal once, within ``ttl`` seconds.

    In memory on purpose — a restart of the host drops every socket anyway, and a ticket that
    outlived its process would be a credential lying in the database. The book holds at most
    ``max_tickets``; past that the oldest goes, so a client asking in a loop cannot grow it.
    """

    def __init__(self, ttl: float = 30.0, max_tickets: int = TICKETS_HELD, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.max_tickets = max_tickets
        self.clock = clock
        self._tickets: dict[str, Ticket] = {}

    def __len__(self) -> int:
        return len(self._tickets)

    def issue(self, terminal_id: str, read_only: bool, who: dict[str, Any], *, ttl: float | None = None) -> str:
        now = self.clock()
        for key in [k for k, t in self._tickets.items() if t.expires <= now]:
            del self._tickets[key]
        while len(self._tickets) >= self.max_tickets:
            del self._tickets[next(iter(self._tickets))]
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = Ticket(terminal_id, read_only, dict(who), now + (self.ttl if ttl is None else ttl))
        return ticket

    def take(self, ticket: str, terminal_id: str) -> Ticket | None:
        """The ticket, spent; ``None`` when unknown, expired, or for another terminal.

        A ticket presented for the wrong terminal is spent all the same: whoever holds it is not
        using it as it was issued, and it must not be tried again elsewhere.
        """
        held = self._tickets.pop(ticket, None) if ticket else None
        if held is None or held.expires <= self.clock() or not secrets.compare_digest(held.terminal_id, terminal_id):
            return None
        return held


# -- the Origin rule --------------------------------------------------------------------------

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _authority(scheme: str, host: str, port: int | None) -> str:
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"  # an IPv6 literal, bracketed as it is written in an origin
    return host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"


def _origin(url: str) -> tuple[str, str] | None:
    """``(scheme, authority)`` of an http(s) URL with its default port left out; ``None`` otherwise."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    return scheme, _authority(scheme, parts.hostname, port)


def allowed_origin(origin: str | None, host_header: str | None, public_url: str) -> bool:
    """Whether a WebSocket from ``origin`` is the app's own.

    The same origin as the ``Host`` it was sent to (the app opened on the machine, or through a proxy
    that keeps ``Host``), or exactly the origin of ``MINIAPP_PUBLIC_URL`` (a proxy that rewrites
    ``Host``, and Telegram's webview, which sends the Mini App's origin). A missing Origin, or
    ``null`` (a sandboxed frame, a file), is refused: every browser sends one on a WebSocket, so its
    absence means something that is not the app.

    The ticket is what proves the operator; this check is what stops a page elsewhere from using a
    signed-in browser's cookie to fetch one and open a terminal with it.
    """
    if not origin or origin.strip().lower() == "null":
        return False
    parsed = _origin(origin)
    if parsed is None:
        return False
    scheme, authority = parsed
    if host_header:
        host = _origin(f"{scheme}://{host_header.strip()}")
        if host is not None and host[1] == authority:
            return True
    public = _origin(public_url) if public_url else None
    return public is not None and public == parsed


# -- the relay ---------------------------------------------------------------------------------


class SocketGone(Exception):
    """The browser's socket cannot be written to any more."""


class FrameSocket(Protocol):
    """A WebSocket as the relay needs it; the API extension adapts the framework's to this."""

    async def receive(self) -> bytes | str | None:
        """The next message: bytes, text (which this protocol never uses), or ``None`` once closed."""

    async def send(self, frame: bytes) -> None:
        """Raises ``SocketGone`` when the browser is no longer there."""

    async def close(self, code: int, reason: str = "") -> None:
        """Close with a code; never raises."""


@dataclass(slots=True)
class RelayResult:
    """How an attachment ended and what went through it: what the audit's detach row records."""

    code: int = CLOSE_NORMAL
    reason: str = ""
    ended_by: str = "browser"
    """``browser`` · ``terminal`` (the daemon closed the channel) · ``service`` (the daemon's
    connection went) · ``protocol`` (the browser sent something this protocol does not have)"""
    bytes_typed: int = 0
    """INPUT bytes passed to the terminal. How many, never which: a host terminal's typing includes
    the password given to every ``sudo``."""
    input_dropped: int = 0
    """INPUT bytes dropped because the attachment is read-only."""
    frames_in: int = 0
    frames_out: int = 0
    bytes_out: int = 0


class _Refused(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _check(frame: bytes, *, read_only: bool) -> bytes | None:
    """The frame to forward, ``None`` to drop it, or ``_Refused`` for one the browser must not send."""
    if not frame:
        raise _Refused(CLOSE_POLICY, "an empty frame")
    kind = frame[0]
    if kind == wire.INPUT:
        if len(frame) - 1 > MAX_INPUT_BYTES:
            raise _Refused(CLOSE_TOO_BIG, f"INPUT carries at most {MAX_INPUT_BYTES} bytes")
        return None if read_only else frame
    if kind in FIXED_SIZES:
        if len(frame) != FIXED_SIZES[kind]:
            raise _Refused(CLOSE_POLICY, f"frame 0x{kind:02x} is {FIXED_SIZES[kind]} bytes")
        if kind == wire.ACK:
            try:
                wire.decode_browser(frame)
            except wire.FrameError as exc:
                raise _Refused(CLOSE_POLICY, str(exc)) from None
        return frame
    if kind == wire.ATTACH:
        if len(frame) - 1 > MAX_ATTACH_JSON_BYTES:
            raise _Refused(CLOSE_TOO_BIG, f"ATTACH carries at most {MAX_ATTACH_JSON_BYTES} bytes")
        try:
            decoded = wire.decode_browser(frame)
        except wire.FrameError as exc:
            raise _Refused(CLOSE_POLICY, f"ATTACH: {exc}") from None
        if read_only and decoded.json is not None and decoded.json.get("readOnly") is not True:
            # The daemon already has the client as read-only; saying it in the frame as well keeps a
            # browser from believing, even for a moment, that it may type.
            return wire.encode_browser(wire.BrowserFrame("attach", json={**decoded.json, "readOnly": True}))
        return frame
    raise _Refused(CLOSE_POLICY, f"frame type 0x{kind:02x} is not one a browser sends")


def _close_reason(text: str) -> str:
    """A close reason fits in 123 bytes of UTF-8, or the close frame is invalid."""
    data = text.encode()
    return text if len(data) <= 123 else data[:123].decode("utf-8", "ignore")


async def relay(sock: FrameSocket, channel: Channel, *, read_only: bool, service_alive: Callable[[], bool]) -> RelayResult:
    """Carry frames both ways until one side ends, then end the other; returns how it went.

    Two pumps, and neither buffers: the daemon's flow control (the browser ACKs what it parsed, the
    daemon stops at its window) runs end to end, so what waits in the host is at most one window.
    The socket is closed here with the code the app acts on, and the channel with its empty frame.
    """
    result = RelayResult()

    def channel_ended() -> tuple[int, str, str]:
        if service_alive():
            return CLOSE_NORMAL, "the terminal closed this attachment", "terminal"
        return CLOSE_SERVICE_RESTART, "the terminal service went away", "service"

    async def browser_to_terminal() -> tuple[int, str, str]:
        while True:
            message = await sock.receive()
            if message is None:
                return CLOSE_NORMAL, "", "browser"
            if isinstance(message, str):
                return CLOSE_UNSUPPORTED, "frames are binary", "protocol"
            result.frames_in += 1
            try:
                forward = _check(message, read_only=read_only)
            except _Refused as refused:
                return refused.code, refused.reason, "protocol"
            if message[0] == wire.INPUT:
                if forward is None:
                    result.input_dropped += len(message) - 1
                    continue
                result.bytes_typed += len(message) - 1
            assert forward is not None
            try:
                await channel.send(forward)
            except Unavailable:
                return channel_ended()

    async def terminal_to_browser() -> tuple[int, str, str]:
        while (payload := await channel.recv()) is not None:
            try:
                await sock.send(payload)
            except SocketGone:
                return CLOSE_NORMAL, "", "browser"
            result.frames_out += 1
            result.bytes_out += len(payload)
        return channel_ended()

    up = asyncio.create_task(browser_to_terminal(), name="terminal-relay-up")
    down = asyncio.create_task(terminal_to_browser(), name="terminal-relay-down")
    try:
        done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        # When both finished in the same turn, the terminal's side is the truer account of why.
        first = down if down in done else up
        try:
            result.code, result.reason, result.ended_by = first.result()
        except Exception:  # noqa: BLE001 — a fault in one pump still closes both ends cleanly
            logger.exception("terminal relay failed")
            result.code, result.reason, result.ended_by = CLOSE_INTERNAL, "the relay failed", "service"
    finally:
        for task in (up, down):
            task.cancel()
        await asyncio.gather(up, down, return_exceptions=True)
        await channel.close()
        if result.ended_by != "browser":
            await sock.close(result.code, _close_reason(result.reason))
    return result


# -- the gateway: what the routes call -----------------------------------------------------------


@dataclass(slots=True)
class Gateway:
    """Tickets and sockets for one application. ``service`` is looked up per call, because the
    terminals extension may be missing (not installed, failed to start) and the routes must say so."""

    service: Callable[[], Terminals | None]
    public_url: Callable[[], str]
    ticket_ttl: Callable[[], float]
    tickets: TicketBook = field(default_factory=TicketBook)

    async def issue(self, terminal_id: str, *, read_only: bool, who: dict[str, Any]) -> dict[str, Any]:
        """A ticket for a terminal that exists and whose environment answers; raises the refusal."""
        service = self.service()
        if service is None:
            raise EnvUnavailable("the terminals subsystem is not running")
        await service.attachable(terminal_id)
        ttl = float(self.ticket_ttl())
        return {"ticket": self.tickets.issue(terminal_id, read_only, who, ttl=ttl), "expires_in": int(ttl)}

    async def serve(self, sock: FrameSocket, terminal_id: str, *, ticket: str, origin: str | None, host: str | None, address: str) -> RelayResult | None:
        """One accepted socket from its first check to its close; ``None`` when it was refused.

        The socket is accepted before it is judged: a WebSocket refused during the handshake reaches
        the browser as a bare 1006, and the app needs the code to know whether to try again.
        """
        if not allowed_origin(origin, host, self.public_url()):
            await sock.close(CLOSE_ORIGIN, "this origin may not open terminals")
            return None
        held = self.tickets.take(ticket, terminal_id)
        if held is None:
            await sock.close(CLOSE_TICKET, "the ticket is unknown, spent or expired")
            return None
        service = self.service()
        if service is None:
            await sock.close(CLOSE_UNAVAILABLE, "the terminals subsystem is not running")
            return None
        via = str(held.who.get("via") or "")
        try:
            attachment = await service.attach(terminal_id, read_only=held.read_only, label=str(held.who.get("user_agent") or "")[:256], via=via)
        except NotFound:
            await sock.close(CLOSE_UNKNOWN, "no such terminal")
            return None
        except EnvUnavailable as exc:
            await sock.close(CLOSE_UNAVAILABLE, _close_reason(exc.message))
            return None
        except TerminalError as exc:
            await sock.close(CLOSE_INTERNAL, _close_reason(exc.message))
            return None
        started = time.monotonic()
        await self._audit(service, attachment, "attach", {**held.who, "socket_address": address, "read_only": held.read_only, "client_id": attachment.client_id})
        result = RelayResult(code=CLOSE_INTERNAL, reason="the relay was interrupted", ended_by="service")
        try:
            result = await relay(sock, attachment.channel, read_only=held.read_only, service_alive=lambda: attachment.service_alive)
        finally:
            await self._audit(
                service,
                attachment,
                "detach",
                {
                    "client_id": attachment.client_id,
                    "bytes_typed": result.bytes_typed,
                    "input_dropped": result.input_dropped,
                    "bytes_out": result.bytes_out,
                    "seconds": round(time.monotonic() - started, 1),
                    "ended_by": result.ended_by,
                    "code": result.code,
                },
            )
        return result

    @staticmethod
    async def _audit(service: Terminals, attachment: Attachment, action: str, detail: dict[str, Any]) -> None:
        if attachment.env not in AUDITED_ENVS:
            return
        try:
            await service.audit(attachment.terminal_id, attachment.env, "operator", action, detail)
        except Exception:  # noqa: BLE001 — a failed audit write must not end or strand a person's terminal
            logger.exception("could not write the %s of terminal %s to the audit", action, attachment.terminal_id)


def ticket_who(via: str, user_agent: str, address: str) -> dict[str, Any]:
    """The audit's "who" as the ticket carries it: the sign-in method, the browser, the address."""
    return {"via": via, "user_agent": user_agent[:256], "address": address}


__all__ = [
    "AUDITED_ENVS",
    "TERMINAL_WS_MAX_BYTES",
    "FrameSocket",
    "Gateway",
    "RelayResult",
    "SocketGone",
    "Ticket",
    "TicketBook",
    "allowed_origin",
    "relay",
    "ticket_who",
]
