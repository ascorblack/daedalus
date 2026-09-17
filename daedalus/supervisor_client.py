"""Talk to the supervisor.

Two transports, because two platforms. A unix socket is a file with an owner and permissions, which
is the right shape for a channel that can restart the installation: nothing that cannot read the
file can ask. Windows has no unix sockets, so there the supervisor listens on the loopback interface
instead and the address is ``tcp://127.0.0.1:<port>``. Everything above this module passes
``Settings.supervisor_address`` and never has to know which it got.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

TCP_PREFIX = "tcp://"


class SupervisorUnavailable(RuntimeError):
    pass


def tcp_endpoint(address: str) -> tuple[str, int] | None:
    """The host and port of a ``tcp://`` address, or ``None`` when it is a socket path."""
    if not address.startswith(TCP_PREFIX):
        return None
    host, _, port = address[len(TCP_PREFIX) :].rpartition(":")
    if not host or not port.isdigit():
        raise SupervisorUnavailable(f"{address!r} is not a supervisor address: expected tcp://host:port")
    return host, int(port)


def present(address: str | Path) -> bool:
    """Whether the supervisor could be there at all — a cheap check for the doctor and the capability
    probe, both of which run on the way up and must not block on a connection."""
    text = str(address)
    try:
        endpoint = tcp_endpoint(text)
    except SupervisorUnavailable:
        return False
    if endpoint is not None:
        return True  # a port is either answering or not, and only a connection can say which
    return Path(text).exists()


async def _open(address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    endpoint = tcp_endpoint(address)
    if endpoint is None:
        if not Path(address).exists():
            raise SupervisorUnavailable(f"supervisor socket {address} does not exist")
        return await asyncio.open_unix_connection(address)
    host, port = endpoint
    return await asyncio.open_connection(host, port)


async def call(address: str | Path, op: str, *, timeout: float = 120.0, **params: Any) -> Any:
    try:
        reader, writer = await _open(str(address))
    except OSError as exc:
        raise SupervisorUnavailable(str(exc)) from exc
    writer.write((json.dumps({"op": op, **params}) + "\n").encode("utf-8"))
    await writer.drain()
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except TimeoutError as exc:
        writer.close()
        raise RuntimeError(f"supervisor did not answer within {timeout:.0f}s") from exc
    writer.close()
    response = json.loads(raw.decode("utf-8") or "{}")
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "supervisor error")
    return response.get("result")


__all__ = ["SupervisorUnavailable", "call", "present", "tcp_endpoint"]
