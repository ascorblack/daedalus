"""Measure the shell's sizes in a real browser and refuse the ones that grew back.

The stylesheet's own test (miniapp/src/density.test.ts) reads the rules; this reads the rendered
page, which is the only place a row's height or a column's width actually exists. Everything the
app asks for is answered by the invented installation in screenshots.py, so the numbers describe
the layout and nothing else. Three windows: a laptop, an ultrawide, a phone.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_density.py

Exit 0 when every size is within its limit. `MEASURE=path.json` writes what was measured (and, with
`ASSERT=0`, measures without judging — the way a build from before the redesign is read for the
before/after table).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import PROJECTS, S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ASSERT = os.environ.get("ASSERT", "1") != "0"
MEASURE = os.environ.get("MEASURE", "")

OPEN_FOLDERS = "try { " + " ".join(f"localStorage.setItem('daedalus.folder.{k}', '1');" for k in [p["id"] for p in PROJECTS] + ["\u0000free"]) + " } catch (e) {}"

# One reading of the page: rectangles and computed sizes of the parts the redesign is measured by.
# Every selector has a fallback to the name the same part had before, so the script reads both builds.
READ = """
() => {
  const px = (el, prop) => el ? parseFloat(getComputedStyle(el)[prop]) : null;
  const box = (el) => el ? { w: Math.round(el.getBoundingClientRect().width), h: Math.round(el.getBoundingClientRect().height) } : null;
  const one = (...sels) => { for (const s of sels) { const el = document.querySelector(s); if (el) return el; } return null; };
  const all = (...sels) => { for (const s of sels) { const list = document.querySelectorAll(s); if (list.length) return [...list]; } return []; };
  const rows = all('.sidebar .erow', '.session-list-pane .erow', '.agents-screen .erow');
  const single = rows.filter((r) => !r.querySelector('.erow-meta') && !r.querySelector('.erow-line2'));
  const double = rows.filter((r) => r.querySelector('.erow-meta') || r.querySelector('.erow-line2'));
  const acts = all('.act:not(.head)');
  const icons = all('.chat-head .iconbtn', '.pagehead .iconbtn');
  return {
    body: px(document.body, 'fontSize'),
    left: box(one('.sidebar', '.rail')),
    list: box(one('.session-list-pane')),
    aside: box(one('.session-aside')),
    chat: box(one('.chat')),
    head: box(one('.chat-head')),
    timeline: box(one('.timeline')),
    composerBox: box(one('.composer-box')),
    roundbtn: box(one('.roundbtn')),
    answerFs: px(one('.answer'), 'fontSize'),
    userFs: px(one('.msg.user'), 'fontSize'),
    userW: box(one('.msg.user')),
    rowSingle: single.map((r) => box(r).h),
    rowDouble: double.map((r) => box(r).h),
    rowFs: px(one('.erow-title'), 'fontSize'),
    avatar: box(one('.sidebar .avatar', '.session-list-pane .avatar', '.agents-screen .avatar')),
    act: acts.map((a) => box(a).h),
    actFs: px(acts[0], 'fontSize'),
    iconbtn: icons.map(box),
    chips: all('.chat-head .chip').map(box),
    menuBtn: box(one('.sidebar-menu')),
  };
}
"""


def open_page(context, route: str, wait: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/{route}{'&' if '?' in route else '?'}token=t&scheme=dark&lang=en")
    page.wait_for_selector(wait, timeout=15000)
    page.wait_for_timeout(600)
    return page


def expand_steps(page: Page) -> None:
    steps = page.get_by_text("8 steps")
    if steps.count():
        steps.first.click()
        page.wait_for_timeout(500)


def measure_session(browser, width: int, height: int, mobile: bool) -> dict:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark", is_mobile=mobile, has_touch=mobile)
    context.add_init_script(OPEN_FOLDERS + " try { localStorage.setItem('daedalus.session.aside', '1'); localStorage.setItem('daedalus.session.view', 'chat'); } catch (e) {}")
    page = open_page(context, f"agents/{S1}", ".chat-scroll .timeline")
    expand_steps(page)
    out = page.evaluate(READ)
    out["vw"] = width
    context.close()
    return out


def measure_agents(browser, width: int, height: int, mobile: bool) -> dict:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark", is_mobile=mobile, has_touch=mobile)
    context.add_init_script(OPEN_FOLDERS)
    page = open_page(context, "agents", ".folder")
    out = page.evaluate(READ)
    out["vw"] = width
    context.close()
    return out


def check_sidebar(browser) -> dict:  # type: ignore[no-untyped-def]
    """The collapse and the menu, which are behaviour rather than sizes: the strip, its persistence, the popover's keyboard."""
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(OPEN_FOLDERS)
    page = open_page(context, f"agents/{S1}", ".sidebar")
    out: dict = {}
    out["open"] = page.evaluate("() => Math.round(document.querySelector('.sidebar').getBoundingClientRect().width)")
    page.keyboard.press("Control+\\")
    page.wait_for_timeout(400)
    out["collapsed"] = page.evaluate("() => Math.round(document.querySelector('.sidebar').getBoundingClientRect().width)")
    page.reload()
    page.wait_for_selector(".sidebar", timeout=15000)
    page.wait_for_timeout(400)
    out["collapsedAfterReload"] = page.evaluate("() => Math.round(document.querySelector('.sidebar').getBoundingClientRect().width)")
    page.keyboard.press("Control+\\")
    page.wait_for_timeout(400)
    out["reopened"] = page.evaluate("() => Math.round(document.querySelector('.sidebar').getBoundingClientRect().width)")

    page.locator(".sidebar-menu").click()
    page.wait_for_selector(".navmenu[role='menu']", timeout=5000)
    out["menuItems"] = page.locator(".navmenu [role='menuitem']").count()
    out["menuLang"] = page.locator(".navmenu .lang").count()
    out["menuFocusInside"] = page.evaluate("() => !!document.activeElement && !!document.activeElement.closest('.navmenu')")
    out["menuBox"] = page.evaluate("() => { const r = document.querySelector('.navmenu').getBoundingClientRect(); return { x: Math.round(r.x), bottom: Math.round(innerHeight - r.bottom), w: Math.round(r.width) }; }")
    out["menuRow"] = page.evaluate("() => Math.round(document.querySelector('.navmenu [role=menuitem]').getBoundingClientRect().height)")
    page.keyboard.press("ArrowDown")
    out["arrowMoves"] = page.evaluate("() => document.activeElement === document.querySelectorAll('.navmenu [role=menuitem]')[1]")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    out["menuClosed"] = page.locator(".navmenu").count() == 0
    out["focusBack"] = page.evaluate("() => document.activeElement === document.querySelector('.sidebar-menu')")
    page.keyboard.press("Control+Shift+M")
    page.wait_for_timeout(300)
    out["shortcutOpens"] = page.locator(".navmenu[role='menu']").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    page.keyboard.press("g")
    page.keyboard.press("i")
    page.wait_for_timeout(500)
    out["gKeyNavigates"] = page.evaluate("() => location.pathname").endswith("/inbox")
    context.close()
    return out


