"""Provider balance monitor: poll, compare with thresholds, alert once per crossing."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


async def fetch_deepseek_balance(base_url: str, api_key: str) -> float | None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(base_url.rstrip("/") + "/user/balance", headers={"authorization": f"Bearer {api_key}"} if api_key else {})
    if response.status_code != 200:
        return None
    data = response.json()
    infos = data.get("balance_infos") or []
    for info in infos:
        if info.get("currency") == "USD":
            return float(info.get("total_balance") or 0.0)
    return float(infos[0]["total_balance"]) if infos else None


async def fetch_openrouter_balance(base_url: str, api_key: str) -> float | None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(base_url.rstrip("/") + "/credits", headers={"authorization": f"Bearer {api_key}"} if api_key else {})
    if response.status_code != 200:
        return None
    data = (response.json() or {}).get("data") or {}
    if "total_credits" in data and "total_usage" in data:
        return float(data["total_credits"]) - float(data["total_usage"])
    return None


class BalanceMonitor:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def current(self) -> dict[str, float | None]:
        """Balances of every configured deepseek/openrouter endpoint, through its own base URL and key
        (so a key proxy in front of the vendor is used the same way the model calls use it)."""
        out: dict[str, float | None] = {}
        manager = self.app.manager
        if manager is None:
            return out
        fetchers = {"deepseek": fetch_deepseek_balance, "openrouter": fetch_openrouter_balance}
        for provider_id in manager.providers.available():
            endpoint = manager.providers.get(provider_id).endpoint
            fetch = fetchers.get(endpoint.kind)
            if fetch is None:
                continue
            try:
                out[provider_id] = await fetch(endpoint.base_url, endpoint.api_key)
            except (httpx.HTTPError, ValueError):
                out[provider_id] = None
        return out

    async def loop(self) -> None:
        while True:
            config = self.app.config.balance
            if config.enabled:
                try:
                    await self.check_once()
                except Exception:  # noqa: BLE001
                    logger.exception("balance check failed")
            await asyncio.sleep(max(15, config.poll_seconds))

    async def check_once(self) -> None:
        balances = await self.current()
        db = self.app.db
        for provider, balance in balances.items():
            if balance is None:
                continue
            await db.kv_set(f"balance:{provider}", {"usd": balance, "at": datetime.now(UTC).isoformat()})
            for threshold in sorted(self.app.config.balance.thresholds_usd):
                key = f"{provider}:{threshold}"
                row = await db.fetchone("SELECT fired_at FROM balance_alerts WHERE threshold = ?", (key,))
                fired = row is not None and row["fired_at"] is not None
                if balance < threshold and not fired:
                    await db.execute(
                        "INSERT INTO balance_alerts(threshold, fired_at) VALUES (?, ?)"
                        " ON CONFLICT(threshold) DO UPDATE SET fired_at = excluded.fired_at",
                        (key, datetime.now(UTC).isoformat()),
                    )
                    if self.app.front is not None:
                        await self.app.front.notify(
                            f"💸 {provider} balance is ${balance:.2f}, below the ${threshold:.2f} threshold.",
                            markdown=False,
                        )
                elif balance >= threshold and fired:
                    await db.execute("UPDATE balance_alerts SET fired_at = NULL WHERE threshold = ?", (key,))


async def install(app: Application) -> list[asyncio.Task[None]]:
    monitor = BalanceMonitor(app)
    app.extensions["balance"] = monitor
    if app.front is not None:

        async def cmd_balance(message, command) -> None:  # type: ignore[no-untyped-def]
            balances = await monitor.current()
            if not balances:
                await message.answer("No provider with a balance endpoint is configured.")
                return
            await message.answer("\n".join(f"{p}: {f'${b:.2f}' if b is not None else 'unavailable'}" for p, b in balances.items()))

        app.front.command_hooks["balance"] = cmd_balance
    return [asyncio.create_task(monitor.loop(), name="balance-monitor")]


__all__ = ["BalanceMonitor", "install"]
