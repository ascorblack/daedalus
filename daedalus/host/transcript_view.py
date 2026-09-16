"""What the Mini App shows for one transcript row.

A transcript row never changes after it is written, so neither does the shape the app
draws from it — but the shape costs about a millisecond a message to derive, nearly all of
it secret redaction, and the app asks for six hundred of them every time a run emits an
event. So the view is computed once, when the row is appended, and stored beside it.

:class:`TranscriptViewBuilder` is what does the computing, and :meth:`~TranscriptViewBuilder.key`
says which code and which redactor produced a stored view. A view whose key no longer
matches is recomputed from the message and rewritten, which is what makes a newly
configured secret reach rows written before it was known.
"""

from __future__ import annotations

import json
import re
from typing import Any

from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from daedalus.host import prompts
from daedalus.host.prompts import split_headline
from daedalus.security import redact

VIEW_VERSION = 2
"""Bumped whenever the shape below changes; stored views from an older version are recomputed."""

TOOL_RESULT_PREVIEW_CHARS = 400
"""Characters of a tool result a listed turn carries. The whole text is one request away
(``/api/sessions/{id}/tool-results/{call_id}``), and previews were the larger half of a
session payload while they were ten times this long."""

_SUMMARY_WRAP_RE = re.compile(r"</?compacted-turn[^>]*>")
_NUDGE_MARKERS = ("[internal control", "tool repeatedly failed with the same error", "has been disabled for the rest of this run", "The run has reached its budget")


def _looks_like_core_nudge(text: str) -> bool:
    head = text.lstrip()[:400]
    return any(marker in head for marker in _NUDGE_MARKERS)


def message_view(message: Message) -> dict[str, Any]:
    text: list[str] = []
    thinking: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for block in message.content_blocks:
        if isinstance(block, TextBlock):
            text.append(block.text)
        elif isinstance(block, ThinkingBlock):
            thinking.append(block.text)
        elif isinstance(block, ToolUseBlock):
            try:
                args = json.loads(block.arguments_json or "{}")
            except json.JSONDecodeError:
                args = {"raw": block.arguments_json}
            tool_calls.append({"id": block.tool_call_id, "name": block.name, "arguments": redact.shared().redact_any(args)})
        elif isinstance(block, ToolResultBlock):
            # The listing carries a preview; the full text (a skill body, a long command output) is one request away.
            # Whether there is more to fetch is decided here, on the text itself: the length the app
            # is told is the text's, while what it holds is a redacted preview, and a secret that
            # redacts to something shorter would otherwise read as a result cut short.
            tool_results.append(
                {
                    "id": block.tool_call_id,
                    "content": redact.redact(block.content[:TOOL_RESULT_PREVIEW_CHARS]),
                    "is_error": block.is_error,
                    "length": len(block.content),
                    "clipped": len(block.content) > TOOL_RESULT_PREVIEW_CHARS,
                }
            )
    compaction = message.metadata.get("daedalus.compaction") if isinstance(message.metadata, dict) else None
    is_summary = bool(message.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)) if isinstance(message.metadata, dict) else False
    body = prompts.without_turn_context("".join(text))
    if is_summary:
        body = _SUMMARY_WRAP_RE.sub("", body).strip()
        # A summary without the host's record came from the core mid-run; the host's own (auto/manual) sit between runs.
        compaction = compaction or {"reason": "core"}
    origin = message.metadata.get("daedalus.origin") if isinstance(message.metadata, dict) else None
    delivery = message.metadata.get("daedalus.delivery") if isinstance(message.metadata, dict) else None
    internal = message.role is MessageRole.user and not is_summary and (
        origin == "core" or delivery == "drained" or (origin != "operator" and _looks_like_core_nudge(body))
    )
    headline = ""
    if message.role is MessageRole.assistant:
        body, headline = split_headline(body)
        body = redact.redact(body)
    archived = message.metadata.get("daedalus.archived") if isinstance(message.metadata, dict) else None
    return {
        "role": message.role.value,
        "summary": is_summary,
        "internal": internal,
        "origin": origin or ("operator" if message.role is MessageRole.user and not internal else ""),
        "seq": message.metadata.get("daedalus.seq") if isinstance(message.metadata, dict) else None,
        "compaction": compaction,
        "archived": archived,
        "headline": headline,
        "text": body,
        "thinking": "".join(thinking) or (message.reasoning_content or ""),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "created_at": message.created_at.isoformat(),
    }


class TranscriptViewBuilder:
    """Builds and keys the stored view. Held by the store, which knows nothing else about it."""

    def key(self) -> str:
        """Version of the code and of the redactor that a stored view was produced by."""
        return f"{VIEW_VERSION}.{redact.shared().fingerprint()}"

    def build(self, message: Message) -> dict[str, Any]:
        return message_view(message)


__all__ = ["TOOL_RESULT_PREVIEW_CHARS", "VIEW_VERSION", "TranscriptViewBuilder", "message_view"]
