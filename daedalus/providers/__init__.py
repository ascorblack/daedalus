"""LLM provider adapters (all OpenAI-compatible wire format)."""

from daedalus.providers.chain import ProviderChain, build_chain
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.registry import ProviderRegistry

__all__ = [
    "OpenAICompatibleProvider",
    "ProviderChain",
    "ProviderEndpoint",
    "ProviderRegistry",
    "build_chain",
]
