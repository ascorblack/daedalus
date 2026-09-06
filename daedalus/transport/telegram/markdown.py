"""Markdown → Telegram HTML, and splitting long texts on safe boundaries."""

from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096
DOCUMENT_THRESHOLD = 12_000
"""Answers longer than this go out as a file instead of many messages."""

_FENCE_RE = re.compile(r"```([\w+-]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADER_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)


def markdown_to_html(text: str) -> str:
    """Convert the subset of Markdown models actually emit into Telegram HTML."""
    parts: list[str] = []
    last = 0
    for match in _FENCE_RE.finditer(text):
        parts.append(_inline(text[last : match.start()]))
        lang = html.escape(match.group(1) or "")
        code = html.escape(match.group(2).rstrip("\n"))
        if lang:
            parts.append(f'<pre><code class="language-{lang}">{code}</code></pre>')
        else:
            parts.append(f"<pre>{code}</pre>")
        last = match.end()
    parts.append(_inline(text[last:]))
    return "".join(parts).strip()


def _inline(segment: str) -> str:
    codes: list[str] = []

    def stash(m: re.Match[str]) -> str:
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    segment = _INLINE_CODE_RE.sub(stash, segment)
    segment = html.escape(segment, quote=False)
    segment = _HEADER_RE.sub(lambda m: f"<b>{m.group(1)}</b>", segment)
    segment = _BOLD_RE.sub(r"<b>\1</b>", segment)
    segment = _ITALIC_RE.sub(r"<i>\1</i>", segment)
    segment = _LINK_RE.sub(lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', segment)
    segment = _BULLET_RE.sub(r"\1• ", segment)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], segment)


_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(markup: str) -> str:
    """Rich HTML → readable plain text (line breaks for block tags, entities decoded)."""
    text = re.sub(r"</(p|li|details|summary|blockquote|h[1-6]|pre|tr)>", "\n", markup)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = _TAG_RE.sub("", text)
    return html.unescape(re.sub(r"\n{3,}", "\n\n", text)).strip()[:4000]


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split Markdown text into chunks under ``limit`` characters.

    Code fences are kept balanced: a fence that has to be cut is closed at the
    chunk end and reopened at the start of the next one.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    open_fence: str | None = None
    for line in text.splitlines(keepends=True):
        fence = re.match(r"^```([\w+-]*)", line)
        addition = line
        if len(current) + len(addition) > limit - 8:
            if open_fence is not None:
                current += "```\n"
            if current.strip():
                chunks.append(current)
            current = f"```{open_fence}\n" if open_fence is not None else ""
        if len(addition) > limit - 8:
            step = limit - 32
            for i in range(0, len(addition), step):
                piece = addition[i : i + step]
                if open_fence is not None:
                    piece = f"```{open_fence}\n{piece}\n```\n"
                if current.strip():
                    chunks.append(current)
                    current = ""
                chunks.append(piece)
            continue
        current += addition
        if fence:
            open_fence = None if open_fence is not None else fence.group(1)
    if current.strip():
        chunks.append(current)
    return chunks


__all__ = ["DOCUMENT_THRESHOLD", "TELEGRAM_LIMIT", "markdown_to_html", "split_message", "strip_tags"]
