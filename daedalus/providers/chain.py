"""Provider fallback chain — the core's ``IProviderChain`` over configured endpoints."""

from __future__ import annotations

from collections.abc import Sequence

from protocore.contracts.llm import ILLMProvider, IProviderChain

from daedalus.providers.openai_compat import OpenAICompatibleProvider


class ProviderChain(IProviderChain):
    """A one-way cursor over ``(provider, model)`` rungs."""

    def __init__(self, rungs: Sequence[tuple[OpenAICompatibleProvider, str]]) -> None:
        if not rungs:
            raise ValueError("a provider chain needs at least one rung")
        self._rungs = list(rungs)
        self._index = 0
        self._attempted: list[str] = [self._label(0)]

    def _label(self, index: int) -> str:
        provider, model = self._rungs[index]
        return f"{provider.endpoint.id}:{model}"

    def current(self) -> ILLMProvider:
        return self._rungs[self._index][0]

    def current_model_name(self) -> str:
        return self._rungs[self._index][1]

    async def advance(self, reason: str) -> bool:
        if self._index + 1 >= len(self._rungs):
            return False
        self._index += 1
        self._attempted.append(self._label(self._index))
        return True

    def attempted(self) -> Sequence[str]:
        return tuple(self._attempted)

    @property
    def rungs(self) -> Sequence[tuple[OpenAICompatibleProvider, str]]:
        return tuple(self._rungs)


def build_chain(
    rungs: Sequence[tuple[OpenAICompatibleProvider, str]],
) -> ProviderChain | None:
    return ProviderChain(rungs) if len(rungs) > 1 else None


__all__ = ["ProviderChain", "build_chain"]
