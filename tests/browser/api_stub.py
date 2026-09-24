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


def folder(path: str, *, position: int = 0, label: str = "", env: str = "container", reach: str = "agents", reachable: bool = True, writable: bool | None = None, readonly: bool = False, is_git: bool = False) -> dict[str, object]:
    """One folder of a project as ``/api/projects`` reports it."""
    return {
        "id": "f-" + path.rstrip("/").rsplit("/", 1)[-1],
        "path": path, "label": label, "env": env, "is_git": is_git, "readonly": readonly, "position": position, "managed": False,
        "reachable": reachable, "writable": (reachable and not readonly) if writable is None else writable, "reach": reach,
    }


def folders(path: str, *, reachable: bool = True, writable: bool | None = None) -> list[dict[str, object]]:
    """A project's folders as the host reports them, for a project whose one folder is ``path``."""
    return [folder(path, reachable=reachable, writable=writable)]


ENVIRONMENTS = {"local": "container", "available": ["container"], "host_bridge": False, "docker": True}
"""Where a folder may live: a Docker installation without the host terminal bridge."""

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
    # The badge on the Inbox entry of the navigation and on the bell.
    "/api/notifications/summary": {"unseen": 0, "needs_you": 0},
    # The centre itself, whichever view the bell's popover or the Inbox asks for: nothing yet.
    "/api/notifications": {"entries": [], "next_before": None, "summary": {"unseen": 0, "needs_you": 0}},
    # Web Push on this device: a harness serves plain http, and its host has no public https address.
    "/api/push/config": {"available": False, "reason": "no_https_url", "public_key": ""},
    "/api/push/subscriptions": {"subscriptions": []},
    "/api/modes": {},
    "/api/commands": [],
    "/api/asr": {"configured": False, "reason": "", "provider": "", "model": "", "max_seconds": 120, "autosend": False},
    "/api/proposals": [],
    "/api/schedules": [],
    "/api/sessions": {"sessions": [], "projects": []},
    # The shell asks which projects there are before it draws the rail.
    "/api/projects": [],
    # The folder form asks where a folder may live before it offers the environment choice.
    "/api/project-environments": ENVIRONMENTS,
    # The composer offers the voice page only where the installation has one; a harness has none.
    "/api/voice": {"enabled": False},
    # Terminal environments and the terminals in them: a container environment that works, a host
    # one that is not installed, and no terminals yet. Every session screen lists its terminals (with
    # `?owner_kind=session&owner_id=…`, answered here by path). A harness that opens terminals installs
    # `terminal_stub.TerminalStub`, which answers the rest of `/api/terminals*` and the WebSocket.
    "/api/terminals": {
        "envs": [
            {"env": "container", "available": True, "reason": "", "version": "0.1.0", "sandbox": "ok", "shell": "/bin/bash", "home": "/root", "port_range": "8120-8139", "public_host": "", "preview_poll_ms": 3000},
            {"env": "host", "available": False, "reason": "not_installed", "version": "", "sandbox": "", "shell": "", "home": "", "port_range": "", "public_host": "", "preview_poll_ms": 3000},
        ],
        "terminals": [],
        "capacity": {"running": 0, "cap": 20, "queued": 0},
    },
}


SHARED_WRITES: dict[tuple[str, str], tuple[int, str, str]] = {
    # Every signed-in window reports what it shows, whatever screen a harness drives; the host
    # answers with no content.
    ("POST", "/api/presence"): (204, "application/json", ""),
    # "Mark all read" in the bell's popover and on the Inbox.
    ("POST", "/api/notifications/seen"): (200, "application/json", json.dumps({"marked": 0, "summary": {"unseen": 0, "needs_you": 0}})),
}


EVENTS = "/api/events"


def event_stream_hello() -> str:
    """The host's event stream as a stub gives it: one comment line, and then the end.

    No ``hello`` on purpose. The app counts a stream as up only once the host has greeted it, and
    while it is up the lists stop polling every few seconds and wait for events instead. A stub
    cannot send those events, so a harness that changes its own answers and waits for the list to
    follow (``check_conversation_search.py`` does) would wait a minute. Without the greeting the app
    stays on its polls, exactly as it behaves when the host is unreachable, and reconnects on its
    backoff (one, two, four seconds and up to fifteen), which costs a harness a request now and then.
    Finite rather than held open, so it never occupies one of the browser's six connections. A
    harness that tests the stream itself (``check_event_stream.py``) serves its own.
    """
    return ": no events from a stub\n\n"


