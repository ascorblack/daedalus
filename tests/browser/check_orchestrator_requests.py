"""Every request an orchestrator puts to the operator is a card in its own chat, a folder as well as a
question, at 1440 and at 390 px, in both languages.

The case this guards: the orchestrator asked for a host folder with ``Folders(op=add)``. The chat drew
cards only for ``AskOperator``, and a notification is not raised as a toast over the chat it
concerns, so the operator watching that chat saw the request nowhere but the Notifications screen.
Here a turn asks for two folders; each is a card with the request's own options and no field for
words (a folder is added or it is not), the first option posts exactly the option, and an approval
the host takes but cannot carry out says why on the card and in a toast. Nothing scrolls sideways.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, expect_app  # noqa: E402
from check_project_focus import PID, fits, serve  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

LABS, SHOP = "/home/operator/labs", "/home/operator/shop"
REFUSED = f"Bakery 2.0 already has the folder {SHOP}"
WORDS = {
    "en": {"head": "The orchestrator wants a folder", "answered": "answered in the project's chat: Add", "failed": f"Approved, but it failed: {REFUSED}", "notdone": f"Not carried out: {REFUSED}"},
    "ru": {"head": "Оркестратор просит папку", "answered": "ответ в чате проекта: Add", "failed": f"Одобрено, но не выполнено: {REFUSED}", "notdone": f"Не выполнено: {REFUSED}"},
}


def folder_request(short: str, path: str, also: list[str]) -> dict:
    text = f"Add the host folder {path} to Bakery 2.0?" + (f" It is also in the project {', '.join(also)}." if also else "")
    return {
        "id": f"ask-{short}", "short_id": short, "project_id": PID, "origin": "orchestrator", "kind": "folder", "staff_id": None, "staff_session_id": None, "task_id": None,
        "request_ref": "", "text": text, "detail": {"path": path, "env": "host", "readonly": False, "also_in": also, "options": ["Add", "Don't add"]},
        "routed_to": "operator", "suggestion": "", "created_at": "2026-09-24T09:58:00Z", "routed_at": "2026-09-24T09:58:00Z", "resolved_at": None, "resolved_by": None, "resolution": {},
    }


def with_folder_requests(lang: str) -> FocusStub:
    """Bakery, whose orchestrator asked in its last turn for two host folders, one of them already
    in another project, the other one the host will refuse once it is approved."""
    focus = FocusStub.bakery(lang)
    focus.asks += [folder_request("qf0ld3", LABS, ["Labs"]), folder_request("qsh0p5", SHOP, [])]
    focus.refusals["ask-qsh0p5"] = REFUSED
    messages = focus.details["orch-bakery"]["messages"]
    seq = messages[-1]["seq"]
    asked = "a host folder needs the operator's confirmation; asked as [{}]. The answer arrives as an event."
    messages += [
        {"role": "assistant", "seq": seq + 1, "text": "", "thinking": "", "tool_calls": [
            {"id": "f1", "name": "Folders", "arguments": {"op": "add", "path": LABS, "env": "host", "label": "labs"}},
            {"id": "f2", "name": "Folders", "arguments": {"op": "add", "path": SHOP, "env": "host"}},
        ], "tool_results": [], "created_at": "2026-09-24T09:58:00Z"},
        {"role": "tool", "seq": seq + 2, "text": "", "thinking": "", "tool_calls": [], "tool_results": [
            {"id": "f1", "content": asked.format("qf0ld3"), "is_error": False},
            {"id": "f2", "content": asked.format("qsh0p5"), "is_error": False},
        ], "created_at": "2026-09-24T09:58:01Z"},
        {"role": "assistant", "seq": seq + 3, "text": "Asked for the two folders.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T09:58:05Z"},
    ]
    return focus


def check(page: Page, lang: str, where: str) -> None:
    words = WORDS[lang]
    focus = with_folder_requests(lang)
    serve(page, focus)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    chat = page.locator(".chat.in-project.orchestrator")
    expect(chat).to_be_visible()
    # The question the orchestrator asked earlier is still a card, and so is each folder.
    expect(chat.locator(".ask-card[data-ask='q4r8tz']")).to_be_visible()
    labs = chat.locator(".ask-card[data-ask='qf0ld3']")
    shop = chat.locator(".ask-card[data-ask='qsh0p5']")
    expect(labs).to_be_visible()
    expect(shop).to_be_visible()
    expect(chat.locator(".ask-card")).to_have_count(3)
    expect(labs.locator(".ask-head")).to_contain_text(words["head"])
    expect(labs.locator(".ask-text")).to_contain_text("It is also in the project Labs.")
    expect(labs.locator(".ask-options .btn")).to_have_text(["Add", "Don't add"])
    expect(labs.locator(".ask-own")).to_have_count(0)
    fits(page, f"{lang} {where} folder cards")

    labs.get_by_role("button", name="Add", exact=True).click()
    expect(labs.locator(".ask-answer")).to_have_text(words["answered"])
    expect(labs.locator(".ask-failed")).to_have_count(0)
    assert focus.answers == [("ask-qf0ld3", {"selected": ["Add"], "window": "project"})], focus.answers

    shop.get_by_role("button", name="Add", exact=True).click()
    expect(shop.locator(".ask-failed")).to_have_text(words["notdone"])
    expect(page.get_by_text(words["failed"])).to_be_visible()
    assert focus.answers[-1] == ("ask-qsh0p5", {"selected": ["Add"], "window": "project"}), focus.answers
    fits(page, f"{lang} {where} after the answers")

    # Read again from the host, the failed approval still says why.
    page.reload()
    expect(chat.locator(".ask-card[data-ask='qsh0p5'] .ask-failed")).to_have_text(words["notdone"])


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            check(context.new_page(), lang, "1440")
            context.close()
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            check(context.new_page(), lang, "390")
            context.close()
        browser.close()
    print("orchestrator requests: ok")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
