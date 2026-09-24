"""Terminals: the terminal daemons of this installation, reached through ``daedalus.terminals``.

Installs the service, ties terminals to the lives of their owners — a session or a project that is
deleted ends its terminals — retells what the daemons report on the event bus, and hands a session's
agent read access to its own ones.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from daedalus.terminals.bus import BusBridge
from daedalus.terminals.owners import ManagerOwners
from daedalus.terminals.service import Terminals

if TYPE_CHECKING:
    from daedalus.app import Application


def build(app: Application) -> Terminals:
    assert app.manager is not None
    settings = app.settings
    return Terminals(
        app.db,
        run_dirs={"container": settings.terminals_container_dir, "host": settings.terminals_host_dir},
        config=lambda: app.config.terminals,
        owners=ManagerOwners(app.manager),
        bus=app.manager.bus,
        public_host=settings.services_public_host.strip(),
        # A server in a container terminal is published on the terminals service's own range. One in
        # a host terminal is on the machine itself, on whatever port it picked.
        port_ranges={"container": settings.terminals_port_range, "host": ""},
    )


async def install(app: Application) -> list[asyncio.Task[None]]:
    manager = app.manager
    assert manager is not None
    terminals = build(app)
    app.extensions["terminals"] = terminals

    async def session_deleted(session_id: str) -> None:
        await terminals.close_owned("session", session_id)

    async def project_deleted(project_id: str) -> None:
        await terminals.close_owned("project", project_id)

    manager.delete_hooks.append(session_deleted)
    manager.project_delete_hooks.append(project_deleted)
    manager.service_hooks["terminals"] = terminals.agent_service
    # Before the connections start, so the events replayed on the first one are retold as well.
    terminals.subscribe(BusBridge(terminals))
    tasks = await terminals.start()

    async def closer() -> None:
        # Cancelled at shutdown like every background task; the connections go with it, which leaves
        # the daemons and their terminals running for the next start to find.
        try:
            await asyncio.Event().wait()
        finally:
            await terminals.close()

    return [*tasks, asyncio.create_task(closer(), name="terminals-close")]
