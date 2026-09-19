"""The Voice row in the list of folders goes to the voice page, and still opens.

The Voice project is where the voice agents live, so the list of agents draws it as a folder like any
other — and clicking its name opened that folder, which is not what the word means to the operator
reaching for it. The word is the mode. So the row is a link to the voice page and the chevron beside
it is a disclosure button of its own, with its own label, for the sessions underneath. Two controls
in one row only work if each says which it is, so that is what is asserted here, on the Agents screen
and in the sidebar that carries the same list on every other screen.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8203 /tmp/app-root &
    APP_URL=http://127.0.0.1:8203/app python3 tests/browser/check_voice_entry.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE


def open_list(browser, viewport: dict, route: str, single: bool = False):  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport=viewport, color_scheme="dark")
    page = context.new_page()

    def stub(request):  # type: ignore[no-untyped-def]
        if single and request.request.url.split("?", 1)[0].endswith("/api/sessions"):
            listing = shots.listing()
            listing["sessions"] = [s for s in listing["sessions"] if s["project_id"] != shots.PV or s["id"] == shots.S9]
            listing["projects"] = [{**p, "total": 1, "members": 1, "active": 0} if p["id"] == shots.PV else p for p in listing["projects"]]
            return shots.respond(request, listing)
        return shots.stub(request)

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/{route}?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".folder.system", timeout=15000)
    return context, page


def check_the_row(browser, check, where: str, scope: str, viewport: dict, route: str, single: bool = False) -> None:  # type: ignore[no-untyped-def]
    context, page = open_list(browser, viewport, route, single)
    head = page.locator(f"{scope} .folder.system .folder-head").first
    link = page.locator(f"{scope} .folder.system .folder-go").first
    chevron = page.locator(f"{scope} .folder.system .folder-disclose").first
    check(link.count() == 1 and chevron.count() == 1, f"{where}: the Voice row is a link and a disclosure, not one control doing both")
    check((link.get_attribute("href") or "").endswith("/voice"), f"{where}: the link goes to the voice page ({link.get_attribute('href')})")
    label = chevron.get_attribute("aria-label") or ""
    check("Voice" in label, f"{where}: the disclosure says what it opens ({label!r})")
    check(head.get_attribute("aria-expanded") is None and chevron.get_attribute("aria-expanded") is not None, f"{where}: and it is the one that carries the expanded state")
    check("Voice" in (link.inner_text() or ""), f"{where}: the name is on the link, which is what a reader clicks")

    # An ordinary project has nowhere else to go: its whole header stays the one control it was.
    plain = page.locator(f"{scope} .folder:not(.system) .folder-head").first
    check(plain.evaluate("el => el.tagName") == "BUTTON", f"{where}: an ordinary folder's header is still a single button")
    check(plain.get_attribute("aria-expanded") is not None, f"{where}: and still says whether it is open")

    # The chevron opens the folder where it stands, without leaving the screen.
    was = chevron.get_attribute("aria-expanded")
    chevron.click()
    page.wait_for_timeout(300)
    check(chevron.get_attribute("aria-expanded") != was, f"{where}: the chevron opens and closes the folder")
    check(page.evaluate("location.pathname").endswith(route), f"{where}: and does not navigate anywhere ({page.evaluate('location.pathname')})")

    if single:
        if chevron.get_attribute("aria-expanded") != "true":
            chevron.click()
        rows = page.locator(f"{scope} .folder.system .erow")
        check(rows.count() == 1 and rows.first.is_visible(), f"{where}: expanding exposes the sole Voice conversation")
        rows.first.click()
        page.wait_for_url(f"**/agents/{shots.S9}")
        check(page.evaluate("location.pathname").endswith(f"/agents/{shots.S9}"), f"{where}: the agent row opens its conversation")
        page.goto(f"{BASE}/{route}?token=t&scheme=dark&lang=en")
        page.wait_for_selector(f"{scope} .folder.system .folder-go")

    link.click()
    page.wait_for_selector(".voice-stage, .voice-grid", timeout=15000)
    check(page.evaluate("location.pathname").endswith("/voice"), f"{where}: clicking the name opens the voice page ({page.evaluate('location.pathname')})")
    context.close()


def main() -> int:
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM, args=shots.FAKE_MEDIA)
        check_the_row(browser, check, "the Agents screen", ".main", {"width": 1440, "height": 900}, "agents")
        # The sidebar carries the same list beside every other screen, and is where the operator
        # reaches for Voice most of the time. It has no column of its own beside Agents, which is the
        # list itself, so it is checked from a screen that has one.
        check_the_row(browser, check, "the sidebar", ".sidebar", {"width": 1680, "height": 1000}, "inbox")
        check_the_row(browser, check, "one Voice agent on Agents", ".main", {"width": 1440, "height": 900}, "agents", single=True)
        check_the_row(browser, check, "one Voice agent in the sidebar", ".sidebar", {"width": 1680, "height": 1000}, "inbox", single=True)
        browser.close()
    return failures


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
