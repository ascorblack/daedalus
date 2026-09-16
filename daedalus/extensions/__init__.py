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
    "daedalus.extensions.loops",
    "daedalus.extensions.services",
    "daedalus.extensions.balance",
    "daedalus.extensions.voice",
    "daedalus.extensions.api",
)


def enabled(app: Application) -> tuple[str, ...]:
    """The extensions this installation actually runs.

    Self-development is the one subsystem an installation may not have at all: with the mode off
    there is no worktree to open, no proposal to decide and no rebuild to ask for, so the extension
    is not installed and nothing it would have registered exists.
    """
    mode = app.manager.capabilities.selfdev.mode if app.manager is not None else "off"
    if mode == "off":
        return tuple(name for name in EXTENSIONS if name != "daedalus.extensions.selfdev")
    return EXTENSIONS


async def install_all(app: Application) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []
    for name in enabled(app):
        try:
            module = importlib.import_module(name)
        except ImportError:
            logger.exception("extension %s failed to import", name)
            continue
        tasks.extend(await module.install(app))
    return tasks


__all__ = ["EXTENSIONS", "enabled", "install_all"]
