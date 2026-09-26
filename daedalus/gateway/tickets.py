"""Tickets: what an authenticated request hands a WebSocket that cannot carry the authentication."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TICKETS_HELD = 256


@dataclass(frozen=True, slots=True)
class Ticket:
    target: str
    """The one thing the ticket opens: a terminal's id, a browser group's id."""
    read_only: bool
    who: dict[str, Any]
    """How the person who asked for it signed in, from which address, with which browser: the
    audit's "who", captured where the authentication happened."""
    expires: float
    extra: dict[str, Any]
    """What the target's own gateway asked to carry from the request to the socket (the browser's
    tier and tab); nothing the relay itself reads."""


class TicketBook:
    """Tickets in memory: each opens one socket to one target once, within ``ttl`` seconds.

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

    def issue(self, target: str, read_only: bool, who: dict[str, Any], *, ttl: float | None = None, extra: dict[str, Any] | None = None) -> str:
        now = self.clock()
        for key in [k for k, t in self._tickets.items() if t.expires <= now]:
            del self._tickets[key]
        while len(self._tickets) >= self.max_tickets:
            del self._tickets[next(iter(self._tickets))]
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = Ticket(target, read_only, dict(who), now + (self.ttl if ttl is None else ttl), dict(extra or {}))
        return ticket

    def take(self, ticket: str, target: str) -> Ticket | None:
        """The ticket, spent; ``None`` when unknown, expired, or for another target.

        A ticket presented for the wrong target is spent all the same: whoever holds it is not
        using it as it was issued, and it must not be tried again elsewhere.
        """
        held = self._tickets.pop(ticket, None) if ticket else None
        if held is None or held.expires <= self.clock() or not secrets.compare_digest(held.target, target):
            return None
        return held


def ticket_who(via: str, user_agent: str, address: str) -> dict[str, Any]:
    """The audit's "who" as the ticket carries it: the sign-in method, the browser, the address."""
    return {"via": via, "user_agent": user_agent[:256], "address": address}


__all__ = ["TICKETS_HELD", "Ticket", "TicketBook", "ticket_who"]
