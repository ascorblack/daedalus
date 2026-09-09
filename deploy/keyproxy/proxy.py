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

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from aiohttp import web
from claude import (
    CLAUDE_API,
    CLAUDE_FALLBACK_MODELS,
    ClaudeAuth,
    chat_to_messages,
    claude_usage_view,
    messages_events_to_chunks,
)
from subscriptions import (
    CODEX_BASE,
    CODEX_FALLBACK_MODELS,
    GROK_BASE,
    CodexAuth,
    GrokAuth,
    SubscriptionError,
    chat_to_responses,
    codex_usage_view,
    collect_completion,
    grok_usage_view,
    responses_events_to_chunks,
)

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
_token_cache: tuple[float, str] = (0.0, "")
METERED_HEADER = "x-daedalus-metered"
"""The bot marks its own calls: it records their usage itself. A call without the mark (a shell's curl) is metered here."""
AGENT_API = os.environ.get("KEYPROXY_AGENT_API", "").rstrip("/")
COMPLETION_TAILS = (("chat", "completions"), ("completions",), ("responses",), ("messages",))
CAPTURE_LIMIT = 8 * 1024 * 1024
CODEX_AUTH = CodexAuth(Path(os.environ.get("KEYPROXY_CODEX_AUTH", os.path.expanduser("~/.codex/auth.json"))))
GROK_AUTH = GrokAuth(Path(os.environ.get("KEYPROXY_GROK_AUTH", os.path.expanduser("~/.grok/auth.json"))))
CLAUDE_AUTH = ClaudeAuth(Path(os.environ.get("KEYPROXY_CLAUDE_AUTH", os.path.expanduser("~/.claude/.credentials.json"))))
"""The operator's subscriptions: served as ``/codex/v1``, ``/grok/v1`` and ``/claude/v1`` with the CLIs' own logins."""
_usage_cache: dict[str, tuple[float, dict[str, Any]]] = {}
USAGE_CACHE_SECONDS = 60.0


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


def agent_api_token() -> str:
    """The bot's API token, read from its database (read-only) so a metered call can be reported back."""
    global _token_cache
    at, value = _token_cache
    if value and time.monotonic() - at < 300:
        return value
    if BUDGET_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{BUDGET_DB}?mode=ro", uri=True, timeout=2.0)
            try:
                row = conn.execute("SELECT value FROM kv WHERE key = 'api_token'").fetchone()
            finally:
                conn.close()
            value = str(json.loads(row[0])) if row else ""
        except (sqlite3.Error, ValueError) as exc:
            logger.warning("api token read failed: %s", exc)
    _token_cache = (time.monotonic(), value)
    return value


def is_completion(rest: str) -> bool:
    segments = tuple(s for s in rest.strip("/").split("/") if s)
    if segments and segments[0] == "v1":
        segments = segments[1:]
    return any(segments[-len(tail):] == tail for tail in COMPLETION_TAILS if len(segments) >= len(tail))


def parse_usage(body: bytes, content_type: str) -> tuple[str, dict[str, int]] | None:
    """``(model, tokens)`` from a completion response, streamed or not; None when it carries no usage."""
    candidates: list[dict[str, Any]] = []
    if "text/event-stream" in content_type:
        for line in body.decode("utf-8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                candidates.append(json.loads(payload))
            except ValueError:
                continue
    else:
        try:
            candidates.append(json.loads(body))
        except ValueError:
            return None
    model = ""
    usage: dict[str, Any] | None = None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        inner = item.get("response") if isinstance(item.get("response"), dict) else item  # Responses API wraps the final object
        model = inner.get("model") or model
        if isinstance(inner.get("usage"), dict):
            usage = inner["usage"]
    if usage is None:
        return None
    details_in = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    details_out = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    tokens = {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("prompt_cache_hit_tokens") or usage.get("cache_read_input_tokens") or (details_in.get("cached_tokens") if isinstance(details_in, dict) else 0) or 0),
        "reasoning_tokens": int((details_out.get("reasoning_tokens") if isinstance(details_out, dict) else 0) or 0),
    }
    return model, tokens


