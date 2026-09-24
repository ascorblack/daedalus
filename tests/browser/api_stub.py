"""The API answers every browser harness in this directory has to agree on.

Each script here invents an installation of its own — a session of three thousand synthetic
messages, a small studio's agents — but none of them reaches a screen at all until a handful of
routes answer sensibly. The app asks whether a model is configured before it draws anything, and
what this installation is allowed to do before it draws the navigation; a stub that answers ``{}``
to those sits on the "Add a model" screen until its selector times out, which is exactly what
happened to ``perf_session.py`` when the onboarding gate landed in the app and only
``screenshots.py`` was taught about it.

So the gates live here, once. A script answers what it has invented and falls back to this table
for the rest, and records every ``/api/`` path it did not recognise so the run can fail with the
list: a gate added to the app cannot be answered by one harness and silently missed by another.

``expect_app`` is here for the same reason one level down: before any of this matters, the address
a harness was pointed at has to be *this* build and not something else that happens to hold the
port. It is checked once, in a sentence, rather than discovered as a selector that never appears.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

# Outside services_port_range (8100-8119), the range this product hands to an agent's own preview
# servers: a harness that serves its build into that range competes with the installation running
# beside it, and loses silently — the browser is pointed at the address either way.
DEFAULT_PORT = 8163
DEFAULT_APP = f"http://127.0.0.1:{DEFAULT_PORT}/app"


def folders(path: str, *, reachable: bool = True, writable: bool | None = None) -> list[dict[str, object]]:
    """A project's folders as the host reports them, for a project whose one folder is ``path``."""
    return [{
        "id": "f-" + path.rstrip("/").rsplit("/", 1)[-1],
        "path": path, "label": "", "env": "container", "is_git": False, "readonly": False, "position": 0, "managed": False,
        "reachable": reachable, "writable": reachable if writable is None else writable,
    }]

GATES: dict[str, object] = {
    "/api/maintenance": {"notice": None},
    "/api/conversation-search/settings": {"mode": "off", "paused": False, "reason": "off", "busy": False, "indexed": 0, "pending": 0, "label": "Multilingual E5 Small", "size_bytes": 135429554, "licence": "MIT", "installed": False},
    # Drawn before any screen: no model means the whole app is the "Add a model" flow.
    "/api/onboarding": {"has_model": True, "presets": 1, "default_preset": "p", "providers": [], "needs": [], "message": ""},
    # Decides which screens the navigation has at all.
    "/api/capabilities": {
        "selfdev": {"mode": "off", "configured": "off", "reasons": [], "missing": [], "tools": []},
        # The shell marks the Components entry from this; a stub without it marks nothing, which is
        # the right answer for a harness that has invented no missing component.
        "components": {"mode": "docker", "missing": [], "needed": [], "installing": ""},
    },
    # Settings asks what the installation is missing before it draws its index.
    "/api/components": {"mode": "docker", "launcher": False, "components": [], "disk_bytes": 0, "missing": [], "busy": ""},
    "/api/dependencies": {"capability": {"mode": "docker", "python": True, "system": True, "manager": "apt", "reason": ""}, "tools": [{"name": "python", "available": True, "version": "Python 3.12"}, {"name": "gcc", "available": False, "version": ""}], "packages": [], "recipe": {"python": [], "system": []}, "models": [{"id": "default", "label": "Test model"}], "proposal": None, "job": None},
    "/api/prompt-change": {"models": [{"id": "default", "label": "Test model"}], "proposal": None},
    "/api/auth/me": {"user": "operator"},
    "/api/auth/config": {"passkeys": 1},
    "/api/status": {"ok": True},
    # The badge on the Inbox entry of the navigation.
    "/api/notifications/summary": {"unseen": 0, "needs_you": 0},
    "/api/modes": {},
    "/api/commands": [],
    "/api/asr": {"configured": False, "reason": "", "provider": "", "model": "", "max_seconds": 120, "autosend": False},
    "/api/proposals": [],
    "/api/schedules": [],
    "/api/sessions": {"sessions": [], "projects": []},
    # The shell asks which projects there are before it draws the rail.
    "/api/projects": [],
    # The composer offers the voice page only where the installation has one; a harness has none.
    "/api/voice": {"enabled": False},
    # Terminal environments and the terminals in them: a container environment that works, a host
    # one that is not installed, and no terminals yet.
    "/api/terminals": {
        "envs": [
            {"env": "container", "available": True, "reason": "", "version": "0.1.0", "sandbox": True, "shell": "/bin/bash", "home": "/root", "port_range": "8120-8139", "public_host": "", "preview_poll_ms": 3000},
            {"env": "host", "available": False, "reason": "not_installed", "version": "", "sandbox": False, "shell": "", "home": "", "port_range": "", "public_host": "", "preview_poll_ms": 3000},
        ],
        "terminals": [],
    },
}