def answer_shared(method: str, path: str) -> tuple[int, str, str] | None:
    """The answer every harness gives the same way: ``(status, content type, body)``, or ``None``.

    ``path`` may be a whole URL; the query string and everything before ``/api/`` are ignored. A
    harness asks this where it used to look ``GATES`` up, after its own routes, so what it invented
    still wins.
    """
    path = path.split("?", 1)[0]
    path = path[path.index("/api/"):] if "/api/" in path else path
    if method.upper() == "GET" and path == EVENTS:
        return 200, "text/event-stream", event_stream_hello()
    write = SHARED_WRITES.get((method.upper(), path))
    if write is not None:
        return write
    if method.upper() == "POST" and path.startswith("/api/notifications/") and path.endswith("/act"):
        # The shared centre is empty, so every entry a harness might answer is one the host no
        # longer has; a harness that invents entries answers this route itself.
        return 404, "application/json", json.dumps({"detail": "no such notification"})
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


__all__ = ["CATALOG", "DEFAULT_APP", "DEFAULT_PORT", "ENVIRONMENTS", "EVENTS", "GATES", "BoardStub", "TeamStub", "Unhandled", "answer_shared", "event_stream_hello", "expect_app", "folder", "folders", "fulfil_shared", "serve_shared_post"]

# What the harness manager reports for the container: Claude Code installed and signed in, Codex
# installed but signed out, the rest absent. Enough for the hiring form to show one command-line agent
# it can offer and one it cannot, with the reason.
CATALOG: dict[str, object] = {
    "claude": {"installed": True, "version": "2.1.40", "latest": "2.1.40", "logged_in": True, "agents": [{"name": "code-reviewer", "source": "project"}], "models": ["opus", "sonnet"], "error": "", "checked_at": "2026-09-24T09:00:00Z"},
    "codex": {"installed": True, "version": "0.40.0", "latest": "0.41.0", "logged_in": False, "agents": [], "models": ["gpt-5.2-codex"], "error": "", "checked_at": "2026-09-24T09:00:00Z"},
}


