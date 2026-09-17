"""Take the README screenshots of the built app over an invented installation.

Everything the app asks the API for is answered here with made-up data (a small studio's
agents, a bakery site, a weekly digest), so the pictures show the product and nothing of the
operator's own sessions. Build the app, serve it with tests/browser/serve_app.py, then:

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8101 /tmp/app-root &
    APP_URL=http://127.0.0.1:8101/app OUT=docs/screenshots python3 tests/browser/screenshots.py

One PNG per screen lands in OUT (default: the directory this file is in). The Russian set is the
same run in the other language:

    LANG_UI=ru OUT=docs/screenshots/ru python3 tests/browser/screenshots.py

CHROMIUM points at the browser to drive and defaults to /usr/local/bin/chromium, which is where a
container puts one. On a machine that installed its browser through Playwright it has to be set, or
the launch fails on a path that is not there.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import GATES, Unhandled  # noqa: E402

BASE = os.environ.get("APP_URL", "http://127.0.0.1:8101/app")
# The app is bilingual, and so is this set: LANG_UI=ru opens every page with ?lang=ru and the words
# the helpers click on come from the same column the app reads. The pictures go wherever OUT says.
LANG = os.environ.get("LANG_UI", "en")
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
OUT = Path(os.environ.get("OUT", str(Path(__file__).parent)))
NOW = datetime.now(UTC)


def ago(**kw: float) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def ahead(**kw: float) -> str:
    return (NOW + timedelta(**kw)).isoformat()


# ---- the invented installation ------------------------------------------------------------

S1, S2, S3, S4, S5, S6, S7, S8 = "a1b2c3d4e5f6", "b2c3d4e5f6a1", "c3d4e5f6a1b2", "d4e5f6a1b2c3", "e5f6a1b2c3d4", "f6a1b2c3d4e5", "0a1b2c3d4e5f", "1b2c3d4e5f6a"

LOOP = {"mode": "interval", "interval_seconds": 5400, "status": "active", "run_count": 14, "max_runs": None, "next_run_at": ahead(minutes=38), "last_run_at": ago(minutes=52), "last_reason": None, "stop_reason": None, "pause_note": None, "instruction": "Read the support inbox, answer what you can, and put the rest on the board."}


P1, P2 = "9f3c2a1b7d40", "2e7b5c9a1f88"

PROJECTS = [
    {"id": P1, "name": "Bakery site", "root": "/home/operator/work/bakery", "created_at": ago(days=9), "settings": {"snapshots": True}, "reachable": True, "writable": True, "sessions": [{"id": "a1b2c3d4e5f6", "title": "Bakery site", "running": True}, {"id": "b2c3d4e5f6a1", "title": "Bakery site: photos", "running": False}, {"id": "e5f6a1b2c3d4", "title": "Bakery site (fork @412)", "running": False}]},
    {"id": P2, "name": "Expenses", "root": "/home/operator/work/expenses", "created_at": ago(days=4), "settings": {"snapshots": False}, "reachable": True, "writable": True, "sessions": [{"id": "f6a1b2c3d4e5", "title": "Expense tracker", "running": False}]},
    # A folder added but not mounted yet: in Docker that is a restart away, and the app says so.
    {"id": "5a8d1c0b6e22", "name": "Courier rates", "root": "/home/operator/documents/courier", "created_at": ago(hours=2), "settings": {"snapshots": False}, "reachable": False, "writable": False, "sessions": []},
]


def session(id_: str, title: str, model: str, *, status: str = "idle", last: str, workspace: str | None = None, own: bool = True, meta: dict | None = None, project: str | None = None) -> dict:
    name = next((p["name"] for p in PROJECTS if p["id"] == project), None)
    return {"id": id_, "title": title, "status": status, "created_at": ago(days=3), "last_message_at": last, "run_id": "run1" if status == "running" else None, "model": model, "workspace": workspace or id_, "workspace_own": own, "metadata": meta or {}, "project_id": project, "project": name}


SESSIONS = [
    session(S1, "Bakery site", "Claude Opus 5", status="running", last=ago(seconds=40), project=P1),
    session(S2, "Bakery site: photos", "Local Qwen3.8", last=ago(minutes=12), workspace=S1, own=False, project=P1),
    session(S3, "Support inbox", "GPT-5.6 Luna", last=ago(minutes=52), meta={"loop": LOOP}),
    session(S7, "[sub] triage", "GPT-5.6 Luna", last=ago(minutes=53), meta={"subagent_of": S3, "subagent_name": "triage"}),
    session(S8, "[sub] reply-drafts", "GPT-5.6 Luna", last=ago(minutes=51), meta={"subagent_of": S3, "subagent_name": "reply-drafts"}),
    session(S4, "Weekly digest", "DeepSeek Flash", status="waiting", last=ago(minutes=4)),
    session(S5, "Bakery site (fork @412)", "DeepSeek Flash", last=ago(hours=1), meta={"forked_from": {"session_id": S1, "seq": 412}}, project=P1),
    session(S6, "Expense tracker", "Local Qwen3.8", last=ago(days=1), project=P2),
]

ANSWER = """The menu page is live and checked on a phone.

**What changed**

- `menu.html` — the seasonal section reads from `data/menu.json`, so the owner edits one file
- prices are formatted from the same JSON; no more hand-typed `€` in the markup
- the hero image is 46 KB now (was 1.9 MB), same crop

**Checks**

| check | result |
|---|---|
| Lighthouse, mobile | 98 / 100 / 100 / 100 |
| links | 41 ok, 0 broken |
| menu items | 27, matches the sheet |

