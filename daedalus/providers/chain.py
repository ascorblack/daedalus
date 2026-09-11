"""Provider fallback chain — the core's ``IProviderChain`` over configured endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence

from protocore.contracts.llm import ILLMProvider, IProviderChain

from daedalus.providers.openai_compat import OpenAICompatibleProvider

Rung = tuple[OpenAICompatibleProvider, str]
Room = tuple[int, int]
"""A rung's ``(context_window, max_output_tokens)``: what it can hold and what a request on it asks for."""


class ProviderChain(IProviderChain):
    """A one-way cursor over ``(provider, model)`` rungs.

    Every demotion is recorded with its reason: ``attempted()`` answers
    *why* each rung was ruled out, not only *that* it was — a fallback that
    cannot be explained after the fact is a receipt without a cause.

    A rung is only a fallback if it can hold the conversation: one whose window
    is smaller than the prompt the run is carrying plus its own output cap is
    skipped with that reason, or the demotion trades a stalled stream for a
    context-window error and the run ends on the fallback instead of retrying
    where it was.
    """

    def __init__(
        self,
        rungs: Sequence[Rung],
        *,
        room: Mapping[tuple[str, str], Room] | None = None,
        prompt_tokens: Callable[[], int] | None = None,
    ) -> None:
        if not rungs:
            raise ValueError("a provider chain needs at least one rung")
        self._rungs = list(rungs)
        self._room = dict(room or {})
        self._prompt_tokens = prompt_tokens or (lambda: 0)
        self._index = 0
        self._ruled_out: list[tuple[str, str]] = []
        self._advance_lock = asyncio.Lock()

    def bind_prompt_size(self, prompt_tokens: Callable[[], int]) -> None:
        """Where the current prompt size is read from (the engine, once it exists)."""
        self._prompt_tokens = prompt_tokens

    def _fits(self, index: int) -> str | None:
        """``None`` when the rung can hold the prompt, else why it cannot."""
        provider, model = self._rungs[index]
        room = self._room.get((provider.endpoint.id, model))
        if room is None:
            return None
        window, output_cap = room
        prompt = int(self._prompt_tokens() or 0)
        if window and prompt and prompt + output_cap > window:
            return f"context window {window} cannot hold the prompt ({prompt} tokens) and the reply ({output_cap})"
        return None

    def _label(self, index: int) -> str:
        provider, model = self._rungs[index]
        return f"{provider.endpoint.id}:{model}"

    def current(self) -> ILLMProvider:
        return self._rungs[self._index][0]

    def current_model_name(self) -> str:
        return self._rungs[self._index][1]

    async def advance(self, *, reason: str) -> bool:
        # The check-and-advance is one transition. The contract makes advance
        # async precisely because a rung may be materialised (with awaits)
        # when the run reaches it; concurrent failures must not interleave
        # inside the transition and demote past each other.
        async with self._advance_lock:
            skipped: list[tuple[str, str]] = []
            candidate = self._index + 1
            while candidate < len(self._rungs):
                too_small = self._fits(candidate)
                if too_small is None:
                    self._ruled_out.append((self._label(self._index), reason))
                    self._ruled_out.extend(skipped)
                    self._index = candidate
                    return True
                skipped.append((self._label(candidate), too_small))
                candidate += 1
            # Exhausted: the current rung stays current and nothing is recorded, as before —
            # the loop's next move (a retry in place, a wind-down) is not a demotion.
            return False

    def attempted(self) -> Sequence[tuple[str, str]]:
        """``(provider_name, reason)`` for every rung already ruled out, in order."""
        return tuple(self._ruled_out)

    @property
    def rungs(self) -> Sequence[tuple[OpenAICompatibleProvider, str]]:
        return tuple(self._rungs)


def build_chain(
    rungs: Sequence[Rung],
    *,
    room: Mapping[tuple[str, str], Room] | None = None,
) -> ProviderChain | None:
    return ProviderChain(rungs, room=room) if len(rungs) > 1 else None


__all__ = ["ProviderChain", "Room", "Rung", "build_chain"]