class TeamStub:
    """A project's team, answered the way the host answers it, and kept between requests.

    The page hires, edits and dismisses; a stub that forgot each change would show a list that never
    moves, and a check of those three would pass against nothing. The rules the host enforces that
    the page shows back — a name already taken, dismissing a member who works — are kept here too.
    """

    def __init__(self, project: dict, *, staff: list[dict] | None = None, catalog: dict | None = None, presets: list[dict] | None = None, personas: list[str] | None = None) -> None:
        folders = project.get("folders") or []
        self.project = {
            "id": project["id"], "name": project["name"], "ephemeral": False, "system": "", "default_env": "container", "local_env": "container",
            "concurrency": 6, "concurrency_cap": 10, "orchestrator": False,
            "folders": [{k: f[k] for k in ("id", "path", "label", "env", "is_git", "readonly")} for f in folders],
        }
        self.staff: list[dict] = [dict(m) for m in staff or []]
        self.catalog = CATALOG if catalog is None else catalog
        self.presets = presets if presets is not None else [{"id": "strong", "label": "Claude Opus 5"}, {"id": "fast", "label": "DeepSeek Flash"}]
        self.personas = personas if personas is not None else ["reviewer", "tester"]
        self.hired: list[dict] = []
        self.patched: list[dict] = []

    @staticmethod
    def member(id_: str, name: str, *, harness: str = "daedalus", status: str = "off", sessions: int = 0, **fields: object) -> dict:
        row = {
            "id": id_, "project_id": "", "name": name, "color": "blue", "role": "", "harness": harness, "agent": "", "model": "", "effort": "",
            "permission_mode": "", "env": "", "default_folder_id": None, "isolation": "worktree", "instructions": "", "notes": "", "one_off": False,
            "created_by": "operator", "created_at": "2026-09-24T09:00:00Z", "archived_at": None, "sessions": sessions, "status": status,
            "live": None if status == "off" else {"id": f"ss-{id_}", "status": status, "waiting_for": "", "task_id": None, "started_at": "2026-09-24T09:00:00Z", "ended_at": None, "branch": None, "worktree_path": None},
        }
        row.update(fields)
        return row

    def listing(self, archived: bool) -> dict:
        rows = [m for m in self.staff if archived or not m["archived_at"]]
        working = sum(1 for m in self.staff if m["status"] in ("starting", "working", "question", "permission", "no_signal"))
        return {
            "project": self.project,
            "staff": rows,
            "counts": {"staff": sum(1 for m in self.staff if not m["archived_at"]), "working": working},
            "choices": {"harnesses": ["daedalus", "claude", "codex", "grok", "opencode", "pi"], "personas": self.personas, "presets": self.presets, "default_preset": self.presets[0]["id"] if self.presets else ""},
        }

    def answer(self, method: str, path: str, query: str, body: dict | None) -> tuple[int, object] | None:
        """``(status, body)`` for a route of the team, or None for anything else."""
        if path == "/api/harnesses/catalog":
            return (200, self.catalog) if self.catalog is not None else (404, {"detail": "Not Found"})
        if path == f"/api/projects/{self.project['id']}/staff":
            if method == "GET":
                return 200, self.listing("archived=1" in query)
            if method == "POST":
                payload = body or {}
                if any(m["name"].lower() == str(payload.get("name", "")).strip().lower() and not m["archived_at"] for m in self.staff):
                    return 400, {"detail": f"the team already has someone called {payload['name']}"}
                self.hired.append(payload)
                row = self.member(f"st-{len(self.staff) + 1}", str(payload["name"]).strip(), harness=payload.get("harness", "daedalus"))
                row.update({k: payload[k] for k in ("role", "agent", "model", "effort", "permission_mode", "env", "isolation", "instructions", "one_off") if k in payload})
                row["default_folder_id"] = payload.get("folder_id") or None
                row["project_id"] = self.project["id"]
                self.staff.append(row)
                return 201, row
        if path.startswith("/api/staff/"):
            sid = path.split("/")[3]
            row = next((m for m in self.staff if m["id"] == sid), None)
            if row is None:
                return 404, {"detail": "no such staff member"}
            if method == "PATCH":
                payload = dict(body or {})
                self.patched.append(payload)
                if "folder_id" in payload:
                    payload["default_folder_id"] = payload.pop("folder_id") or None
                row.update(payload)
                return 200, row
            if method == "DELETE":
                if row["live"] is not None:
                    return 409, {"detail": f"{row['name']} is working; release the session first"}
                row["archived_at"] = "2026-09-24T10:00:00Z"
                return 200, {"ok": True, "staff": row}
            if path.endswith("/sessions"):
                return 200, [row["live"]] if row["live"] else []
            return 200, row
        return None