The preview runs at the address in the panel. Next I would wire the order form to the sheet; say the word."""

IMAGE_CALL, SEND_CALL = "toolu_img1", "toolu_send1"

MESSAGES = [
    {"role": "user", "seq": 401, "text": "Rebuild the menu page from the owner's sheet: seasonal items on top, prices from the sheet, and make the hero image smaller. Check it on a phone.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=9)},
    {"role": "assistant", "seq": 402, "text": "", "thinking": "The sheet has three sections; seasonal is the third. I will move it first and generate the page from JSON so the owner never edits markup.", "tool_calls": [{"id": "toolu_1", "name": "Read", "arguments": {"path": "data/menu.csv"}}, {"id": "toolu_2", "name": "Read", "arguments": {"path": "menu.html"}}], "tool_results": [], "created_at": ago(minutes=9)},
    {"role": "tool", "seq": 403, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "toolu_1", "content": "section,name,price,note\nseasonal,Pumpkin loaf,4.80,October\nseasonal,Fig tart,5.20,\nbread,Sourdough,6.50,\n… 24 more rows", "is_error": False, "length": 1412}, {"id": "toolu_2", "content": "<!doctype html>\n<html lang=\"en\">…\n<section id=\"menu\">…</section>", "is_error": False, "length": 6120}], "created_at": ago(minutes=9)},
    {"role": "assistant", "seq": 404, "text": "", "thinking": "", "tool_calls": [{"id": "toolu_3", "name": "Write", "arguments": {"path": "data/menu.json", "content": "[{\"section\": \"seasonal\", \"items\": […]}]"}}, {"id": "toolu_4", "name": "Edit", "arguments": {"path": "menu.html", "old": "<section id=\"menu\">", "new": "<section id=\"menu\" data-source=\"data/menu.json\">"}}], "tool_results": [], "created_at": ago(minutes=8)},
    {"role": "tool", "seq": 405, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "toolu_3", "content": "wrote data/menu.json (27 items)", "is_error": False}, {"id": "toolu_4", "content": "menu.html: 1 replacement", "is_error": False}], "created_at": ago(minutes=8)},
    {"role": "assistant", "seq": 406, "text": "", "thinking": "", "tool_calls": [{"id": "toolu_5", "name": "Exec", "arguments": {"command": "convert images/hero.jpg -resize 1600x -quality 78 images/hero.jpg && ls -la images/hero.jpg"}}], "tool_results": [], "created_at": ago(minutes=7)},
    {"role": "tool", "seq": 407, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "toolu_5", "content": "exit_code=0 elapsed=0.8s cwd=/srv/workspaces/a1b2c3d4e5f6 sandbox=workspace\n-rw-r--r-- 1 agent agent 46812 images/hero.jpg", "is_error": False}], "created_at": ago(minutes=7)},
    {"role": "assistant", "seq": 408, "text": "", "thinking": "", "tool_calls": [{"id": IMAGE_CALL, "name": "ImageView", "arguments": {"path": "shots/menu-phone.png", "question": "Is the seasonal section first and readable at 390 px?"}}], "tool_results": [], "created_at": ago(minutes=6)},
    {"role": "tool", "seq": 409, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": IMAGE_CALL, "content": "Yes: 'Seasonal' is the first heading under the hero, three items with prices aligned right, nothing overflows the viewport.", "is_error": False}], "created_at": ago(minutes=6)},
    {"role": "assistant", "seq": 410, "text": "", "thinking": "", "tool_calls": [{"id": "toolu_7", "name": "Verify", "arguments": {"command": "npx lighthouse http://127.0.0.1:8100/menu.html --preset=mobile --quiet", "criterion": "performance ≥ 90"}}, {"id": SEND_CALL, "name": "SendFile", "arguments": {"path": "reports/menu-check.md", "caption": "the check report"}}], "tool_results": [], "created_at": ago(minutes=4)},
    {"role": "tool", "seq": 411, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "toolu_7", "content": "receipt v212: performance 98, accessibility 100, best-practices 100, seo 100 — PASS", "is_error": False}, {"id": SEND_CALL, "content": "sent reports/menu-check.md (3.1 KB)", "is_error": False}], "created_at": ago(minutes=4)},
    {"role": "assistant", "seq": 412, "text": ANSWER, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=3)},
]

MESSAGES_S2 = [
    {"role": "user", "seq": 12, "text": "Pick the six best photos from photos/raw for the gallery and crop them square.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=30)},
    {"role": "assistant", "seq": 13, "text": "", "thinking": "", "tool_calls": [{"id": "t1", "name": "Find", "arguments": {"pattern": "photos/raw/*.jpg"}}], "tool_results": [], "created_at": ago(minutes=30)},
    {"role": "tool", "seq": 14, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": "t1", "content": "38 files", "is_error": False}], "created_at": ago(minutes=29)},
    {"role": "assistant", "seq": 15, "text": "Six picked and cropped into `photos/gallery/`: the counter, the oven, two loaves, the window at dusk and the sign. The rest were either blurred or too dark for a square crop.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=12)},
]

SERVICES_S1 = [{"name": "site-preview", "command": "python3 -m http.server $PORT --bind 0.0.0.0 --directory site", "cwd": "/srv/workspaces/a1b2c3d4e5f6", "port": 8100, "url": "http://192.168.1.20:8100", "pid": 4212, "status": "running", "restart": True, "note": None, "started_at": ago(minutes=7), "stopped_at": None, "share": {"mode": "key", "slug": "site-preview-k3f9", "key": "kM_x9pQ2rT7v", "url": "https://agent.example.com/s/site-preview-k3f9/?key=kM_x9pQ2rT7v", "public_base": "https://agent.example.com"}}]
SERVICES_ALL = [
    {**SERVICES_S1[0], "session_id": S1, "session_title": "Bakery site"},
    {"name": "digest-api", "command": "uvicorn app:api --host 0.0.0.0 --port $PORT", "cwd": "/srv/workspaces/d4e5f6a1b2c3", "port": 8101, "url": "http://192.168.1.20:8101", "pid": 4380, "status": "running", "restart": True, "note": None, "started_at": ago(hours=3), "stopped_at": None, "share": {"mode": "local", "slug": None, "key": None, "url": None, "public_base": "https://agent.example.com"}, "session_id": S4, "session_title": "Weekly digest"},
    {"name": "expense-ui", "command": "npm run dev -- --port $PORT", "cwd": "/srv/workspaces/f6a1b2c3d4e5", "port": 8102, "url": None, "pid": None, "status": "stopped", "restart": False, "note": "stopped by the operator", "started_at": ago(days=1), "stopped_at": ago(hours=20), "share": {"mode": "local", "slug": None, "key": None, "url": None, "public_base": "https://agent.example.com"}, "session_id": S6, "session_title": "Expense tracker"},
]

SUBAGENTS_S1 = [{"session_id": "sub1", "name": "link-check", "running": False, "status": "done", "model": "Local Qwen3.8", "kept": False}]

QUESTION = {"questions": [{"question": "The digest has 14 items this week; keep it to the usual 8?", "header": "Length", "options": [{"label": "Top 8", "description": "the usual"}, {"label": "All 14"}, {"label": "Split into two mails"}], "allow_custom": True}]}


def detail(id_: str) -> dict:
    s = next(x for x in SESSIONS if x["id"] == id_)
    messages = MESSAGES if id_ == S1 else MESSAGES_S2 if id_ == S2 else [{"role": "user", "seq": 1, "text": "Start.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(hours=1)}, {"role": "assistant", "seq": 2, "text": "Started. Waiting for the sheet.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(hours=1)}]
    project = next((p for p in PROJECTS if p["id"] == s["project_id"]), None)
    # A project session works in the project root, and the server answers exactly that: the folder is
    # not a directory of the session's own, and its name is the folder's.
    workspace = project["root"] if project else f"/srv/workspaces/{s['workspace']}"
    return {
        "id": id_, "title": s["title"], "status": "idle" if id_ != S4 else "waiting", "run_id": None, "compacting": None,
        "workspace": workspace, "workspace_name": workspace.rsplit("/", 1)[-1], "workspace_own": False if project else s["workspace_own"],
        "project": project,
        "workspace_sessions": [{"id": S2, "title": "Bakery site: photos"}] if id_ == S1 else [{"id": S1, "title": "Bakery site"}] if id_ == S2 else [],
        "pending": QUESTION if id_ == S4 else None, "model": s["model"], "provider": "claude" if id_ == S1 else "opencode",
        "messages": messages, "mode": "", "usd_cap": 4.0, "brief": "Site of a small bakery. Static HTML, no frameworks; the owner edits data files, never markup.", "spawned_by": None, "tools_off": [],
        "loop": LOOP if id_ == S3 else None, "services": SERVICES_S1 if id_ == S1 else [], "subagents": SUBAGENTS_S1 if id_ == S1 else [],
        "context": {"tokens": 41200, "window": 200000, "messages": len(messages), "summaries": 1, "operator_turns": 6},
        "usage": {"c": 38, "i": 612000, "o": 24100, "ch": 540000, "usd": 1.84},
    }


PROVIDER_USAGE = {"provider": "claude", "today": {"calls": 38, "input_tokens": 612000, "output_tokens": 24100, "cache_read_tokens": 540000, "cost_usd": 0}, "subscription": {"logged_in": True, "plan": "max", "windows": [{"name": "5h", "used_percent": 23, "resets_at": ahead(hours=3, minutes=12)}, {"name": "weekly", "used_percent": 41, "resets_at": ahead(days=4)}]}, "balance": None}

FILES = {
    "": [{"name": "data", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=8)).timestamp()}, {"name": "images", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=7)).timestamp()}, {"name": "photos", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=12)).timestamp()}, {"name": "reports", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=4)).timestamp()}, {"name": "shots", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=6)).timestamp()}, {"name": "site", "dir": True, "size": 0, "mtime": (NOW - timedelta(minutes=8)).timestamp()}, {"name": "index.html", "dir": False, "size": 8120, "mtime": (NOW - timedelta(days=2)).timestamp()}, {"name": "menu.html", "dir": False, "size": 6340, "mtime": (NOW - timedelta(minutes=8)).timestamp()}, {"name": "NOTES.md", "dir": False, "size": 2210, "mtime": (NOW - timedelta(hours=5)).timestamp()}, {"name": "PLAN.md", "dir": False, "size": 1480, "mtime": (NOW - timedelta(minutes=4)).timestamp()}, {"name": "style.css", "dir": False, "size": 11020, "mtime": (NOW - timedelta(days=1)).timestamp()}],
}

NOTES_MD = """# Bakery site — notes

