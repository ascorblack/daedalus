"""The main orchestrator in the app, at 1440 px and on a 390 px phone, in both languages.

What is checked is what the operator relies on. Its entry is pinned above every project in the
sidebar (and in a project's own column), with a pill that counts the questions waiting; on a phone it
is the first thing under the start composer. Its chat shows the questions the projects put to the
operator as cards grouped by project — options as buttons, words of one's own, Allow and Deny — and
answering one posts exactly the answer with the window it came from, after which the card is one line
that says where it was answered. A card that lost to an answer given elsewhere says so. The work
handed out is a card per dispatch with its state and the project's last word, and it can be
cancelled; a project being set up offers "Finish setup". Settings → Models has its own model choice
with the mid-tier preset preselected. Nothing scrolls sideways.
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
    "en": {"title": "Main orchestrator", "pill": "3 questions", "pill2": "2 questions", "answered": "answered in the main chat: Postgres", "conflict": "Already answered elsewhere",
           "stalled": "stalled", "done": "done", "finish": "Finish setup", "confirm": "Create this project?", "host": "answered here only", "placeholder": "Say which project and what to do…",
           "middle": "mid-tier", "cancel": "Cancel", "going": "under way"},
    "ru": {"title": "Главный оркестратор", "pill": "3 вопроса", "pill2": "2 вопроса", "answered": "ответ в главном чате: Postgres", "conflict": "Уже ответили в другом месте",
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

    entry = page.locator(".sidebar .sidebar-pinned .main-entry")
    expect(entry).to_be_visible()
    expect(entry.locator(".main-entry-name")).to_have_text(words["title"])
    expect(entry.locator(".main-pill")).to_have_text(words["pill"])
    entry.click()
    page.wait_for_url("**/app/main")
    expect(page.locator(".chat .chat-title")).to_have_text("Main")
    assert ("/api/main", {}) in main.posts, "the first visit opens the main chat's session"
    expect(entry).to_have_class(re.compile(r"\bcurrent\b"))

    board = page.locator(".main-board")
    expect(board).to_be_visible()
    groups = board.locator(".main-ask-group")
    expect(groups).to_have_count(2)
    expect(groups.nth(0).locator(".main-group-head")).to_have_text("Bakery")
    expect(groups.nth(0).locator(".ask-card")).to_have_count(2)
    confirm = board.locator('.ask-card[data-ask="q3nw00"]')
    expect(confirm).to_have_class(re.compile(r"\bhost\b"))
    expect(confirm.locator(".ask-head")).to_contain_text(words["confirm"])
    expect(confirm.locator(".ask-host")).to_contain_text(words["host"])
    expect(confirm.locator(".ask-own")).to_have_count(0)
    expect(board.locator('.main-answered-line[data-ask="q4ol00"]')).to_be_visible()

    # The dispatches: under way first (the stalled setup, then the menu), the finished one folded.
    cards = board.locator(".main-dispatches").first.locator(".dispatch-card")
    expect(cards).to_have_count(2)
    expect(cards.nth(0).locator(".dispatch-state")).to_have_text(words["stalled"])
    expect(cards.nth(1).locator(".dispatch-last")).to_contain_text("Recipes collected")
    expect(board.locator(".main-setup")).to_contain_text("Garden")

    # The reports it was woken with are a card too.
    expect(page.locator(".event-card")).to_contain_text("Garden closed dispatch dg4h1x")
    expect(page.locator(".composer textarea, .composer [contenteditable]").first).to_have_attribute("placeholder", words["placeholder"])
    fits(page, f"{lang} main chat")

    # Answer with an option: exactly that, from this window.
    board.locator('.ask-card[data-ask="q1db00"] .ask-options button', has_text="Postgres").click()
    expect(board.locator('.main-answered-line[data-ask="q1db00"]')).to_contain_text(words["answered"])
    assert ("/api/asks/ask-db/answer", {"selected": ["Postgres"], "window": "main"}) in main.posts, main.posts
    expect(entry.locator(".main-pill")).to_have_text(words["pill2"])

    # A card that lost to the phone: told so, then shown as answered elsewhere.
    late = board.locator('.ask-card[data-ask="q2lt00"]')
    late.locator(".ask-own .field").fill("Only in summer")
    late.locator(".ask-own button[type=submit]").click()
    expect(page.get_by_text(words["conflict"]).first).to_be_visible()
    expect(board.locator('.main-answered-line[data-ask="q2lt00"]')).to_be_visible()
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
    first = page.locator(".start-list .start-main .main-entry")
    expect(first).to_be_visible()
    expect(first.locator(".main-pill")).to_have_text(WORDS[lang]["pill"])
    fits(page, f"{lang} phone start")
    first.click()
    page.wait_for_url("**/app/main")
    expect(page.locator(".main-board .ask-card").first).to_be_visible()
    expect(page.locator(".tabbar")).to_have_count(0)
    fits(page, f"{lang} phone main chat")
    for button in page.locator(".main-board .ask-options button").all()[:2]:
        box = button.bounding_box()
        assert box and box["height"] >= 32, f"{lang} phone: a card button is {box and box['height']}px tall"


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