class BoardStub:
    """A project's board, answered the way the host answers it, and kept between requests.

    The page creates, edits, assigns and accepts; each is remembered, so a card that moved is drawn
    where it moved to. The host's own refusal to accept what is not in review is kept as well.
    """

    ORDER = {"doing": 0, "review": 1, "todo": 2, "blocked": 3, "done": 4, "dropped": 5}

    def __init__(self, project: dict, *, staff: list[dict] | None = None, tasks: list[dict] | None = None, needs_you: list[dict] | None = None) -> None:
        self.project = {"id": project["id"], "name": project["name"], "ephemeral": False, "system": ""}
        self.staff = [dict(m) for m in staff or []]
        self.tasks = [dict(t) for t in tasks or []]
        self.needs_you = [dict(n) for n in needs_you or []]
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.accepted: list[str] = []

    @staticmethod
    def task(id_: str, title: str, *, status: str = "todo", priority: int = 3, assignee: dict | None = None, **fields: object) -> dict:
        row = {
            "id": id_, "title": title, "status": status, "priority": priority, "acceptance": "", "checklist": [], "depends_on": [], "session_id": None, "notes": "",
            "created_at": "2026-09-24T09:00:00Z", "updated_at": "2026-09-24T09:30:00Z", "project_id": "", "assignee_staff_id": assignee["id"] if assignee else None,
            "brief": {"objective": "", "deliverable": "", "boundaries": "", "done_when": ""}, "branch": None, "merge_state": "", "assignee": assignee,
        }
        row.update(fields)
        return row

    @staticmethod
    def assignee(id_: str, name: str, *, harness: str = "daedalus", color: str = "blue", status: str = "off", on_task: bool = False, **fields: object) -> dict:
        row = {"id": id_, "name": name, "color": color, "harness": harness, "archived_at": None, "status": status, "on_task": on_task, "waiting_for": "", "status_at": "2026-09-24T09:40:00Z" if on_task else None, "session_id": f"sess-{id_}" if status != "off" else None}
        row.update(fields)
        return row

    def listing(self, include_done: bool) -> dict:
        rows = [t for t in self.tasks if include_done or t["status"] not in ("done", "dropped")]
        rows.sort(key=lambda t: (self.ORDER.get(t["status"], 9), t["priority"], t["created_at"]))
        counts = {s: sum(1 for t in self.tasks if t["status"] == s) for s in self.ORDER}
        counts["needs_you"] = len(self.needs_you)
        team = [{k: m[k] for k in ("id", "name", "color", "harness")} for m in self.staff]
        return {"project": self.project, "tasks": rows, "needs_you": self.needs_you, "counts": counts, "staff": team}

    def _assign(self, row: dict, staff_id: str | None) -> None:
        member = next((m for m in self.staff if m["id"] == staff_id), None)
        row["assignee_staff_id"] = member["id"] if member else None
        row["assignee"] = self.assignee(member["id"], member["name"], harness=member["harness"], color=member["color"]) if member else None

    def answer(self, method: str, path: str, query: str, body: dict | None) -> tuple[int, object] | None:
        """``(status, body)`` for a route of the board, or None for anything else."""
        base = f"/api/projects/{self.project['id']}/board"
        if path == base and method == "GET":
            return 200, self.listing("include_done=1" in query)
        if path == base and method == "POST":
            payload = dict(body or {})
            self.created.append(payload)
            brief = {"objective": "", "deliverable": "", "boundaries": "", "done_when": ""}
            brief.update(payload.get("brief") or {})
            row = self.task(f"n{len(self.tasks) + 1}", payload["title"], priority=int(payload.get("priority", 3)), brief=brief, depends_on=payload.get("depends_on") or [], project_id=self.project["id"])
            self._assign(row, payload.get("assignee_staff_id"))
            self.tasks.append(row)
            launch = {"state": "queued", "position": 1} if row["assignee_staff_id"] else None
            return 201, {**row, "launch": launch}
        if path == "/api/board" and method == "GET":
            return 200, [t for t in self.tasks if "include_done=1" in query or t["status"] not in ("done", "dropped")]
        if path.startswith("/api/board/"):
            parts = path.split("/")
            row = next((t for t in self.tasks if t["id"] == parts[3]), None)
            if row is None:
                return 404, {"detail": "no such task"}
            if method == "POST" and path.endswith("/accept"):
                if row["status"] != "review":
                    return 409, {"detail": f"only a task in review can be accepted; this one is {row['status']}"}
                self.accepted.append(row["id"])
                row["status"] = "done"
                return 200, row
            if method == "PUT":
                payload = dict(body or {})
                self.updated.append((row["id"], payload))
                before = row.get("assignee_staff_id")
                if "assignee_staff_id" in payload:
                    self._assign(row, payload["assignee_staff_id"] or None)
                if "brief" in payload:
                    row["brief"] = {**row["brief"], **payload["brief"]}
                for key in ("title", "status", "priority", "depends_on"):
                    if key in payload:
                        row[key] = payload[key]
                if payload.get("note"):
                    row["notes"] = (row["notes"] + "\n" if row["notes"] else "") + payload["note"]
                launched = row["assignee_staff_id"] and row["assignee_staff_id"] != before
                return 200, {**row, "launch": {"state": "started"} if launched else None}
            if method == "DELETE":
                self.tasks.remove(row)
                return 200, {"deleted": True}
        return None


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
