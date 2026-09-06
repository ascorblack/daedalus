"""Key proxy: the only process that holds provider API keys.

The agent container talks to ``http://keyproxy:3200/<upstream>/...`` with no credentials;
this service injects the real ``Authorization`` header for each upstream from its own
environment and forwards the request, streaming the response back (SSE included). When
the daily-budget flag exists on the shared state volume, calls to model upstreams are
refused here — below the agent, where a self-modification cannot reach.

Upstreams: ``deepseek`` → https://api.deepseek.com, ``openrouter`` → https://openrouter.ai/api/v1,
``openai`` → https://api.openai.com/v1. Keys: ``DEEPSEEK_API_KEY``, ``OPENROUTER_API_KEY``,
``OPENAI_API_KEY``. Extra upstreams: ``KEYPROXY_UPSTREAM_<NAME>=https://host/base`` with
``KEYPROXY_KEY_<NAME>=…``.

The budget is checked in two independent ways: the supervisor's flag file, and — when
``KEYPROXY_USD_PER_DAY`` is set — the proxy's own read of today's spend from the read-only
database, which nothing in the agent container can unlink.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import asyncio

import httpx
from aiohttp import web

logging.basicConfig(level=logging.WARNING, format="%(asctime)s keyproxy %(levelname)s: %(message)s")
logger = logging.getLogger("keyproxy")

DEFAULT_UPSTREAMS = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}
BUDGET_FLAG = Path(os.environ.get("KEYPROXY_BUDGET_FLAG", "/srv/state/BUDGET_EXCEEDED"))
BUDGET_DB = Path(os.environ.get("KEYPROXY_BUDGET_DB", "/srv/state/daedalus.sqlite"))
BUDGET_USD_PER_DAY = float(os.environ.get("KEYPROXY_USD_PER_DAY", "0") or 0)
BUDGET_CACHE_SECONDS = 30.0
BUDGET_EXEMPT_TAILS = (("user", "balance"), ("credits",), ("models",))
"""Reading a balance or the model list spends nothing; only completions are refused over budget.
Matched on path segments (a leading ``v1`` ignored), so a ``…/deepseek/v1`` base_url behaves the same."""
HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "authorization", "x-api-key", "api-key", "x-goog-api-key", "cookie", "proxy-authorization"}
DROP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding"}
"""The body is streamed decoded, so the upstream's framing and encoding headers no longer describe it."""
_spend_cache: tuple[float, float] = (0.0, 0.0)


def upstreams() -> dict[str, tuple[str, str]]:
    """name → (base_url, api_key) from the environment."""
    out: dict[str, tuple[str, str]] = {}
    for name, (base, env_key) in DEFAULT_UPSTREAMS.items():
        key = os.environ.get(env_key, "")
        if key:
            out[name] = (base, key)
    for var, value in os.environ.items():
        if var.startswith("KEYPROXY_UPSTREAM_") and value:
            name = var[len("KEYPROXY_UPSTREAM_"):].lower()
            out[name] = (value.rstrip("/"), os.environ.get(f"KEYPROXY_KEY_{name.upper()}", ""))
    return out


def spent_today() -> float:
    """Today's priced spend from the agent's database (read-only), cached briefly."""
    global _spend_cache
    at, value = _spend_cache
    if time.monotonic() - at < BUDGET_CACHE_SECONDS:
        return value
    value = 0.0
    if BUDGET_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{BUDGET_DB}?mode=ro", uri=True, timeout=2.0)
            try:
                row = conn.execute("SELECT sum(cost_usd) FROM usage_events WHERE at >= ?", (datetime.now(UTC).strftime("%Y-%m-%d"),)).fetchone()
            finally:
                conn.close()
            value = float(row[0] or 0.0)
        except sqlite3.Error as exc:
            logger.warning("spend query failed: %s", exc)
    _spend_cache = (time.monotonic(), value)
    return value


def budget_exceeded() -> bool:
    if BUDGET_FLAG.exists():
        return True
    return BUDGET_USD_PER_DAY > 0 and spent_today() > BUDGET_USD_PER_DAY


def budget_exempt(rest: str) -> bool:
    segments = tuple(s for s in rest.split("/") if s)
    if segments and segments[0] == "v1":
        segments = segments[1:]
    return any(segments[-len(tail):] == tail for tail in BUDGET_EXEMPT_TAILS if len(segments) >= len(tail))


def target_url(base: str, rest: str, query: str) -> str:
    return base.rstrip("/") + "/" + rest.lstrip("/") + (f"?{query}" if query else "")


async def handle(request: web.Request) -> web.StreamResponse:
    name = request.match_info["upstream"].lower()
    rest = request.match_info.get("rest", "")
    table = upstreams()
    if name not in table:
        return web.json_response({"error": f"unknown upstream {name!r}"}, status=404)
    base, key = table[name]
    if budget_exceeded() and not budget_exempt(rest):
        return web.json_response({"error": {"message": "daily budget exceeded; refused by the key proxy", "type": "budget_exceeded"}}, status=402)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    if key:
        headers["authorization"] = f"Bearer {key}"
    body = await request.read()
    client: httpx.AsyncClient = request.app["client"]
    try:
        upstream = client.build_request(request.method, target_url(base, rest, request.query_string), headers=headers, content=body)
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        return web.json_response({"error": {"message": f"upstream unreachable: {type(exc).__name__}", "type": "proxy_error"}}, status=502)
    out = web.StreamResponse(status=response.status_code)
    for k, v in response.headers.items():
        if k.lower() not in DROP_RESPONSE_HEADERS:
            out.headers[k] = v
    await out.prepare(request)
    try:
        # aiter_bytes() decodes gzip/br on the way through; aiter_raw() would hand the client compressed bytes with the header gone.
        async for chunk in response.aiter_bytes():
            await out.write(chunk)
        await out.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        pass  # the caller went away mid-stream
    finally:
        await response.aclose()
    return out


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "upstreams": sorted(upstreams()), "budget_exceeded": budget_exceeded(), "spent_today_usd": round(spent_today(), 4) if BUDGET_USD_PER_DAY > 0 else None})


def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["client"] = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=30.0, pool=10.0), limits=httpx.Limits(max_connections=64, max_keepalive_connections=16), follow_redirects=False)
    app.router.add_get("/healthz", health)
    app.router.add_route("*", "/{upstream}/{rest:.*}", handle)

    async def close(app: web.Application) -> None:
        await app["client"].aclose()

    app.on_cleanup.append(close)
    return app


def main() -> None:
    port = int(os.environ.get("KEYPROXY_PORT", "3200"))
    if not upstreams():
        logger.warning("no upstream keys configured; every call will be refused")
    web.run_app(make_app(), host="0.0.0.0", port=port, print=None, access_log=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
