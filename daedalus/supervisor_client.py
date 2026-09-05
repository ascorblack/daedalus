"""Talk to the supervisor over its unix socket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class SupervisorUnavailable(RuntimeError):
    pass


async def call(socket_path: Path, op: str, **params: Any) -> Any:
    if not socket_path.exists():
        raise SupervisorUnavailable(f"supervisor socket {socket_path} does not exist")
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except OSError as exc:
        raise SupervisorUnavailable(str(exc)) from exc
    writer.write((json.dumps({"op": op, **params}) + "\n").encode("utf-8"))
    await writer.drain()
    raw = await reader.readline()
    writer.close()
    response = json.loads(raw.decode("utf-8") or "{}")
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "supervisor error")
    return response.get("result")


__all__ = ["SupervisorUnavailable", "call"]
