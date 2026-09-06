"""Build provider adapters from configuration."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx

from daedalus.config import ProviderConfig, RuntimeConfig, Settings
from daedalus.providers.openai_compat import (
    ImageLoader,
    OpenAICompatibleProvider,
    ProviderEndpoint,
    UsageSink,
)
from daedalus.providers.pricing import pricing_table

VENDOR_HOSTS = {"deepseek": "api.deepseek.com", "openrouter": "openrouter.ai"}


def _is_vendor_host(kind: str, base_url: str) -> bool:
    host = (urlsplit(base_url if "://" in base_url else "//" + base_url).hostname or "").lower()
    return host == VENDOR_HOSTS.get(kind, "")


class ProviderRegistry:
    """Configured endpoints → live adapters (one shared HTTP client each)."""

    def __init__(
        self,
        settings: Settings,
        config: RuntimeConfig,
        *,
        usage_sink: UsageSink | None = None,
        image_loader: ImageLoader | None = None,
    ) -> None:
        self._settings = settings
        self._usage_sink = usage_sink
        self._image_loader = image_loader
        self._providers: dict[str, OpenAICompatibleProvider] = {}
        self._retired: list[OpenAICompatibleProvider] = []
        self.reload(config)

    def _images_for(self, provider_id: str, model: str) -> bool:
        config = getattr(self, "_config", None)
        if config is None:
            return False
        return any(p.provider == provider_id and p.model == model and p.images for p in config.presets.values())

    def reload(self, config: RuntimeConfig) -> None:
        fresh: dict[str, OpenAICompatibleProvider] = {}
        for provider_id, pc in config.providers.items():
            endpoint = self._endpoint(provider_id, pc)
            if endpoint is None:
                continue
            existing = self._providers.get(provider_id)
            if existing is not None and existing.endpoint == endpoint:
                fresh[provider_id] = existing
                continue
            if existing is not None:
                self._retired.append(existing)
            fresh[provider_id] = OpenAICompatibleProvider(
                endpoint,
                client=httpx.AsyncClient(
                    timeout=httpx.Timeout(endpoint.timeout_seconds, connect=30.0)
                ),
                usage_sink=self._usage_sink,
                image_loader=self._image_loader,
                images_for=lambda model, pid=provider_id: self._images_for(pid, model),
            )
        self._config = config
        self._retired.extend(p for pid, p in self._providers.items() if pid not in fresh)
        self._providers = fresh

    async def close_retired(self) -> None:
        """Close adapters replaced by :meth:`reload` (their in-flight requests keep their client alive)."""
        retired, self._retired = self._retired, []
        for provider in retired:
            await provider.aclose()

    def _endpoint(self, provider_id: str, pc: ProviderConfig) -> ProviderEndpoint | None:
        base_url = pc.base_url
        headers: dict[str, str] = {}
        env_keys = {
            "deepseek": self._settings.deepseek_api_key,
            "openrouter": self._settings.openrouter_api_key,
            "vllm": self._settings.vllm_api_key,
        }
        if pc.kind == "openrouter":
            headers = {"HTTP-Referer": "https://github.com/ascorblack/daedalus", "X-Title": "Daedalus"}
        elif pc.kind == "vllm":
            # The environment pair is the deployment-time default; the operator may point
            # any of these at a different endpoint (base_url, api_key) in config.toml.
            base_url = base_url or self._settings.vllm_base_url
        # A per-endpoint key stored in config.toml wins; otherwise fall back to the
        # environment key of the built-in kinds. Self-hosted endpoints need no key at all.
        api_key = pc.api_key or env_keys.get(pc.kind, "")
        if not base_url:
            return None
        if pc.kind in ("deepseek", "openrouter") and not api_key and _is_vendor_host(pc.kind, base_url):
            return None  # the vendor itself needs a key; a key proxy in front of it does not
        return ProviderEndpoint(
            id=provider_id,
            kind=pc.kind,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=pc.timeout_seconds,
            extra_headers=headers,
            pricing=pricing_table(pc.kind, pc.pricing),
        )

    def get(self, provider_id: str) -> OpenAICompatibleProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(
                f"provider {provider_id!r} is not configured or has no API key; "
                f"available: {sorted(self._providers)}"
            ) from exc

    def available(self) -> Sequence[str]:
        return tuple(sorted(self._providers))

    def rungs_for(self, config: RuntimeConfig, preset_id: str | None = None) -> list[tuple[OpenAICompatibleProvider, str]]:
        """``(adapter, model)`` pairs: the chosen (or default) preset first, then the fallback chain."""
        first_id, first = config.preset(preset_id)
        order = [first_id, *[c for c in config.model.chain if c != first_id]]
        if config.model.preset not in order:
            order.append(config.model.preset)
        rungs: list[tuple[OpenAICompatibleProvider, str]] = []
        for pid in order:
            preset = config.presets.get(pid)
            provider = self._providers.get(preset.provider) if preset else None
            if preset is None or provider is None or not preset.model:
                continue
            rungs.append((provider, preset.model))
        if not rungs:
            raise RuntimeError("no usable model: every preset points at a client without a URL or key")
        return rungs

    def rungs_for_pair(self, config: RuntimeConfig, provider_id: str, model: str) -> list[tuple[OpenAICompatibleProvider, str]]:
        """An ad-hoc provider/model pair first (a manual ``/model vllm/x``), the chain behind it."""
        target = self._providers.get(provider_id)
        if target is None or not model:
            return self.rungs_for(config)
        rest = [(p, m) for p, m in self.rungs_for(config) if not (p.endpoint.id == provider_id and m == model)]
        return [(target, model), *rest]

    async def aclose(self) -> None:
        await self.close_retired()
        for provider in self._providers.values():
            await provider.aclose()


__all__ = ["ProviderRegistry"]
