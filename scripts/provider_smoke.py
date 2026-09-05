"""Live smoke check for a configured provider: streaming text, thinking, one tool call.

Usage: ``uv run python scripts/provider_smoke.py [provider_id] [model]``
"""

from __future__ import annotations

import asyncio
import json
import sys

from protocore.contracts.llm import LLMRequest, ProviderDeltaKind
from protocore.contracts.types import (
    Message,
    MessageRole,
    TextBlock,
    ToolDefinition,
    ToolParameterSchema,
)

from daedalus.config import RuntimeConfig, Settings
from daedalus.providers.openai_compat import UsageRecord
from daedalus.providers.registry import ProviderRegistry


class PrintSink:
    async def record(self, record: UsageRecord) -> None:
        print("\n[usage]", json.dumps(record.normalized), "raw:", json.dumps(record.raw))


async def main() -> None:
    settings = Settings()
    config = RuntimeConfig()
    provider_id = sys.argv[1] if len(sys.argv) > 1 else config.model.provider
    registry = ProviderRegistry(settings, config, usage_sink=PrintSink())
    provider = registry.get(provider_id)
    model = sys.argv[2] if len(sys.argv) > 2 else (config.model.name if provider_id == config.model.provider else provider.endpoint.default_model)
    tool = ToolDefinition(
        name="get_weather",
        description="Get the weather for a city.",
        parameters=ToolParameterSchema(
            properties={"city": {"type": "string"}}, required=["city"]
        ),
    )
    request = LLMRequest(
        model=model,
        messages=[
            Message(role=MessageRole.system, content_blocks=[TextBlock(text="Be brief.")]),
            Message(
                role=MessageRole.user,
                content_blocks=[TextBlock(text="What is the weather in Paris? Use the tool.")],
            ),
        ],
        tools=[tool],
        max_tokens=300,
        extra={"enable_thinking": True, "reasoning_effort": "low"},
    )
    kinds: list[str] = []
    async for delta in provider.stream_with_tools(request):
        kinds.append(delta.kind.value)
        if delta.kind is ProviderDeltaKind.thinking:
            print(f"\x1b[2m{delta.content}\x1b[0m", end="", flush=True)
        elif delta.kind is ProviderDeltaKind.text:
            print(delta.content, end="", flush=True)
        elif delta.kind is ProviderDeltaKind.tool_use_stop:
            print(f"\n[tool_use_stop] {delta.tool_name} {delta.tool_input_final}")
        elif delta.kind is ProviderDeltaKind.finish:
            print(f"\n[finish] {delta.finish_reason}")
    print("[kinds]", sorted(set(kinds)))
    structured = await provider.complete_structured(
        LLMRequest(
            model=model,
            messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text='Return JSON {"ok": true, "n": 3}')])],
            max_tokens=100,
        ),
        {},
    )
    print("[structured]", structured)
    await registry.aclose()


if __name__ == "__main__":
    asyncio.run(main())
