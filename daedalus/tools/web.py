"""Web tools: fetch a page as text, search the web."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse

import httpx
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import clip, error, ok, services_for, tool_config

_TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL_RE.sub("\n\n", text).strip()


@tool(
    name="WebFetch",
    description="Fetch a URL and return its content as plain text (HTML is converted).",
)
async def web_fetch(context: ToolContext, url: str, max_chars: int | None = None) -> ToolResult:
    services = services_for(context)
    web = tool_config(context).web
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=web.fetch_timeout_seconds, headers={"user-agent": web.user_agent}, proxy=web.proxy or None
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return error(context, f"fetch failed: {exc}")
    content_type = response.headers.get("content-type", "")
    if "html" in content_type:
        body = html_to_text(response.text)
    elif "json" in content_type or content_type.startswith("text/"):
        body = response.text
    else:
        return ok(context, f"HTTP {response.status_code}, {content_type}, {len(response.content)} bytes (binary; use exec with curl -o to save it)")
    limit = min(max_chars or web.fetch_max_chars, services.max_tool_output_chars)
    return ok(context, f"HTTP {response.status_code} {url}\n\n{clip(body, limit)}", status=response.status_code)


_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)


@tool(name="WebSearch", description="Search the web and return the top results with snippets.")
async def web_search(context: ToolContext, query: str, limit: int | None = None) -> ToolResult:
    web = tool_config(context).web
    limit = limit or web.search_results
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=web.search_timeout_seconds, headers={"user-agent": web.user_agent}, proxy=web.proxy or None
        ) as client:
            response = await client.post(web.search_url, data={"q": query, "kl": web.search_region})
    except httpx.HTTPError as exc:
        return error(context, f"search failed: {exc}")
    results: list[str] = []
    for href, title, snippet in _RESULT_RE.findall(response.text):
        parsed = urlparse(html.unescape(href))
        target = parse_qs(parsed.query).get("uddg", [html.unescape(href)])[0]
        results.append(f"- {html_to_text(title)}\n  {target}\n  {html_to_text(snippet)}")
        if len(results) >= limit:
            break
    if not results:
        return ok(context, "(no results)", count=0)
    return ok(context, "\n".join(results), count=len(results))


TOOLS = [web_fetch, web_search]

__all__ = ["TOOLS", "html_to_text"]
