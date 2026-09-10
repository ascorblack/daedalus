"""OpenAI-compatible ``ILLMProvider`` adapter.

One adapter serves every configured endpoint (DeepSeek, OpenRouter, a self-hosted
vLLM, any other ``chat/completions`` server). Vendor differences are confined to
:class:`ProviderEndpoint` flags: how thinking is switched on, which field carries
the reasoning stream, and how usage is shaped.
"""

from __future__ import annotations

import json
import logging
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

from daedalus import __version__
from daedalus.providers.dsml import DsmlGuard
from daedalus.providers.pricing import ModelPricing
from daedalus.providers.wire import messages_to_wire, parse_json_arguments, tools_to_wire

logger = logging.getLogger(__name__)

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


def message_shape(messages: list[dict[str, Any]]) -> str:
    """One token per wire message — role, and for an assistant turn whether it has text, tool calls and
    reasoning — so a rejected request can be read from the log without its content."""
    parts = []
    for m in messages:
        role = str(m.get("role"))[0]
        if m.get("role") == "assistant":
            role += ("t" if m.get("content") else "") + ("c" if m.get("tool_calls") else "") + ("r" if m.get("reasoning_content") else ("R" if "reasoning_content" in m else ""))
        parts.append(role)
    return " ".join(parts)


DEEPSEEK_SHAPED = ("deepseek", "opencode")
"""Endpoints that speak DeepSeek's thinking dialect: the ``thinking`` object, ``reasoning_effort`` in DeepSeek's
names, ``reasoning_content`` on every assistant turn of a tool-call round."""

DEEPSEEK_EFFORTS = {"minimal": "low", "low": "low", "medium": "high", "high": "high", "xhigh": "max"}
"""DeepSeek knows ``low``, ``high`` and ``max`` and treats anything else as ``high``: a preset that asks for
``medium`` means the model's default, and ``minimal`` is the step below it, so both are sent as what they mean."""