## What the owner asked for

- one page per section: **bread**, **pastry**, **seasonal**
- prices come from the sheet, never typed by hand
- the site must load fast on the shop's old phone

## Decisions

| topic | decision | why |
|---|---|---|
| framework | none, static HTML | the owner edits files over SFTP |
| images | ≤ 60 KB each, 1600 px wide | the phone in the shop is on 3G |
| menu | `data/menu.json`, rendered at build | one file to edit |

## Open

- [x] seasonal section first
- [x] hero image under 50 KB
- [ ] order form → the sheet
- [ ] opening hours from the calendar
"""

REPORT_MD = """# Menu page check

Lighthouse (mobile preset), 3 runs, median:

| category | score |
|---|---|
| performance | 98 |
| accessibility | 100 |
| best practices | 100 |
| seo | 100 |

Links: 41 checked, 0 broken. Menu items: 27, matching `data/menu.csv`.
"""

MEMORY = {
    "records": [
        {"id": "m1", "scope": "global", "scope_key": "", "kind": "preference", "text": "The operator writes in English and wants answers under 200 words unless asked for a report.", "salience": 0.9, "version": 1, "created_at": ago(days=6), "last_accessed_at": ago(hours=2)},
        {"id": "m2", "scope": "global", "scope_key": "", "kind": "fact", "text": "Deploys go through the `deploy` script in each project; never rsync by hand.", "salience": 0.8, "version": 2, "created_at": ago(days=5), "last_accessed_at": ago(days=1)},
        {"id": "m3", "scope": "session", "scope_key": S1, "kind": "fact", "text": "The bakery owner edits data files over SFTP; markup changes must not be required of them.", "salience": 0.85, "version": 1, "created_at": ago(days=3), "last_accessed_at": ago(minutes=9)},
        {"id": "m4", "scope": "session", "scope_key": S1, "kind": "fact", "text": "Images are capped at 60 KB and 1600 px; the shop's phone is on 3G.", "salience": 0.7, "version": 1, "created_at": ago(days=2), "last_accessed_at": ago(minutes=7)},
        {"id": "m5", "scope": "session", "scope_key": S3, "kind": "procedure", "text": "Refund questions are answered with the template in templates/refund.md and put on the board for the operator.", "salience": 0.75, "version": 1, "created_at": ago(days=4), "last_accessed_at": ago(minutes=52)},
        {"id": "m6", "scope": "session", "scope_key": S4, "kind": "preference", "text": "The digest goes out on Friday at 09:00; eight items, newest first.", "salience": 0.8, "version": 1, "created_at": ago(days=7), "last_accessed_at": ago(minutes=4)},
    ],
    "buckets": [{"scope": "global", "scope_key": "", "title": None, "count": 2}, {"scope": "session", "scope_key": S1, "title": "Bakery site", "count": 2}, {"scope": "session", "scope_key": S3, "title": "Support inbox", "count": 1}, {"scope": "session", "scope_key": S4, "title": "Weekly digest", "count": 1}],
    "sessions": {S1: "Bakery site", S3: "Support inbox", S4: "Weekly digest"},
}

BOARD = [
    {"id": "7f2a1c", "title": "Order form posts to the owner's sheet", "status": "doing", "priority": 2, "acceptance": "A test order appears as a row in the sheet within a minute; the form says so.", "checklist": [{"text": "sheet API token in the key proxy", "done": True}, {"text": "form → POST /order", "done": True}, {"text": "confirmation page", "done": False}], "depends_on": [], "session_id": S1, "origin_session_id": S1, "notes": "", "created_at": ago(hours=2), "updated_at": ago(minutes=20)},
    {"id": "3b9e44", "title": "Opening hours from the shop calendar", "status": "todo", "priority": 3, "acceptance": "The footer shows this week's hours, read from the calendar at build time.", "checklist": [], "depends_on": ["7f2a1c"], "session_id": None, "origin_session_id": S1, "notes": "", "created_at": ago(hours=2), "updated_at": ago(hours=2)},
    {"id": "c41d07", "title": "Refund policy page", "status": "review", "priority": 2, "acceptance": "The page answers the five questions from the support inbox.", "checklist": [{"text": "draft", "done": True}, {"text": "owner's wording", "done": True}], "depends_on": [], "session_id": S3, "origin_session_id": S3, "notes": "", "created_at": ago(days=1), "updated_at": ago(minutes=50)},
    {"id": "9d0b12", "title": "Answer the three delivery questions", "status": "blocked", "priority": 1, "acceptance": "Each customer has a reply; the operator confirmed the courier's rates.", "checklist": [], "depends_on": ["e8a5f3"], "session_id": None, "origin_session_id": S3, "notes": "", "created_at": ago(hours=5), "updated_at": ago(hours=1)},
    {"id": "e8a5f3", "title": "Courier rates for next month", "status": "todo", "priority": 1, "acceptance": "The operator posted the rates.", "checklist": [], "depends_on": [], "session_id": None, "origin_session_id": None, "notes": "", "created_at": ago(hours=5), "updated_at": ago(hours=5)},
    {"id": "51c8aa", "title": "Gallery: six square photos", "status": "done", "priority": 3, "acceptance": "Six photos in photos/gallery, 800×800.", "checklist": [{"text": "pick", "done": True}, {"text": "crop", "done": True}], "depends_on": [], "session_id": S2, "origin_session_id": S2, "notes": "", "created_at": ago(hours=1), "updated_at": ago(minutes=12)},
]

INBOX = [
    {"id": 31, "at": ago(minutes=4), "kind": "ask_user", "severity": "notice", "title": "Weekly digest asks: keep it to the usual 8?", "body": "The digest has 14 items this week.", "session_id": S4, "run_id": "r4", "read": 0},
    {"id": 30, "at": ago(minutes=20), "kind": "self_change", "severity": "notice", "title": "Pull request #57 is waiting for your decision", "body": "WebSearch: retry a backend that timed out once before falling back", "session_id": None, "run_id": None, "read": 0},
    {"id": 29, "at": ago(minutes=52), "kind": "loop", "severity": "info", "title": "Support inbox: 6 answered, 2 on the board", "body": "Two delivery questions wait for the courier's rates.", "session_id": S3, "run_id": "r3", "read": 1},
    {"id": 28, "at": ago(hours=3), "kind": "service", "severity": "warning", "title": "digest-api restarted after the rebuild", "body": "It was running before the rebuild and is running again on :8101.", "session_id": S4, "run_id": None, "read": 1},
    {"id": 27, "at": ago(hours=20), "kind": "balance", "severity": "warning", "title": "DeepSeek balance below $5", "body": "$4.62 left; the next threshold is $2.", "session_id": None, "run_id": None, "read": 1},
]

PROPOSALS = [{"id": "p57", "repo": "daedalus", "branch": "bot/websearch-retry", "pr_number": 57, "pr_url": "https://github.com/example/daedalus/pull/57", "title": "WebSearch: retry a backend that timed out once before falling back", "summary": "A backend that answers 504 once is tried again after a second; only a second failure falls through to the next backend. Unit test added.", "status": "pending", "reason": None, "created_at": ago(minutes=20)}]

SCHEDULES = [
    {"id": "sch1", "name": "Weekly digest", "run_in": "self", "cron": "0 9 * * 5", "run_at": None, "prompt": "Collect the week's notes and send the digest.", "enabled": 1, "next_run_at": ahead(days=2, hours=8), "last_run_at": ago(days=5), "last_summary": "Eight items sent; two links fixed.", "kind": "agent", "target_session": S4, "created_by_session": S4, "active_session_id": None, "failure_count": 0, "last_error": None},
    {"id": "sch2", "name": "Backup check", "run_in": "new", "cron": "30 3 * * *", "run_at": None, "prompt": "Verify last night's backup restored into a scratch directory; report sizes.", "enabled": 1, "next_run_at": ahead(hours=5), "last_run_at": ago(hours=19), "last_summary": "Restore ok: 2.1 GB, 12 340 files.", "kind": "agent", "target_session": None, "created_by_session": None, "active_session_id": None, "failure_count": 0, "last_error": None},
    {"id": "sch3", "name": "Remind: renew the domain", "run_in": "new", "cron": None, "run_at": ahead(days=12), "prompt": "The bakery domain renews in 3 days.", "enabled": 1, "next_run_at": ahead(days=12), "last_run_at": None, "last_summary": None, "kind": "message", "target_session": None, "created_by_session": S1, "active_session_id": None, "failure_count": 0, "last_error": None},
]

WORKSPACES = [
    {"name": S1, "path": f"/srv/workspaces/{S1}", "sessions": [{"id": S1, "title": "Bakery site"}, {"id": S2, "title": "Bakery site: photos"}], "files": 214, "size": 48_300_000, "mtime": (NOW - timedelta(minutes=4)).timestamp(), "own_session": True, "kind": "session", "schedule": None},
    {"name": S3, "path": f"/srv/workspaces/{S3}", "sessions": [{"id": S3, "title": "Support inbox"}], "files": 58, "size": 1_200_000, "mtime": (NOW - timedelta(minutes=52)).timestamp(), "own_session": True, "kind": "session", "schedule": None},
    {"name": "sched-backup-check", "path": "/srv/workspaces/sched-backup-check", "sessions": [], "files": 9, "size": 40_000, "mtime": (NOW - timedelta(hours=19)).timestamp(), "own_session": False, "kind": "schedule", "schedule": "Backup check"},
    {"name": "shared-assets", "path": "/srv/workspaces/shared-assets", "sessions": [], "files": 131, "size": 260_000_000, "mtime": (NOW - timedelta(days=2)).timestamp(), "own_session": False, "kind": "named", "schedule": None},
]


def usage_data() -> dict:
    daily = []
    for d in range(14):
        day = (NOW - timedelta(days=13 - d)).date().isoformat()
        daily.append({"day": day, "provider_id": "deepseek", "model": "deepseek-flash", "calls": 40 + (d * 7) % 23, "input_tokens": 900_000 + (d * 131_000) % 700_000, "output_tokens": 38_000 + (d * 9_000) % 30_000, "cache_read_tokens": 600_000, "reasoning_tokens": 12_000, "cost_usd": round(0.42 + ((d * 37) % 90) / 100, 2), "unmetered": 0})
        daily.append({"day": day, "provider_id": "claude", "model": "claude-opus-5", "calls": 12 + (d * 5) % 11, "input_tokens": 500_000, "output_tokens": 21_000, "cache_read_tokens": 450_000, "reasoning_tokens": 0, "cost_usd": 0, "unmetered": 12 + (d * 5) % 11})
        daily.append({"day": day, "provider_id": "opencode", "model": "gpt-5.6-luna", "calls": 20 + (d * 3) % 9, "input_tokens": 300_000, "output_tokens": 15_000, "cache_read_tokens": 200_000, "reasoning_tokens": 4_000, "cost_usd": round(0.2 + ((d * 17) % 50) / 100, 2), "unmetered": 0})
    recent = [
        {"at": ago(minutes=3), "provider_id": "claude", "model": "claude-opus-5", "purpose": "stream", "session_id": S1, "session_title": "Bakery site", "run_id": "r1", "input_tokens": 41_200, "output_tokens": 1_180, "cache_read_tokens": 39_000, "reasoning_tokens": 0, "cost_usd": 0, "duration_ms": 8_400, "raw": {}},
        {"at": ago(minutes=4), "provider_id": "deepseek", "model": "deepseek-flash", "purpose": "stream", "session_id": S4, "session_title": "Weekly digest", "run_id": "r4", "input_tokens": 18_300, "output_tokens": 640, "cache_read_tokens": 16_000, "reasoning_tokens": 210, "cost_usd": 0.0061, "duration_ms": 3_100, "raw": {}},
        {"at": ago(minutes=12), "provider_id": "opencode", "model": "gpt-5.6-luna", "purpose": "stream", "session_id": S3, "session_title": "Support inbox", "run_id": "r3", "input_tokens": 22_800, "output_tokens": 2_010, "cache_read_tokens": 20_000, "reasoning_tokens": 800, "cost_usd": 0.0142, "duration_ms": 12_900, "raw": {}},
        {"at": ago(minutes=40), "provider_id": "deepseek", "model": "deepseek-flash", "purpose": "structured", "session_id": S6, "session_title": "Expense tracker", "run_id": None, "input_tokens": 61_000, "output_tokens": 1_900, "cache_read_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.019, "duration_ms": 22_000, "raw": {}},
    ]
    sessions = [
        {"session_id": S1, "title": "Bakery site", "calls": 38, "input_tokens": 612_000, "output_tokens": 24_100, "cost_usd": 0, "unmetered": 38},
        {"session_id": S3, "title": "Support inbox", "calls": 210, "input_tokens": 3_900_000, "output_tokens": 180_000, "cost_usd": 2.31, "unmetered": 0},
        {"session_id": S4, "title": "Weekly digest", "calls": 64, "input_tokens": 1_100_000, "output_tokens": 41_000, "cost_usd": 0.58, "unmetered": 0},
        {"session_id": S6, "title": "Expense tracker", "calls": 31, "input_tokens": 700_000, "output_tokens": 22_000, "cost_usd": 0.29, "unmetered": 0},
    ]
    subscriptions = {
        "claude": {"provider": "claude", "logged_in": True, "plan": "max", "windows": [{"name": "5h", "used_percent": 23, "resets_at": ahead(hours=3)}, {"name": "weekly", "used_percent": 41, "resets_at": ahead(days=4)}]},
        "opencode": {"provider": "opencode", "logged_in": True, "plan": "go", "windows": [{"name": "5h", "used_percent": 8, "resets_at": ahead(hours=1)}, {"name": "weekly", "used_percent": 12, "resets_at": ahead(days=6)}, {"name": "monthly", "used_percent": 31, "resets_at": ahead(days=17)}]},
    }
    return {"daily": daily, "recent": recent, "sessions": sessions, "subscriptions": subscriptions}


SETTINGS = {
    "model": {"preset": "deepseek-flash", "chain": ["deepseek-flash", "gpt-5.6-luna"]},
    "presets": {
        "deepseek-flash": {"provider": "deepseek", "model": "deepseek-flash", "label": "DeepSeek Flash", "thinking": True, "reasoning_effort": "medium", "images": False, "context_window": 128000, "max_output_tokens": 16384},
        "claude-opus-5": {"provider": "claude", "model": "claude-opus-5", "label": "Claude Opus 5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200000, "max_output_tokens": 32000},
        "gpt-5.6-luna": {"provider": "opencode", "model": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "thinking": True, "reasoning_effort": "medium", "images": True, "context_window": 200000, "max_output_tokens": 32000},
        "qwen-local": {"provider": "vllm", "model": "Qwen3.8", "label": "Local Qwen3.8", "thinking": False, "reasoning_effort": "", "images": False, "context_window": 65536, "max_output_tokens": 8192},
    },
    "providers": {}, "prompt": {"rules": ""}, "vision": {"preset": "gpt-5.6-luna", "max_output_tokens": 800},
    "asr": {"provider": "", "url": "", "api_key": "", "model": "", "language": "auto", "timeout_seconds": 60, "max_seconds": 120, "autosend": False},
    "tools": {"web": {"fetch_timeout_seconds": 30, "proxy": "", "user_agent": "", "fetch_max_chars": 40000, "search": {"backend": "searxng", "fallback": [], "results": 8, "timeout_seconds": 20, "searxng": {"url": "", "engines": "", "categories": "", "safesearch": 0}, "duckduckgo": {"url": "", "region": ""}, "serper": {"base_url": "", "gl": "", "hl": ""}, "keenable": {"base_url": "", "snippet_max_length": 0}, "tavily": {"base_url": "", "depth": ""}, "exa": {"base_url": "", "type": ""}, "perplexity": {"base_url": ""}}}, "exec": {"max_output_chars": 20000}},
    "self_change": {"approval": "manual", "auto_rebuild": True},
    "limits": {"max_iterations": 200, "tool_timeout_seconds": 900, "usd_per_run": 5, "usd_total": 0, "usd_total_per_provider": {}, "total_since": ""},
    "balance": {"enabled": True, "poll_seconds": 60, "thresholds_usd": [5, 2, 0.5]},
    "scheduler": {"topic_mode": "per_task", "catch_up_missed": True},
    "compaction": {"auto_ratio": 0.7, "keep_recent_messages": 6, "max_words": 900, "chunk_tokens": 30000, "min_messages": 12, "core_trigger_ratio": 0.85},
    "telegram": {"verbosity": 1, "reactions": True, "topic_status_emoji": True, "stale_after_seconds": 600, "max_inbound_file_mb": 200, "forward_unknown_commands": True, "slow_tool_seconds": 30},
}


def png(width: int, height: int) -> bytes:
    """A small warm gradient with a lighter block: the 'phone screenshot' the agent looked at."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            base = (28 + y * 40 // height, 22 + y * 25 // height, 20 + y * 18 // height)
            if height * 0.18 < y < height * 0.42 and width * 0.08 < x < width * 0.92:
                base = (236, 224, 205)
            elif height * 0.5 < y < height * 0.9 and width * 0.08 < x < width * 0.92 and int((y - height * 0.5) // (height * 0.1)) % 2 == 0:
                base = (60, 48, 40)
            row.extend(base)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


PHONE_PNG = png(390, 520)


def wav(seconds: float = 30.0, rate: int = 8000) -> bytes:
    """A playable, silent WAV.

    The speaking state is not a flag the page can be put into: it is true while an `<audio>` element
    is playing, so the picture needs something that really plays. Silence for half a minute is the
    smallest thing that is really audio.
    """
    frames = int(seconds * rate)
    body = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(body))
    return header + body


