"""The main chat holds still while the operator works beside it, as a project's focus page always did.

What the operator saw: on orchestration's home, toggling the right panel or switching its tabs rebuilt
the whole page, and opening a project jerked it; inside a project the same actions were calm. The
panel of the main chat wrote its tab into the main session's plain address, /app/agents/<id>, so
each click drew Agents mode — its column, a second conversation — and came back when that
conversation moved itself home. Opening a project for the first time drew the column's and the
centre's loading fallbacks before the project itself.

For each action, on both pages at 1440 px, the elements are marked beforehand and read afterwards:

- the panel toggled closed and open, and every panel tab in turn: the rail, the left column and the
  conversation are the very same elements, and the address never leaves the page's own;
- a project opened from the main chat's column, and the main chat opened from a project's column:
  the rail and the centre's frame survive, and the new column and conversation arrive without a
  loading fallback drawn first;
- all of them: no layout shift above 0.01 and no task longer than 50 ms.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from check_orchestration_mode import PID, go, serve, stubs  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

MAX_SHIFT = 0.01
MAX_TASK_MS = 50

# Installed before the app: layout shifts and long tasks from the start, and every address the app
# writes, so a detour through another page is seen even when it is undone within the same click.
WATCH = """
(() => {
  window.__shifts = []; window.__long = []; window.__paths = [];
  new PerformanceObserver((list) => { for (const e of list.getEntries()) if (!e.hadRecentInput) window.__shifts.push(e.value); }).observe({ type: "layout-shift", buffered: true });
  new PerformanceObserver((list) => { for (const e of list.getEntries()) window.__long.push(Math.round(e.duration)); }).observe({ type: "longtask", buffered: true });
  for (const name of ["pushState", "replaceState"]) {
    const original = history[name].bind(history);
    history[name] = (state, title, url) => { if (url !== undefined && url !== null) window.__paths.push(new URL(String(url), location.href).pathname); return original(state, title, url); };
  }
})();
"""

# The parts that must survive, and a record of every fallback drawn while the action runs.
PARTS = {"rail": "nav.rail", "sidebar": "nav.sidebar", "centre": ".app > .main", "chat": ".chat"}
BEGIN = """
(parts) => {
  window.__shifts = []; window.__long = []; window.__paths = []; window.__fallbacks = [];
  if (window.__watch) window.__watch.disconnect();
  window.__watch = new MutationObserver((records) => {
    for (const r of records) for (const n of r.addedNodes) {
      if (n.nodeType !== 1) continue;
      // A Suspense fallback: the empty column placeholder, or the centre's loading line.
      if (n.matches("nav.sidebar") && n.childElementCount === 0) window.__fallbacks.push("empty column");
      if (n.matches(".main > .empty")) window.__fallbacks.push("loading: " + n.textContent.trim());
    }
  });
  window.__watch.observe(document.querySelector(".app"), { childList: true, subtree: true });
  for (const [name, selector] of Object.entries(parts)) { const el = document.querySelector(selector); if (el) el.dataset.stability = name; }
}
"""
END = """
(parts) => {
  const same = {};
  for (const [name, selector] of Object.entries(parts)) { const el = document.querySelector(selector); same[name] = !!el && el.dataset.stability === name; }
  return { same, shift: window.__shifts.reduce((a, v) => a + v, 0), long: window.__long, paths: window.__paths, fallbacks: window.__fallbacks };
}
"""


def settle(page: Page) -> None:
    """Two frames and a pause: long enough for a detour to go out and come back, and for its shifts to be reported."""
    page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
    page.wait_for_timeout(500)


def measured(page: Page, what: str, action, *, keep: tuple[str, ...], stay: re.Pattern[str] | None) -> dict:
    page.evaluate(BEGIN, PARTS)
    action()
    settle(page)
    result = page.evaluate(END, PARTS)
    for part in keep:
        assert result["same"][part], f"{what}: the {part} was replaced ({result})"
    assert result["shift"] <= MAX_SHIFT, f"{what}: layout shift {result['shift']:.4f} ({result})"
    assert all(ms <= MAX_TASK_MS for ms in result["long"]), f"{what}: long task {max(result['long'])} ms ({result})"
    assert not result["fallbacks"], f"{what}: a loading fallback was drawn first ({result['fallbacks']})"
    if stay is not None:
        strays = [p for p in result["paths"] if not stay.search(p)]
        assert not strays, f"{what}: the address left the page for {strays}"
    return result


def chunks_ready(page: Page, *names: str) -> None:
    """Give the app the idle moment in which it fetches these chunks ahead.

    Bounded and not asserted: an app that does not fetch them ahead is caught by what it draws when
    the click has to download them, which is the fault the operator sees."""
    try:
        page.wait_for_function(
            "(names) => names.every((n) => performance.getEntriesByType('resource').some((e) => e.name.includes('/assets/' + n + '-')))",
            arg=list(names),
            timeout=5000,
        )
    except PlaywrightTimeout:
        pass


def panel_actions(page: Page, where: str, stay: re.Pattern[str]) -> None:
    everything = ("rail", "sidebar", "centre", "chat")
    toggle = page.locator(".chat .head-actions button[aria-label='Panel']")
    if page.locator(".panel").count() == 0:
        toggle.click()
        expect(page.locator(".panel")).to_have_count(1)
        settle(page)
    measured(page, f"{where}: panel closed", toggle.click, keep=everything, stay=stay)
    expect(page.locator(".panel")).to_have_count(0)
    measured(page, f"{where}: panel opened", toggle.click, keep=everything, stay=stay)
    expect(page.locator(".panel")).to_have_count(1)
    tabs = page.locator(".panel .panel-tab")
    names = tabs.evaluate_all("(tabs) => tabs.map((t) => t.dataset.tab)")
    assert len(names) >= 3, f"{where}: the panel offers {names}"
    for name in [*names[1:], names[0]]:
        tab = page.locator(f".panel .panel-tab[data-tab='{name}']")
        measured(page, f"{where}: panel tab {name}", tab.click, keep=everything, stay=stay)
        expect(tab).to_have_attribute("aria-selected", "true")


def main_chat(page: Page) -> None:
    focus, main = stubs("en")
    serve(page, focus, main, "en")
    go(page, "/orchestration", "en")
    expect(page.locator(".main-flow")).to_be_visible()
    expect(page.locator("nav.orch-sidebar")).to_be_visible()
    chunks_ready(page, "ProjectScreen", "ProjectSidebar")
    panel_actions(page, "main chat", re.compile(r"^/app/orchestration/?$"))

    def open_project() -> None:
        page.locator("nav.orch-sidebar .orch-row").first.click()
        expect(page.locator(".chat.in-project.orchestrator")).to_be_visible()

    measured(page, "main chat: open a project", open_project, keep=("rail", "centre"), stay=re.compile(rf"^/app/orchestration/project/{PID}/?$"))
    expect(page.locator("nav.project-sidebar")).to_be_visible()


def project_focus(page: Page) -> None:
    focus, main = stubs("en")
    main.opened = True  # the main chat's session exists already, as it does for the operator
    serve(page, focus, main, "en")
    go(page, f"/orchestration/project/{PID}", "en")
    expect(page.locator(".chat.in-project.orchestrator")).to_be_visible()
    expect(page.locator("nav.project-sidebar")).to_be_visible()
    chunks_ready(page, "MainScreen")
    panel_actions(page, "project focus", re.compile(rf"^/app/orchestration/project/{PID}/?$"))

    def open_main() -> None:
        page.locator("nav.project-sidebar .main-entry").first.click()
        expect(page.locator(".main-flow")).to_be_visible()

    measured(page, "project focus: open the main chat", open_main, keep=("rail", "centre"), stay=re.compile(r"^/app/orchestration/?$"))
    expect(page.locator("nav.orch-sidebar")).to_be_visible()


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for name, run in (("main chat", main_chat), ("project focus", project_focus)):
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.add_init_script(WATCH)
            run(page)
            context.close()
            print(f"stability of the {name}: ok")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
