"""The app's way to a daemon's live channel: one-use tickets, the Origin rule, and the frame relay.

A browser cannot put a header on a WebSocket, so it first asks, authenticated the usual way, for a
ticket naming one target (a terminal, a browser group), and spends it on the socket within seconds.
The socket then carries the target's frames to the daemon's channel and back, unchanged: the host is
a relay. Terminals and browsers share all of that and differ only in which frames the app may send,
so each supplies its own frame check and this package does the rest.

Nothing here imports the web framework (only the API extensions may); the routes hand this package a
``FrameSocket`` and it does the rest.
"""

from __future__ import annotations

from daedalus.gateway.origin import allowed_origin
from daedalus.gateway.relay import (
    CLOSE_INTERNAL,
    CLOSE_NORMAL,
    CLOSE_ORIGIN,
    CLOSE_POLICY,
    CLOSE_SERVICE_RESTART,
    CLOSE_TICKET,
    CLOSE_TOO_BIG,
    CLOSE_UNAVAILABLE,
    CLOSE_UNKNOWN,
    CLOSE_UNSUPPORTED,
    Check,
    FrameSocket,
    Refused,
    RelayResult,
    SocketGone,
    close_reason,
    relay,
)
from daedalus.gateway.tickets import TICKETS_HELD, Ticket, TicketBook, ticket_who

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
    "TICKETS_HELD",
    "Check",
    "FrameSocket",
    "Refused",
    "RelayResult",
    "SocketGone",
    "Ticket",
    "TicketBook",
    "allowed_origin",
    "close_reason",
    "relay",
    "ticket_who",
]
