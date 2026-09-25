"""The host's event stream for a browser check that needs events to arrive while it watches.

The shared stub ends its stream at once (``api_stub.event_stream_hello``), which keeps the app on its
polls; a check that proves a view follows an event must hold a stream open and write the event into
it. This is the server of ``check_notifications.py`` as a reusable piece: start it, point the page's
``/api/events`` at it with ``route``, then ``send`` events.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class EventFeed:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.open: list[queue.Queue[str | None]] = []
        self.seq = 100
        feed = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                lane: queue.Queue[str | None] = queue.Queue()
                with feed.lock:
                    feed.open.append(lane)
                    head = feed.seq
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                try:
                    self.wfile.write(f'event: hello\ndata: {json.dumps({"head": head, "oldest": 1, "server_time": now, "client": ""})}\n\n'.encode())
                    self.wfile.flush()
                    while True:
                        try:
                            item = lane.get(timeout=1.0)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        if item is None:
                            return
                        self.wfile.write(item.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                finally:
                    with feed.lock:
                        if lane in feed.open:
                            feed.open.remove(lane)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/events"

    def route(self, route: Any) -> None:
        """``page.route("**/api/events**", feed.route)``, registered after the page's other routes."""
        query = route.request.url.split("?", 1)[1] if "?" in route.request.url else ""
        route.continue_(url=f"{self.url}?{query}")

    def connected(self) -> int:
        with self.lock:
            return len(self.open)

    def send(self, kind: str, payload: dict, *, project: str | None = None, staff: str | None = None, session: str | None = None, terminal: str | None = None) -> None:
        with self.lock:
            self.seq += 1
            event = {"seq": self.seq, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "type": kind, "project_id": project, "session_id": session, "staff_id": staff, "terminal_id": terminal, "payload": payload}
            frame = f"id: {self.seq}\nevent: {kind}\ndata: {json.dumps(event)}\n\n"
            for lane in self.open:
                lane.put(frame)

    def close(self) -> None:
        with self.lock:
            for lane in self.open:
                lane.put(None)
        self.server.shutdown()


__all__ = ["EventFeed"]
