from __future__ import annotations

import json

import httpx
import pytest
from protocore.contracts.llm import LLMRateLimitError, LLMRequest, ProviderDeltaKind
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolDefinition, ToolParameterSchema

from daedalus.providers.openai_compat import (
    OpenAICompatibleProvider,
    ProviderEndpoint,
    UsageRecord,
    normalize_usage,
    parse_json_text,
)
from daedalus.providers.wire import messages_to_wire, parse_json_arguments


def _sse(chunks: list[dict]) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "usage": usage}


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, record: UsageRecord) -> None:
        self.records.append(record)


def _provider(body: str, status: int = 200, sink: RecordingSink | None = None) -> OpenAICompatibleProvider:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})

    endpoint = ProviderEndpoint(id="deepseek", kind="deepseek", base_url="https://x.test", api_key="k", supports_thinking=True)
    provider = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), usage_sink=sink)
    provider.captured = captured  # type: ignore[attr-defined]
    return provider


def _request(**extra) -> LLMRequest:
    return LLMRequest(
        model="deepseek-v4-flash",
        messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])],
        tools=[ToolDefinition(name="t", description="d", parameters=ToolParameterSchema(properties={"a": {"type": "string"}}, required=["a"]))],
        extra=extra,
    )


async def test_stream_orders_usage_before_finish_and_pairs_tool_calls() -> None:
    body = _sse(
        [
            _chunk({"role": "assistant", "reasoning_content": "think"}),
            _chunk({"content": "Hello"}),
            _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "t", "arguments": '{"a":'}}]}),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"b"}'}}]}),
            _chunk({}, finish="tool_calls"),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": 4, "completion_tokens_details": {"reasoning_tokens": 2}}},
        ]
    )
    sink = RecordingSink()
    provider = _provider(body, sink=sink)
    kinds = []
    deltas = []
    async for delta in provider.stream_with_tools(_request(enable_thinking=True, reasoning_effort="low")):
        kinds.append(delta.kind)
        deltas.append(delta)
    assert kinds == [
        ProviderDeltaKind.thinking,
        ProviderDeltaKind.text,
        ProviderDeltaKind.tool_use_start,
        ProviderDeltaKind.tool_use_input,
        ProviderDeltaKind.tool_use_input,
        ProviderDeltaKind.tool_use_stop,
        ProviderDeltaKind.usage,
        ProviderDeltaKind.finish,
    ]
    stop = deltas[5]
    assert stop.tool_call_id == "call_1" and stop.tool_input_final == {"a": "b"} and not stop.args_partial_truncated
    assert deltas[6].usage["input_tokens"] == 10 and deltas[6].usage["cache_read_tokens"] == 4
    assert deltas[7].finish_reason == "tool_use"
    assert sink.records[0].normalized["reasoning_tokens"] == 2
    sent = provider.captured["json"]  # type: ignore[attr-defined]
    assert sent["thinking"] == {"type": "enabled"} and sent["reasoning_effort"] == "low"
    assert sent["stream_options"] == {"include_usage": True}
    assert sent["tools"][0]["function"]["name"] == "t"


async def test_truncated_tool_call_is_flagged() -> None:
    body = _sse(
        [
            _chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "t", "arguments": '{"a": "unfinished'}}]}),
            _chunk({}, finish="length"),
        ]
    )
    provider = _provider(body)
    stops = [d async for d in provider.stream_with_tools(_request()) if d.kind is ProviderDeltaKind.tool_use_stop]
    assert stops[0].args_partial_truncated and stops[0].truncated_by_output_cap
    assert stops[0].tool_input_final == {"a": "unfinished"}


async def test_rate_limit_maps_to_core_error() -> None:
    provider = _provider('{"error": {"message": "slow down"}}', status=429)
    with pytest.raises(LLMRateLimitError):
        async for _ in provider.stream_with_tools(_request()):
            pass


async def test_thinking_disabled_sends_disabled_flag() -> None:
    provider = _provider(_sse([_chunk({"content": "x"}, finish="stop")]))
    async for _ in provider.stream_with_tools(_request(enable_thinking=False)):
        pass
    sent = provider.captured["json"]  # type: ignore[attr-defined]
    assert sent["thinking"] == {"type": "disabled"} and "reasoning_effort" not in sent


def test_parse_json_text_strips_fences() -> None:
    assert parse_json_text('Sure:\n```json\n{"ok": true}\n```') == {"ok": True}
    assert parse_json_text('prefix {"a": 1} suffix') == {"a": 1}
    assert parse_json_text("no json") is None


def test_parse_json_arguments_repairs_truncation() -> None:
    assert parse_json_arguments('{"a": [1, 2') == {"a": [1, 2]}
    assert parse_json_arguments("") == {}


def test_normalize_usage_openai_shape() -> None:
    n = normalize_usage({"prompt_tokens": 7, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 5}})
    assert n["input_tokens"] == 7 and n["cache_read_tokens"] == 5 and n["total_tokens"] == 10


async def test_messages_to_wire_roundtrips_tool_history() -> None:
    from protocore.contracts.types import ToolResultBlock, ToolUseBlock

    messages = [
        Message(role=MessageRole.system, content_blocks=[TextBlock(text="sys")]),
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="do it")]),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="t", arguments_json='{"a":"b"}')]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="done")]),
    ]
    wire = await messages_to_wire(messages)
    assert wire[0] == {"role": "system", "content": "sys"}
    assert wire[2]["tool_calls"][0]["id"] == "c1" and wire[2]["content"] is None
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": "done"}
