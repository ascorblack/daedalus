"""Conversion between core ``Message`` history and OpenAI chat-completion payloads."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import Any

from protocore.contracts.types import (
    ImageRefBlock,
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)


def tools_to_wire(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for tool in tools:
        params = tool.parameters.model_dump(mode="json") if tool.parameters is not None else {}
        schema = {
            "type": "object",
            "properties": params.get("properties", {}),
            "required": params.get("required", []),
        }
        wire.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": schema,
                },
            }
        )
    return wire


async def messages_to_wire(
    messages: Sequence[Message],
    *,
    image_loader: Any | None = None,
    supports_images: bool = False,
) -> list[dict[str, Any]]:
    """Flatten core messages into OpenAI ``messages``.

    ``image_loader`` is an async callable ``(blob_ref) -> (bytes, mime)``; when the
    endpoint cannot take images the block degrades to a text placeholder so the
    model still knows a file was attached.
    """
    wire: list[dict[str, Any]] = []
    for message in messages:
        if message.role is MessageRole.system:
            wire.append({"role": "system", "content": _text_of(message)})
            continue
        if message.role is MessageRole.tool:
            for block in message.content_blocks:
                if isinstance(block, ToolResultBlock):
                    wire.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.tool_call_id,
                            "content": block.content or "",
                        }
                    )
            continue
        if message.role is MessageRole.assistant:
            entry: dict[str, Any] = {"role": "assistant"}
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            reasoning: list[str] = []
            for block in message.content_blocks:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ThinkingBlock):
                    reasoning.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(
                        {
                            "id": block.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": block.arguments_json or "{}",
                            },
                        }
                    )
            entry["content"] = "".join(text_parts) or None
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if message.reasoning_content:
                entry["reasoning_content"] = message.reasoning_content
            elif reasoning:
                entry["reasoning_content"] = "".join(reasoning)
            if entry["content"] is None and not tool_calls:
                entry["content"] = ""
            wire.append(entry)
            continue
        # user: one text block; images ride in ``metadata["image_refs"]`` (the core allows a single block)
        refs: list[tuple[str, str]] = []
        texts: list[str] = []
        for block in message.content_blocks:
            if isinstance(block, TextBlock):
                texts.append(block.text)
            elif isinstance(block, ImageRefBlock):
                refs.append((block.blob_ref, block.mime_type))
        for ref in message.metadata.get("image_refs") or []:
            if isinstance(ref, dict) and ref.get("ref"):
                refs.append((str(ref["ref"]), str(ref.get("mime") or "image/png")))
        content: list[dict[str, Any]] = [{"type": "text", "text": t} for t in texts]
        has_image = False
        for blob_ref, mime_hint in refs:
            if supports_images and image_loader is not None:
                data, mime = await image_loader(blob_ref)
                b64 = base64.b64encode(data).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime or mime_hint};base64,{b64}"}})
                has_image = True
            else:
                content.append({"type": "text", "text": f"[image attached: {blob_ref}]"})
        if has_image:
            wire.append({"role": "user", "content": content})
        else:
            wire.append({"role": "user", "content": "\n".join(p["text"] for p in content)})
    return wire


def _text_of(message: Message) -> str:
    return "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))


def parse_json_arguments(raw: str) -> dict[str, Any]:
    """Parse tool-call arguments, tolerating empty strings and trailing junk."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = _repair_json(text)
    return value if isinstance(value, dict) else {"value": value}


def _repair_json(text: str) -> Any:
    """Best-effort repair of a truncated JSON object (balance braces and quotes)."""
    depth_curly = depth_square = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
    candidate = text
    if in_string:
        candidate += '"'
    candidate += "]" * max(depth_square, 0) + "}" * max(depth_curly, 0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return {}


__all__ = ["messages_to_wire", "parse_json_arguments", "tools_to_wire"]
