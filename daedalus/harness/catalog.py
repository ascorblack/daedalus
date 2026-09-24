"""What the installation knows about its command-line agents, for the hiring form, the orchestrator's
``Harnesses`` tool and the Harnesses screen.

This is the reading side: the capability table joined with the last check of each environment.
Checking, installing and updating write the rows through the same store.
"""

from __future__ import annotations

from typing import Any

from daedalus.harness import ADAPTERS
from daedalus.harness.capabilities import CAPABILITIES, Capabilities, capabilities, version_supported, version_tested
from daedalus.stores.harness import CatalogRow, HarnessStore


class HarnessCatalog:
    def __init__(self, store: HarnessStore) -> None:
        self.store = store

    def capabilities(self, harness: str) -> Capabilities:
        return capabilities(harness)

    async def harnesses(self, env: str) -> list[dict[str, Any]]:
        """One entry per command-line harness, checked or not, in the table's order.

        A harness never checked in ``env`` is listed as not installed rather than left out: the
        hiring form shows every executor and says why one cannot be chosen.
        """
        rows = {row.harness: row for row in await self.store.catalog_rows(env)}
        return [self._entry(env, caps, rows.get(name)) for name, caps in CAPABILITIES.items()]

    def _entry(self, env: str, caps: Capabilities, row: CatalogRow | None) -> dict[str, Any]:
        view: dict[str, Any] = row.view() if row is not None else {"env": env, "harness": caps.harness, "installed": False, "installed_version": "", "logged_in": "unknown", "agents": [], "models": [], "checked_at": None, "error": ""}
        version = view["installed_version"]
        view.update(
            label=caps.label,
            status_channel=caps.status_channel,
            status_channel_label=caps.status_channel_label,
            steer=caps.steer,
            tested_versions=list(caps.tested_versions),
            # Derived here, not stored: the tested range moves with the adapter's code.
            tested=bool(version) and version_tested(caps, version),
            supported=bool(version) and version_supported(caps, version),
            adapter=caps.harness in ADAPTERS,
        )
        return view


__all__ = ["HarnessCatalog"]
