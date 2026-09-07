"""Optional subsystems attached to the running application.

Each extension exposes ``async def install(app) -> list[asyncio.Task]``; the tasks
are background loops the application cancels on shutdown.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

EXTENSIONS = (
    "daedalus.extensions.inbox",
    "daedalus.extensions.selfdev",
    "daedalus.extensions.scheduler",
    "daedalus.extensions.heartbeat",
    "daedalus.extensions.learning",
    "daedalus.extensions.inbound",
    "daedalus.extensions.board",
    "daedalus.extensions.peers",
    "daedalus.extensions.subagents",
    "daedalus.extensions.balance",
    "daedalus.extensions.api",
)


async def install_all(app: Application) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []
    for name in EXTENSIONS:
        try:
            module = importlib.import_module(name)
        except ImportError:
            logger.exception("extension %s failed to import", name)
            continue
        tasks.extend(await module.install(app))
    return tasks


__all__ = ["EXTENSIONS", "install_all"]
