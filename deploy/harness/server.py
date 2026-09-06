"""Harness: the operator's coding-agent subscriptions, reachable by the agent as a service.

The vendor CLIs (``claude``, ``codex``, ``grok``) and their logins live in this container
only; the agent container talks to it over HTTP and never sees a token. Two ways in:

``POST /run`` hands a bounded task to a harness. The CLI runs headlessly in a session
workspace with its own tools and loop; progress, text and the final result stream back as
NDJSON. This is the sanctioned shape for every vendor: the subscription is used through
the vendor's own product.

(Grok and Codex as *model providers* live in the key proxy, which speaks to their backends
with the CLIs' own logins; this container is only for delegated work.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

logging.basicConfig(level=logging.WARNING, format="%(asctime)s harness %(levelname)s: %(message)s")
logger = logging.getLogger("harness")

ROOTS = tuple(Path(p) for p in os.environ.get("HARNESS_ROOTS", "/srv/workspaces:/srv/state/worktrees").split(":") if p)
"""Working directories a task may use: the session workspaces and the self-development worktrees."""
MAX_PARALLEL = int(os.environ.get("HARNESS_MAX_PARALLEL", "4"))
DEFAULT_TIMEOUT = float(os.environ.get("HARNESS_TIMEOUT_SECONDS", "1800"))
PROGRESS_CHARS = 160
GROK_READ_ONLY_TOOLS = "read_file,list_dir,grep,web_search,web_fetch"
CLAUDE_READ_ONLY_TOOLS = "Read,Grep,Glob,LS,WebFetch,WebSearch"
VENDORS = ("claude", "codex", "grok")


class HarnessError(RuntimeError):
    pass


def allowed_cwd(raw: str | None) -> Path:
    if not raw:
        raise HarnessError("cwd is required")
    path = Path(raw).resolve()
    if not any(path == root or root in path.parents for root in ROOTS):
        raise HarnessError(f"cwd must be under {', '.join(str(r) for r in ROOTS)}")
    if not path.is_dir():
        raise HarnessError(f"cwd does not exist: {path}")
    return path


def cli_status() -> dict[str, Any]:
    """Which CLIs are installed and logged in (by their credential files; no network)."""
    home = Path.home()
    return {
        "claude": {"installed": shutil.which("claude") is not None, "logged_in": (home / ".claude" / ".credentials.json").is_file()},
        "codex": {"installed": shutil.which("codex") is not None, "logged_in": (home / ".codex" / "auth.json").is_file()},
        "grok": {"installed": shutil.which("grok") is not None, "logged_in": (home / ".grok" / "auth.json").is_file()},
    }


# -- argv builders ------------------------------------------------------------------------


def claude_argv(prompt: str, *, model: str, effort: str, max_turns: int, read_only: bool, system_prompt: str) -> list[str]:
    argv = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--no-session-persistence", "--strict-mcp-config", "--max-turns", str(max_turns)]
    argv += ["--tools", CLAUDE_READ_ONLY_TOOLS] if read_only else ["--permission-mode", "bypassPermissions"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    return argv


def codex_argv(prompt: str, *, model: str, effort: str, cwd: Path, out_file: Path) -> list[str]:
    argv = ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--dangerously-bypass-approvals-and-sandbox", "-C", str(cwd), "-o", str(out_file)]
    if model:
        argv += ["-m", model]
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    return argv + [prompt]


def grok_argv(prompt: str, *, model: str, max_turns: int, read_only: bool, cwd: Path, tools: str | None = None, system_prompt: str = "") -> list[str]:
    argv = ["grok", "--no-auto-update", "-p", prompt, "--output-format", "streaming-messages-json", "--include-partial-messages", "--always-approve", "--max-turns", str(max_turns), "--cwd", str(cwd)]
    if model:
        argv += ["-m", model]
    if tools is not None:
        argv += ["--tools", tools]
    elif read_only:
        argv += ["--tools", GROK_READ_ONLY_TOOLS]
    if system_prompt:
        argv += ["--system-prompt-override", system_prompt]
    return argv


# -- event translation ----------------------------------------------------------------------


def _brief(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text[:PROGRESS_CHARS] + ("…" if len(text) > PROGRESS_CHARS else "")


def translate_messages_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Claude Code's stream-json (Grok speaks the same dialect) → harness events."""
    kind = event.get("type")
    if kind == "stream_event":
        inner = event.get("event") or {}
        if inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [{"type": "text", "delta": delta["text"]}]
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                return [{"type": "thinking", "delta": delta["thinking"]}]
        return []
    if kind == "assistant":
        out = []
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use":
                out.append({"type": "progress", "text": f"→ {block.get('name')} {_brief(block.get('input') or {})}"})
        return out
    if kind == "result":
        usage = event.get("usage") or {}
        return [{
            "type": "result",
            "text": str(event.get("result") or ""),
            "ok": not event.get("is_error", False) and event.get("subtype", "success") == "success",
            "subtype": event.get("subtype"),
            "turns": event.get("num_turns"),
            "cost_usd": event.get("total_cost_usd"),
            "session_id": event.get("session_id"),
            "usage": {
                "input_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
                "reasoning_tokens": int((usage.get("output_tokens_details") or {}).get("thinking_tokens") or usage.get("reasoning_tokens") or 0),
            },
        }]
    return []


