"""Serve a built Mini App as a SPA for the browser checks.

The app is built with ``base: "/app/"``, so its assets live under ``/app``. Point this at a
directory that holds ``app/`` (the contents of ``miniapp/dist``); any route that is not a file
falls back to ``app/index.html``, the way the real server does.

    python3 tests/browser/serve_app.py 8163 /path/to/root

The port is deliberately outside ``services_port_range`` (8100-8119), which is the range this
product hands to an agent's own preview servers: a harness that serves the build into that range
is competing with the installation it was built to photograph.
"""
from __future__ import annotations

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8163

ROOT = Path(sys.argv[2] if len(sys.argv) > 2 else ".").resolve()


class Spa(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:  # noqa: D102
        local = super().translate_path(path)
        p = Path(local)
        if not p.is_file() and "." not in p.name:
            # The app is built with base=/app/, so its index lives there; any route falls back to it.
            return str(ROOT / "app" / "index.html")
        return local

    def log_message(self, *args: object) -> None:  # quiet
        pass


if __name__ == "__main__":
    os.chdir(ROOT)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", str(DEFAULT_PORT)))
    try:
        # Loopback: this serves a build to a browser on this machine and has no business on the network.
        server = ThreadingHTTPServer(("127.0.0.1", port), Spa)
    except OSError as exc:
        # Usually this is started in the background with its output redirected, so a traceback here
        # is a file nobody opens: the harness then points a browser at whatever else holds the port
        # and photographs that instead. Say it in one line and leave with a failing status.
        print(f"port {port} is taken ({exc}); nothing is serving the build — pick another port", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"serving {ROOT} on http://127.0.0.1:{port}", flush=True)
    with server:
        server.serve_forever()
