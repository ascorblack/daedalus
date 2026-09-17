"""How long after the last token does the session stop looking like it is still writing?

The complaint this answers: the answer finished, and for a couple of seconds — sometimes ten — the
app went on showing a run in progress with a cursor blinking on the last line of it. The host is
the reason there is a gap at all (a finished run still has files to snapshot and other fronts to
tell), but the gap is not supposed to be visible: the turn ends on the model's own full stop, and
the chip flips on the event the host sends the moment the run is over.

The stub here is the worst honest case. It streams an answer, ends it with ``message_stop`` /
``end_turn``, and then goes on reporting the session as running for ``--gap`` seconds before it
sends ``run_settled`` — which is what a run with slow housekeeping behind it used to look like.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    CHROMIUM=... python3 tests/browser/check_settle.py

Exit 0 when the cursor is gone within 300 ms of the final token and the run chip within 300 ms of
``run_settled``, and non-zero with the measured numbers when either lags.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import GATES, Unhandled  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "miniapp" / "dist"
SESSION = "a1b2c3d4e5f6"
ANSWER = "The log shows one slow query on the events table, and the plan for it is below."
BUDGET_MS = 300
"""What counts as "at once" here: a frame, a repaint and the room a throttled browser needs."""


class Stub(BaseHTTPRequestHandler):
    gap = 5.0
    settled = False
    housekeeping = False
    written = ""
    lock = threading.Lock()
    unhandled = Unhandled()

    def log_message(self, *args: object) -> None:
        return

    def _send(self, body: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _detail(self) -> dict:
        with Stub.lock:
            done = Stub.settled
            text = Stub.written
        messages = [
            {"role": "user", "summary": False, "internal": False, "origin": "operator", "seq": 101, "compaction": None, "archived": None, "headline": "", "text": "Check the run and tell me what the log says.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-13T12:00:00+00:00"},
        ]
        if text:
            # The answer is in the transcript from the moment the message ended, the way the host
            # writes it down before it announces anything.
            messages.append({"role": "assistant", "summary": False, "internal": False, "origin": "", "seq": 102, "compaction": None, "archived": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-13T12:00:05+00:00"})
        return {
            "id": SESSION, "title": "A session", "status": "idle" if done else "running",
            "housekeeping": Stub.housekeeping, "run_id": None if done else "r1",
            "workspace": "/workspace", "workspace_name": "ws", "workspace_own": True, "workspace_sessions": [],
            "pending": None, "model": "some-model", "mode": "", "brief": "", "tools_off": [], "loop": None,
            "services": [], "subagents": [], "usage": {}, "first_seq": 101, "has_older": False,
            "context": {"tokens": 10, "window": 100000, "messages": 2, "summaries": 0, "operator_turns": 1},
            "messages": messages,
        }

    def _stream(self) -> None:
        """One run: an answer, its full stop, a long silence, and then the end of the run."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.write(b"event: message_start\ndata: {}\n\n")
            self.wfile.flush()
            for word in ANSWER.split(" "):
                self.wfile.write(f'event: content_block_delta\ndata: {{"delta": {{"type": "text_delta", "text": "{word} "}}}}\n\n'.encode())
                self.wfile.flush()
                time.sleep(0.03)
            with Stub.lock:
                Stub.written = ANSWER
                Stub.housekeeping = True
            stopped = time.time()
            self.wfile.write(b'event: message_stop\ndata: {"stop_reason": "end_turn"}\n\n')
            self.wfile.flush()
            print(f"stub: final token at +0.00s, message_stop sent; run_settled in {Stub.gap:.1f}s")
            time.sleep(Stub.gap)
            with Stub.lock:
                Stub.settled = True
                Stub.housekeeping = False
            self.wfile.write(b'event: run_settled\ndata: {"status": "completed", "housekeeping": false}\n\n')
            self.wfile.flush()
            print(f"stub: run_settled sent at +{time.time() - stopped:.2f}s")
            while True:
                time.sleep(1)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            return

    def do_GET(self) -> None:  # noqa: N802
        raw, _, _qs = self.path.partition("?")
        if raw.endswith("/stream"):
            return self._stream()
        if raw.startswith("/api/"):
            if raw == f"/api/sessions/{SESSION}":
                return self._send(json.dumps(self._detail()).encode(), "application/json")
            if raw == "/api/sessions":
                return self._send(json.dumps([{"id": SESSION, "title": "A session", "status": "running", "created_at": "2026-09-13T12:00:00+00:00", "last_message_at": "2026-09-13T12:00:05+00:00", "run_id": "r1"}]).encode(), "application/json")
            if raw == "/api/auth/me":
                return self._send(json.dumps({"user_id": 1, "via": "token"}).encode(), "application/json")
            if raw in GATES:
                return self._send(json.dumps(GATES[raw]).encode(), "application/json")
            if raw.startswith(f"/api/sessions/{SESSION}/"):
                return self._send(b"[]", "application/json")
            Stub.unhandled.record(raw)
            return self._send(b"[]", "application/json")
        rel = raw[len("/app/"):] if raw.startswith("/app/") else raw.lstrip("/")
        target = DIST / rel
        if not target.is_file() or "." not in target.name:
            target = DIST / "index.html"
        kind = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".json": "application/json", ".png": "image/png", ".webmanifest": "application/manifest+json"}.get(target.suffix, "application/octet-stream")
        self._send(target.read_bytes(), kind)


