"""Talk to the supervisor.

Two transports, because two platforms. A unix socket is a file with an owner and permissions, which
is the right shape for a channel that can restart the installation: nothing that cannot read the
file can ask. Windows has no unix sockets, so there the supervisor listens on the loopback interface
instead and the address is ``tcp://127.0.0.1:<port>``. Everything above this module passes
``Settings.supervisor_address`` and never has to know which it got.

A port, unlike a file, has no owner: anything running on the machine can connect to it. So a command
sent that way carries a secret the supervisor wrote into the state directory with the permissions the
socket would have had, and a command sent to a socket carries nothing — the file is the answer there.
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


def loopback_token(token_path: str | Path | None) -> str:
    """The secret for a loopback channel, read per call so that a supervisor which minted a new one
    does not need the bot restarted as well. Missing or unreadable is an empty string: the supervisor
    says what is wrong with a command that carries none, and says it better than a traceback here."""
    if token_path is None:
        return ""
    try:
        return Path(token_path).read_text("utf-8").strip()
    except OSError:
        return ""


async def call(address: str | Path, op: str, *, token_path: str | Path | None = None, timeout: float = 120.0, **params: Any) -> Any:
    try:
        reader, writer = await _open(str(address))
    except OSError as exc:
        raise SupervisorUnavailable(str(exc)) from exc
    if tcp_endpoint(str(address)) is not None and (token := loopback_token(token_path)):
        params["token"] = token
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


__all__ = ["SupervisorUnavailable", "call", "loopback_token", "present", "tcp_endpoint"]
