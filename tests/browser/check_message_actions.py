"""Are a message's actions reachable, on a phone as well as on a desk?

The turn's actions used to sit in an overflow menu beside the message bubble. On a phone that
row is pushed past the right edge: the menu was simply not there. They are now a row of icon
buttons under the message — copy, and (for an operator's turn) fork and revert.

The check loads a session in headless Chromium with the API stubbed and asserts, at a phone
width and a desktop width, that every action button is inside the viewport, that the answer
has a copy button, and that pressing it puts the answer on the clipboard.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_message_actions.py

Exit 0 when the actions are reachable and copy works.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, GATES, Unhandled, expect_app  # noqa: E402

UNHANDLED = Unhandled()

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

ANSWER = "The answer the operator wants to copy, with a detail worth keeping."
SESSION = "sess-1"

DETAIL = {
    "id": SESSION,
    "title": "A session",
    "status": "idle",
    "run_id": None,
    "workspace": "/workspace",
    "workspace_name": "ws",
    "workspace_own": True,
    "workspace_sessions": [],
    "pending": None,
    "model": "some-model",
    "mode": "",
    "brief": "",
    "tools_off": [],
    "loop": None,
    "services": [],
    "subagents": [],
    "usage": {},
    "context": {"tokens": 10, "window": 100000, "messages": 2, "summaries": 0, "operator_turns": 1},
    "messages": [
        {
            "role": "user",
            "summary": False,
            "internal": False,
            "origin": "operator",
            "seq": 101,
            "compaction": None,
            "archived": None,
            "headline": "",
            "text": "A question from the operator",
            "thinking": "",
            "tool_calls": [],
            "tool_results": [],
            "created_at": "2026-09-13T12:00:00+00:00",
        },
        {
            "role": "assistant",
            "summary": False,
            "internal": False,
            "origin": "",
            "seq": 102,
            "compaction": None,
            "archived": None,
            "headline": "",
            "text": ANSWER,
            "thinking": "",
            "tool_calls": [],
            "tool_results": [],
            "created_at": "2026-09-13T12:00:05+00:00",
        },
    ],
}


def stub(route) -> None:  # type: ignore[no-untyped-def]
    url = route.request.url
    if url.split("?", 1)[0].endswith("/stream"):
        # The session's own event stream. An empty JSON body puts the reader in a retry loop for the
        # whole run; one hello frame and nothing after it is a session that is simply quiet.
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if "auth/me" in url:
        body = json.dumps({"user_id": 1, "via": "token"})
    elif f"/api/sessions/{SESSION}" in url and "/events" not in url and "/stream" not in url:
        body = json.dumps(DETAIL)
    elif "unread" in url:
        body = json.dumps({"unread": 0})
    elif url.rstrip("/").endswith("/api/sessions"):
        body = json.dumps({"sessions": [{"id": SESSION, "title": "A session", "status": "idle", "created_at": "2026-09-13T12:00:00+00:00", "last_message_at": "2026-09-13T12:00:05+00:00", "run_id": None}], "projects": [], "free": {"total": 1, "active": 0, "loops": 0, "last_message_at": ""}})
    else:
        # As in every other harness here: the shared gates first, then a report of what was missed.
        rel = url.split("?", 1)[0]
        rel = rel[rel.index("/api/"):] if "/api/" in rel else ""
        if rel in GATES:
            body = json.dumps(GATES[rel])
        else:
            if rel:
                UNHANDLED.record(rel)
            body = "[]"
    route.fulfill(status=200, content_type="application/json", body=body)


def visible_actions(page: Page) -> list[dict]:
    return page.evaluate(
        """() => [...document.querySelectorAll('.msg-actions')].map(row => {
             const r = row.getBoundingClientRect();
             return { left: r.left, right: r.right, width: r.width,
                      buttons: [...row.querySelectorAll('button')].map(b => b.getAttribute('aria-label')) };
           })"""
    )


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for name, width, height in [("phone", 390, 844), ("desktop", 1440, 900)]:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                is_mobile=name == "phone",
                has_touch=name == "phone",
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents/{SESSION}?token=t")
            page.wait_for_selector(".msg.user", timeout=15000)
            page.wait_for_timeout(400)
            # Hover brings the row in on a desktop; on a phone it is there already.
            page.locator(".turn").first.hover()
            page.wait_for_timeout(200)
            rows = visible_actions(page)
            print(f"{name}: {len(rows)} action row(s)")
            if len(rows) < 2:
                problems.append(f"{name}: expected actions under both the message and the answer, found {len(rows)}")
            for row in rows:
                print(f"   buttons={row['buttons']} left={row['left']:.0f} right={row['right']:.0f}")
                if row["right"] > width + 1 or row["left"] < -1:
                    problems.append(f"{name}: an action row is outside the viewport (left {row['left']:.0f}, right {row['right']:.0f}, width {width})")
                if row["width"] < 1:
                    problems.append(f"{name}: an action row has no size")
            labels = [b for row in rows for b in row["buttons"]]
            if "Copy" not in labels:
                problems.append(f"{name}: no copy button ({labels})")
            if not any(b and "Fork" in b for b in labels):
                problems.append(f"{name}: the operator's turn has no fork action ({labels})")
            if not any(b and "Revert" in b for b in labels):
                problems.append(f"{name}: the operator's turn has no revert action ({labels})")

            # The answer's copy button puts the answer on the clipboard.
            copy_button = page.locator(".msg-actions").last.locator("button[aria-label='Copy']")
            if copy_button.count() == 0:
                problems.append(f"{name}: the answer has no copy button")
            else:
                copy_button.click()
                page.wait_for_timeout(400)
                clip = page.evaluate("() => navigator.clipboard.readText()")
                print(f"{name}: clipboard = {clip[:40]!r}")
                if ANSWER not in (clip or ""):
                    problems.append(f"{name}: copying the answer did not reach the clipboard ({clip!r})")
            if os.environ.get("SHOTS"):
                page.screenshot(path=str(Path(__file__).parent / f"message-actions-{name}.png"))
            context.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    # A gate the app grew and this stub does not know about fails the run by name, rather than by a
    # selector that never appears somewhere further down.
    failed = run()
    sys.exit(failed or UNHANDLED.report())
