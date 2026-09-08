"""``ServiceStart`` / ``ServiceStop`` / ``ServiceList`` / ``ServiceLogs`` — processes that outlive the turn."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import clip, error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("services") if manager is not None else None


def _line(s: dict) -> str:  # type: ignore[type-arg]
    where = f" → {s['url']}" if s.get("url") else ""
    note = f" ({s['note']})" if s.get("note") else ""
    return f"- {s['name']} [{s['status']}]{where} pid {s.get('pid')} · {s['command'][:80]}{note}"


@tool(
    name="ServiceStart",
    description=(
        "Run a long-lived process (a demo site, a dev server, a worker) detached from this turn, in the "
        "workspace, with its output in a log the operator and you can read. port='auto' (default) picks a "
        "free port from the range the container publishes and passes it as $PORT — bind to 0.0.0.0 and use "
        "it, e.g. `python3 -m http.server $PORT --bind 0.0.0.0` or `npx vite --host 0.0.0.0 --port $PORT`; "
        "the result carries the URL the operator opens from their network. port='none' for a process without "
        "a listener. restart=true (default) brings it back after a rebuild of the bot. Stop what is no longer "
        "needed with ServiceStop; a command that exits within a second is reported with its log instead."
    ),
)
async def service_start(context: ToolContext, name: str, command: str, cwd: str | None = None, port: str = "auto", restart: bool = True) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "services are not available")
    try:
        s = await hook("start", session_id=context.session_id, name=name, command=command, cwd=cwd, port=port, restart=restart)
    except (ValueError, RuntimeError, KeyError) as exc:
        return error(context, str(exc))
    where = f"reachable at {s['url']} (inside the container: http://127.0.0.1:{s['port']})" if s.get("url") else "no port"
    return ok(context, f"service {s['name']!r} running (pid {s['pid']}), {where}. Log: ServiceLogs({s['name']!r}).", url=s.get("url"), port=s.get("port"), pid=s.get("pid"))


@tool(name="ServiceStop", description="Stop one of this session's services by name (SIGTERM, then SIGKILL after a few seconds). It will not be restarted after a rebuild.")
async def service_stop(context: ToolContext, name: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "services are not available")
    try:
        s = await hook("stop", session_id=context.session_id, name=name)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, f"service {s['name']!r} {s['status']}")


@tool(name="ServiceList", description="This session's services: name, status, URL, pid and command.")
async def service_list(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "services are not available")
    items = await hook("list", session_id=context.session_id)
    return ok(context, "\n".join(_line(s) for s in items) or "(no services)")


@tool(name="ServiceLogs", description="The last lines of a service's log (stdout and stderr together).")
async def service_logs(context: ToolContext, name: str, lines: int = 60) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "services are not available")
    try:
        text = await hook("logs", session_id=context.session_id, name=name, lines=lines)
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, clip(text, services_for(context).max_tool_output_chars))


TOOLS = [service_start, service_stop, service_list, service_logs]

__all__ = ["TOOLS"]
