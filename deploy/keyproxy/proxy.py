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
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

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
BUDGET_EXEMPT_PATHS = ("/user/balance", "/credits", "/models")
"""Reading a balance or the model list spends nothing; only completions are refused over budget."""
HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "authorization", "x-api-key"}


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


def budget_exceeded() -> bool:
    return BUDGET_FLAG.exists()


def target_url(base: str, rest: str, query: str) -> str:
    return base.rstrip("/") + "/" + rest.lstrip("/") + (f"?{query}" if query else "")


async def handle(request: web.Request) -> web.StreamResponse:
    name = request.match_info["upstream"].lower()
    rest = request.match_info.get("rest", "")
    table = upstreams()
    if name not in table:
        return web.json_response({"error": f"unknown upstream {name!r}"}, status=404)
    base, key = table[name]
    if budget_exceeded() and not any(rest.startswith(p.lstrip("/")) for p in BUDGET_EXEMPT_PATHS):
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
        if k.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding"):
            out.headers[k] = v
    await out.prepare(request)
    try:
        async for chunk in response.aiter_raw():
            await out.write(chunk)
    finally:
        await response.aclose()
    await out.write_eof()
    return out


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "upstreams": sorted(upstreams()), "budget_exceeded": budget_exceeded()})


def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["client"] = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=30.0), follow_redirects=False)
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
