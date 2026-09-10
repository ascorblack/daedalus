"""Current list prices from models.dev for gateways that publish none of their own.

OpenCode Go and Zen answer ``/models`` with ids only; models.dev (the catalogue OpenCode itself reads) carries
each model's USD per 1M tokens for input, output and cached input. The table is fetched once a day, kept in the
database between starts, and overlaid under the operator's own ``pricing`` entries, which always win.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from daedalus.providers.pricing import ModelPricing

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
SOURCES: dict[str, tuple[str, ...]] = {"opencode": ("opencode-go", "opencode")}
"""Provider kind → the models.dev provider ids whose prices apply, first one winning on a shared model id (a Go
key is billed at Go's prices; a model only Zen offers is priced pay-as-you-go)."""
REFRESH_SECONDS = 24 * 3600
KV_KEY = "modelsdev_prices"


def prices_from_catalog(catalog: dict[str, Any], provider_ids: tuple[str, ...]) -> dict[str, ModelPricing]:
    """The pricing table one kind gets from a models.dev catalogue."""
    table: dict[str, ModelPricing] = {}
    for provider_id in reversed(provider_ids):
        models = (catalog.get(provider_id) or {}).get("models") or {}
        for model_id, model in models.items():
            cost = model.get("cost") if isinstance(model, dict) else None
            if not isinstance(cost, dict):
                continue
            table[str(model_id)] = ModelPricing(
                input=float(cost.get("input") or 0.0),
                output=float(cost.get("output") or 0.0),
                cache_hit=float(cost.get("cache_read") or 0.0),
            )
    return table


def entries_from_table(table: dict[str, ModelPricing]) -> dict[str, dict[str, float]]:
    return {model: {"input": p.input, "output": p.output, "cache_hit": p.cache_hit} for model, p in table.items()}


def table_from_entries(entries: dict[str, dict[str, Any]]) -> dict[str, ModelPricing]:
    return {model: ModelPricing.from_entry(entry) for model, entry in entries.items() if isinstance(entry, dict)}


async def fetch_catalog(client: httpx.AsyncClient | None = None, *, timeout: float = 15.0) -> dict[str, Any]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.get(MODELS_DEV_URL, headers={"accept": "application/json"})
        response.raise_for_status()
        data = response.json()
    finally:
        if own:
            await client.aclose()
    if not isinstance(data, dict):
        raise ValueError("models.dev catalogue is not an object")
    return data


__all__ = ["KV_KEY", "MODELS_DEV_URL", "REFRESH_SECONDS", "SOURCES", "entries_from_table", "fetch_catalog", "prices_from_catalog", "table_from_entries"]
