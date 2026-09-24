"""A stand-in for the terminal daemon's ``team-mcp`` command: a stdio MCP server with the two team
tools, ``Report`` and ``AskOrchestrator``, posting to the launch's hook listener.

The unit tests run it because the real command is a Go binary they do not build; the integration
tests run the real one (``Rig(ptyd_bin=…)``). It keeps the real command's wire contract, described in
``docs/architecture/terminals.md`` ("The team tools"):

- ``Report(kind, note, artifacts?, remember?)`` → ``POST $DAEDALUS_HOOK_URL/team?wait_ms=…`` with
  ``{"tool": "report", kind, note, artifacts[, remember]}``, held ``$DAEDALUS_REPORT_HOLD_MS``
  (15 s); the host's reply is the result, and silence means ``recorded``.
- ``AskOrchestrator(question, options?, context?)`` → the same with ``{"tool": "ask", question,
  options[, context]}``, held ``$DAEDALUS_ASK_HOLD_MS`` (5 minutes); silence gives the fixed advice
  to carry on or report ``needs_input``.
- A reply ``{"text": …, "error"?: bool}``, a JSON string or plain text is the tool's result.

Messages are one JSON object per line, as MCP's stdio transport has them. Argument checking is left
to the real command; this one only fills the shape.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

EXPIRED = "No answer yet. Continue with what the brief allows, or call Report with kind needs_input and stop."
TOOLS = [
    {
        "name": "Report",
        "description": "Tell your team how your task stands.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["checkpoint", "needs_input", "stuck", "done"]},
                "note": {"type": "string"},
                "artifacts": {"type": "array", "items": {"type": "string"}},
                "remember": {"type": "string"},
            },
            "required": ["kind", "note"],
        },
    },
    {
        "name": "AskOrchestrator",
        "description": "Ask your project's orchestrator a question you cannot settle yourself.",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}, "context": {"type": "string"}},
            "required": ["question"],
        },
    },
]


def hold_ms(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def post(body: dict[str, Any], hold: int) -> tuple[int, bytes]:
    url = os.environ.get("DAEDALUS_HOOK_URL", "").rstrip("/") + f"/team?wait_ms={hold}"
    timeout = hold / 1000 + 10
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("DAEDALUS_HOOK_TOKEN", ""),
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


def result(raw: bytes) -> tuple[str, bool]:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", "replace"), False
    if isinstance(body, dict) and isinstance(body.get("text"), str):
        return body["text"], body.get("error") is True
    return (body, False) if isinstance(body, str) else (json.dumps(body), False)


def call(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    if name == "Report":
        body: dict[str, Any] = {"tool": "report", "kind": arguments.get("kind", "checkpoint"), "note": arguments.get("note", ""), "artifacts": arguments.get("artifacts") or []}
        if arguments.get("remember"):
            body["remember"] = arguments["remember"]
        silence, hold = "recorded", hold_ms("DAEDALUS_REPORT_HOLD_MS", 15_000)
    elif name == "AskOrchestrator":
        body = {"tool": "ask", "question": arguments.get("question", ""), "options": arguments.get("options") or []}
        if arguments.get("context"):
            body["context"] = arguments["context"]
        silence, hold = EXPIRED, hold_ms("DAEDALUS_ASK_HOLD_MS", 300_000)
    else:
        return f"no tool {name}", True
    status, raw = post(body, hold)
    if 200 <= status < 300:
        return result(raw) if raw.strip() else (silence, False)
    if status in (401, 410):
        return "this session is no longer connected to its team; nobody received the call", True
    return f"the team refused the call ({status or 'no answer'})", True


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            continue  # a notification
        result: Any
        if method == "initialize":
            version = (message.get("params") or {}).get("protocolVersion") or "2025-06-18"
            result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": {"name": "daedalus_team", "version": "fake"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            text, error = call(str(params.get("name")), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": error}
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"no method {method}"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