SILENCE = wav()


VOICE = {
    "enabled": True,
    "session_id": S2,
    "model": "Qwen 3.7 Flash",
    "tts": {"configured": True, "reason": "", "voice": "alloy", "model": "kokoro"},
    # A model that runs on this machine, in memory and ready: the case the page is designed around.
    "stt": {
        "configured": True,
        "reason": "",
        "kind": "local",
        "state": "ready",
        "loaded_in_ms": 5400,
        "error": "",
        "local": {"model": "nemotron-streaming-multi", "label": "Nemotron 3.5 Streaming 0.6B", "installed": True, "active": True, "streaming": True, "loaded": "nemotron-streaming-multi"},
    },
    # One of each state the panel can be in: a line it said mid-run, which wins over the answer and
    # carries its own timestamp; a finished answer; and the two things it can be waiting for.
    "agents": [
        {
            "session_id": S1,
            "title": "Bakery site",
            "status": "running",
            "last_message_at": ago(minutes=6),
            "answer": "Rewriting the menu page so the seasonal section reads from one JSON file.",
            "progress": "Reading the menu page to see how the seasonal section is put together.",
            "progress_at": ago(seconds=20),
        },
        {"session_id": S3, "title": "Support inbox", "status": "idle", "last_message_at": ago(minutes=52), "answer": "Answered nine of eleven; two went on the board as they need a price decision."},
        {"session_id": S4, "title": "Weekly digest", "status": "waiting", "last_message_at": ago(minutes=4), "answer": "Which week should the digest cover — the one that just ended, or the running one?", "waiting": "operator"},
        {
            "session_id": S2,
            "title": "Invoice run",
            "status": "waiting",
            "last_message_at": ago(minutes=1),
            "answer": "Ready to send the eleven invoices for last month.",
            "progress": "Asking to send the invoices.",
            "progress_at": ago(seconds=50),
            "waiting": "approval",
        },
    ],
    "listening": True,
}


