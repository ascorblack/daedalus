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

NOTIFICATION_CATEGORIES = ["run_finished", "question", "permission", "run_failed", "staff_turn", "staff_review", "orchestrator_report", "agent_notify", "reminder", "spend", "system"]


def notification_preferences(**over: object) -> dict[str, object]:
    """``GET /api/notifications/preferences`` with the host's defaults, and whatever a harness changes."""
    def cells(push: str = "on", desktop: str = "on", telegram: str = "off") -> dict[str, str]:
        return {"in_app": "on", "push": push, "desktop": desktop, "telegram": telegram}
    matrix = {c: cells() for c in NOTIFICATION_CATEGORIES}
    for c in ("question", "permission", "run_failed", "orchestrator_report", "reminder", "spend"):
        matrix[c] = cells(telegram="on")
    matrix["staff_turn"] = cells(push="off", desktop="off")
    matrix["agent_notify"] = cells(telegram="urgent")
    preferences: dict[str, object] = {
        "matrix": matrix, "finished_min_seconds": 30, "quiet_hours": "", "muted_projects": {}, "quick_actions": True, "telegram_covers_push": True,
        "keep_days": 30, "push_per_session": 6, "push_window_minutes": 10, "push_per_hour": 60, "notify_tool_per_session": 5,
        "notify_tool_window_minutes": 10, "notify_tool_urgent_per_hour": 2, "orchestrator_hold_seconds": 60,
    }
    preferences.update(over)
    return {"preferences": preferences, "revision": "r1", "categories": NOTIFICATION_CATEGORIES, "zone": "Europe/Amsterdam"}


GIB = 1 << 30
MIB = 1 << 20


def terminal_load(*, running: int = 3, cap: int = 20, total: int = 62 * GIB, available: int = 38 * GIB, each: int = 700 * MIB, cpu: float = 14.0) -> dict[str, object]:
    """``GET /api/terminals/load`` for a 62 GB, 16-CPU machine: three sessions running, each about 700 MB."""
    daemon = 45 * MIB
    used = running * each + daemon
    extra = max(0, cap - running)
    machine_used = total - available + extra * each
    return {
        "cap": cap, "running": running, "queued": [],
        "used": {"rss_bytes": used, "daemon_rss_bytes": daemon, "cpu_percent": 9.0, "cpus": 16, "mem_total_bytes": total, "mem_available_bytes": available, "machine_cpu_percent": cpu},
        "profiles": {"harness:claude": {"rss_bytes": each, "cpu_percent": 3.0, "samples": 60}},
        "likely": {"rss_bytes": each, "cpu_percent": 3.0, "samples": 60, "basis": "running"},
        "projection": {"cap": cap, "sessions": max(cap, running), "terminals_rss_bytes": used + extra * each, "machine_used_bytes": machine_used, "mem_total_bytes": total,
                       "mem_percent": round(100 * machine_used / total, 1), "cpu_percent": round(cpu + extra * 3.0 / 16, 1), "level": "ok", "cpu_level": "ok"},
        "envs": [{"env": "container", "supported": True, "terminals": running, "rss_bytes": running * each, "cpu_percent": 9.0, "daemon_rss_bytes": daemon, "mem_total_bytes": total, "mem_available_bytes": available, "cpus": 16}],
        "thresholds": {"warn": 70.0, "bad": 90.0},
    }

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
    # Settings → Notifications: the host's defaults, and a machine to draw the terminal load bar for.
    "/api/notifications/preferences": notification_preferences(),
    "/api/terminals/load": terminal_load(),
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



__all__ = ["CATALOG", "DEFAULT_APP", "DEFAULT_PORT", "ENVIRONMENTS", "EVENTS", "FOCUS_WORDS", "GATES", "NOTIFICATION_CATEGORIES", "BoardStub", "FocusStub", "TeamStub", "Unhandled", "answer_shared", "event_stream_hello", "expect_app", "folder", "folders", "fulfil_shared", "notification_preferences", "serve_shared_post", "terminal_load"]

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
        # The project's column counts its wake-ups and watches; this project has none.
        if path == f"/api/projects/{self.project['id']}/wakeups" and method == "GET":
            return 200, {"wakeups": [], "max": 20}
        if path == f"/api/projects/{self.project['id']}/watches" and method == "GET":
            return 200, {"watches": [], "max": 50, "min_cooldown_minutes": 1, "providers": []}
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


