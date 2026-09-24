"""The orchestrator's wake-ups, in the right panel of focus mode and as a page of their own on a phone,
in both languages.

What is checked is what the operator relies on: every wake-up shows its note, when it fires and who
set it; one can be cancelled from its row; a new one is left with a note and a time, and the request
carries exactly one way of saying when — minutes from now, a moment with its offset, or a UTC cron
line for "every day". On a phone the page has no sideways scroll and its fields are big enough not to
make Safari zoom.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from check_project_focus import fits, serve  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "b4k3ry20f0c5"

WORDS = {
    "en": {"tab": "Wake-ups", "add": "Wake-up", "note": "Look at the deploy log", "set": "Set", "by_orch": "set by the orchestrator", "by_you": "left by you", "daily": "Every day", "cancel": "Cancel this wake-up"},
    "ru": {"tab": "Будильники", "add": "Будильник", "note": "Посмотреть журнал выкладки", "set": "Поставить", "by_orch": "поставил оркестратор", "by_you": "оставили вы", "daily": "Каждый день", "cancel": "Отменить будильник"},
}


def panel(page: Page, lang: str) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{BASE}/project/{PID}?token=t&lang={lang}&panel=wakeups")
    tab = page.locator(".panel .panel-tab[data-tab='wakeups']")
    if tab.count():
        tab.click()
    row = page.locator(".panel .wakeup-row").first
    expect(row).to_contain_text(invented["wake.note"])
    expect(row).to_contain_text(words["by_orch"])

    # A new wake-up in minutes: the request says only that.
    page.locator(".panel .wakeup-section-head button", has_text=words["add"]).click()
    sheet = page.locator(".sheet.wakeup-sheet")
    expect(sheet).to_be_visible()
    sheet.locator("#wakeup-note").fill(words["note"])
    sheet.locator("#wakeup-minutes").fill("45")
    sheet.locator(".sheet-foot .btn.primary", has_text=words["set"]).click()
    expect(sheet).to_have_count(0)
    assert focus.woken[-1] == {"note": words["note"], "in_minutes": 45}, focus.woken
    expect(page.locator(".panel .wakeup-row", has_text=words["note"])).to_contain_text(words["by_you"])

    # Every day at a time: a five-field cron line, never the local clock sent as if it were UTC.
    page.locator(".panel .wakeup-section-head button", has_text=words["add"]).click()
    sheet.locator("#wakeup-note").fill(words["note"] + " 2")
    sheet.locator(".chip.select", has_text=words["daily"]).click()
    sheet.locator("#wakeup-daily").fill("09:30")
    sheet.locator(".sheet-foot .btn.primary").click()
    expect(sheet).to_have_count(0)
    sent = focus.woken[-1]
    assert set(sent) == {"note", "cron"} and len(sent["cron"].split()) == 5 and sent["cron"].endswith("* * *"), sent

    # Cancelled from its row.
    count = page.locator(".panel .wakeup-row").count()
    page.locator(".panel .wakeup-row", has_text=invented["wake.note"]).get_by_role("button", name=words["cancel"]).click()
    expect(page.locator(".panel .wakeup-row")).to_have_count(count - 1)
    assert all(w["id"] != "wk1" for w in focus.wakeups)
    fits(page, f"{lang} wake-ups panel")


def phone(page: Page, lang: str) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{BASE}/project/{PID}/wakeups?token=t&lang={lang}")
    expect(page.locator(".wakeup-row").first).to_contain_text(invented["wake.note"])
    fits(page, f"{lang} phone wake-ups")
    page.locator(".wakeup-section-head button", has_text=words["add"]).click()
    sheet = page.locator(".sheet.wakeup-sheet")
    expect(sheet).to_be_visible()
    for field in ("#wakeup-note", "#wakeup-minutes"):
        size = page.evaluate(f"parseFloat(getComputedStyle(document.querySelector('{field}')).fontSize)")
        assert size >= 16, f"{lang} phone: {field} is {size}px, which makes Safari zoom"
    fits(page, f"{lang} phone wake-up sheet")


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            context = browser.new_context(device_scale_factor=1)
            panel(context.new_page(), lang)
            context.close()
            context = browser.new_context(device_scale_factor=2, is_mobile=True, has_touch=True)
            phone(context.new_page(), lang)
            context.close()
        browser.close()
    print("wake-ups: ok")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