# An installation that has already been set up, and one that has not: "Add a model" is the first
# thing a fresh install shows, so it gets a picture of its own.
# One key, two command-line logins, and the rest with nothing behind them: the mixture a machine
# really has, and the one the cards have to tell apart.
PROVIDERS = [
    {"id": "openrouter", "kind": "openrouter", "base_url": "http://keyproxy:3200/openrouter", "via_proxy": True, "key_held": True, "key_kind": "api_key", "ready": True},
    {"id": "codex", "kind": "openai_compat", "base_url": "http://keyproxy:3200/codex/v1", "via_proxy": True, "key_held": True, "key_kind": "cli_login", "ready": True},
    {"id": "grok", "kind": "openai_compat", "base_url": "http://keyproxy:3200/grok/v1", "via_proxy": True, "key_held": True, "key_kind": "cli_login", "ready": True},
    {"id": "opencode", "kind": "opencode", "base_url": "http://keyproxy:3200/opencode", "via_proxy": True, "key_held": False, "key_kind": "api_key", "ready": False},
    {"id": "deepseek", "kind": "deepseek", "base_url": "http://keyproxy:3200/deepseek", "via_proxy": True, "key_held": False, "key_kind": "api_key", "ready": False},
    {"id": "claude", "kind": "openai_compat", "base_url": "http://keyproxy:3200/claude/v1", "via_proxy": True, "key_held": False, "key_kind": "cli_login", "ready": False},
    {"id": "vllm", "kind": "vllm", "base_url": "", "via_proxy": False, "key_held": False, "key_kind": "endpoint", "ready": False},
]
ONBOARDING = {"has_model": True, "presets": 4, "default_preset": "deepseek-flash", "providers": PROVIDERS, "needs": [], "message": ""}
FRESH = {"has_model": False, "presets": 0, "default_preset": "", "providers": PROVIDERS, "needs": ["model"], "message": "No model is configured yet. Add one in the app: Settings \u2192 Models \u2192 Add a model."}


