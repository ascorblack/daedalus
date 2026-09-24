"""Command-line agents: what is installed where, and what each one can do, as ``app.extensions["harness"]``.

The staff lifecycle is the staff extension's; this one holds what is particular to the CLIs
themselves, so the hiring form and the orchestrator ask one place whether a harness can be chosen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from daedalus.harness.catalog import HarnessCatalog
from daedalus.stores.harness import HarnessStore

if TYPE_CHECKING:
    import asyncio

    from daedalus.app import Application


async def install(app: Application) -> list[asyncio.Task[None]]:
    app.extensions["harness"] = HarnessCatalog(HarnessStore(app.db))
    return []
