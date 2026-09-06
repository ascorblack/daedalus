"""``Delegate`` — hand a bounded task to a coding harness running on the operator's subscription.

The harness service runs ``claude``, ``codex`` or ``grok`` headlessly in the session
workspace with the vendor's own tools and loop; this tool streams its progress into the
chat and returns the final answer with the usage the vendor reported. The subscriptions
themselves never enter this container.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.providers.openai_compat import UsageRecord, normalize_usage
from daedalus.tools._common import clip, error, ok, services_for

PROGRESS_EVERY_SECONDS = 2.0
RESULT_FOOTER_MODELS = 3


@tool(
    name="Delegate",
    description=(
        "Hand a bounded task to a coding harness on the operator's subscription: vendor is "
        "'claude' (Claude Code), 'codex' (OpenAI Codex) or 'grok' (Grok Build). The harness runs "
        "in the given directory (default: this workspace) with its own tools — it reads and edits "
        "files, runs commands, searches the web — and returns its final answer. Write the task as a "
        "complete brief: goal, constraints, what 'done' looks like, what not to touch. Use it for "
        "large multi-file coding work, long investigations or an independent second opinion; "
        "verify the outcome yourself afterwards (Verify). read_only=true forbids changes. "
        "Progress is shown to the operator while it runs."
    ),
)
async def delegate(
    context: ToolContext,
    vendor: str,
    task: str,
    cwd: str | None = None,
    model: str | None = None,
    read_only: bool = False,
    max_turns: int | None = None,
    timeout_seconds: int | None = None,
) -> ToolResult:
    services = services_for(context)
    manager = services.extra.get("manager")
    config = getattr(getattr(manager, "config", None), "harness", None)
    if config is None:
        return error(context, "the harness is not configured")
    vendor = vendor.strip().lower()
    vendor_config = config.vendors.get(vendor)
    if vendor_config is None or not vendor_config.enabled:
        return error(context, f"vendor {vendor!r} is not enabled; enabled: {', '.join(v for v, c in config.vendors.items() if c.enabled) or 'none'}")
    workdir = services.resolve(cwd)
    if not workdir.exists():
        return error(context, f"working directory does not exist: {workdir}")
    body = {
        "vendor": vendor,
        "prompt": task,
        "cwd": str(workdir),
        "model": model or vendor_config.model,
        "effort": vendor_config.effort,
        "max_turns": max_turns or vendor_config.max_turns,
        "read_only": read_only,
        "timeout_seconds": float(timeout_seconds or config.timeout_seconds),
    }
    started = time.monotonic()
    last_progress = 0.0
    text_parts: list[str] = []
    result: dict[str, Any] | None = None
    failure: str | None = None

    async def progress(text: str) -> None:
        nonlocal last_progress
        if services.progress is None or time.monotonic() - last_progress < PROGRESS_EVERY_SECONDS:
            return
        last_progress = time.monotonic()
        await services.progress(f"{vendor}: {text[:160]}")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(body["timeout_seconds"] + 60, connect=15.0)) as client:
            async with client.stream("POST", config.url.rstrip("/") + "/run", json=body) as response:
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", "replace")
                    return error(context, f"harness refused: HTTP {response.status_code} {raw[:300]}")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type")
                    if kind == "progress":
                        await progress(str(event.get("text") or ""))
                    elif kind == "text":
                        text_parts.append(str(event.get("delta") or ""))
                    elif kind == "result":
                        result = event
                    elif kind == "error":
                        failure = str(event.get("message") or "harness error")
    except httpx.HTTPError as exc:
        return error(context, f"harness unreachable: {type(exc).__name__}: {exc}")
    elapsed = time.monotonic() - started
    if result is None:
        return error(context, f"{vendor} did not finish: {failure or 'no result'}" + (f"\n\nPartial output:\n{clip(''.join(text_parts), 4000)}" if text_parts else ""))
    usage = result.get("usage") or {}
    if manager is not None and usage:
        try:
            await manager.usage.record(
                UsageRecord(
                    provider_id=f"harness:{vendor}",
                    model=str(result.get("model") or body["model"] or vendor),
                    purpose="delegate",
                    raw={**usage, "reported_cost_usd": result.get("cost_usd"), "turns": result.get("turns"), "harness_session": result.get("session_id")},
                    normalized=normalize_usage(usage),
                    cost_usd=0.0,  # a subscription: no per-call price
                    duration_ms=int(result.get("duration_ms") or elapsed * 1000),
                    run_id=context.run_id,
                    session_id=context.session_id,
                )
            )
        except Exception:  # noqa: BLE001 — bookkeeping must not fail the tool
            pass
    answer = str(result.get("text") or "").strip() or "".join(text_parts).strip()
    footer = f"[{vendor}{' ' + str(body['model']) if body['model'] else ''} · {result.get('turns') or '?'} turn(s) · {elapsed:.0f}s · in {usage.get('input_tokens', 0)} out {usage.get('output_tokens', 0)} tokens · subscription]"
    if result.get("ok"):
        return ok(context, f"{clip(answer, services.max_tool_output_chars)}\n\n{footer}", vendor=vendor, turns=result.get("turns"), harness_session=result.get("session_id"))
    reason = result.get("error") or failure or result.get("subtype") or "the harness reported a failure"
    return error(context, f"{vendor} did not complete: {reason}\n\n{clip(answer, 6000)}\n\n{footer}", vendor=vendor)


TOOLS = [delegate]

__all__ = ["TOOLS"]