def catalogue_entry(id_: str, name: str, context: int, *, images: bool, reasoning: bool, price_in: float, price_out: float) -> dict:
    return {
        "id": id_,
        "name": name,
        "context_length": context,
        "max_output_tokens": 32000,
        "input_modalities": ["text", "image"] if images else ["text"],
        "images": images,
        "reasoning": reasoning,
        "pricing": {"input": price_in, "output": price_out, "cache_hit": round(price_in / 10, 4)},
    }


CATALOGUE = [
    catalogue_entry("anthropic/claude-opus-5", "Claude Opus 5", 1000000, images=True, reasoning=True, price_in=5.0, price_out=25.0),
    catalogue_entry("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash", 128000, images=True, reasoning=True, price_in=0.28, price_out=0.42),
    catalogue_entry("openai/gpt-5.6-luna", "GPT-5.6 Luna", 400000, images=True, reasoning=True, price_in=0.4, price_out=1.8),
    catalogue_entry("qwen/qwen3.8-max", "Qwen3.8 Max", 256000, images=False, reasoning=True, price_in=2.0, price_out=6.0),
    catalogue_entry("moonshot/kimi-k3", "Kimi K3", 256000, images=False, reasoning=True, price_in=3.0, price_out=15.0),
    catalogue_entry("z-ai/glm-5.3-flash", "GLM-5.3 Flash", 200000, images=False, reasoning=True, price_in=0.15, price_out=0.5),
]


# The installation in the pictures develops itself on a server: it has both checkouts, a token and a
# rebuilder, so Changes is in the nav and carries no qualifying tag.
CAPABILITIES = {"selfdev": {"mode": "server", "configured": "auto", "reasons": ["checkouts: both writable", "token: configured", "remotes: both have an origin", "rebuild: the rebuilder service"], "missing": [], "tools": ["SelfPropose", "SelfRebuild", "SelfRollback", "SelfWorkspace"]}, "restart_required": None, "last_change": None}


# ---- the stub API -------------------------------------------------------------------------


def respond(route, body, *, content_type: str = "application/json", status: int = 200) -> None:  # type: ignore[no-untyped-def]
    route.fulfill(status=status, content_type=content_type, body=body if isinstance(body, (bytes, str)) else json.dumps(body))


def stub(route) -> None:  # type: ignore[no-untyped-def]
    request = route.request
    url = request.url
    path = url.split("?", 1)[0]
    rel = path[path.index("/api/") :]
    if rel == "/api/providers/lookup-models":
        return respond(route, {"base_url": "http://keyproxy:3200/openrouter/v1", "models": [e["id"] for e in CATALOGUE], "entries": CATALOGUE})
    if rel == "/api/voice/tts":
        return respond(route, SILENCE, content_type="audio/wav")
    if request.method != "GET":
        return respond(route, {"ok": True})
    if rel == "/api/stt/progress":
        # The voice page opens this to hear the engine finish loading; the picker opens it for the
        # download bars. Neither needs anything to happen here — what is true now is on /api/voice.
        over = getattr(stub, "voice_over", None) or {}
        load = {"kind": "engine", "state": over.get("state", VOICE["stt"]["state"]), "model": VOICE["stt"]["local"]["model"], "loaded_in_ms": over.get("loaded_in_ms", VOICE["stt"]["loaded_in_ms"]), "error": ""}
        return respond(route, f"data: {json.dumps(load)}\n\n", content_type="text/event-stream")
    if rel == "/api/voice/stream":
        # The concierge's half of the conversation, canned: the page has no other way into its
        # thinking, delegating and speaking states, and those are three of the five worth a picture.
        return respond(route, "event: hello\ndata: {}\n\n" + getattr(stub, "voice_frames", ""), content_type="text/event-stream")
    if rel.endswith("/stream"):
        return respond(route, "event: hello\ndata: {}\n\n", content_type="text/event-stream")
    if rel == "/api/auth/me":
        if getattr(stub, "signedout", False):
            return respond(route, {"detail": "not signed in"}, status=401)
        return respond(route, {"user_id": 1, "via": "token"})
    if rel == "/api/auth/config":
        return respond(route, {"telegram": None, "passkeys": 1, "pairing": True})
    if rel == "/api/sessions":
        return respond(route, SESSIONS)
    if rel == "/api/services":
        return respond(route, SERVICES_ALL)
    if rel.startswith("/api/sessions/"):
        parts = rel.split("/")
        sid = parts[3]
        tail = "/".join(parts[4:])
        if tail == "":
            return respond(route, detail(sid))
        if tail == "files":
            return respond(route, {"path": "", "kind": "dir", "entries": FILES[""]})
        if tail == "download":
            q = url.split("?", 1)[1] if "?" in url else ""
            p = [kv.split("=", 1)[1] for kv in q.split("&") if kv.startswith("path=")][0].replace("%2F", "/")
            if p.endswith(".png"):
                return respond(route, PHONE_PNG, content_type="image/png")
            return respond(route, NOTES_MD if "NOTES" in p else REPORT_MD, content_type="text/markdown")
        if tail.startswith("sent/") and tail.endswith("download"):
            return respond(route, REPORT_MD, content_type="text/markdown")
        if tail == "tools/timing":
            return respond(route, {"items": [{"name": "Exec", "calls": 9, "errors": 0, "total_ms": 21000, "mean_ms": 2333}, {"name": "Read", "calls": 14, "errors": 0, "total_ms": 900, "mean_ms": 64}]})
        if tail == "checkpoints":
            return respond(route, {"checkpoints": [], "total": 0, "pruned": False, "pruned_before": None, "removed": 0, "note": "", "keep_days": 30, "keep_last": 50})
        if tail == "mcp":
            return respond(route, {"enabled": [], "servers": []})
        if tail == "tools":
            return respond(route, [])
        return respond(route, {})
    if rel.startswith("/api/usage/provider/"):
        return respond(route, PROVIDER_USAGE)
    if rel == "/api/usage":
        return respond(route, usage_data())
    if rel == "/api/balance":
        return respond(route, {"balances": {"deepseek": 4.62, "openrouter": 11.08}, "thresholds": [5, 2, 0.5]})
    if rel == "/api/memory":
        return respond(route, MEMORY)
    if rel == "/api/board":
        return respond(route, BOARD)
    if rel == "/api/inbox/unread":
        return respond(route, {"unread": 2})
    if rel == "/api/inbox":
        return respond(route, {"entries": INBOX, "unread": 2})
    if rel == "/api/proposals":
        return respond(route, PROPOSALS)
    if rel == "/api/schedules":
        return respond(route, SCHEDULES)
    if rel == "/api/projects":
        return respond(route, PROJECTS)
    if rel == "/api/workspaces":
        return respond(route, WORKSPACES)
    if rel.startswith("/api/workspaces/"):
        return respond(route, {"path": "", "kind": "dir", "entries": FILES[""]})
    if rel == "/api/settings":
        return respond(route, SETTINGS)
    if rel == "/api/onboarding":
        return respond(route, FRESH if getattr(stub, "fresh", False) else ONBOARDING)
    if rel == "/api/modes":
        return respond(route, {"quick": {}, "deep": {}, "careful": {}, "plan": {}})
    if rel == "/api/commands":
        return respond(route, [])
    if rel == "/api/voice":
        over = getattr(stub, "voice_over", None)
        return respond(route, {**VOICE, "stt": {**VOICE["stt"], **over}} if over else VOICE)
    if rel == "/api/tts":
        return respond(route, TTS)
    if rel == "/api/stt":
        return respond(route, STT)
    if rel.endswith("/progress"):
        # The two pickers subscribe to a download stream as they mount. Nothing is downloading in a
        # picture, so this is an empty stream rather than an unhandled route the reporter complains
        # about; an empty body would leave the reader in its retry loop for the whole shot.
        return route.fulfill(status=200, content_type="text/event-stream", body=": keepalive\n\n")
    if rel == "/api/asr":
        return respond(route, {"configured": False, "reason": "", "provider": "", "model": "", "max_seconds": 120, "autosend": False})
    if rel == "/api/heartbeat":
        return respond(route, {"enabled": True, "armed": True, "interval_minutes": 60, "active_hours": "08:00-23:00", "preset": "", "max_runs_per_day": 24, "last_run": ago(minutes=35), "runs_today": 9, "session_id": None, "running": False, "file": "/srv/state/HEARTBEAT.md", "text": "Check the services and the board; write to the inbox only when something needs me."})
    if rel == "/api/capabilities":
        return respond(route, CAPABILITIES)
    if rel == "/api/status":
        return respond(route, {"ok": True})
    if rel in GATES:
        return respond(route, GATES[rel])
    # A route nobody taught this stub about is answered with nothing and reported at the end: the
    # app grows gates (a model, the capabilities) that decide whether a screen is drawn at all, and
    # one harness knowing about them while another does not is how the pictures and the numbers
    # come to describe different apps.
    UNHANDLED.record(rel)
    return respond(route, [])