async def report_direct_usage(client: httpx.AsyncClient, provider_id: str, request_model: str, body: bytes, content_type: str, started: float) -> None:
    """Tell the bot about a metered call so its spend counters and caps see it."""
    parsed = parse_usage(body, content_type)
    if parsed is None:
        logger.warning("direct call to %s carried no usage; not recorded", provider_id)
        return
    model, tokens = parsed
    token = agent_api_token()
    if not token:
        logger.warning("direct call to %s not recorded: no api token", provider_id)
        return
    payload = {"provider_id": provider_id, "model": model or request_model, **tokens, "duration_ms": int((time.monotonic() - started) * 1000)}
    try:
        response = await client.post(f"{AGENT_API}/api/usage/direct", json=payload, headers={"x-daedalus-token": token}, timeout=10.0)
        if response.status_code >= 300:
            logger.warning("direct usage not recorded: HTTP %s %s", response.status_code, response.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("direct usage not recorded: %s", type(exc).__name__)


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


def subscriptions_status() -> dict[str, Any]:
    return {"codex": {"logged_in": CODEX_AUTH.available()}, "grok": {"logged_in": GROK_AUTH.available()}, "claude": {"logged_in": CLAUDE_AUTH.available()}}


async def _sse_lines(response: httpx.Response) -> Any:
    async for line in response.aiter_lines():
        yield line


async def handle_claude(request: web.Request, rest: str) -> web.StreamResponse:
    """Anthropic Messages behind the Claude Code login, presented as chat completions."""
    client: httpx.AsyncClient = request.app["client"]
    tail = rest.strip("/").removeprefix("v1/")
    try:
        headers = await CLAUDE_AUTH.headers(client)
    except SubscriptionError as exc:
        return web.json_response({"error": {"message": str(exc), "type": "subscription_error"}}, status=502)
    if tail == "models":
        models = list(CLAUDE_FALLBACK_MODELS)
        try:
            response = await client.get(CLAUDE_API + "/v1/models", headers={**headers, "accept": "application/json"})
            if response.status_code == 200:
                models = sorted({str(m.get("id") or "") for m in (response.json().get("data") or []) if isinstance(m, dict) and m.get("id")} | set(models))
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        return web.json_response({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "anthropic"} for m in models if m]})
    if tail != "chat/completions":
        return web.json_response({"error": {"message": f"claude serves chat/completions and models, not {tail!r}", "type": "invalid_request_error"}}, status=404)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": {"message": "body must be JSON", "type": "invalid_request_error"}}, status=400)
    model = str(body.get("model") or "")
    stream = bool(body.get("stream", False))
    payload, names = chat_to_messages(body)
    upstream = client.build_request("POST", CLAUDE_API + "/v1/messages?beta=true", json=payload, headers={**headers, "accept": "text/event-stream", "content-type": "application/json"})
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        return web.json_response({"error": {"message": f"upstream unreachable: {type(exc).__name__}", "type": "proxy_error"}}, status=502)
    if response.status_code >= 400:
        raw = (await response.aread()).decode("utf-8", "replace")
        await response.aclose()
        try:
            payload_err = json.loads(raw)
        except ValueError:
            payload_err = {"error": {"message": raw[:400], "type": "upstream_error"}}
        return web.json_response(payload_err, status=response.status_code)
    chunks = messages_events_to_chunks(_sse_lines(response), model=model, names=names)
    try:
        if not stream:
            return web.json_response(await collect_completion(chunks, model=model))
        out = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await out.prepare(request)
        async for chunk in chunks:
            await out.write(chunk.encode("utf-8"))
        await out.write_eof()
        return out
    except (ConnectionResetError, asyncio.CancelledError):
        return web.Response(status=499)
    finally:
        await response.aclose()


async def handle_codex(request: web.Request, rest: str) -> web.StreamResponse:
    """The ChatGPT backend speaks only the Responses API: translate chat completions both ways."""
    client: httpx.AsyncClient = request.app["client"]
    tail = rest.strip("/").removeprefix("v1/")
    try:
        headers = await CODEX_AUTH.headers(client)
    except SubscriptionError as exc:
        return web.json_response({"error": {"message": str(exc), "type": "subscription_error"}}, status=502)
    if tail == "models":
        models = list(CODEX_FALLBACK_MODELS)
        try:
            usage = await _codex_usage(client, headers)
            models = sorted(set(models) | set(usage.get("models") or []))  # the plan's usage table names only some of them
        except SubscriptionError:
            pass
        return web.json_response({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "openai"} for m in models]})
    if tail != "chat/completions":
        return web.json_response({"error": {"message": f"codex serves chat/completions and models, not {tail!r}", "type": "invalid_request_error"}}, status=404)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": {"message": "body must be JSON", "type": "invalid_request_error"}}, status=400)
    model = str(body.get("model") or "")
    stream = bool(body.get("stream", False))
    upstream = client.build_request("POST", CODEX_BASE + "/codex/responses", json=chat_to_responses(body), headers={**headers, "accept": "text/event-stream", "content-type": "application/json"})
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        return web.json_response({"error": {"message": f"upstream unreachable: {type(exc).__name__}", "type": "proxy_error"}}, status=502)
    if response.status_code >= 400:
        raw = (await response.aread()).decode("utf-8", "replace")
        await response.aclose()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error": {"message": raw[:400], "type": "upstream_error"}}
        return web.json_response(payload, status=response.status_code)
    chunks = responses_events_to_chunks(_sse_lines(response), model=model)
    try:
        if not stream:
            return web.json_response(await collect_completion(chunks, model=model))
        out = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await out.prepare(request)
        async for chunk in chunks:
            await out.write(chunk.encode("utf-8"))
        await out.write_eof()
        return out
    except (ConnectionResetError, asyncio.CancelledError):
        return web.Response(status=499)
    finally:
        await response.aclose()