def judge(m: dict) -> list[str]:
    problems: list[str] = []
    phone = m["vw"] < 1024
    if m["body"] != 14:
        problems.append(f"{m['vw']}: body is {m['body']}px, not 14")
    if not phone:
        if not m["left"] or m["left"]["w"] != 272:
            problems.append(f"{m['vw']}: the left column is {m['left']}, not 272 wide")
        if m["list"]:
            problems.append(f"{m['vw']}: a second list column is still there ({m['list']})")
    for h in m["rowSingle"]:
        if h > (44 if phone else 36):
            problems.append(f"{m['vw']}: a one-line row is {h}px")
    for h in m["rowDouble"]:
        if h > (60 if phone else 52):
            problems.append(f"{m['vw']}: a two-line row is {h}px")
    if m["avatar"] and m["avatar"]["w"] > 24:
        problems.append(f"{m['vw']}: an avatar in a row is {m['avatar']['w']}px")
    if m["head"] and m["head"]["h"] > 56:
        problems.append(f"{m['vw']}: the chat header is {m['head']['h']}px")
    for h in m["act"]:
        if h > 28:
            problems.append(f"{m['vw']}: a step row is {h}px")
    want = 40 if phone else 32
    for b in m["iconbtn"]:
        if b["w"] != want or b["h"] != want:
            problems.append(f"{m['vw']}: an icon button is {b['w']}×{b['h']}, not {want}")
    for c in m["chips"]:
        if c["h"] > 22:
            problems.append(f"{m['vw']}: a header chip is {c['h']}px")
    if m["answerFs"] != (16 if phone else 15):
        problems.append(f"{m['vw']}: the answer is {m['answerFs']}px")
    if m["timeline"]:
        if m["timeline"]["w"] > 920:
            problems.append(f"{m['vw']}: the timeline is {m['timeline']['w']}px, over the 920 stripe")
        if m["vw"] == 2560 and m["timeline"]["w"] != 920:
            problems.append(f"2560: the timeline is {m['timeline']['w']}px, not the 920 stripe")
        if m["vw"] == 1440 and m["timeline"]["w"] < 800:
            problems.append(f"1440: the timeline is {m['timeline']['w']}px, still narrower than 800")
    return problems


def judge_sidebar(s: dict) -> list[str]:
    problems: list[str] = []
    if s["open"] != 272 or s["collapsed"] != 48 or s["collapsedAfterReload"] != 48 or s["reopened"] != 272:
        problems.append(f"sidebar widths open/collapsed/after reload/reopened: {s['open']}/{s['collapsed']}/{s['collapsedAfterReload']}/{s['reopened']}")
    if s["menuItems"] < 11:
        problems.append(f"the menu has {s['menuItems']} items")
    if not s["menuLang"]:
        problems.append("the menu has no language switch")
    if s["menuBox"]["x"] > 16 or s["menuBox"]["bottom"] > 64 or s["menuBox"]["w"] != 300:
        problems.append(f"the menu is not anchored bottom-left at 300 wide: {s['menuBox']}")
    if s["menuRow"] != 36:
        problems.append(f"a menu row is {s['menuRow']}px")
    for key in ("menuFocusInside", "arrowMoves", "menuClosed", "focusBack", "shortcutOpens", "gKeyNavigates"):
        if not s[key]:
            problems.append(f"{key} is false")
    return problems


def run() -> int:
    problems: list[str] = []
    measured: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for name, (w, h, mobile) in {"session-1440": (1440, 900, False), "session-2560": (2560, 1400, False), "session-390": (390, 844, True)}.items():
            measured[name] = measure_session(browser, w, h, mobile)
        for name, (w, h, mobile) in {"agents-1440": (1440, 900, False), "agents-390": (390, 844, True)}.items():
            measured[name] = measure_agents(browser, w, h, mobile)
        if ASSERT:
            measured["sidebar"] = check_sidebar(browser)
        browser.close()
    for name, m in measured.items():
        print(name, json.dumps(m))
    if ASSERT:
        for name, m in measured.items():
            if name.startswith("session-"):
                problems += judge(m)
        problems += judge_sidebar(measured["sidebar"])
    if MEASURE:
        Path(MEASURE).write_text(json.dumps(measured, indent=1) + "\n")
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
