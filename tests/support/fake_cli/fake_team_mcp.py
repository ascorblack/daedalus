"""A stand-in for the terminal daemon's ``team-mcp`` command: a stdio MCP server with the two team
tools, ``Report`` and ``AskOrchestrator``, posting to the launch's hook ingress.

It keeps the wire contract the real bridge keeps, so the host side can be built and tested before
that command exists and against the same shapes after:

- ``Report(kind, note, artifacts?)`` → ``POST $DAEDALUS_HOOK_URL/team`` with ``{"tool": "report",
  kind, note, artifacts}``, answered at once with ``recorded``.
- ``AskOrchestrator(question, options?)`` → the same endpoint with ``{"tool": "ask", question,
  options, "daedalus_hold_ms": $DAEDALUS_ASK_HOLD_MS}``: the ingress holds the post until the host
  replies. The reply's ``answer`` (or its text) is the tool's result; a hold that expires (an empty
  204) gives the fixed advice to carry on or report ``needs_input``.

Messages are one JSON object per line, as MCP's stdio transport has them. Any protocol version the
client proposes is accepted, since the tools do not depend on it.
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
        "description": "Tell the orchestrator where the work stands.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["checkpoint", "needs_input", "stuck", "done"]},
                "note": {"type": "string"},
                "artifacts": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["kind", "note"],
        },
    },
    {
        "name": "AskOrchestrator",
        "description": "Ask the orchestrator a question and wait for the answer.",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
            "required": ["question"],
        },
    },
]


def post(body: dict[str, Any], timeout: float) -> tuple[int, bytes]:
    url = os.environ.get("DAEDALUS_HOOK_URL", "").rstrip("/") + "/team"
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


def call(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    if name == "Report":
        status, _ = post({"tool": "report", "kind": arguments.get("kind", "checkpoint"), "note": arguments.get("note", ""), "artifacts": arguments.get("artifacts") or []}, 10)
        return ("recorded", False) if 200 <= status < 300 else (f"the report was not delivered ({status or 'no answer'})", True)
    if name == "AskOrchestrator":
        hold_ms = int(os.environ.get("DAEDALUS_ASK_HOLD_MS") or 300_000)
        status, raw = post({"tool": "ask", "question": arguments.get("question", ""), "options": arguments.get("options") or [], "daedalus_hold_ms": hold_ms}, hold_ms / 1000 + 30)
        if status == 200 and raw.strip():
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                return raw.decode("utf-8", "replace"), False
            if isinstance(body, dict) and "answer" in body:
                return str(body["answer"]), False
            return json.dumps(body), False
        if status in (200, 204):
            return EXPIRED, False
        return f"the question was not delivered ({status or 'no answer'})", True
    return f"no tool {name}", True


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
