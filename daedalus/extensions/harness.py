"""Command-line agents: what is installed where, and what each one can do, as ``app.extensions["harness"]``.

The staff lifecycle is the staff extension's; this one holds what is particular to the CLIs
themselves, so the hiring form and the orchestrator ask one place whether a harness can be chosen.
It also puts one staff runtime per CLI that has an adapter into the team's ``runtimes``, and takes up
the CLIs a previous host left running.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from daedalus.harness import ADAPTERS
from daedalus.harness.catalog import HarnessCatalog
from daedalus.harness.runtime import CliStaffRuntime, install_runtimes
from daedalus.stores.harness import HarnessStore

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.extensions.staff import Team
    from daedalus.terminals.service import Terminals

logger = logging.getLogger(__name__)


async def install(app: Application) -> list[asyncio.Task[None]]:
    store = HarnessStore(app.db)
    app.extensions["harness"] = HarnessCatalog(store)
    team = cast("Team | None", app.extensions.get("staff"))
    terminals = cast("Terminals | None", app.extensions.get("terminals"))
    if team is None or terminals is None:
        # Without the team or the terminals there is nothing to run a CLI for or in; command-line
        # members are then refused at assignment with the runtime's absence as the reason.
        return []
    runtimes = install_runtimes(
        ADAPTERS,
        team.runtimes,
        terminals=terminals,
        store=store,
        ingress=team.ingress,
        lookup=team.live,
        config=lambda: app.config.harness,
    )
    if not runtimes:
        return []

    async def take_up(runtime: CliStaffRuntime) -> None:
        try:
            count = await runtime.reconcile()
        except Exception:  # noqa: BLE001 — one CLI's reconcile failing leaves the others to theirs
            logger.exception("the %s sessions left by the previous host were not taken up", runtime.kind)
            return
        if count:
            logger.info("%d %s staff session(s) taken up from the previous host", count, runtime.kind)

    async def keeper() -> None:
        # Cancelled at shutdown like every background task; the CLIs keep running in their daemons.
        try:
            await asyncio.Event().wait()
        finally:
            for runtime in runtimes:
                runtime.close()

    return [asyncio.create_task(take_up(r), name=f"harness-reconcile-{r.kind}") for r in runtimes] + [asyncio.create_task(keeper(), name="harness-runtimes")]
