"""Serve a built Mini App as a SPA for the browser checks.

The app is built with ``base: "/app/"``, so its assets live under ``/app``. Point this at a
directory that holds ``app/`` (the contents of ``miniapp/dist``); any route that is not a file
falls back to ``app/index.html``, the way the real server does.

    python3 tests/browser/serve_app.py 8101 /path/to/root
"""
from __future__ import annotations

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8899"))
    ThreadingHTTPServer(("0.0.0.0", port), Spa).serve_forever()