SHARED_WRITES: dict[tuple[str, str], tuple[int, str, str]] = {
    # Every signed-in window reports what it shows, whatever screen a harness drives; the host
    # answers with no content.
    ("POST", "/api/presence"): (204, "application/json", ""),
}


def answer_shared(method: str, path: str) -> tuple[int, str, str] | None:
    """The answer every harness gives the same way: ``(status, content type, body)``, or ``None``.

    ``path`` may be a whole URL; the query string and everything before ``/api/`` are ignored. A
    harness asks this where it used to look ``GATES`` up, after its own routes, so what it invented
    still wins.
    """
    path = path.split("?", 1)[0]
    path = path[path.index("/api/"):] if "/api/" in path else path
    write = SHARED_WRITES.get((method.upper(), path))
    if write is not None:
        return write
    if path in GATES:
        return 200, "application/json", json.dumps(GATES[path])
    return None


def fulfil_shared(route) -> bool:  # type: ignore[no-untyped-def]
    """:func:`answer_shared` for a Playwright route: ``True`` when it answered."""
    shared = answer_shared(route.request.method, route.request.url)
    if shared is None:
        return False
    status, content_type, body = shared
    route.fulfill(status=status, content_type=content_type, body=body)
    return True


def serve_shared_post(handler, unhandled: Unhandled) -> None:  # type: ignore[no-untyped-def]
    """``do_POST`` for a harness built on ``http.server``, which otherwise answers every write 501.

    The body is read whatever it is, so the connection stays usable; a write nobody here knows is
    recorded like an unknown read and answered 404.
    """
    length = int(handler.headers.get("Content-Length") or 0)
    if length:
        handler.rfile.read(length)
    shared = answer_shared("POST", handler.path)
    if shared is None:
        unhandled.record(handler.path.split("?", 1)[0])
        shared = (404, "application/json", '{"detail": "Not Found"}')
    status, content_type, body = shared
    data = body.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if data:
        handler.wfile.write(data)


class Unhandled:
    """Every ``/api/`` path a stub had no answer for, so a run can end by saying so."""

    def __init__(self) -> None:
        self.paths: set[str] = set()

    def record(self, path: str) -> None:
        self.paths.add(path)

    def report(self) -> int:
        """Print what was missed. Non-zero when something was, which is the run's exit code."""
        if not self.paths:
            return 0
        print("\nthe stub had no answer for these API paths (add them here or to tests/browser/api_stub.py):")
        for path in sorted(self.paths):
            print(f"  {path}")
        return 1


def expect_app(base: str) -> None:
    """Refuse to drive a browser at an address that is not the built Mini App.

    ``serve_app.py`` is normally started in the background, and a port it could not bind leaves
    something else answering there — an agent's own preview server, a stale run. The browser loads
    that page quite happily, and the failure arrives much later as a selector timeout on ``.screen``
    that reads like a broken component. One request up front says what actually happened.
    """
    try:
        with urllib.request.urlopen(f"{base}/index.html", timeout=5) as answer:  # noqa: S310
            body = answer.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        print(f"nothing is serving the app at {base} ({exc}); build it and start tests/browser/serve_app.py", file=sys.stderr)
        raise SystemExit(1) from None
    if "/app/assets/" not in body:
        print(f"{base} answers, but not with the built Mini App — something else holds that port", file=sys.stderr)
        raise SystemExit(1)


__all__ = ["DEFAULT_APP", "DEFAULT_PORT", "GATES", "Unhandled", "answer_shared", "expect_app", "fulfil_shared", "serve_shared_post"]