WATCH = """
() => {
  window.__settle = { cursor: null, chip: null, start: performance.now() };
  const look = () => {
    const s = window.__settle;
    if (s.cursor === null && !document.querySelector(".answer.streaming")) s.cursor = performance.now();
    if (s.chip === null && !document.querySelector(".livebar")) s.chip = performance.now();
    requestAnimationFrame(look);
  };
  requestAnimationFrame(look);
}
"""


def run(gap: float, chromium: str, base: str, port: int) -> int:
    Stub.gap = gap
    server = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    problems: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.goto(f"{base}/agents/{SESSION}?token=t", wait_until="commit")
            page.wait_for_selector(".answer.streaming", timeout=20000)
            page.evaluate(WATCH)
            stopped = None
            # The last token is the moment the whole answer is on the screen.
            page.wait_for_function("() => document.querySelector('.answer') && document.querySelector('.answer').textContent.includes('plan for it is below')", timeout=20000)
            stopped = page.evaluate("() => performance.now()")
            page.wait_for_timeout(int(gap * 1000) + 2000)
            seen = page.evaluate("() => window.__settle")
            cursor_ms = (seen["cursor"] - stopped) if seen["cursor"] else None
            chip_ms = (seen["chip"] - stopped) if seen["chip"] else None
            print(f"cursor gone {cursor_ms if cursor_ms is None else round(cursor_ms)} ms after the last token (budget {BUDGET_MS} ms)")
            print(f"run chip gone {chip_ms if chip_ms is None else round(chip_ms)} ms after the last token (the host said so at {gap * 1000:.0f} ms)")
            if cursor_ms is None:
                problems.append("the cursor was still under the answer when the run had been over for seconds")
            elif cursor_ms > BUDGET_MS:
                problems.append(f"the cursor stayed {cursor_ms:.0f} ms past the last token")
            if chip_ms is None:
                problems.append("the session still reads as running after run_settled")
            elif chip_ms > gap * 1000 + BUDGET_MS:
                problems.append(f"the run chip stayed {chip_ms - gap * 1000:.0f} ms past run_settled")
            context.close()
            browser.close()
    finally:
        server.shutdown()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=5.0, help="seconds the host keeps saying 'running' after the last token")
    ap.add_argument("--port", type=int, default=8164)
    ap.add_argument("--chromium", default="/usr/local/bin/chromium")
    args = ap.parse_args()
    import os

    failed = run(args.gap, os.environ.get("CHROMIUM", args.chromium), f"http://127.0.0.1:{args.port}/app", args.port)
    sys.exit(failed or Stub.unhandled.report())