def _tts_voice(vid, label, language, gender, size, disk, quality, speed, rtf, note, installed=False, selected=False, speakers=(), recommended=()):  # type: ignore[no-untyped-def]
    return {
        "id": vid, "label": label, "kind": "vits", "language": language, "gender": gender,
        "size_bytes": size, "disk_bytes": disk, "memory_mb": 120, "sample_rate": 22050,
        "licence": "CC0 (public domain)", "quality": quality, "speed": speed, "rtf": rtf,
        "keeps_up": rtf < 1.0, "note": note, "speakers": list(speakers),
        "recommended_for": list(recommended), "installed": installed, "installed_bytes": disk if installed else 0,
        "selected": selected,
    }


TTS = {
    "models": [
        _tts_voice("ru-dmitri", "Dmitri (Russian)", "ru", "male", 21_129_441, 36_577_368, 74, 53, 0.219,
                   "A clear male Russian, four times faster than speech.", installed=True, selected=True, recommended=("ru",)),
        _tts_voice("ru-irina", "Irina (Russian)", "ru", "female", 21_149_417, 36_577_296, 73, 39, 0.337,
                   "The female Russian voice. Warm and unhurried.", installed=True),
        _tts_voice("en-amy", "Amy (American English)", "en", "female", 21_028_122, 36_679_476, 72, 57, 0.195,
                   "Twenty megabytes, five times faster than speech.", recommended=("en",)),
        dict(_tts_voice("en-kokoro", "Kokoro (English, 11 voices)", "en", "mixed", 103_248_205, 157_947_103, 92, 13, 1.203,
                        "The best-sounding English here, and slower than real time.",
                        speakers=("af", "af_bella", "am_adam", "bf_emma", "bm_george")),
             kind="kokoro", sample_rate=24000, licence="Apache-2.0", memory_mb=320),
        _tts_voice("de-thorsten", "Thorsten (German)", "de", "male", 20_949_833, 36_577_367, 76, 60, 0.183,
                   "The German voice most German projects use.", recommended=("de",)),
        _tts_voice("fr-siwis", "Siwis (French)", "fr", "female", 20_914_888, 36_577_449, 73, 59, 0.189,
                   "A steady French, quick enough that nothing waits for it.", recommended=("fr",)),
    ],
    "languages": ["de", "en", "fr", "ru"],
    "selected": "ru-dmitri",
    "disk_bytes": 73_154_664,
    "root": "/srv/state/models/tts",
    "engine_installed": True,
    "recommended": {"de": "de-thorsten", "en": "en-amy", "fr": "fr-siwis", "ru": "ru-dmitri"},
    "state": {
        "voice": "ru-dmitri", "label": "Dmitri (Russian)", "language": "ru", "speaker": "", "speed": 1.0,
        "threads": 2, "installed": True, "active": True, "engine_installed": True, "state": "ready",
        "error": "", "loaded": "ru-dmitri", "encoder": True,
    },
}

STT = {
    "models": [{
        "id": "gigaam-ru", "label": "GigaAM v3 Russian", "kind": "nemo_transducer", "streaming": False,
        "languages": ["ru"], "language_count": 1, "size_bytes": 170_197_019, "disk_bytes": 178_000_000,
        "memory_mb": 380, "licence": "MIT", "accuracy": 91, "speed": 94,
        "note": "The best Russian here, and it writes the punctuation itself.", "url": "",
        "recommended_for": ["ru"], "verified": True, "detects_language": True,
        "installed": True, "installed_bytes": 178_000_000, "selected": True,
    }],
    "languages": ["en", "ru"], "selected": "gigaam-ru", "disk_bytes": 178_000_000,
    "root": "/srv/state/models/stt", "language": "auto", "threads": 2, "engine_installed": True,
    "decoders": {"opus": True, "any": False}, "recommended": {"ru": "gigaam-ru"},
}


UNHANDLED = Unhandled()

# ---- the shots ----------------------------------------------------------------------------

DESK = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


# The handful of words these helpers click on, in the language the run is in. Everything else is
# picked by class or by data, which no translation moves.
WORDS = {
    "en": {"steps": "8 steps", "files": "Workspace files", "workspaces": "Workspaces", "access": "Access"},
    "ru": {"steps": "8 шагов", "files": "Файлы рабочей папки", "workspaces": "Рабочие папки", "access": "Доступ"},
}


def word(key: str) -> str:
    return WORDS[LANG][key]


def shot(page: Page, name: str, route: str, *, wait: str = ".screen, .chat", settle: int = 900, before=None, full: bool = False) -> None:  # type: ignore[no-untyped-def]
    page.goto(f"{BASE}/{route}{'&' if '?' in route else '?'}token=t&scheme=dark&lang={LANG}")
    page.wait_for_selector(wait, timeout=15000)
    if before:
        before(page)
    page.wait_for_timeout(settle)
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
    print("wrote", name)


def expand_steps(page: Page) -> None:
    page.get_by_text(word("steps")).first.click()
    page.wait_for_timeout(600)
    # Scroll like a hand does: the timeline follows the newest content only until the reader scrolls.
    box = page.locator(".chat-scroll").bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -4000)
    page.wait_for_timeout(400)


def open_files_and_preview(page: Page) -> None:
    page.evaluate("() => { localStorage.setItem('daedalus.sessionList', '0'); localStorage.setItem('daedalus.session.aside', '0'); }")
    page.reload()
    page.wait_for_selector(".chat-scroll .timeline", timeout=15000)
    page.locator(f"button[aria-label='{word('files')}']").click()
    page.wait_for_selector(".side-pane", timeout=5000)
    page.locator(".side-pane .title", has_text="NOTES.md").click()
    page.wait_for_selector(".preview-backdrop .markdown, .preview-backdrop .md, .preview-backdrop", timeout=5000)


def open_share(page: Page) -> None:
    page.locator(".service-row").first.locator("button[aria-haspopup='menu']").click()
    page.wait_for_selector(".menu[role='menu']", timeout=5000)
    page.locator(".menu[role='menu'] button", has_text=word("access")).first.click()
    page.wait_for_selector(".access-options", timeout=5000)


