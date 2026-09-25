"""A stand-in for a model provider on loopback: OpenAI's chat-completions API, answered from a script.

A real CLI can be pointed at it (pi through a custom provider in ``~/.pi/agent/models.json``, Grok
Build through ``GROK_MODELS_BASE_URL``), so an integration test drives the real CLI through a whole
turn — its tools, its team channel, its end of turn — without a sign-in, without spending anything,
and without leaving the machine.

The script is read from the last user message:

- the self-check's sentence ("Call the Report tool …"), or any message naming ``Report``: a call to
  the CLI's tool whose name ends in ``Report``, with kind ``checkpoint``; after its result, "ready";
- anything else: "ready".

Every request is kept in ``requests`` (the tools offered, the system prompt), for a test to look at
what the CLI sent.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "stub-model"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return ""


class ModelStub:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: Any) -> None:
                pass

            def _json(self, code: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path.rstrip("/").endswith("/models"):
                    self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "stub", "created": 0}]})
                else:
                    self._json(404, {"error": {"message": "not here"}})

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                stub.requests.append(request)
                self._answer(request, stub.reply(request))

            def _answer(self, request: dict[str, Any], reply: tuple[str, dict[str, Any] | None]) -> None:
                text, call = reply
                if not request.get("stream"):
                    message: dict[str, Any] = {"role": "assistant", "content": text}
                    if call is not None:
                        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}]}
                    self._json(200, {"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": MODEL, "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if call else "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

                def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
                    body = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(f"data: {json.dumps(body)}\n\n".encode())

                chunk({"role": "assistant", "content": ""})
                if call is not None:
                    chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}]})
                    chunk({}, "tool_calls")
                else:
                    chunk({"content": text})
                    chunk({}, "stop")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def reply(self, request: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        messages = request.get("messages") or []
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            return "ready", None
        prompt = _text(next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), ""))
        tools = [str((t.get("function") or t).get("name") or "") for t in request.get("tools") or []]
        report = next((name for name in tools if name.endswith("Report")), "")
        if report and "Report" in prompt:
            return "", {"name": report, "arguments": {"kind": "checkpoint", "note": "self-check"}}
        return "ready", None

    def __enter__(self) -> ModelStub:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


__all__ = ["MODEL", "ModelStub"]
