"""OpenAI-compatible ``ILLMProvider`` adapter.

One adapter serves every configured endpoint (DeepSeek, OpenRouter, a self-hosted
vLLM, any other ``chat/completions`` server). Vendor differences are confined to
:class:`ProviderEndpoint` flags: how thinking is switched on, which field carries
the reasoning stream, and how usage is shaped.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from protocore.contracts.llm import (
    ILLMProvider,
    LLMContextWindowExceeded,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMResponseUsage,
    LLMTimeoutError,
    ProviderDelta,
    ProviderDeltaKind,
)
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

from daedalus.providers.wire import messages_to_wire, parse_json_arguments, tools_to_wire

ImageLoader = Callable[[str], Awaitable[tuple[bytes, str]]]

_CONTEXT_ERROR_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
    "exceeds the model",
)


@dataclass(slots=True)
class ModelPricing:
    """USD per one million tokens; optional off-peak rates apply inside ``off_peak_utc`` (HH:MM-HH:MM)."""

    input: float = 0.0
    output: float = 0.0
    cache_hit: float = 0.0
    input_off_peak: float | None = None
    output_off_peak: float | None = None
    cache_hit_off_peak: float | None = None
    off_peak_utc: str = ""

    def _off_peak_now(self) -> bool:
        if not self.off_peak_utc or "-" not in self.off_peak_utc:
            return False
        from datetime import UTC, datetime

        start_s, end_s = self.off_peak_utc.split("-", 1)
        now = datetime.now(UTC)
        minutes = now.hour * 60 + now.minute
        start = int(start_s[:2]) * 60 + int(start_s[3:5])
        end = int(end_s[:2]) * 60 + int(end_s[3:5])
        return start <= minutes < end if start <= end else minutes >= start or minutes < end

    def cost(self, usage: dict[str, Any]) -> float:
        cache_hit = int(usage.get("cache_read_tokens") or 0)
        prompt = int(usage.get("input_tokens") or 0)
        fresh = max(prompt - cache_hit, 0)
        output = int(usage.get("output_tokens") or 0)
        off = self._off_peak_now()
        p_in = self.input_off_peak if off and self.input_off_peak is not None else self.input
        p_out = self.output_off_peak if off and self.output_off_peak is not None else self.output
        p_hit = self.cache_hit_off_peak if off and self.cache_hit_off_peak is not None else self.cache_hit
        return (fresh * p_in + cache_hit * p_hit + output * p_out) / 1_000_000


@dataclass(slots=True)
class ProviderEndpoint:
    id: str
    kind: str
    base_url: str
    api_key: str = ""
    default_model: str = ""
    supports_images: bool = False
    supports_thinking: bool = False
    timeout_seconds: float = 600.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    pricing: dict[str, ModelPricing] = field(default_factory=dict)

    def pricing_for(self, model: str) -> ModelPricing | None:
        if model in self.pricing:
            return self.pricing[model]
        for key, value in self.pricing.items():
            if model.startswith(key):
                return value
        return None


@dataclass(slots=True)
class UsageRecord:
    provider_id: str
    model: str
    purpose: str
    raw: dict[str, Any]
    normalized: dict[str, Any]
    cost_usd: float | None
    duration_ms: int
    run_id: str | None
    session_id: str | None


class UsageSink(Protocol):
    async def record(self, record: UsageRecord) -> None: ...


class OpenAICompatibleProvider(ILLMProvider):
    """Streams ``chat/completions`` SSE into core :class:`ProviderDelta` envelopes."""

    def __init__(
        self,
        endpoint: ProviderEndpoint,
        *,
        client: httpx.AsyncClient | None = None,
        usage_sink: UsageSink | None = None,
        image_loader: ImageLoader | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(endpoint.timeout_seconds, connect=30.0)
        )
        self._usage_sink = usage_sink
        self._image_loader = image_loader

    # -- ILLMProvider -----------------------------------------------------------

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        """Yield deltas in the order the core requires: content, tool closes, usage, finish.

        The core stops reading at ``finish``, so the stream is drained to its end
        (usage arrives in a trailing chunk) before ``usage`` and ``finish`` are yielded.
        """
        body = await self._build_body(request, stream=True)
        started = time.monotonic()
        usage_raw: dict[str, Any] | None = None
        open_tools: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        try:
            async with self._client.stream(
                "POST", self._url("/chat/completions"), json=body, headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    raw = await response.aread()
                    self._raise_for_status(response.status_code, raw.decode("utf-8", "replace"))
                async for data in _sse_data(response):
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if "error" in chunk and not chunk.get("choices"):
                        self._raise_for_status(500, json.dumps(chunk["error"]))
                    if chunk.get("usage"):
                        usage_raw = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            yield ProviderDelta(kind=ProviderDeltaKind.thinking, content=reasoning)
                        content = delta.get("content")
                        if content:
                            yield ProviderDelta(kind=ProviderDeltaKind.text, content=content)
                        for tc in delta.get("tool_calls") or []:
                            for out in self._tool_call_chunk(open_tools, tc, started):
                                yield out
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"{self.endpoint.id}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"{self.endpoint.id}: transport error: {exc}") from exc
        resolved_finish = finish_reason or ("tool_calls" if open_tools else "stop")
        for delta_out in self._close_tools(open_tools, resolved_finish):
            yield delta_out
        normalized = normalize_usage(usage_raw or {})
        await self._record_usage(request, "stream", usage_raw or {}, normalized, started)
        yield ProviderDelta(kind=ProviderDeltaKind.usage, usage=normalized)
        yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason=_map_finish(resolved_finish))

    def _tool_call_chunk(
        self, open_tools: dict[int, dict[str, Any]], tc: dict[str, Any], started: float
    ) -> list[ProviderDelta]:
        out: list[ProviderDelta] = []
        index = int(tc.get("index", len(open_tools)))
        fn = tc.get("function") or {}
        if index not in open_tools:
            open_tools[index] = {
                "id": tc.get("id") or f"call_{index}_{int(started * 1000)}",
                "name": fn.get("name") or "",
                "args": "",
                "started": False,
            }
        slot = open_tools[index]
        if tc.get("id"):
            slot["id"] = tc["id"]
        if fn.get("name"):
            slot["name"] = fn["name"]
        if not slot["started"] and slot["name"]:
            slot["started"] = True
            out.append(
                ProviderDelta(
                    kind=ProviderDeltaKind.tool_use_start,
                    tool_call_id=slot["id"],
                    tool_name=slot["name"],
                )
            )
        args_piece = fn.get("arguments") or ""
        if args_piece:
            slot["args"] += args_piece
            if slot["started"]:
                out.append(
                    ProviderDelta(
                        kind=ProviderDeltaKind.tool_use_input,
                        tool_call_id=slot["id"],
                        tool_input_delta=args_piece,
                    )
                )
        return out

    async def complete_structured(
        self, request: LLMRequest, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        body = await self._build_body(request, stream=False)
        body["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        data = await self._post(body)
        text = _message_text(data)
        usage_raw = data.get("usage") or {}
        await self._record_usage(request, "structured", usage_raw, normalize_usage(usage_raw), started)
        parsed = parse_json_text(text)
        if parsed is None:
            raise LLMProviderError(f"{self.endpoint.id}: structured response is not JSON")
        return parsed

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        body = await self._build_body(request, stream=False)
        started = time.monotonic()
        data = await self._post(body)
        text = _message_text(data)
        usage_raw = data.get("usage") or {}
        normalized = normalize_usage(usage_raw)
        cost = await self._record_usage(request, "text", usage_raw, normalized, started)
        finish = (data.get("choices") or [{}])[0].get("finish_reason") or "stop"
        return LLMResponse(
            message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=text)]),
            stop_reason=StopReason.max_tokens if finish == "length" else StopReason.end_turn,
            usage=LLMResponseUsage(
                input_tokens=normalized["input_tokens"],
                output_tokens=normalized["output_tokens"],
                cache_read_tokens=normalized["cache_read_tokens"],
                cached_tokens=normalized["cache_read_tokens"],
                response_cost_usd=cost,
            ),
        )

    def count_tokens(self, text: str, model: str | None = None) -> int:
        # A cheap estimate; the loop re-anchors on the provider's reported prompt size.
        return max(1, len(text) // 3)

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals ----------------------------------------------------------------

    async def _build_body(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        extra = dict(request.extra or {})
        model = request.model or self.endpoint.default_model
        body: dict[str, Any] = {
            "model": model,
            "messages": await messages_to_wire(
                request.messages,
                image_loader=self._image_loader,
                supports_images=self.endpoint.supports_images,
            ),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if request.tools:
            body["tools"] = tools_to_wire(request.tools)
            forced = extra.get("forced_tool_choice")
            if forced:
                body["tool_choice"] = {"type": "function", "function": {"name": forced}}
            elif isinstance(extra.get("tool_choice"), dict):
                body["tool_choice"] = extra["tool_choice"]
        thinking = bool(extra.get("enable_thinking", False))
        effort = str(extra.get("reasoning_effort") or "medium")
        self._apply_thinking(body, thinking=thinking, effort=effort)
        return body

    def _apply_thinking(self, body: dict[str, Any], *, thinking: bool, effort: str) -> None:
        kind = self.endpoint.kind
        if not self.endpoint.supports_thinking:
            return
        if kind == "deepseek":
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
            if thinking:
                body["reasoning_effort"] = effort
        elif kind == "openrouter":
            body["reasoning"] = {"effort": effort} if thinking else {"enabled": False}
            body["usage"] = {"include": True}
        elif kind == "vllm":
            body["chat_template_kwargs"] = {"enable_thinking": thinking}
        else:
            if thinking:
                body["reasoning_effort"] = effort

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._url("/chat/completions"), json=body, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"{self.endpoint.id}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"{self.endpoint.id}: transport error: {exc}") from exc
        if response.status_code >= 400:
            self._raise_for_status(response.status_code, response.text)
        data = response.json()
        if "error" in data and not data.get("choices"):
            self._raise_for_status(500, json.dumps(data["error"]))
        return data

    def _raise_for_status(self, status: int, text: str) -> None:
        lowered = text.lower()
        if status == 429:
            raise LLMRateLimitError(f"{self.endpoint.id}: rate limited: {text[:300]}")
        if status in (400, 413, 422) and any(m in lowered for m in _CONTEXT_ERROR_MARKERS):
            raise LLMContextWindowExceeded(f"{self.endpoint.id}: {text[:300]}")
        if status in (408, 504):
            raise LLMTimeoutError(f"{self.endpoint.id}: HTTP {status}: {text[:300]}")
        raise LLMProviderError(f"{self.endpoint.id}: HTTP {status}: {text[:500]}")

    def _url(self, path: str) -> str:
        return self.endpoint.base_url.rstrip("/") + path

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json", **self.endpoint.extra_headers}
        if self.endpoint.api_key:
            headers["authorization"] = f"Bearer {self.endpoint.api_key}"
        return headers

    def _close_tools(
        self, open_tools: dict[int, dict[str, Any]], finish_reason: str
    ) -> list[ProviderDelta]:
        out: list[ProviderDelta] = []
        for index in sorted(open_tools):
            slot = open_tools[index]
            if not slot["started"]:
                slot["started"] = True
                out.append(
                    ProviderDelta(
                        kind=ProviderDeltaKind.tool_use_start,
                        tool_call_id=slot["id"],
                        tool_name=slot["name"] or "unknown",
                    )
                )
            raw_args = slot["args"]
            try:
                json.loads(raw_args or "{}")
                partial = False
            except json.JSONDecodeError:
                partial = True
            out.append(
                ProviderDelta(
                    kind=ProviderDeltaKind.tool_use_stop,
                    tool_call_id=slot["id"],
                    tool_name=slot["name"] or "unknown",
                    tool_input_final=parse_json_arguments(raw_args),
                    is_block_end=True,
                    truncated_by_output_cap=partial and finish_reason == "length",
                    args_partial_truncated=partial,
                )
            )
        open_tools.clear()
        return out

    async def _record_usage(
        self,
        request: LLMRequest,
        purpose: str,
        raw: dict[str, Any],
        normalized: dict[str, Any],
        started: float,
    ) -> float | None:
        model = request.model or self.endpoint.default_model
        cost: float | None = None
        if raw.get("cost") is not None:
            cost = float(raw["cost"])
        else:
            pricing = self.endpoint.pricing_for(model)
            if pricing is not None:
                cost = pricing.cost(normalized)
        normalized["cost_usd"] = cost
        if self._usage_sink is not None:
            obs = request.observability
            await self._usage_sink.record(
                UsageRecord(
                    provider_id=self.endpoint.id,
                    model=model,
                    purpose=purpose,
                    raw=raw,
                    normalized=normalized,
                    cost_usd=cost,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    run_id=obs.run_id if obs else None,
                    session_id=obs.session_id if obs else None,
                )
            )
        return cost


def normalize_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Map every OpenAI-compatible usage shape onto the keys the core reads."""
    prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
    completion = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
    details = raw.get("prompt_tokens_details") or {}
    cache_hit = int(
        raw.get("prompt_cache_hit_tokens")
        or details.get("cached_tokens")
        or raw.get("cache_read_input_tokens")
        or 0
    )
    completion_details = raw.get("completion_tokens_details") or {}
    reasoning = int(completion_details.get("reasoning_tokens") or 0)
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "cache_read_tokens": cache_hit,
        "cache_read_input_tokens": cache_hit,
        "cache_creation_input_tokens": int(raw.get("cache_creation_input_tokens") or 0),
        "reasoning_tokens": reasoning,
        "total_tokens": int(raw.get("total_tokens") or prompt + completion),
    }


def _map_finish(reason: str | None) -> Any:
    mapping = {
        "stop": "stop",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "tool_use": "tool_use",
        "length": "length",
        "content_filter": "content_filter",
    }
    return mapping.get(reason or "stop", "stop")


def _message_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_text(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from model output, stripping code fences and prose."""
    candidates = [text.strip()]
    candidates.extend(m.group(1).strip() for m in _FENCE_RE.finditer(text))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


async def _sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield the ``data:`` payload of each SSE event."""
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
    if buffer:
        yield "\n".join(buffer)


__all__ = [
    "ImageLoader",
    "ModelPricing",
    "OpenAICompatibleProvider",
    "ProviderEndpoint",
    "UsageRecord",
    "UsageSink",
    "normalize_usage",
    "parse_json_text",
]