class FocusStub:
    """A project with its orchestrator switched on, as focus mode reads it, kept between requests.

    Everything the focus column, the orchestrator's chat and the project's pages ask for is answered
    here: the project list and the agents listing with the orchestrated entry, the sessions of the
    orchestrator and of a Daedalus staff member, the project's requests (answered once, the second
    answer refused as the host refuses it), the brief, the journal in pages, the wake-ups, the
    terminals, what was sent to a member and the member's controls. The team and the board are the
    two stubs above. What the page sent is recorded, so a check can assert the exact request.

    ``bakery(lang)`` invents the one project the check and the pictures share; the operator's words and
    the orchestrator's answers are in the page's language, and the lines the host writes for the
    orchestrator stay in English, as the host writes them.
    """

    def __init__(self, *, projects: list[dict], listing: dict, details: dict[str, dict], team: TeamStub, board: BoardStub, others: list[TeamStub | BoardStub] | None = None, asks: list[dict], brief: list[dict], journal: list[dict], schedules: list[dict], wakeups: list[dict] | None = None, watches: list[dict] | None = None, terminals: list[dict], messages: dict[str, list[dict]]) -> None:
        self.projects = projects
        self.listing = listing
        self.details = details
        self.team = team
        self.board = board
        self.others = others or []
        self.asks = asks
        self.brief = brief
        self.journal = journal
        self.schedules = schedules
        self.wakeups = wakeups or []
        self.woken: list[dict] = []
        self.watches = watches or []
        self.watched: list[tuple[str, dict]] = []
        self.terminals = terminals
        self.messages = messages
        self.answers: list[tuple[str, dict]] = []
        self.enabled: list[tuple[str, dict]] = []
        self.notes: list[str] = []
        self.briefed: list[dict] = []
        self.controls: list[tuple[str, str]] = []

    def project(self, pid: str) -> dict | None:
        return next((p for p in self.projects if p["id"] == pid), None)

    def answer(self, method: str, path: str, query: str, body: dict | None) -> tuple[int, object] | None:
        """``(status, body)`` for a route of focus mode, or None for anything else."""
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        if path == "/api/projects" and method == "GET":
            return 200, self.projects
        if path == "/api/sessions" and method == "GET":
            return 200, self.listing
        if path.startswith("/api/sessions/") and method == "GET" and path.count("/") == 3:
            sid = path.split("/")[3]
            if sid in self.details:
                return 200, self.details[sid]
        if path == "/api/asks" and method == "GET":
            rows = [a for a in self.asks if a["project_id"] == params.get("project")]
            if params.get("open") != "0":
                rows = [a for a in rows if not a["resolved_at"]]
            return 200, {"asks": rows}
        if path.startswith("/api/asks/") and path.endswith("/answer") and method == "POST":
            ref = path.split("/")[3]
            ask = next((a for a in self.asks if ref in (a["id"], a["short_id"])), None)
            if ask is None:
                return 404, {"detail": "no such request"}
            if ask["resolved_at"]:
                return 409, {"detail": f"request {ask['short_id']} was already answered by the {ask['resolved_by']}"}
            payload = dict(body or {})
            self.answers.append((ask["id"], payload))
            ask.update(resolved_at="2026-09-24T10:00:00Z", resolved_by="operator", resolution={"allow": payload.get("allow"), "text": payload.get("text") or "", "selected": payload.get("selected") or [], "via": "app"})
            return 200, {"state": "answered", "delivered": True, "error": "", "ask": ask}
        if path.startswith("/api/projects/") and path.endswith("/orchestrator") and method == "POST":
            pid = path.split("/")[3]
            project = self.project(pid)
            if project is None:
                return 404, {"detail": "no such project"}
            payload = dict(body or {})
            self.enabled.append((pid, payload))
            sid = f"orch-{pid}"
            project["settings"]["orchestrator"] = {"enabled": True, "session_id": sid, "model": payload.get("model", ""), "autonomy": payload.get("autonomy", "normal"), "concurrency": 6, "concurrency_cap": payload.get("concurrency_cap", 10), "telegram_topic_id": 0}
            self.details[sid] = FocusStub.session_detail(sid, f"Orchestrator · {project['name']}", project, [], orchestrator_of=pid)
            return 200, {**project["settings"]["orchestrator"], "effective_model": "strong", "project_id": pid}
        if path.startswith("/api/projects/") and path.endswith("/brief"):
            if method == "PUT":
                payload = dict(body or {})
                self.briefed.append(payload)
                section = next((s for s in self.brief if s["section"] == payload.get("section")), None)
                if section is not None:
                    section.update(body=payload.get("body", ""), updated_by="operator", updated_at="2026-09-24T10:00:00Z")
                return 200, section or {}
            return 200, {"sections": self.brief}
        if path.startswith("/api/projects/") and path.endswith("/journal"):
            if method == "POST":
                text = str((body or {}).get("text", ""))
                self.notes.append(text)
                entry = {"id": max((e["id"] for e in self.journal), default=0) + 1, "at": "2026-09-24T10:00:00Z", "author": "operator", "kind": "note", "text": text, "refs": {}}
                self.journal.insert(0, entry)
                return 200, entry
            limit = int(params.get("limit", "50"))
            before = int(params["before"]) if params.get("before") else None
            rows = [e for e in self.journal if before is None or e["id"] < before][:limit]
            return 200, {"entries": rows, "next_before": rows[-1]["id"] if len(rows) == limit else None}
        if path == "/api/schedules" and method == "GET":
            return 200, self.schedules
        if path.startswith("/api/projects/") and "/watches" in path:
            parts = path.split("/")
            pid = parts[3]
            if len(parts) == 5 and method == "GET":
                return 200, {"watches": [w for w in self.watches if w["project_id"] == pid], "max": 50, "min_cooldown_minutes": 1, "providers": ["github"]}
            if len(parts) == 5 and method == "POST":
                payload = dict(body or {})
                self.watched.append(("create", payload))
                row = {"id": f"w{len(self.watches) + 1}", "project_id": pid, "when": payload.get("when", {}), "then": payload.get("then", {}), "cooldown_minutes": payload.get("cooldown_minutes", 10), "once": bool(payload.get("once")), "note": payload.get("note", ""),
                       "created_by": "operator", "created_at": "2026-09-24T10:00:00Z", "last_fired_at": None, "fire_count": 0, "enabled": True, "stopped": "", "last_error": "", "describe": ""}
                self.watches.append(row)
                return 200, row
            found = next((w for w in self.watches if w["project_id"] == pid and len(parts) == 6 and w["id"] == parts[5]), None)
            if found is None:
                return 404, {"detail": "no such watch"}
            if method == "PATCH":
                payload = dict(body or {})
                self.watched.append(("update", {"id": found["id"], **payload}))
                found.update({k: v for k, v in payload.items() if v is not None})
                if payload.get("enabled"):
                    found["stopped"] = ""
                return 200, found
            if method == "DELETE":
                self.watched.append(("delete", {"id": found["id"]}))
                self.watches.remove(found)
                return 200, {"deleted": True}
        if path.startswith("/api/projects/") and "/wakeups" in path:
            parts = path.split("/")
            pid = parts[3]
            if len(parts) == 5 and method == "GET":
                return 200, {"wakeups": [w for w in self.wakeups if w["project_id"] == pid], "max": 20}
            if len(parts) == 5 and method == "POST":
                payload = dict(body or {})
                self.woken.append(payload)
                if not str(payload.get("note", "")).strip():
                    return 400, {"detail": "a wake-up needs a note: what to look at when it fires"}
                at = payload.get("at") or ("2026-09-24T12:30:00Z" if payload.get("in_minutes") else None)
                row = {"id": f"wk{len(self.wakeups) + 1}", "project_id": pid, "note": payload["note"], "cron": payload.get("cron"), "at": at, "next_run_at": at or "2026-09-25T07:00:00Z", "last_run_at": None, "enabled": True, "set_by": "operator", "created_at": "2026-09-24T10:00:00Z"}
                self.wakeups.append(row)
                return 200, row
            if len(parts) == 6 and method == "DELETE":
                before = len(self.wakeups)
                self.wakeups = [w for w in self.wakeups if not (w["project_id"] == pid and w["id"] == parts[5])]
                return (200, {"deleted": True}) if len(self.wakeups) < before else (404, {"detail": "no such wake-up"})
        if path == "/api/terminals" and method == "GET" and params.get("project_id"):
            rows = [term for term in self.terminals if term["project_id"] == params["project_id"]]
            return 200, {**GATES["/api/terminals"], "terminals": rows}  # type: ignore[dict-item]
        if path.startswith("/api/staff/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "messages" and method == "GET":
                return 200, self.messages.get(parts[3], [])
            if len(parts) == 5 and parts[4] in ("interrupt", "pause", "release") and method == "POST":
                self.controls.append((parts[3], parts[4]))
                member = next((m for m in self.team.staff if m["id"] == parts[3]), None)
                if member is not None and member["live"] and parts[4] == "pause":
                    member["live"]["pause_requested"] = True
                return 200, {"ok": True} if parts[4] != "pause" else {"state": "pausing"}
        for part in (self.team, self.board, *self.others):
            answered = part.answer(method, path, query, body)
            if answered is not None:
                return answered
        return None

    @staticmethod
    def session_detail(sid: str, title: str, project: dict, messages: list[dict], *, orchestrator_of: str | None = None, staff: dict | None = None, status: str = "idle") -> dict:
        root = project["folders"][0]["path"]
        return {
            "id": sid, "title": title, "status": status, "run_id": None, "compacting": None, "housekeeping": False, "error": "",
            "workspace": root, "workspace_name": root.rsplit("/", 1)[-1], "workspace_own": False, "workspace_sessions": [],
            "project": {k: v for k, v in project.items() if k != "sessions"}, "folder_id": project["folders"][0]["id"], "folders": [],
            "pending": None, "model": "Claude Opus 5", "provider": "claude", "thinking": True, "reasoning_effort": "high",
            "messages": messages, "mode": "", "usd_cap": None, "brief": "", "spawned_by": None, "tools_off": [], "loop": None, "services": [], "subagents": [],
            "subagent_of": None, "subagent_name": None, "leader_title": None, "orchestrator_of": orchestrator_of, "staff": staff, "telegram_linked": False,
            "verifications": {}, "first_seq": messages[0]["seq"] if messages else 0, "has_older": False,
            "context": {"tokens": 18400, "window": 200000, "messages": len(messages), "summaries": 0, "operator_turns": 1},
            "usage": {"c": 12, "i": 184000, "o": 9100, "ch": 150000, "usd": 0.84},
        }

    @classmethod
    def bakery(cls, lang: str = "en", *, others: list[dict] | None = None, other_sessions: list[dict] | None = None) -> FocusStub:
        pid, garden = "b4k3ry20f0c5", "9a4d3e2f1c0b"
        words = FOCUS_WORDS[lang]
        project_folders = [
            folder("/home/operator/work/bakery-site", is_git=True),
            folder("/home/operator/work/bakery-api", position=1, is_git=True),
            folder("/home/operator/work/bakery-bot", position=2, env="host", reach="terminals", reachable=False),
        ]
        orchestration = {"enabled": True, "session_id": "orch-bakery", "model": "", "autonomy": "normal", "concurrency": 6, "concurrency_cap": 10, "telegram_topic_id": 0}
        bakery = {"id": pid, "name": "Bakery 2.0", "folders": project_folders, "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": True, "system": "", "ephemeral": False, "default_env": "container", "orchestrator": orchestration}, "system": "", "sessions": [{"id": "orch-bakery", "title": "Orchestrator · Bakery 2.0", "running": False}]}
        garden_project = {"id": garden, "name": "Garden", "folders": [folder("/home/operator/work/garden")], "created_at": "2026-09-22T00:00:00Z", "settings": {"snapshots": True, "system": "", "ephemeral": False}, "system": "", "sessions": []}
        projects = [bakery, garden_project, *(others or [])]

        def live(sid: str, status: str, task: str | None, session_id: str | None = None, **extra: object) -> dict:
            return {"id": sid, "session_id": session_id, "terminal_id": None, "status": status, "waiting_for": "", "task_id": task, "started_at": "2026-09-24T09:00:00Z", "ended_at": None, "branch": None, "worktree_path": None, "pause_requested": False, **extra}

        machine = [{"staff_id": "st-olga", "task_id": "t-hours", "priority": 2, "position": 1, "reason": "machine", "detail": "20 of the machine's 20 terminal sessions are running", "since": 1_727_164_800, "by": "orchestrator"}]
        staff = [
            TeamStub.member("st-ira", "Ira", harness="claude", status="working", sessions=9, color="orange", role=words["ira.role"], model="opus", permission_mode="acceptEdits"),
            TeamStub.member("st-max", "Max", harness="codex", status="turn_done_unseen", sessions=5, color="blue", role="API"),
            TeamStub.member("st-naya", "Naya", harness="opencode", status="permission", sessions=3, color="green", role=words["naya.role"], env="host", isolation="shared"),
            TeamStub.member("st-lev", "Lev", status="working", sessions=4, color="teal", role=words["lev.role"], agent="reviewer", model="strong"),
            TeamStub.member("st-olga", "Olga", harness="claude", sessions=1, color="violet", role=words["olga.role"], queued=machine),
            TeamStub.member("st-link", words["link.name"], status="working", sessions=1, color="rose", one_off=True, isolation="shared"),
        ]
        staff[0]["live"] = live("ss-ira", "working", "t-checkout", branch="agent/ira/checkout")
        staff[1]["live"] = live("ss-max", "turn_done_unseen", "t-endpoint", branch="agent/max/endpoint")
        staff[2]["live"] = live("ss-naya", "permission", "t-bot")
        staff[3]["live"] = live("ss-lev", "working", "t-photos", session_id="sess-lev", branch="agent/lev/photos", worktree_path="/home/operator/work/bakery-site/.agents/worktrees/lev")
        staff[5]["live"] = live("ss-link", "working", None)
        for member in staff:
            member["project_id"] = pid
        team = TeamStub({**bakery}, staff=staff)
        team.project["orchestrator"] = True

        ira = BoardStub.assignee("st-ira", "Ira", harness="claude", color="orange", status="working", on_task=True)
        tasks = [
            BoardStub.task("t-checkout", words["task.checkout"], status="doing", priority=1, assignee=ira, checklist=[{"text": "cart", "done": True}, {"text": "promo", "done": True}, {"text": "payment", "done": False}]),
            BoardStub.task("t-bot", words["task.bot"], status="doing", assignee=BoardStub.assignee("st-naya", "Naya", harness="opencode", color="green", status="permission", on_task=True)),
            BoardStub.task("t-endpoint", words["task.endpoint"], status="review", assignee=BoardStub.assignee("st-max", "Max", harness="codex"), branch="agent/max/endpoint"),
            BoardStub.task("t-photos", words["task.photos"], status="doing", assignee=BoardStub.assignee("st-lev", "Lev", color="teal", status="working", on_task=True, session_id="sess-lev")),
            BoardStub.task("t-hours", words["task.hours"], status="todo", priority=2, assignee=BoardStub.assignee("st-olga", "Olga", harness="claude", color="violet")),
        ]
        needs = [{
            "id": "ask-spring", "short_id": "q4r8tz", "origin": "orchestrator", "kind": "question", "text": words["ask.spring"], "suggestion": "",
            "created_at": "2026-09-24T09:55:00Z", "task_id": "t-checkout", "task_title": words["task.checkout"], "staff": None, "session_id": "orch-bakery",
        }]
        board = BoardStub({**bakery}, staff=[{k: m[k] for k in ("id", "name", "color", "harness")} for m in staff], tasks=tasks, needs_you=needs)

        asks = [
            {"id": "ask-spring", "short_id": "q4r8tz", "project_id": pid, "origin": "orchestrator", "kind": "question", "staff_id": None, "staff_session_id": None, "task_id": "t-checkout", "request_ref": "", "text": words["ask.spring"], "detail": {"options": [words["ask.before"], words["ask.after"]]}, "routed_to": "operator", "suggestion": "", "created_at": "2026-09-24T09:55:00Z", "routed_at": "2026-09-24T09:55:00Z", "resolved_at": None, "resolved_by": None, "resolution": {}},
            {"id": "ask-grammy", "short_id": "qk7m2x", "project_id": pid, "origin": "staff", "kind": "permission", "staff_id": "st-naya", "staff_session_id": "ss-naya", "task_id": "t-bot", "request_ref": "", "text": "Exec: npm install grammy", "detail": {}, "routed_to": "orchestrator", "suggestion": "", "created_at": "2026-09-24T09:53:00Z", "routed_at": "2026-09-24T09:53:00Z", "resolved_at": "2026-09-24T09:54:00Z", "resolved_by": "orchestrator", "resolution": {"allow": True, "text": "", "selected": [], "via": "orchestrator"}},
        ]

        def call(cid: str, name: str, **arguments: object) -> dict:
            return {"id": cid, "name": name, "arguments": arguments}

        def result(cid: str, content: str, error: bool = False) -> dict:
            return {"id": cid, "content": content, "is_error": error}

        events = (
            "[events · Bakery 2.0 · 3 since 09:51]\n"
            '- 09:51 Max (Codex) finished a turn on "Notify: endpoint" (t-endpoint): "3 files, tests green" — ReadStaff("Max") for the whole reply\n'
            "- 09:53 Naya needs permission [qk7m2x]: Exec: npm install grammy (in bakery-bot) (autonomy normal) — yours to answer or escalate\n"
            "- 09:54 Ira asks [q9w2e1]: SPRING10: before delivery or after? — options: before / after — yours to answer or escalate"
        )
        transcript = [
            {"role": "user", "seq": 10, "origin": "operator", "text": words["op.ask"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:40:00Z"},
            {"role": "assistant", "seq": 11, "text": "", "thinking": "", "tool_calls": [
                call("c1", "Folders", op="add", path="/home/operator/work/bakery-bot", env="host"),
                call("c2", "Tasks", op="create", title=words["task.endpoint"], objective="POST /orders/notify", deliverable="endpoint + tests", boundaries="api/ only", done_when="tests green", assignee="Max"),
                call("c3", "Tasks", op="create", title=words["task.bot"], objective="grammy bot", deliverable="bot", boundaries="bakery-bot/", done_when="a test order reaches the chat", depends_on=["t-endpoint"], assignee="Naya"),
                call("c4", "Assign", staff="Max", task_id="t-endpoint"),
                call("c5", "Watch", when={"event": "staff_finished", "staff": "Max"}, then={"action": "wake"}, note=words["watch.note"]),
            ], "tool_results": [], "created_at": "2026-09-24T09:40:10Z"},
            {"role": "tool", "seq": 12, "text": "", "thinking": "", "tool_calls": [], "tool_results": [
                result("c1", "added /home/operator/work/bakery-bot (host)"), result("c2", "created t-endpoint"), result("c3", "created t-bot"), result("c4", "started Max on t-endpoint"), result("c5", "watch w1 set"),
            ], "created_at": "2026-09-24T09:40:12Z"},
            {"role": "assistant", "seq": 13, "text": words["orch.plan"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:40:20Z"},
            {"role": "user", "seq": 14, "origin": "events", "text": events, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:54:10Z"},
            {"role": "assistant", "seq": 15, "text": "", "thinking": "", "tool_calls": [
                call("c6", "ReadStaff", staff="Max", what="last"),
                call("c7", "Answer", request_id="qk7m2x", allow=True, basis="installing dependencies in the bot folder"),
                call("c8", "AskOperator", question=words["ask.spring"], options=[words["ask.before"], words["ask.after"]], task_id="t-checkout"),
            ], "tool_results": [], "created_at": "2026-09-24T09:54:20Z"},
            {"role": "tool", "seq": 16, "text": "", "thinking": "", "tool_calls": [], "tool_results": [
                result("c6", "3 files changed, tests green"), result("c7", "answered qk7m2x: allowed"), result("c8", "asked the operator as [q4r8tz]; do not wait — the answer arrives as an event in a later wake-up"),
            ], "created_at": "2026-09-24T09:54:22Z"},
            {"role": "assistant", "seq": 17, "text": words["orch.after"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:55:00Z"},
        ]
        lev = [
            {"role": "user", "seq": 3, "origin": "orchestrator", "text": words["lev.task"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:30:00Z"},
            {"role": "assistant", "seq": 4, "text": words["lev.reply"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:36:00Z"},
        ]
        details = {
            "orch-bakery": cls.session_detail("orch-bakery", "Orchestrator · Bakery 2.0", bakery, transcript, orchestrator_of=pid),
            # Between turns, so its composer offers to write to it rather than to steer a run.
            "sess-lev": cls.session_detail("sess-lev", f"Lev · {words['task.photos']}", bakery, lev, staff={"id": "st-lev", "session_id": "ss-lev"}),
        }
        brief = [
            {"section": "goals", "body": words["brief.goals"], "updated_at": "2026-09-22T10:00:00Z", "updated_by": "operator"},
            {"section": "constraints", "body": words["brief.constraints"], "updated_at": "2026-09-22T10:00:00Z", "updated_by": "operator"},
            {"section": "preferences", "body": "", "updated_at": None, "updated_by": None},
            {"section": "done_when", "body": words["brief.done"], "updated_at": "2026-09-22T10:00:00Z", "updated_by": "operator"},
            {"section": "allowed_without_operator", "body": "installing dependencies in the bot folder\nrunning the test suites", "updated_at": "2026-09-22T10:00:00Z", "updated_by": "operator"},
            {"section": "notes", "body": words["brief.notes"], "updated_at": "2026-09-24T09:41:00Z", "updated_by": "orchestrator"},
        ]
        kinds = [("orchestrator", "decision", words["journal.decision"], {"task_id": "t-bot"}), ("system", "grant", "The orchestrator allowed Naya: Exec: npm install grammy (basis: installing dependencies in the bot folder)", {"staff_id": "st-naya", "ask_id": "qk7m2x"}), ("staff", "report", "Max: done — 3 files, tests green", {"staff_id": "st-max", "task_id": "t-endpoint"}), ("operator", "note", words["journal.note"], {})]
        journal = [
            {"id": 100 - i, "at": f"2026-09-24T{9 - i // 12:02d}:{59 - (i % 12) * 5:02d}:00Z", "author": kinds[i % 4][0], "kind": kinds[i % 4][1], "text": f"{kinds[i % 4][2]} ({i + 1})" if i else kinds[0][2], "refs": kinds[i % 4][3]}
            for i in range(35)
        ]
        schedules = [{"id": "wk1", "name": words["wake.name"], "run_in": "self", "cron": None, "run_at": "2026-09-24T12:00:00Z", "prompt": words["wake.note"], "enabled": 1, "next_run_at": "2026-09-24T12:00:00Z", "last_run_at": None, "last_summary": None, "kind": "lazy", "target_session": "orch-bakery", "failure_count": 0, "last_error": None}]
        wakeups = [
            {"id": "wk1", "project_id": pid, "note": words["wake.note"], "cron": None, "at": "2026-09-24T12:00:00Z", "next_run_at": "2026-09-24T12:00:00Z", "last_run_at": None, "enabled": True, "set_by": "orchestrator", "created_at": "2026-09-24T09:40:00Z"},
        ]
        watches = [
            {"id": "w1", "project_id": pid, "when": {"event": "staff_finished", "staff": "Max", "staff_id": "st-max"}, "then": {"action": "wake"}, "cooldown_minutes": 10, "once": False, "note": words["watch.note"],
             "created_by": "orchestrator", "created_at": "2026-09-24T09:40:12Z", "last_fired_at": "2026-09-24T09:51:00Z", "fire_count": 1, "enabled": True, "stopped": "", "last_error": "", "describe": "when Max finishes a turn → wake the orchestrator"},
            {"id": "w2", "project_id": pid, "when": {"event": "ci", "provider": "github", "repo": "bakery/api", "conclusion": "failure"}, "then": {"action": "notify", "title": "CI", "level": "urgent"}, "cooldown_minutes": 30, "once": False, "note": "",
             "created_by": "operator", "created_at": "2026-09-23T18:00:00Z", "last_fired_at": None, "fire_count": 0, "enabled": False, "stopped": "budget", "last_error": "", "describe": ""},
        ]
        terminals = [
            {"id": "tm-api", "env": "container", "title": "bash · bakery-api", "owner": {"kind": "project", "id": pid}, "project_id": pid, "profile": "shell", "sandbox": False, "cwd": "/home/operator/work/bakery-api", "status": "running", "exit_code": None, "exit_signal": None, "created_at": "2026-09-24T09:00:00Z", "exited_at": None, "last_output_at": "2026-09-24T09:50:00Z", "last_input_at": None, "cols": 120, "rows": 30},
            {"id": "tm-psql", "env": "container", "title": "psql · orders", "owner": {"kind": "project", "id": pid}, "project_id": pid, "profile": "shell", "sandbox": False, "cwd": "/home/operator/work/bakery-api", "status": "exited", "exit_code": 0, "exit_signal": None, "created_at": "2026-09-24T08:00:00Z", "exited_at": "2026-09-24T08:30:00Z", "last_output_at": None, "last_input_at": None, "cols": 120, "rows": 30},
        ]
        messages = {"st-lev": [
            {"id": "m2", "staff_id": "st-lev", "staff_session_id": "ss-lev", "origin": "orchestrator", "text": words["lev.message"], "mode": "queue", "state": "acknowledged", "attempts": 1, "created_at": "2026-09-24T09:45:00Z", "updated_at": "2026-09-24T09:45:05Z", "error": ""},
            {"id": "m1", "staff_id": "st-lev", "staff_session_id": "ss-lev", "origin": "operator", "text": words["lev.first"], "mode": "queue", "state": "acknowledged", "attempts": 1, "created_at": "2026-09-24T09:30:00Z", "updated_at": "2026-09-24T09:30:02Z", "error": ""},
        ]}
        sessions = [
            {"id": "orch-bakery", "title": "Orchestrator · Bakery 2.0", "status": "idle", "created_at": "2026-09-20T00:00:00Z", "last_message_at": "2026-09-24T09:55:00Z", "run_id": None, "model": "Claude Opus 5", "metadata": {"orchestrator_of": pid}, "project_id": pid, "project": "Bakery 2.0"},
            {"id": "sess-lev", "title": f"Lev · {words['task.photos']}", "status": "idle", "created_at": "2026-09-24T09:30:00Z", "last_message_at": "2026-09-24T09:36:00Z", "run_id": None, "model": "Claude Opus 5", "metadata": {"staff_id": "st-lev"}, "project_id": pid, "project": "Bakery 2.0"},
            *(other_sessions or []),
        ]
        counts = {"total": 2, "active": 1, "loops": 0, "last_message_at": "2026-09-24T09:55:00Z"}
        folders_listed = [
            {**{k: v for k, v in bakery.items() if k != "sessions"}, **counts, "orchestrator": {"enabled": True, "session_id": "orch-bakery", "staff": 6, "working": 4, "needs_you": 1}},
            {**{k: v for k, v in garden_project.items() if k != "sessions"}, "total": 0, "active": 0, "loops": 0, "last_message_at": "", "orchestrator": None},
        ]
        listing = {"sessions": sessions, "projects": folders_listed}
        # The project without an orchestrator has a team and a board of its own, both empty.
        others: list[TeamStub | BoardStub] = [TeamStub(garden_project), BoardStub(garden_project)]
        return cls(projects=projects, listing=listing, details=details, team=team, board=board, others=others, asks=asks, brief=brief, journal=journal, schedules=schedules, wakeups=wakeups, watches=watches, terminals=terminals, messages=messages)


FOCUS_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "op.ask": "Add the folder ~/work/bakery-bot. The baker should get a Telegram message for every new order: the endpoint is Max's, the bot is Naya's.",
        "orch.plan": "Split into two tasks. The bot depends on the endpoint, so Naya starts after Max. Ira is busy with the checkout; I am not pulling her off it.",
        "orch.after": "Naya may install the bot's dependencies: the brief allows it. The discount question is yours — Ira's checkout waits on it.",
        "ask.spring": "SPRING10: is the discount taken before delivery or after?",
        "ask.before": "Before delivery", "ask.after": "After delivery",
        "watch.note": "Max's turn finished → wake me",
        "task.checkout": "Checkout", "task.bot": "Notify: bot", "task.endpoint": "Notify: endpoint", "task.photos": "Menu photo captions", "task.hours": "Opening hours",
        "ira.role": "front end", "naya.role": "the baker's bot", "lev.role": "review", "olga.role": "copy", "link.name": "Menu link check",
        "lev.task": "Task: captions for the menu photos. Done when every photo in the gallery has a caption from the sheet.",
        "lev.reply": "Twelve of eighteen captions are written from the sheet; the rest have no row yet, so I am asking the orchestrator.",
        "lev.message": "Take the captions from the owner's sheet, column C", "lev.first": "Start with the seasonal photos",
        "brief.goals": "Orders arrive in the baker's Telegram within a minute.\nThe checkout takes SPRING10.",
        "brief.constraints": "No new services; the bot runs on the host.", "brief.done": "A test order reaches the baker's chat.",
        "brief.notes": "Max's endpoint is merged before Naya starts.",
        "journal.decision": "The bot waits for the endpoint: one contract, not two.", "journal.note": "Remember the Friday price change.",
        "wake.name": "Check the checkout", "wake.note": "Look at Ira's checkout after lunch",
    },
    "ru": {
        "op.ask": "Добавь в проект папку ~/work/bakery-bot. Нужно, чтобы пекарь получал в Telegram сообщение о каждом новом заказе: эндпоинт — Максу, бота — Нае.",
        "orch.plan": "Разложено на две задачи. Бот зависит от эндпоинта, поэтому Ная начнёт после Макса. Ира занята оформлением заказа, её не отвлекаю.",
        "orch.after": "Нае можно ставить зависимости бота: это есть в брифе. Вопрос о скидке — ваш, от него зависит оформление заказа у Иры.",
        "ask.spring": "SPRING10: скидка до доставки или после?",
        "ask.before": "До доставки", "ask.after": "После доставки",
        "watch.note": "ход Макса завершён → разбудить меня",
        "task.checkout": "Оформление заказа", "task.bot": "Уведомление: бот", "task.endpoint": "Уведомление: эндпоинт", "task.photos": "Подписи к фото в меню", "task.hours": "Часы работы",
        "ira.role": "фронтенд", "naya.role": "бот пекаря", "lev.role": "ревью", "olga.role": "тексты", "link.name": "Проверка ссылок меню",
        "lev.task": "Задача: подписи к фото в меню. Готово, когда у каждого фото в галерее есть подпись из таблицы.",
        "lev.reply": "Двенадцать из восемнадцати подписей взяты из таблицы; для остальных строк нет, спрашиваю оркестратора.",
        "lev.message": "Подписи бери из таблицы владельца, колонка C", "lev.first": "Начни с сезонных фото",
        "brief.goals": "Заказы приходят пекарю в Telegram за минуту.\nОформление заказа принимает SPRING10.",
        "brief.constraints": "Без новых сервисов; бот работает на хосте.", "brief.done": "Тестовый заказ доходит до чата пекаря.",
        "brief.notes": "Эндпоинт Макса сливается до того, как начнёт Ная.",
        "journal.decision": "Бот ждёт эндпоинт: один контракт, а не два.", "journal.note": "Не забыть про смену цен в пятницу.",
        "wake.name": "Проверить оформление заказа", "wake.note": "После обеда посмотреть оформление заказа у Иры",
    },
}
