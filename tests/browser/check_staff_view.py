"""A command-line staff member's own view (M5) on a desktop and on a phone, and refuse what does not behave.

On a desktop at 1440 px: the header says who, what runs them (CLI and version, model, mode), the
worktree, the task, the status with the turn and its minutes, and whether the host still hears them;
the terminal is the default view and the Feed is a toggle away, with the tool uses folded; the strip
above the composer shows three messages in three states and moves one on its `staff.message` event;
the keyboard banner appears while a person holds the terminal's keyboard and an orchestrator's
message waits, and Release gives the keyboard back; the permission waiting on the operator is answered
"Always" with the exact body, and an answer someone else gave first is named; the composer sends "now"
as a steer; a CLI that cannot take a message into a running turn (OpenCode) offers "now" disabled and
says why; the column beside it has the session's events, the changes and the notes; the sidebar and a
Terminals-screen card lead here.

On a phone at 390 px: the Feed is the default, the request's buttons are at least 44 px and above the
composer, nothing scrolls sideways, and "Terminal" opens the phone's terminal with the request (and
"Always") above its keys. The Russian page is checked on the same points with its own words.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_staff_view.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import IRA_SCREEN, S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
DESK = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}

WORDS = {
    "en": {"working": "working", "turn": r"turn 3 · (18|19|20) min", "runs": "Claude Code 2.1.281 · opus · acceptEdits", "worktree": "worktree agent/ira/checkout", "task": "Checkout",
           "tools": "team tools connected", "missing": "team tools not connected", "held": "You are typing in the terminal", "always": "Always", "allow": "Allow", "deny": "Deny", "no": "No, because…",
           "by": "Already answered by the orchestrator", "degrades": "OpenCode cannot take a message into a running turn", "changes": "+212 −31 · 3 files", "placeholder": "Write to Ira…",
           "permission": "needs permission", "answer": "Answer", "feed": "Feed", "terminal": "Terminal"},
    "ru": {"working": "работает", "turn": r"ход 3 · (18|19|20) мин", "runs": "Claude Code 2.1.281 · opus · acceptEdits", "worktree": "worktree agent/ira/checkout", "task": "Оформление заказа",
           "tools": "инструменты команды подключены", "missing": "инструменты команды не подключены", "held": "Вы печатаете в терминале", "always": "Всегда", "allow": "Разрешить", "deny": "Запретить", "no": "Нет, потому что…",
           "by": "Уже ответил: оркестратор", "degrades": "OpenCode не принимает сообщения во время хода", "changes": "+212 −31 · 3 файла", "placeholder": "Написать сотруднику Ira…",
           "permission": "ждёт разрешения", "answer": "Ответить", "feed": "Лента", "terminal": "Терминал"},
}


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def that(self, ok: bool, problem: str) -> None:
        if not ok:
            self.problems.append(problem)


def sideways(page: Page) -> int:
    return int(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"))


def stand(lang: str):  # type: ignore[no-untyped-def]
    focus = FocusStub.bakery(lang)
    focus.staff_view_of_ira(lang)
    pid = focus.projects[0]["id"]
    term = TerminalStub(S1)
    term.add("tm-ira", title="claude · Ira", owner_kind="staff", owner_id="st-ira", project_id=pid, owner_label="Ira", cwd="/home/operator/work/bakery-site",
             activity={"status": "permission", "label": "permission [k7m2qd]: Bash", "level": "warn", "action": {"kind": "answer", "label": "Answer", "path": f"/app/project/{pid}/staff/st-ira"}})
    term.emit("tm-ira", IRA_SCREEN)
    term.add("tm-naya", title="opencode · Naya", owner_kind="staff", owner_id="st-naya", project_id=pid, owner_label="Naya", env="container")
    return focus, term, pid


def open_page(context, focus: FocusStub, term: TerminalStub, feed: EventFeed, url: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path[parts.path.index("/api/"):]
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = focus.answer(request.method, path, parts.query, body)
        if answered is not None:
            return route.fulfill(status=answered[0], content_type="application/json", body=json.dumps(answered[1]))
        return stub(route)

    page.route("**/api/**", handle)
    term.install(page)
    page.route("**/api/events**", feed.route)
    page.goto(url)
    return page


def desktop(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    invented = FOCUS_WORDS[lang]
    focus, term, pid = stand(lang)
    feed = EventFeed()
    context = browser.new_context(viewport=DESK, color_scheme="dark")
    context.add_init_script(DEBUG)
    page = open_page(context, focus, term, feed, f"{BASE}/project/{pid}/staff/st-ira?token=t&lang={lang}")
    page.wait_for_selector(".staff-cli .staff-head", timeout=15000)

    # The header: who, what runs them, where, on what, how it goes, and whether the host hears them.
    head = page.locator(".staff-cli .staff-head")
    expect(head.locator(".staff-head-name")).to_contain_text("Ira")
    expect(head.locator(".staff-head-runs")).to_have_text(words["runs"])
    meta = head.locator(".staff-head-meta").inner_text()
    check.that(words["worktree"] in meta and words["task"] in meta, f"{lang}: the header's second line is {meta!r}")
    expect(head.locator(".focus-pill")).to_contain_text(words["working"], timeout=5000)
    pill = head.locator(".focus-pill").inner_text()
    check.that(re.search(words["turn"], pill) is not None, f"{lang}: the status pill says {pill!r}, not the turn and its minutes")
    health = head.locator(".staff-health")
    check.that(health.get_attribute("data-tools") == "connected" and words["tools"] in health.inner_text(), f"{lang}: the health line is {health.inner_text()!r}")
    check.that(head.locator(".staff-head-actions .btn").count() == 3, f"{lang}: the header has {head.locator('.staff-head-actions .btn').count()} controls, not Interrupt · Pause · Release")

    # The terminal first on a desktop; the Feed a toggle away, its tool uses folded.
    page.wait_for_selector(".staff-term .term-view[data-terminal-view='tm-ira']", timeout=10000)
    wait_live(page, "tm-ira")
    check.that(page.locator(".staff-cli").get_attribute("data-mode") == "terminal", f"{lang}: a desktop opened on {page.locator('.staff-cli').get_attribute('data-mode')}")
    page.locator(".staff-mode button[data-mode='feed']").click()
    page.wait_for_selector(".feed-turn", timeout=5000)
    roles = page.eval_on_selector_all(".feed-turn", "els => els.map(e => e.dataset.role)")
    check.that(roles == ["orchestrator", "assistant", "orchestrator", "assistant", "user", "assistant"], f"{lang}: the Feed's turns are {roles}")
    check.that(page.locator(".feed-tool").count() == 0, f"{lang}: the tool uses are not folded")
    page.locator(".feed-turn[data-turn='1'] .feed-tools-head").click()
    check.that(page.locator(".feed-turn[data-turn='1'] .feed-tool").count() == 3, f"{lang}: the first reply's three tool uses do not unfold")
    check.that(invented["ira.accepted"] in page.locator(".feed-turn[data-turn='2']").inner_text(), f"{lang}: the orchestrator's message is not in the Feed")
    page.locator(".staff-mode button[data-mode='terminal']").click()
    page.wait_for_selector(".staff-term .term-view[data-terminal-view='tm-ira']", timeout=5000)

    # The strip: three messages in three states, newest last; one moves on its event.
    states = page.eval_on_selector_all(".staff-foot .staff-message", "els => els.map(e => e.dataset.state)")
    check.that(states == ["acknowledged", "submitted", "queued"], f"{lang}: the strip's receipts are {states}")
    check.that(feed.connected() >= 1, f"{lang}: the app did not open the event stream")
    next(m for m in focus.messages["st-ira"] if m["id"] == "mi2")["state"] = "acknowledged"
    feed.send("staff.message", {"message_id": "mi2", "state": "acknowledged"}, project=pid, staff="st-ira")
    expect(page.locator(".staff-foot .staff-message[data-message='mi2']")).to_have_attribute("data-state", "acknowledged", timeout=4000)

    # A person holds the keyboard while the orchestrator's message waits: the banner, and Release.
    check.that(page.locator(".staff-keyboard").count() == 0, f"{lang}: the keyboard banner shows before anyone typed")
    term.hold_keyboard("tm-ira", "human")
    expect(page.locator(".staff-keyboard")).to_contain_text(words["held"], timeout=4000)
    page.locator(".staff-keyboard .btn").click()
    expect(page.locator(".staff-keyboard")).to_have_count(0, timeout=4000)
    released = [(m, p, b) for m, p, b in term.requests if p.endswith("/keyboard")]
    check.that(released == [("POST", "/api/terminals/tm-ira/keyboard", {"owner": "auto"})], f"{lang}: Release sent {released}")

    # The permission: Allow · Always · Deny · No, because…; Always answered with the exact body.
    bar = page.locator(".staff-request[data-ask='k7m2qd']")
    expect(bar).to_be_visible()
    labels = [b.strip() for b in bar.locator(".ask-answers-row .btn").all_inner_texts()]
    check.that(labels == [words["allow"], words["always"], words["deny"], words["no"]], f"{lang}: the permission offers {labels}")
    bar.locator(".btn[data-answer='always']").click()
    expect(bar).to_have_count(0, timeout=5000)
    check.that(focus.answers[-1] == ("ask-stripe", {"allow": True, "always": True}), f"{lang}: Always posted {focus.answers[-1:]}")
    # A request the orchestrator answered first: the refusal names it.
    late = {**focus.asks[0], "id": "ask-late", "short_id": "p3v8nd", "text": "Bash: rm -rf dist", "resolved_at": None, "resolution": {}}
    focus.asks.insert(0, late)
    feed.send("permission.pending", {"request_id": "p3v8nd"}, project=pid, staff="st-ira")
    late_bar = page.locator(".staff-request[data-ask='p3v8nd']")
    expect(late_bar).to_be_visible(timeout=5000)
    late.update(resolved_at="2026-09-25T10:00:00Z", resolved_by="orchestrator")
    late_bar.locator(".ask-answers-row .btn").first.click()
    expect(page.locator(".toast")).to_contain_text(words["by"], timeout=4000)

    # The composer: "now" is a steer.
    field = page.locator(".staff-compose-field")
    check.that(field.get_attribute("placeholder") == words["placeholder"], f"{lang}: the composer says {field.get_attribute('placeholder')!r}")
    field.fill("use the owner's sheet")
    page.locator(".staff-when button[data-when='steer']").click()
    page.locator(".staff-compose-send").click()
    page.wait_for_timeout(600)
    check.that(focus.sent == [("st-ira", {"text": "use the owner's sheet", "mode": "steer"})], f"{lang}: the composer sent {focus.sent}")
    check.that(len(term.inputs("tm-ira")) == 0, f"{lang}: the message was typed into the program")

    # The column beside: events, changes, notes.
    side = page.locator(".staff-aside")
    expect(side.locator(".staff-event")).to_have_count(7, timeout=5000)
    side.locator(".panel-tab[data-tab='changes']").click()
    expect(side.locator(".staff-changes-summary")).to_have_text(words["changes"], timeout=5000)
    check.that(side.locator(".staff-file").count() == 4, f"{lang}: the changes list {side.locator('.staff-file').count()} files, not three and one new")
    side.locator(".panel-tab[data-tab='notes']").click()
    expect(side.locator(".staff-notes")).to_be_visible()

    # OpenCode cannot take a message into a running turn: "now" is offered disabled, and says why.
    page.locator(".focus-staff", has_text="Naya").click()
    page.wait_for_url(f"**/project/{pid}/staff/st-naya**")
    now = page.locator(".staff-when button[data-when='steer']")
    expect(now).to_be_disabled(timeout=5000)
    check.that(words["degrades"] in (now.get_attribute("title") or ""), f"{lang}: the disabled 'now' says {now.get_attribute('title')!r}")
    naya = page.locator(".staff-head .staff-health")
    check.that(naya.get_attribute("data-level") == "warn" and words["missing"] in naya.inner_text(), f"{lang}: Naya's health line is {naya.inner_text()!r}")
    # The sidebar row leads to Ira's view, marked as the current one.
    page.locator(".focus-staff", has_text="Ira").click()
    page.wait_for_url(f"**/project/{pid}/staff/st-ira**")
    expect(page.locator(".focus-staff.current")).to_contain_text("Ira")

    # A staff member's card on the Terminals screen says what it is doing, and Answer leads here.
    page.goto(f"{BASE}/terminals?lang={lang}")
    card = page.locator(".term-card[data-terminal='tm-ira']")
    expect(card.locator(".term-card-status")).to_contain_text(words["permission"], timeout=10000)
    card.locator(".term-card-foot .btn", has_text=words["answer"]).click()
    page.wait_for_url(f"**/project/{pid}/staff/st-ira**")
    check.that(sideways(page) <= 0, f"{lang}: the desktop scrolls sideways")
    context.close()
    feed.close()


def phone(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    focus, term, pid = stand(lang)
    feed = EventFeed()
    context = browser.new_context(viewport=PHONE, color_scheme="dark", is_mobile=True, has_touch=True)
    context.add_init_script(DEBUG)
    page = open_page(context, focus, term, feed, f"{BASE}/project/{pid}/staff/st-ira?token=t&lang={lang}")
    page.wait_for_selector(".staff-cli .feed-turn", timeout=15000)
    check.that(page.locator(".staff-cli").get_attribute("data-mode") == "feed", f"{lang} phone: opened on {page.locator('.staff-cli').get_attribute('data-mode')}, not the Feed")
    check.that(page.locator(".staff-term").count() == 0, f"{lang} phone: a terminal is drawn under the Feed")
    buttons = page.locator(".staff-request .ask-answers-row .btn")
    expect(buttons).to_have_count(4, timeout=5000)
    heights = [b["height"] for b in (buttons.nth(i).bounding_box() for i in range(buttons.count())) if b]
    check.that(min(heights) >= 44, f"{lang} phone: the request's buttons are {heights} px high")
    request = page.locator(".staff-request").bounding_box()
    composer = page.locator(".staff-compose").bounding_box()
    check.that(bool(request and composer and request["y"] + request["height"] <= composer["y"] + 0.5), f"{lang} phone: the request is not above the composer")
    check.that(sideways(page) <= 0, f"{lang} phone: the staff view scrolls sideways by {sideways(page)} px")
    # The column beside is a sheet on a phone.
    page.locator(".staff-cli .chat-head .head-actions .iconbtn").tap()
    expect(page.locator(".sheet .staff-panel")).to_be_visible(timeout=5000)
    page.keyboard.press("Escape")
    expect(page.locator(".sheet .staff-panel")).to_have_count(0, timeout=3000)
    # Terminal: the phone's own, with the request and "Always" above the keys.
    page.locator(".staff-mode button[data-mode='terminal']").tap()
    page.wait_for_url("**/terminals/tm-ira**")
    page.wait_for_selector(".term-phone-actions .ask-answers-row .btn", timeout=10000)
    above = [b.strip() for b in page.locator(".term-phone-actions .ask-answers-row .btn").all_inner_texts()]
    check.that(words["always"] in above, f"{lang} phone: the terminal's request offers {above}")
    context.close()
    feed.close()


def run() -> int:
    expect_app(BASE)
    check = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            desktop(browser, lang, check)
            phone(browser, lang, check)
        browser.close()
    unhandled = UNHANDLED.report()
    for problem in check.problems:
        print("FAIL", problem)
    if not check.problems and not unhandled:
        print("the staff view holds")
    return 1 if check.problems or unhandled else 0


if __name__ == "__main__":
    sys.exit(run())