def pick_a_model(page: Page) -> None:
    """Walk the flow far enough that the picture shows all three steps with something in them."""
    page.locator(".pickgrid .pick", has_text="OpenRouter").first.click()
    page.wait_for_selector(".modelgrid .pick", timeout=15000)
    page.locator(".modelgrid .pick", has_text="Claude Opus 5").first.click()


def open_more(page: Page) -> None:
    """The More sheet on a phone: every other destination, and the language switch under them."""
    page.locator(".tabbar button").last.click()
    page.wait_for_selector(".more-grid", timeout=5000)


def scroll_to_voices(page: Page) -> None:
    """The settings body scrolls inside itself, so a full-page shot would still show only the top of it.

    The synthesis picker is the last card in the Voice section; bringing it into view is what makes
    the picture about the voices rather than about the recogniser above them.
    """
    page.locator(".stt-list").last.scroll_into_view_if_needed()
    page.wait_for_timeout(400)


def open_workspaces(page: Page) -> None:
    page.locator(f"button[aria-label='{word('workspaces')}']").click()
    page.wait_for_selector(".sheet", timeout=5000)


def open_projects(page: Page) -> None:
    """The switcher over a list already grouped by project: the folders on one side, the agents in them on the other."""
    page.locator(".rail .project-chip").click()
    page.wait_for_selector(".project-row", timeout=5000)


# ---- the voice page in each of its states --------------------------------------------------
#
# Five states, three widths. None of them can be reached by clicking: the page's phase comes from the
# concierge's event stream, from the engine loading a model into memory, and from a microphone. So the
# stream is canned above, the engine state is set on /api/voice, and the microphone is a fake capture
# device the browser is launched with — which also gives the orb a real level to react to.


def sse(*frames: tuple[str, dict]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(body)}\n\n" for name, body in frames)


VOICE_STATES: dict[str, dict] = {
    "ready": {"over": {}, "frames": "", "mic": False, "ask": False},
    "loading": {"over": {"state": "loading", "loaded_in_ms": 0}, "frames": "", "mic": False, "ask": False},
    "listening": {"over": {}, "frames": "", "mic": True, "ask": False},
    "thinking": {
        "over": {},
        "frames": sse(("status", {"state": "thinking"}), ("partial", {"text": "Looking at the board and the two invoices that came back"})),
        "mic": True,
        "ask": True,
    },
    "speaking": {
        "over": {},
        "frames": sse(
            ("status", {"state": "idle"}),
            ("say", {"text": "All eleven invoices went out this morning."}),
            ("say", {"text": "Two came back with the wrong VAT line, and I have put both on the board."}),
            ("say", {"text": "Shall I have someone redo them now?"}),
        ),
        "mic": True,
        "ask": True,
    },
}

VOICE_ASKED = "How did the invoice run go?"


def listening(page: Page) -> None:
    """Open the microphone and say nothing: the state the page spends most of its time in."""
    page.locator(".voice-orb").click()
    page.wait_for_timeout(900)


def talking(page: Page) -> None:
    """Open the microphone and ask something, the way a hand does.

    The wait in the middle is not padding. The canned event stream is a finite body, so the page
    reads it, reaches the end and reconnects a second later — and asking something clears the answer
    on the screen. Asking after the first delivery and shooting before the third is what leaves
    exactly one answer under exactly one question.
    """
    page.locator(".voice-orb").click()
    page.wait_for_timeout(1200)
    page.fill(".voice-compose .field", VOICE_ASKED)
    page.locator(".voice-compose button[type=submit]").click()


def voice_shots(page: Page, prefix: str) -> None:
    for name, plan in VOICE_STATES.items():
        stub.voice_over = plan["over"]  # type: ignore[attr-defined]
        stub.voice_frames = plan["frames"]  # type: ignore[attr-defined]
        before = talking if plan["ask"] else listening if plan["mic"] else None
        shot(page, f"{prefix}{name}", "voice", settle=1000, before=before)
    stub.voice_over = None  # type: ignore[attr-defined]
    stub.voice_frames = ""  # type: ignore[attr-defined]


FAKE_MEDIA = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"]


def run_voice() -> int:
    """Only the voice page, in every state, at the three widths that have to hold it."""
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=FAKE_MEDIA)
        for prefix, viewport, mobile in (
            ("voice-", {"width": 1440, "height": 900}, False),
            ("voice-wide-", {"width": 2560, "height": 1300}, False),
            ("voice-phone-", {"width": 390, "height": 844}, True),
        ):
            context = browser.new_context(viewport=viewport, color_scheme="dark", is_mobile=mobile, has_touch=mobile, permissions=["microphone"])
            page = context.new_page()
            page.route("**/api/**", stub)
            voice_shots(page, prefix)
            context.close()
        browser.close()
    return UNHANDLED.report()


def run() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desk = browser.new_context(viewport=DESK, device_scale_factor=2, color_scheme="dark")
        desk.add_init_script("try { localStorage.setItem('daedalus.session.aside', '1'); localStorage.setItem('daedalus.session.view', 'chat'); localStorage.setItem('agents.groupBy', 'workspace'); } catch (e) {}")
        page = desk.new_page()
        page.route("**/api/**", stub)
        shot(page, "bots", "agents")
        shot(page, "session", f"agents/{S1}", wait=".chat-scroll .timeline", before=expand_steps, settle=300)
        shot(page, "dual", f"agents/{S1}?with={S2}", wait=".chat-scroll .timeline", settle=1500)
        shot(page, "files-preview", f"agents/{S1}", wait=".chat-scroll .timeline", before=open_files_and_preview, settle=1200)
        shot(page, "session-share", f"agents/{S1}", wait=".chat-scroll .timeline", before=open_share, settle=800)
        shot(page, "workspaces", "agents", before=open_workspaces)
        desk.add_init_script("try { localStorage.setItem('agents.groupBy', 'project'); } catch (e) {}")
        shot(page, "projects", "agents", before=open_projects)
        shot(page, "voice", "voice")
        shot(page, "voice-settings", "settings/voice", wait=".stt-list .stt-card", before=scroll_to_voices, settle=700)
        shot(page, "board", "board")
        shot(page, "inbox", "inbox")
        shot(page, "cron", "schedules")
        shot(page, "services", "services")
        shot(page, "usage", "usage", settle=1500)
        shot(page, "memory", "memory")
        # The settings index, because the language switch is its first row.
        shot(page, "settings", "settings")
        stub.fresh = True  # type: ignore[attr-defined]
        shot(page, "add-model", "agents", wait=".addmodel", before=pick_a_model, settle=600)
        stub.fresh = False  # type: ignore[attr-defined]
        stub.signedout = True  # type: ignore[attr-defined]
        shot(page, "login", "agents", wait=".login", settle=500)
        stub.signedout = False  # type: ignore[attr-defined]
        desk.close()

        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, color_scheme="dark", is_mobile=True, has_touch=True)
        phone.add_init_script("try { localStorage.setItem('agents.groupBy', 'status'); } catch (e) {}")
        page = phone.new_page()
        page.route("**/api/**", stub)
        shot(page, "phone-bots", "agents")
        shot(page, "phone-session", f"agents/{S1}", wait=".chat-scroll .timeline", before=expand_steps, settle=300)
        shot(page, "phone-voice", "voice")
        shot(page, "phone-memory", "memory")
        shot(page, "phone-more", "agents", before=open_more)
        stub.fresh = True  # type: ignore[attr-defined]
        shot(page, "phone-add-model", "agents", wait=".addmodel", before=pick_a_model, settle=600)
        stub.fresh = False  # type: ignore[attr-defined]
        phone.close()
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run_voice() if os.environ.get("ONLY") == "voice" else run())