def apply_cache_control(messages: list[dict[str, Any]], breakpoints: Any, *, index_map: list[int] | None = None) -> None:
    """Translate the core's cache breakpoints into OpenRouter's ``cache_control`` blocks.

    OpenRouter forwards ``cache_control`` to Anthropic (and ignores it elsewhere); DeepSeek and vLLM cache
    prefixes on their own and need nothing. A breakpoint names a message index; the message's text content
    becomes a content-part list whose last part carries the marker.
    """
    for bp in breakpoints:
        index = getattr(bp, "message_index", None)
        if index is None:
            continue
        if index_map is not None:
            if not (0 <= index < len(index_map)):
                continue
            index = index_map[index]
            if index < 0:
                continue
        if not (0 <= index < len(messages)):
            continue
        entry = messages[index]
        if entry.get("role") == "tool":
            continue  # a tool result's content must stay a string on the wire
        content = entry.get("content")
        if isinstance(content, str):
            if not content:
                continue
            entry["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict) and last.get("type") == "text":
                last["cache_control"] = {"type": "ephemeral"}


@dataclass(slots=True)
class ProviderEndpoint:
    id: str
    kind: str
    base_url: str
    api_key: str = ""
    timeout_seconds: float = 600.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    pricing: dict[str, ModelPricing] = field(default_factory=dict)
    temperature: float | None = None
    """When set, every request to this endpoint samples at this temperature (a benchmark pin)."""

    def pricing_for(self, model: str) -> ModelPricing | None:
        if model in self.pricing:
            return self.pricing[model]
        best = max((key for key in self.pricing if model.startswith(key)), key=len, default=None)
        return self.pricing[best] if best is not None else None


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
        images_for: Callable[[str], bool] | None = None,
    ) -> None:
        """``images_for(model)`` says whether that model takes image parts (a preset setting)."""
        self.endpoint = endpoint
        self._images_for = images_for or (lambda _model: False)
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
        guard = DsmlGuard()
        try:
            async with self._client.stream(
                "POST", self._url("/chat/completions"), json=body, headers=self._headers(request)
            ) as response:
                if response.status_code >= 400:
                    raw = await response.aread()
                    if response.status_code == 400:
                        logger.warning("%s rejected a request; message shape: %s", self.endpoint.id, message_shape(body["messages"]))
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
                            shown = guard.feed(content)
                            if shown:
                                yield ProviderDelta(kind=ProviderDeltaKind.text, content=shown)
                        for tc in delta.get("tool_calls") or []:
                            for out in self._tool_call_chunk(open_tools, tc, started):
                                yield out
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"{self.endpoint.id}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"{self.endpoint.id}: transport error: {exc}") from exc
        rest, recovered = guard.finish()
        if rest:
            yield ProviderDelta(kind=ProviderDeltaKind.text, content=rest)
        for call in recovered:
            # The model wrote its native tool markup as text: surface it as real calls.
            index = len(open_tools)
            call_id = f"call_dsml_{index}_{int(started * 1000)}"
            open_tools[index] = {"id": call_id, "name": call.name, "args": call.arguments_json, "started": True}
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_start, tool_call_id=call_id, tool_name=call.name)
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_input, tool_call_id=call_id, tool_input_delta=call.arguments_json)
        if recovered:
            finish_reason = "tool_calls"
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
    ) -> LLMResponse:
        """JSON-mode completion. The core reads the raw JSON text from ``response.message.text``
        (the compaction summariser parses ``{"summary": ...}`` itself), so the object is
        validated here but returned as text."""
        body = await self._build_body(request, stream=False)
        body["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        data = await self._post(body, request)
        text = _message_text(data)
        usage_raw = data.get("usage") or {}
        normalized = normalize_usage(usage_raw)
        cost = await self._record_usage(request, "structured", usage_raw, normalized, started)
        finish = (data.get("choices") or [{}])[0].get("finish_reason") or "stop"
        parsed = parse_json_text(text)
        if parsed is None:
            # The head and the tail are what tell a summary that outgrew its cap from a refusal or a loop.
            logger.warning("%s: structured reply unusable (finish=%s, %s chars): head=%r tail=%r", self.endpoint.id, finish, len(text), text[:160], text[-160:])
            raise LLMProviderError(
                f"{self.endpoint.id}: structured response is not JSON"
                + (" (output truncated by max_tokens)" if finish == "length" else "")
            )
        return LLMResponse(
            message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=json.dumps(parsed, ensure_ascii=False))]),
            stop_reason=StopReason.max_tokens if finish == "length" else StopReason.end_turn,
            usage=LLMResponseUsage(
                input_tokens=normalized["input_tokens"],
                output_tokens=normalized["output_tokens"],
                cache_read_input_tokens=normalized["cache_read_input_tokens"],
                response_cost_usd=cost,
            ),
        )

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        body = await self._build_body(request, stream=False)
        started = time.monotonic()
        data = await self._post(body, request)
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
        model = request.model
        wire: list[dict[str, Any]] = []
        wire_index: list[int] = []  # core message index -> index of its LAST wire entry (a tool message fans out into several)
        for message in request.messages:
            entries = await messages_to_wire([message], image_loader=self._image_loader, supports_images=self.accepts_images(model))
            wire.extend(entries)
            wire_index.append(len(wire) - 1 if entries else -1)  # -1: this message put nothing on the wire
        body: dict[str, Any] = {
            "model": model,
            "messages": wire,
            "max_tokens": request.max_tokens,
            "temperature": self.endpoint.temperature if self.endpoint.temperature is not None else request.temperature,
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
        if thinking and self.endpoint.kind in DEEPSEEK_SHAPED:
            # DeepSeek refuses a thinking-mode request whose earlier assistant turns carry no
            # reasoning_content; a turn produced with thinking off (a recovery retry, an operator
            # toggle) has none, and an empty one is accepted. An assistant turn with nothing but
            # reasoning — the partial the core keeps when a stream dies mid-thought — is not a turn
            # DeepSeek can use (reasoning is only read back on a completed tool-call turn), so it
            # is left off the wire.
            kept: list[dict[str, Any]] = []
            for entry in wire:
                if entry.get("role") == "assistant":
                    if not entry.get("content") and not entry.get("tool_calls"):
                        continue
                    entry.setdefault("reasoning_content", "")
                kept.append(entry)
            body["messages"] = wire = kept
        breakpoints = extra.get("cache_breakpoints")
        if breakpoints and self.endpoint.kind == "openrouter":
            apply_cache_control(body["messages"], breakpoints, index_map=wire_index)
        return body

    def accepts_images(self, model: str) -> bool:
        return self._image_loader is not None and bool(self._images_for(model))

    def _apply_thinking(self, body: dict[str, Any], *, thinking: bool, effort: str) -> None:
        kind = self.endpoint.kind
        if kind in DEEPSEEK_SHAPED:
            # OpenCode Go passes DeepSeek's fields through unchanged; its other models take the same shape.
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
            if thinking:
                body["reasoning_effort"] = DEEPSEEK_EFFORTS.get(effort, effort)
        elif kind == "openrouter":
            body["reasoning"] = {"effort": effort} if thinking else {"enabled": False}
            body["usage"] = {"include": True}
        elif kind == "vllm":
            body["chat_template_kwargs"] = {"enable_thinking": thinking}
        else:
            if thinking:
                body["reasoning_effort"] = effort

    async def _post(self, body: dict[str, Any], request: LLMRequest | None = None) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._url("/chat/completions"), json=body, headers=self._headers(request)
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

    def _headers(self, request: LLMRequest | None = None) -> dict[str, str]:
        # The key proxy meters calls that do not carry this mark (a shell's curl); the bot records its own.
        headers = {"content-type": "application/json", "x-daedalus-metered": "1", **self.endpoint.extra_headers}
        if self.endpoint.api_key:
            headers["authorization"] = f"Bearer {self.endpoint.api_key}"
        if self.endpoint.kind == "opencode":
            # The gateway routes and caches by conversation: one stable id per session, and a client name.
            headers["user-agent"] = f"daedalus/{__version__}"
            obs = request.observability if request is not None else None
            session = (obs.session_id if obs is not None else None) or (obs.run_id if obs is not None else None)
            headers["x-opencode-session"] = session or self.endpoint.id
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
        model = request.model
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
