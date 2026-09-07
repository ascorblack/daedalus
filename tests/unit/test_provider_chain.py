"""Provider fallback chain: one-way cursor and the reason behind every demotion."""

from __future__ import annotations

import httpx
import pytest

from daedalus.providers.chain import ProviderChain, build_chain
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint


def _provider(endpoint_id: str) -> OpenAICompatibleProvider:
    endpoint = ProviderEndpoint(id=endpoint_id, kind=endpoint_id, base_url="https://x.test", api_key="k")
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))


async def test_advance_records_rung_and_reason_in_order() -> None:
    chain = ProviderChain([(_provider("a"), "m1"), (_provider("b"), "m2"), (_provider("c"), "m3")])
    assert chain.current_model_name() == "m1"
    assert chain.attempted() == ()  # nothing ruled out yet
    assert await chain.advance(reason="rate limit") is True
    assert chain.current_model_name() == "m2"
    assert await chain.advance(reason="5xx") is True
    assert chain.current_model_name() == "m3"
    # a fallback must be explainable after the fact: (rung, reason) in order
    assert chain.attempted() == (("a:m1", "rate limit"), ("b:m2", "5xx"))
    assert await chain.advance(reason="no more rungs") is False
    assert chain.attempted() == (("a:m1", "rate limit"), ("b:m2", "5xx"))


def test_attempted_shape_matches_core_contract() -> None:
    chain = ProviderChain([(_provider("a"), "m1"), (_provider("b"), "m2")])
    pairs = chain.attempted()
    assert isinstance(pairs, tuple)
    for item in pairs:
        assert isinstance(item, tuple) and len(item) == 2 and all(isinstance(x, str) for x in item)


def test_build_chain_needs_two_rungs() -> None:
    assert build_chain([(_provider("a"), "m1")]) is None
    assert build_chain([(_provider("a"), "m1"), (_provider("b"), "m2")]) is not None


def test_empty_chain_is_rejected() -> None:
    with pytest.raises(ValueError):
        ProviderChain([])