def translate_codex_event(event: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Codex ``exec --json`` JSONL → harness events; the final result is assembled by the runner."""
    kind = event.get("type")
    if kind == "thread.started":
        state["session_id"] = event.get("thread_id")
        return []
    if kind in ("item.started", "item.completed", "item.updated"):
        item = event.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message" and kind == "item.completed" and item.get("text"):
            state["last_text"] = item["text"]
            return [{"type": "text", "delta": item["text"]}]
        if item_type == "command_execution" and kind == "item.started":
            return [{"type": "progress", "text": f"→ exec {_brief(item.get('command') or '')}"}]
        if item_type in ("file_change", "web_search", "mcp_tool_call") and kind == "item.started":
            return [{"type": "progress", "text": f"→ {item_type} {_brief(item.get('changes') or item.get('query') or item.get('tool') or '')}"}]
        if item_type == "reasoning" and kind == "item.completed" and item.get("text"):
            return [{"type": "thinking", "delta": item["text"]}]
        return []
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        state["usage"] = {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_input_tokens": int(usage.get("cached_input_tokens") or 0),
            "reasoning_tokens": int(usage.get("reasoning_output_tokens") or 0),
        }
        state["turns"] = int(state.get("turns") or 0) + 1
        return []
    if kind in ("turn.failed", "error"):
        message = (event.get("error") or {}).get("message") if isinstance(event.get("error"), dict) else event.get("message")
        state["error"] = str(message or "codex failed")
        return [{"type": "error", "message": state["error"]}]
    return []


# -- running a CLI ----------------------------------------------------------------------------


async def run_cli(argv: list[str], *, cwd: Path, timeout: float, on_line: Any, stdin_data: bytes | None = None) -> tuple[int, str]:
    """Spawn the CLI, feed every stdout line to ``on_line``; returns (exit code, stderr tail)."""
    env = {**os.environ, "CI": "1", "TERM": "dumb", "NO_COLOR": "1"}
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    stderr_chunks: list[bytes] = []

    async def _drain_err() -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(4096):
            stderr_chunks.append(chunk)
            if sum(len(c) for c in stderr_chunks) > 64_000:
                del stderr_chunks[0]

    async def _pump() -> None:
        assert proc.stdout is not None
        if stdin_data is not None and proc.stdin is not None:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            await on_line(line.decode("utf-8", "replace").rstrip("\n"))

    err_task = asyncio.create_task(_drain_err())
    try:
        await asyncio.wait_for(_pump(), timeout=timeout)
        await asyncio.wait_for(proc.wait(), timeout=30)
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        await err_task
        raise HarnessError(f"timed out after {timeout:.0f} s") from None
    finally:
        if not err_task.done():
            await err_task
    return int(proc.returncode or 0), b"".join(stderr_chunks).decode("utf-8", "replace")[-2000:]


async def run_task(body: dict[str, Any], emit: Any) -> dict[str, Any]:
    """Run one delegated task; ``emit`` receives every intermediate event; returns the result event."""
    vendor = str(body.get("vendor") or "")
    if vendor not in VENDORS:
        raise HarnessError(f"vendor must be one of {', '.join(VENDORS)}")
    status = cli_status()[vendor]
    if not status["installed"]:
        raise HarnessError(f"{vendor} is not installed in the harness container")
    if not status["logged_in"]:
        raise HarnessError(f"{vendor} is not logged in; run `{vendor} login` on the host")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HarnessError("prompt is required")
    cwd = allowed_cwd(body.get("cwd"))
    model = str(body.get("model") or "")
    effort = str(body.get("effort") or "")
    max_turns = int(body.get("max_turns") or 40)
    read_only = bool(body.get("read_only", False))
    timeout = float(body.get("timeout_seconds") or DEFAULT_TIMEOUT)
    system_prompt = str(body.get("system_prompt") or "")
    if read_only:
        prompt += "\n\nThis is a read-only task: do not create, modify or delete files, and do not run commands with side effects."
    started = time.monotonic()
    result: dict[str, Any] = {}
    text_parts: list[str] = []
    codex_state: dict[str, Any] = {}
    out_file = Path("/tmp") / f"codex-{uuid.uuid4().hex}.txt"

    async def on_line(line: str) -> None:
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        events = translate_codex_event(event, codex_state) if vendor == "codex" else translate_messages_event(event)
        for ev in events:
            if ev["type"] == "result":
                result.update(ev)
            else:
                if ev["type"] == "text":
                    text_parts.append(ev["delta"])
                await emit(ev)

    if vendor == "claude":
        argv = claude_argv(prompt, model=model, effort=effort, max_turns=max_turns, read_only=read_only, system_prompt=system_prompt)
    elif vendor == "grok":
        argv = grok_argv(prompt, model=model, max_turns=max_turns, read_only=read_only, cwd=cwd)
    else:
        argv = codex_argv(prompt, model=model, effort=effort, cwd=cwd, out_file=out_file)
    code, stderr = await run_cli(argv, cwd=cwd, timeout=timeout, on_line=on_line)
    elapsed = int((time.monotonic() - started) * 1000)
    if vendor == "codex":
        final = ""
        try:
            final = out_file.read_text(encoding="utf-8").strip()
        except OSError:
            final = str(codex_state.get("last_text") or "")
        finally:
            out_file.unlink(missing_ok=True)
        result = {
            "type": "result", "text": final, "ok": code == 0 and not codex_state.get("error"), "subtype": "error" if codex_state.get("error") else "success",
            "turns": codex_state.get("turns"), "cost_usd": None, "session_id": codex_state.get("session_id"), "usage": codex_state.get("usage") or {},
            "error": codex_state.get("error"),
        }
    if not result:
        result = {"type": "result", "text": "".join(text_parts), "ok": code == 0, "subtype": "success" if code == 0 else "error", "usage": {}}
    if code != 0 and not result.get("error"):
        result["error"] = f"{vendor} exited with {code}: {stderr.strip()[-600:]}"
        result["ok"] = False
    result["vendor"] = vendor
    result["model"] = model
    result["duration_ms"] = elapsed
    return result


# -- HTTP -------------------------------------------------------------------------------------------


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "vendors": cli_status(), "roots": [str(r) for r in ROOTS], "max_parallel": MAX_PARALLEL})


async def handle_run(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    response = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-cache"})
    await response.prepare(request)

    async def emit(event: dict[str, Any]) -> None:
        await response.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))

    sem: asyncio.Semaphore = request.app["sem"]
    try:
        async with sem:
            result = await run_task(body, emit)
        await emit(result)
    except HarnessError as exc:
        await emit({"type": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — the caller must see why, and the service must stay up
        logger.exception("run failed")
        await emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    try:
        await response.write_eof()
    except ConnectionResetError:
        pass
    return response


def make_app() -> web.Application:
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app["sem"] = asyncio.Semaphore(MAX_PARALLEL)
    app.router.add_get("/healthz", handle_health)
    app.router.add_post("/run", handle_run)
    return app


def main() -> None:
    port = int(os.environ.get("HARNESS_PORT", "3300"))
    web.run_app(make_app(), host="0.0.0.0", port=port, print=None, access_log=None)


if __name__ == "__main__":
    main()