# Small documents with deliberately different structures make the explorer and preview checks
# exercise parsing, navigation and media decoding without reading anybody's real workspace.
FILE_TEXT = {
    "src/main.py": "# A small example\nfrom pathlib import Path\n\ndef greeting(name):\n    return f'Hello {name}'\n\nprint(greeting('reader'))\n",
    "src/helper.ts": "export const count: number = 42;\n",
    "data/menu.json": '{"items":[{"name":"Bread","price":3}]}',
    "data/sample.json": '{"title":"Example","items":[{"name":"Bread","price":3}],"ready":true}',
    "data/sample.csv": 'name,price,note\nBread,3,"fresh, daily"\nTea,2,hot\n',
    "data/sample.tsv": "name\tprice\nBread\t3\nTea\t2\n",
    "site/index.html": """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>
body{margin:0;padding:32px;color:#e8eaf3;background:#111421;font:14px/1.6 system-ui}h1{font-size:28px;margin:0 0 8px}p{color:#aab2cb}.status{display:inline-block;color:#65d8a3;border:1px solid #315a50;border-radius:24px;padding:6px 14px}.facts{display:flex;gap:12px;margin:28px 0}.facts article{flex:1;padding:16px;background:#1a1f31;border:1px solid #2b3044;border-radius:12px}.facts b{display:block;font-size:24px}.facts span{color:#aab2cb;font-size:12px}section{padding:20px;background:#1a1f31;border:1px solid #2b3044;border-radius:12px;margin:20px 0}h2{font-size:18px;margin:0 0 14px}table{width:100%;border-collapse:collapse}td{border-bottom:1px solid #2b3044;padding:10px 0}a{color:#b0beff;margin-right:16px}@media(max-width:420px){body{padding:20px}.facts{flex-wrap:wrap}}
</style></head><body><h1>Bakery report</h1><p>The seasonal menu, checked on a phone.</p><div class="status">All checks passed</div><div class="facts"><article><b>27</b><span>Menu items</span></article><article><b>41</b><span>Links checked</span></article><article><b>46 KB</b><span>Hero image</span></article></div><section><h2>What changed</h2><p>Seasonal items now come first. Prices read from the same sheet as the shop menu. The hero image loads quickly on a small screen.</p></section><section id="details"><h2>Details section</h2><table><tr><td>Mobile performance</td><td>98 / 100</td></tr><tr><td>Broken links</td><td>0</td></tr><tr><td>Price mismatches</td><td>0</td></tr></table></section><a href="next.html">Next page</a><a href="#details">Details</a></body></html>""",
    "site/next.html": '<!doctype html><html><body><h1>Second page</h1><a href="index.html">First page</a></body></html>',
    "change.diff": "diff --git a/menu.py b/menu.py\n--- a/menu.py\n+++ b/menu.py\n@@ -1,2 +1,2 @@\n-old = 1\n+new = 2\n keep = True\n",
    "change.patch": "--- a/menu.py\n+++ b/menu.py\n@@ -1 +1 @@\n-old = 1\n+new = 2\n",
}


def file_entries(path: str) -> list[dict]:
    """One level only, the same contract as the directory browse route."""
    prefix = f"{path}/" if path else ""
    entries = {}
    for name, body in FILE_TEXT.items():
        if not name.startswith(prefix):
            continue
        child = name[len(prefix):].split("/", 1)[0]
        entries[child] = {"name": child, "dir": "/" in name[len(prefix):], "size": len(body), "mtime": 0}
    return list(entries.values())


def file_search(path: str, query: str, limit: int = 200) -> dict | None:
    """Both bounded search routes, shared by screenshots and behaviour checks."""
    import fnmatch
    if path.endswith("/files/search"):
        names = [name for name in FILE_TEXT if query.lower() in name.rsplit("/", 1)[-1].lower() or fnmatch.fnmatch(name.lower(), query.lower()) or fnmatch.fnmatch(name.rsplit("/", 1)[-1].lower(), query.lower())]
        return {"query": query, "results": [{"path": name, "kind": "file", "size": len(FILE_TEXT[name]), "mtime": 0} for name in names[:limit]], "truncated": len(names) > limit, "engine": "walk"}
    if path.endswith("/files/grep"):
        hits = [{"path": name, "line": i, "text": line} for name, body in FILE_TEXT.items() for i, line in enumerate(body.splitlines(), 1) if query.lower() in line.lower()]
        return {"query": query, "hits": hits[:limit], "truncated": len(hits) > limit}
    return None
