"""The main orchestrator in the app, at 1440 px and on a 390 px phone, in both languages.

What is checked is what the operator relies on. On a desktop its chat is orchestration mode's home,
reached by the rail's Orchestration item; in that mode its entry comes first in the column, with a
pill that counts the questions waiting. On a phone the Orchestration tab opens the list, Main its
first row, and the chat is a detail of it with a back and no tab bar. The chat shows the questions
the projects put to the operator as cards grouped by project, in the conversation's flow after its
latest turn — there is no strip over the chat — with options as buttons, words of one's own, Allow
and Deny; answering one posts exactly the answer with the window it came from, after which the card
leaves the flow. A card that lost to an answer given elsewhere says so. The work under way is a card
per dispatch with its state and the project's last word, and it can be cancelled; a finished
dispatch is no card, only the line of the reports that closed it. A project being set up offers
"Finish setup". Settings → Models has its own model choice with the mid-tier preset preselected.
Nothing scrolls sideways.
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
from api_stub import DEFAULT_APP, MainStub, expect_app  # noqa: E402
from screenshots import SETTINGS, UNHANDLED  # noqa: E402
from screenshots import stub as installation

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"title": "Main orchestrator", "pill": "3 questions", "pill2": "2 questions", "conflict": "Already answered elsewhere",
           "stalled": "stalled", "done": "done", "finish": "Finish setup", "confirm": "Create this project?", "host": "answered here only", "placeholder": "Say which project and what to do…",
           "middle": "mid-tier", "cancel": "Cancel", "going": "under way"},
    "ru": {"title": "Главный оркестратор", "pill": "3 вопроса", "pill2": "2 вопроса", "conflict": "Уже ответили в другом месте",
           "stalled": "застряло", "done": "готово", "finish": "Завершить настройку", "confirm": "Создать этот проект?", "host": "ответить можно только здесь", "placeholder": "Какой проект и что сделать…",
           "middle": "средняя", "cancel": "Отменить", "going": "в работе"},
}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def serve(page: Page, main: MainStub) -> None:
    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = main.answer(request.method, path, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        if path == "/api/settings" and request.method == "GET":
            presets = SETTINGS["presets"]  # type: ignore[index]
            middle = list(presets)[len(presets) // 2] if presets else ""
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({**SETTINGS, "dispatcher": {"preset": "", "middle": middle, "stalled_minutes": 30}}))
        return installation(route)

    page.route("**/api/**", handle)


def desktop(page: Page, lang: str) -> None:
    words = WORDS[lang]
    main = MainStub()
    serve(page, main)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")

    # Agents mode has no entry of its own for the main chat: the rail's Orchestration item leads there.
    expect(page.locator(".sidebar .main-entry")).to_have_count(0)
    page.locator(".rail [data-rail='orchestration']").click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator(".chat .chat-title")).to_have_text("Main")
    assert ("/api/main", {}) in main.posts, "the first visit opens the main chat's session"
    entry = page.locator("nav.orch-sidebar .orch-main .main-entry")
    expect(entry).to_be_visible()
    expect(entry.locator(".main-entry-name")).to_have_text(words["title"])
    expect(entry.locator(".main-pill")).to_have_text(words["pill"])
    expect(entry).to_have_class(re.compile(r"\bcurrent\b"))

    # The cards are in the conversation's flow, after its latest turn; nothing stands over the chat.
    board = page.locator(".chat .timeline > .main-flow")
    expect(board).to_be_visible()
    assert board.evaluate("el => el === el.parentElement.lastElementChild"), "the cards are not after the latest turn"
    expect(page.locator(".main-board, .main-answered, .main-closed")).to_have_count(0)
    groups = board.locator(".main-ask-group")
    expect(groups).to_have_count(2)
    expect(groups.nth(0).locator(".main-group-head")).to_have_text("Bakery")
    expect(groups.nth(0).locator(".ask-card")).to_have_count(2)
    confirm = board.locator('.ask-card[data-ask="q3nw00"]')
    expect(confirm).to_have_class(re.compile(r"\bhost\b"))
    expect(confirm.locator(".ask-head")).to_contain_text(words["confirm"])
    expect(confirm.locator(".ask-host")).to_contain_text(words["host"])
    expect(confirm.locator(".ask-own")).to_have_count(0)
    # A request answered earlier is nowhere: not a card, not a line.
    expect(page.locator('[data-ask="q4ol00"]')).to_have_count(0)

    # The dispatches under way (the stalled setup, then the menu); the finished one is no card, only
    # the line of the reports that closed it.
    cards = board.locator(".main-dispatches .dispatch-card")
    expect(cards).to_have_count(2)
    expect(board.locator('.dispatch-card[data-dispatch="dg4h1x"]')).to_have_count(0)
    expect(cards.nth(0).locator(".dispatch-state")).to_have_text(words["stalled"])
    expect(cards.nth(1).locator(".dispatch-last")).to_contain_text("Recipes collected")
    expect(board.locator(".main-setup")).to_contain_text("Garden")

    # The reports it was woken with are a card too.
    expect(page.locator(".event-card")).to_contain_text("Garden closed dispatch dg4h1x")
    expect(page.locator(".composer textarea, .composer [contenteditable]").first).to_have_attribute("placeholder", words["placeholder"])
    fits(page, f"{lang} main chat")

    # Answer with an option: exactly that, from this window; the card then leaves the flow.
    board.locator('.ask-card[data-ask="q1db00"] .ask-options button', has_text="Postgres").click()
    expect(page.locator('[data-ask="q1db00"]')).to_have_count(0)
    assert ("/api/asks/ask-db/answer", {"selected": ["Postgres"], "window": "main"}) in main.posts, main.posts
    expect(entry.locator(".main-pill")).to_have_text(words["pill2"])

    # A card that lost to the phone: told so, then gone, the answer given elsewhere standing.
    late = board.locator('.ask-card[data-ask="q2lt00"]')
    late.locator(".ask-own .field").fill("Only in summer")
    late.locator(".ask-own button[type=submit]").click()
    expect(page.get_by_text(words["conflict"]).first).to_be_visible()
    expect(page.locator('[data-ask="q2lt00"]')).to_have_count(0)
    assert ("/api/asks/ask-late/answer", {"text": "Only in summer", "window": "main"}) in main.posts, main.posts

    # Finish the setup by hand, and cancel a dispatch after confirming.
    board.locator(".main-setup button").click()
    expect(board.locator(".main-setup")).to_have_count(0)
    assert ("/api/projects/p-garden/setup/finish", {}) in main.posts
    board.locator('.dispatch-card[data-dispatch="d7k2m9"] .dispatch-actions button').click()
    page.locator(".sheet-backdrop.confirm .dialog button").last.click()
    page.wait_for_timeout(300)
    if not any(p[0] == "/api/dispatches/d7k2m9/cancel" for p in main.posts):
        # The danger button is the first when the safe one takes focus last.
        page.locator(".sheet-backdrop.confirm .dialog button", has_text=words["cancel"]).click()
    page.wait_for_timeout(300)
    assert any(p[0] == "/api/dispatches/d7k2m9/cancel" for p in main.posts), main.posts

    # Settings → Models: the main orchestrator's own choice, preselected with the mid-tier preset.
    page.goto(f"{BASE}/settings/models?token=t&lang={lang}")
    card = page.locator('[data-card="main-orchestrator"]')
    expect(card).to_be_visible()
    selected = card.locator("select option:checked")
    expect(selected).to_contain_text(words["middle"])


def phone(page: Page, lang: str) -> None:
    main = MainStub()
    serve(page, main)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    expect(page.locator(".start-list .main-entry")).to_have_count(0)
    fits(page, f"{lang} phone start")
    # The tab opens orchestration's list, not the main chat: Main is its first row, with the pill.
    page.locator(".tabbar a[data-screen='orchestration']").click()
    page.wait_for_url("**/app/orchestration/projects**")
    assert ("/api/main", {}) not in main.posts, "the list opened the main chat's session"
    first = page.locator(".orch-list > :first-child .main-entry")
    expect(first).to_be_visible()
    expect(first.locator(".main-pill")).to_have_text(WORDS[lang]["pill"])
    # The chat is a detail of the list: a back of its own, no tab bar, the cards in its flow.
    first.click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator(".timeline > .main-flow .ask-card").first).to_be_visible()
    expect(page.locator("nav.tabbar")).to_have_count(0)
    expect(page.locator(".main-board")).to_have_count(0)
    fits(page, f"{lang} phone main chat")
    for button in page.locator(".main-flow .ask-options button").all()[:2]:
        box = button.bounding_box()
        assert box and box["height"] >= 32, f"{lang} phone: a card button is {box and box['height']}px tall"
    page.locator(".chat-head > button.iconbtn").first.click()
    page.wait_for_url("**/app/orchestration/projects**")
    expect(page.locator(".orch-list .main-entry")).to_be_visible()


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
            desktop(page, lang)
            page.context.close()
            small = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True).new_page()
            phone(small, lang)
            small.context.close()
            print(f"main orchestrator {lang}: ok")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
