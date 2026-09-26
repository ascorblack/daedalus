"""The browser: the browser daemons of this installation, reached through ``daedalus.browser``.

Installs the service and the agent's side of it (``manager.service_hooks["browser"]``, which the
browser tools and command-line staff's copies of them call), ties a session's browser to the
session's life, and tells an owner when the operator gives its browser back — into the session, or to
the staff member as a message.

Installed only where the installation has a browser daemon at all (``capabilities.browser``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from daedalus.browser.agent import BrowserAgent
from daedalus.browser.cli import TOOL_SET, StaffBrowser
from daedalus.browser.model import Owner
from daedalus.browser.owners import DatabaseOwners
from daedalus.browser.service import Browsers

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


def build(app: Application) -> Browsers:
    assert app.manager is not None
    manager = app.manager
    settings = app.settings

    async def wake(owner: Owner, text: str) -> None:
        """Tell the owner something without its asking: the one message a give-back is."""
        team = app.extensions.get("staff")
        staff_id = owner.staff_id if owner.kind == "session" else owner.id
        if staff_id and team is not None:
            member = await manager.staff.get(staff_id)
            if member is not None and await team.live_of(member) is not None:  # type: ignore[attr-defined]
                # A staff member hears it the way it hears its team: as a message into the turn.
                await team.tell(member, text, when="now", by="operator")  # type: ignore[attr-defined]
                return
        if owner.kind == "session" and owner.session_id:
            await manager.submit(owner.session_id, text, as_answer=False, origin="browser")

    return Browsers(
        app.db,
        run_dirs={"container": settings.browser_container_dir, "host": settings.browser_host_dir},
        config=lambda: app.config.browser,
        owners=DatabaseOwners(app.db),
        bus=manager.bus,
        wake=wake,
    )


async def install(app: Application) -> list[asyncio.Task[None]]:
    manager = app.manager
    assert manager is not None
    service = build(app)
    agent = BrowserAgent(service)
    app.extensions["browser"] = service
    app.extensions["browser_agent"] = agent
    manager.service_hooks["browser"] = agent

    async def session_deleted(session_id: str) -> None:
        await service.close_owned("session", session_id)

    # Command-line staff reach the same tools through their launch's MCP entry; every CLI runtime the
    # harness installed offers them from its next launch on.
    staff = StaffBrowser(agent, team=lambda: app.extensions.get("staff"), manager=manager)
    team = app.extensions.get("staff")
    for runtime in dict(getattr(team, "runtimes", None) or {}).values():
        sets = getattr(runtime, "tool_sets", None)
        if isinstance(sets, dict):
            sets[TOOL_SET] = staff

    manager.delete_hooks.append(session_deleted)
    tasks = await service.start()

    async def closer() -> None:
        # Cancelled at shutdown like every background task; the connections go with it, which leaves
        # the daemons and their browsers running for the next start to find.
        try:
            await asyncio.Event().wait()
        finally:
            await service.close()

    return [*tasks, asyncio.create_task(closer(), name="browser-close")]