async def _codex_usage(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    cached = _usage_cache.get("codex")
    if cached and time.monotonic() - cached[0] < USAGE_CACHE_SECONDS:
        return cached[1]
    response = await client.get(CODEX_BASE + "/wham/usage", headers={**headers, "accept": "application/json"})
    if response.status_code != 200:
        raise SubscriptionError(f"codex usage: HTTP {response.status_code}")
    view = codex_usage_view(response.json())
    _usage_cache["codex"] = (time.monotonic(), view)
    return view


async def _grok_usage(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    cached = _usage_cache.get("grok")
    if cached and time.monotonic() - cached[0] < USAGE_CACHE_SECONDS:
        return cached[1]
    response = await client.get(GROK_BASE + "/billing?format=credits", headers={**headers, "accept": "application/json"})
    if response.status_code != 200:
        raise SubscriptionError(f"grok usage: HTTP {response.status_code}")
    view = grok_usage_view(response.json())
    _usage_cache["grok"] = (time.monotonic(), view)
    return view


async def _claude_usage(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    cached = _usage_cache.get("claude")
    if cached and time.monotonic() - cached[0] < USAGE_CACHE_SECONDS:
        return cached[1]
    usage = await client.get(CLAUDE_API + "/api/oauth/usage", headers={**headers, "accept": "application/json"})
    if usage.status_code != 200:
        raise SubscriptionError(f"claude usage: HTTP {usage.status_code}")
    profile: dict[str, Any] = {}
    try:
        pr = await client.get(CLAUDE_API + "/api/oauth/profile", headers={**headers, "accept": "application/json"})
        if pr.status_code == 200:
            profile = pr.json()
    except (httpx.HTTPError, ValueError):
        profile = {}
    view = claude_usage_view(usage.json(), profile)
    _usage_cache["claude"] = (time.monotonic(), view)
    return view


async def handle_subscriptions_usage(request: web.Request) -> web.Response:
    """Subscription quota windows, for the Usage screen; a missing login is reported, not an error."""
    client: httpx.AsyncClient = request.app["client"]
    out: dict[str, Any] = {}
    for name, auth, fetch in (("codex", CODEX_AUTH, _codex_usage), ("grok", GROK_AUTH, _grok_usage), ("claude", CLAUDE_AUTH, _claude_usage)):
        if not auth.available():
            out[name] = {"provider": name, "logged_in": False}
            continue
        try:
            out[name] = {"logged_in": True, **await fetch(client, await auth.headers(client))}
        except (SubscriptionError, httpx.HTTPError, ValueError) as exc:
            out[name] = {"provider": name, "logged_in": True, "error": str(exc)[:200]}
    return web.json_response(out)


async def handle(request: web.Request) -> web.StreamResponse:
    name = request.match_info["upstream"].lower()
    rest = request.match_info.get("rest", "")
    if name == "codex":
        return await handle_codex(request, rest)
    if name == "claude":
        return await handle_claude(request, rest)
    table = upstreams()
    client: httpx.AsyncClient = request.app["client"]
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    if name == "grok":
        # A subscription, not a key: no USD budget applies, the CLI's identity travels with the call.
        try:
            headers.update(await GROK_AUTH.headers(client))
        except SubscriptionError as exc:
            return web.json_response({"error": {"message": str(exc), "type": "subscription_error"}}, status=502)
        base, key = GROK_BASE, ""
        rest = rest.strip("/").removeprefix("v1/")
    elif name not in table:
        return web.json_response({"error": f"unknown upstream {name!r}"}, status=404)
    else:
        base, key = table[name]
        if budget_exceeded() and not budget_exempt(rest):
            return web.json_response({"error": {"message": "daily budget exceeded; refused by the key proxy", "type": "budget_exceeded"}}, status=402)
        if key:
            headers["authorization"] = f"Bearer {key}"
    body = await request.read()
    meter = bool(AGENT_API) and METERED_HEADER not in request.headers and request.method == "POST" and is_completion(rest)
    request_model = ""
    if meter:
        try:
            request_model = str(json.loads(body).get("model") or "")
        except (ValueError, AttributeError):
            request_model = ""
    started = time.monotonic()
    captured = bytearray()
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
            if meter and len(captured) < CAPTURE_LIMIT:
                captured.extend(chunk)
            await out.write(chunk)
        await out.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        pass  # the caller went away mid-stream
    finally:
        await response.aclose()
    if meter and response.status_code < 300:
        asyncio.create_task(report_direct_usage(client, name, request_model, bytes(captured), response.headers.get("content-type", ""), started))
    return out


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "upstreams": sorted(upstreams()), "subscriptions": subscriptions_status(), "budget_exceeded": budget_exceeded(), "spent_today_usd": round(spent_today(), 4) if BUDGET_USD_PER_DAY > 0 else None})


def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["client"] = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=30.0, pool=10.0), limits=httpx.Limits(max_connections=64, max_keepalive_connections=16), follow_redirects=False)
    app.router.add_get("/healthz", health)
    app.router.add_get("/subscriptions/usage", handle_subscriptions_usage)
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
