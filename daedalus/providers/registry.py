"""Build provider adapters from configuration."""

from __future__ import annotations

from collections.abc import Sequence
import httpx

from daedalus.config import ProviderConfig, RuntimeConfig, Settings
from daedalus.providers.openai_compat import (
    ImageLoader,
    OpenAICompatibleProvider,
    ProviderEndpoint,
    UsageSink,
)
from daedalus.providers.pricing import pricing_table


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
            )
        self._retired.extend(p for pid, p in self._providers.items() if pid not in fresh)
        self._providers = fresh

    async def close_retired(self) -> None:
        """Close adapters replaced by :meth:`reload` (their in-flight requests keep their client alive)."""
        retired, self._retired = self._retired, []
        for provider in retired:
            await provider.aclose()

    def _endpoint(self, provider_id: str, pc: ProviderConfig) -> ProviderEndpoint | None:
        api_key = ""
        base_url = pc.base_url
        headers: dict[str, str] = {}
        if pc.kind == "deepseek":
            api_key = self._settings.deepseek_api_key
        elif pc.kind == "openrouter":
            api_key = self._settings.openrouter_api_key
            headers = {"HTTP-Referer": "https://github.com/ascorblack/daedalus", "X-Title": "Daedalus"}
        elif pc.kind == "vllm":
            base_url = base_url or self._settings.vllm_base_url
            api_key = self._settings.vllm_api_key
        if not base_url:
            return None
        if pc.kind in ("deepseek", "openrouter") and not api_key:
            return None
        return ProviderEndpoint(
            id=provider_id,
            kind=pc.kind,
            base_url=base_url,
            api_key=api_key,
            default_model=pc.default_model,
            supports_images=pc.supports_images,
            supports_thinking=pc.supports_thinking,
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

    def rungs_for(self, config: RuntimeConfig) -> list[tuple[OpenAICompatibleProvider, str]]:
        """Resolve the configured chain into ``(provider, model)`` pairs."""
        rungs: list[tuple[OpenAICompatibleProvider, str]] = []
        primary = config.model.provider
        order = [primary, *[p for p in config.model.chain if p != primary]]
        for provider_id in order:
            provider = self._providers.get(provider_id)
            if provider is None:
                continue
            model = config.model.name if provider_id == primary else provider.endpoint.default_model
            if not model:
                continue
            rungs.append((provider, model))
        if not rungs:
            raise RuntimeError(
                "no usable provider: set an API key for at least one configured provider"
            )
        return rungs

    async def aclose(self) -> None:
        await self.close_retired()
        for provider in self._providers.values():
            await provider.aclose()


__all__ = ["ProviderRegistry"]
