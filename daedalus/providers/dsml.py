"""Recover tool calls a DeepSeek model wrote as text instead of structured calls.

DeepSeek models occasionally emit their native ``<｜DSML｜tool_calls>`` markup in the
content stream; the API then delivers it as plain text and the core would show it
to the operator and end the turn. :class:`DsmlGuard` holds text back from the first
marker, and at stream end either turns a complete block into tool calls or
releases the text unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

HOLD_LIMIT = 4000
"""Characters buffered after a marker before deciding it was prose, not a call block."""

_MARKERS = ("<｜｜DSML｜｜", "<｜DSML｜", "<|DSML|", "</｜｜DSML｜｜", "</｜DSML｜", "</|DSML|")
_CANON_OPEN = "<DSML:"
_CANON_CLOSE = "</DSML:"
_INVOKE_RE = re.compile(r"<DSML:invoke name=\"([^\"]+)\">(.*?)</DSML:invoke>", re.DOTALL)
_PARAM_RE = re.compile(r"<DSML:parameter name=\"([^\"]+)\"([^>]*)>(.*?)</DSML:parameter>", re.DOTALL)
_BLOCK_RE = re.compile(r"<DSML:tool_calls>(.*?)(?:</DSML:tool_calls>|$)", re.DOTALL)


def _canonical(text: str) -> str:
    for marker in _MARKERS:
        text = text.replace(marker, _CANON_CLOSE if marker.startswith("</") else _CANON_OPEN)
    return text


@dataclass(slots=True)
class RecoveredCall:
    name: str
    arguments: dict[str, Any]

    @property
    def arguments_json(self) -> str:
        return json.dumps(self.arguments, ensure_ascii=False)


def parse_dsml(text: str) -> tuple[str, list[RecoveredCall]]:
    """Split ``text`` into the prose before the markup and the calls it encodes."""
    canon = _canonical(text)
    block = _BLOCK_RE.search(canon)
    if block is None:
        return text, []
    calls: list[RecoveredCall] = []
    for invoke in _INVOKE_RE.finditer(block.group(1)):
        arguments: dict[str, Any] = {}
        for param in _PARAM_RE.finditer(invoke.group(2)):
            pname, attrs, raw = param.group(1), param.group(2), param.group(3)
            value = raw.strip()
            if 'string="true"' not in attrs and (value[:1] in "{[" or value in ("true", "false", "null")):
                try:
                    arguments[pname] = json.loads(value)
                    continue
                except json.JSONDecodeError:
                    pass
            arguments[pname] = value
        calls.append(RecoveredCall(name=invoke.group(1), arguments=arguments))
    if not calls:
        return text, []
    prose = (text[: block.start()] if text[: block.start()] == canon[: block.start()] else canon[: block.start()]).rstrip()
    trailing = canon[block.end() :].strip()
    if trailing:
        prose = f"{prose}\n\n{trailing}" if prose else trailing
    return prose, calls


@dataclass(slots=True)
class DsmlGuard:
    """Streams text through, withholding anything from a DSML marker onwards."""

    held: str = ""
    holding: bool = False
    _tail: str = field(default="", repr=False)

    def feed(self, text: str) -> str:
        """Return the part of ``text`` that can be shown now."""
        if self.holding:
            self.held += text
            if len(self.held) > HOLD_LIMIT and "tool_calls" not in _canonical(self.held[:200]):
                # A mention of the marker in prose, not a call block: stop buffering.
                self.holding = False
                held, self.held = self.held, ""
                return held
            return ""
        buffer = self._tail + text
        self._tail = ""
        for marker in _MARKERS:
            index = buffer.find(marker)
            if index != -1:
                self.holding = True
                self.held = buffer[index:]
                return buffer[:index]
        # A marker may be split across chunks: keep a possible prefix for the next call.
        longest = max(len(m) for m in _MARKERS)
        for size in range(min(longest - 1, len(buffer)), 0, -1):
            suffix = buffer[-size:]
            if any(m.startswith(suffix) for m in _MARKERS):
                self._tail = suffix
                return buffer[:-size]
        return buffer

    def finish(self) -> tuple[str, list[RecoveredCall]]:
        """Flush: text to release (prose before a valid block, or everything) and recovered calls."""
        rest = self._tail
        self._tail = ""
        if not self.holding:
            return rest, []
        self.holding = False
        held, self.held = self.held, ""
        prose, calls = parse_dsml(held)
        if calls:
            return rest + prose, calls
        return rest + held, []


__all__ = ["DsmlGuard", "RecoveredCall", "parse_dsml"]
