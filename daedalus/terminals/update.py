"""Updating the container's terminal daemon, which only the operator does, knowing what it ends.

The ``terminals`` compose service runs ``ptyd`` from the same image as this container, but it is never
recreated by a deploy: that is the point of it being a service apart, since recreating it ends every
container terminal. So after an image rebuild the two can differ, and the host is the one that can
tell — this container's image carries the new daemon at ``/usr/local/bin/ptyd``, the running service
answers ``daemon.info`` with the old one's version.

The update itself is the rebuilder's: it is the only container that reaches Docker, and it recreates
the service from the image already built when a request appears in the trigger directory it shares
with this container (``deploy/rebuild.sh``). Without a rebuilder, the operator runs the one command
the refusal names.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from daedalus.host.capabilities import REBUILDER_HEARTBEAT, REBUILDER_HEARTBEAT_SECONDS

IMAGE_BINARY = Path("/usr/local/bin/ptyd")
"""Where the image puts the daemon (``deploy/Dockerfile``). Absent natively and in a checkout."""

REQUEST_FILE = "terminals-request"
"""The rebuilder takes this file, recreates the service and writes ``terminals-<job>.result``."""

BY_HAND = "docker compose -f deploy/compose.yaml --env-file .env up -d terminals"
"""What the operator runs on the server when no rebuilder is there to do it."""


async def daemon_version(binary: Path, *, timeout: float = 5.0) -> str:
    """The version ``binary version`` reports (``ptyd <version> (protocol <n>)``), or "" when there is
    no such binary or it does not answer: then nothing is offered, which is the safe reading."""
    if not binary.is_file():
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary), "version", stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    words = out.decode("utf-8", "replace").split()
    if proc.returncode != 0 or len(words) < 2 or words[0] != "ptyd":
        return ""
    return words[1]


class DaemonUpdate:
    """The container environment's daemon as the image holds it, and the way to ask for it."""

    def __init__(self, trigger_dir: Path | None, *, binary: Path = IMAGE_BINARY, clock: Callable[[], float] = time.time) -> None:
        self.trigger_dir = trigger_dir
        self.binary = binary
        self.image_version = ""
        self._clock = clock

    async def probe(self) -> str:
        """Read the image's version once. The image does not change under a running container."""
        self.image_version = await daemon_version(self.binary)
        return self.image_version

    def rebuilder_alive(self) -> bool:
        """Whether the rebuilder is on the other end of the trigger directory: a request written
        where nothing reads it would leave the operator waiting for an update that never comes."""
        if self.trigger_dir is None:
            return False
        try:
            age = self._clock() - (self.trigger_dir / REBUILDER_HEARTBEAT).stat().st_mtime
        except OSError:
            return False
        return age <= REBUILDER_HEARTBEAT_SECONDS

    def request(self) -> str:
        """Leave a request for the rebuilder and return its job id. Written under another name and
        renamed, so the rebuilder never reads half an id."""
        assert self.trigger_dir is not None
        job = secrets.token_hex(16)
        pending = self.trigger_dir / f"{REQUEST_FILE}.pending"
        pending.write_text(job + "\n", encoding="utf-8")
        os.replace(pending, self.trigger_dir / REQUEST_FILE)
        return job

    def result(self, job: str) -> dict[str, str]:
        """``pending`` until the rebuilder has written its result, then ``completed`` or ``failed``
        with its line."""
        if self.trigger_dir is None:
            return {"state": "pending", "detail": ""}
        try:
            line = (self.trigger_dir / f"terminals-{job}.result").read_text(encoding="utf-8").strip()
        except OSError:
            return {"state": "pending", "detail": ""}
        return {"state": "completed" if line == "completed" else "failed", "detail": "" if line == "completed" else line}
