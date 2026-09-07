"""Provider fallback chain — the core's ``IProviderChain`` over configured endpoints."""

from __future__ import annotations

from collections.abc import Sequence

from protocore.contracts.llm import ILLMProvider, IProviderChain

from daedalus.providers.openai_compat import OpenAICompatibleProvider


class ProviderChain(IProviderChain):
    """A one-way cursor over ``(provider, model)`` rungs.

    Every demotion is recorded with its reason: ``attempted()`` answers
    *why* each rung was ruled out, not only *that* it was — a fallback that
    cannot be explained after the fact is a receipt without a cause.
    """

    def __init__(self, rungs: Sequence[tuple[OpenAICompatibleProvider, str]]) -> None:
        if not rungs:
            raise ValueError("a provider chain needs at least one rung")
        self._rungs = list(rungs)
        self._index = 0
        self._ruled_out: list[tuple[str, str]] = []

    def _label(self, index: int) -> str:
        provider, model = self._rungs[index]
        return f"{provider.endpoint.id}:{model}"

    def current(self) -> ILLMProvider:
        return self._rungs[self._index][0]

    def current_model_name(self) -> str:
        return self._rungs[self._index][1]

    async def advance(self, *, reason: str) -> bool:
        if self._index + 1 >= len(self._rungs):
            return False
        self._ruled_out.append((self._label(self._index), reason))
        self._index += 1
        return True

    def attempted(self) -> Sequence[tuple[str, str]]:
        """``(provider_name, reason)`` for every rung already ruled out, in order."""
        return tuple(self._ruled_out)

    @property
    def rungs(self) -> Sequence[tuple[OpenAICompatibleProvider, str]]:
        return tuple(self._rungs)


def build_chain(
    rungs: Sequence[tuple[OpenAICompatibleProvider, str]],
) -> ProviderChain | None:
    return ProviderChain(rungs) if len(rungs) > 1 else None


__all__ = ["ProviderChain", "build_chain"]
