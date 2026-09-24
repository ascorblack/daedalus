"""Command-line agents: what is installed where, and what each one can do, as ``app.extensions["harness"]``.

The staff lifecycle is the staff extension's; this one holds what is particular to the CLIs
themselves — the harness manager — so the hiring form and the orchestrator ask one place whether a
harness can be chosen, and the Harnesses screen installs, updates and checks them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from daedalus.harness.manager import HarnessManager
from daedalus.harness.ports import ServiceEnvironmentPort, TerminalRunner
from daedalus.stores.harness import HarnessStore

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.harness.contract import EnvironmentPort
    from daedalus.terminals.service import Terminals

logger = logging.getLogger(__name__)

CATALOG_ROOTS = (".claude/agents", ".codex", ".pi/agent")
"""What of each environment's home the manager reads, besides the project folders: the user's own
agent definitions, Codex's profiles, pi's settings (to learn its provider). The daemon's deny list
keeps every credential file under them unreadable all the same."""


def build(app: Application) -> HarnessManager:
    manager = app.manager
    assert manager is not None

    def terminals() -> Terminals | None:
        found: Any = app.extensions.get("terminals")
        return found  # type: ignore[no-any-return]

    def port(env: str) -> EnvironmentPort | None:
        service = terminals()
        if service is None:
            return None
        status = next((s for s in service.environments() if s.env == env), None)
        if status is None or not status.available:
            return None
        if status.home:
            service.set_extra_roots(env, "harness-catalog", [f"{status.home.rstrip('/')}/{path}" for path in CATALOG_ROOTS])
        return ServiceEnvironmentPort(service, env, home=status.home)  # type: ignore[arg-type]

    def environments() -> list[str]:
        service = terminals()
        return [s.env for s in service.environments() if s.available] if service is not None else []

    async def folder(folder_id: str) -> tuple[str, str] | None:
        found = await manager.projects.folder_by_id(folder_id)
        return (found.env, str(found.path)) if found is not None else None

    service = terminals()
    return HarnessManager(
        HarnessStore(app.db),
        ports=port,
        environments=environments,
        config=lambda: app.config.harness,
        live_staff=manager.staff.live_by_harness,
        folder=folder,
        runner=TerminalRunner(service) if service is not None else None,
        bus=manager.bus,
    )


async def install(app: Application) -> list[asyncio.Task[None]]:
    harness = build(app)
    app.extensions["harness"] = harness

    async def closer() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await harness.close()

    return [asyncio.create_task(harness.run(), name="harness-check"), asyncio.create_task(closer(), name="harness-close")]
