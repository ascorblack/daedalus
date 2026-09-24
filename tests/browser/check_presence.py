"""The app tells the host what each window shows: visible, focused, and which sessions.

Opens a session and reads the report the page posts; hides the page and reads that it says so;
opens the split view and reads both sessions in one report. Everything else is answered by the
composer check's stub, which knows a session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import expect_app  # noqa: E402
from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, stub  # noqa: E402

SECOND = "sess-2"
REPORTS: list[dict] = []


def route_api(route) -> None:  # type: ignore[no-untyped-def]
    request = route.request
    path = request.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel == "/api/presence" and request.method == "POST":
        REPORTS.append(json.loads(request.post_data or "{}"))
        return route.fulfill(status=204, content_type="application/json", body="")
    if rel == f"/api/sessions/{SECOND}":
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({**HOST.detail(), "id": SECOND, "title": "Another session"}))
    if rel.startswith(f"/api/sessions/{SECOND}/"):
        return route.fulfill(status=200, content_type="application/json", body="[]")
    return stub(route)


def wait_for(page, predicate, what: str, problems: list[str]) -> dict | None:  # type: ignore[no-untyped-def]
    for _ in range(50):
        found = next((r for r in reversed(REPORTS) if predicate(r)), None)
        if found is not None:
            return found
        page.wait_for_timeout(100)
    problems.append(f"no report {what} (last: {REPORTS[-1] if REPORTS else None})")
    return None


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(f"{BASE}/agents/{SESSION}?token=t&lang=en")
        page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)

        seen = wait_for(page, lambda r: r.get("sessions") == [SESSION] and r.get("visible") and r.get("focused"), "naming the open session as visible and focused", problems)
        if seen is not None:
            print("open:", seen)
            if not seen.get("client") or seen.get("kind") != "browser" or seen.get("lang") != "en" or "tz" not in seen:
                problems.append(f"the report lacks its client, kind, language or zone: {seen}")

        # Hidden: the page says so at once rather than waiting to expire on the host.
        before = len(REPORTS)
        page.evaluate("""() => {
            Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        hidden = wait_for(page, lambda r: r.get("visible") is False and REPORTS.index(r) >= before, "saying the page is hidden", problems)
        print("hidden:", hidden)
        page.evaluate("""() => {
            Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
            document.dispatchEvent(new Event('visibilitychange'));
        }""")

        # The split view: both panes in one report.
        page.goto(f"{BASE}/agents/{SESSION}?with={SECOND}&token=t&lang=en")
        page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
        both = wait_for(page, lambda r: sorted(r.get("sessions") or []) == sorted([SESSION, SECOND]), "naming both sessions of the split view", problems)
        print("split:", both)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
