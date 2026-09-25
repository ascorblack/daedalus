"""A run that ended without an answer says so as the closing line of its turn.

The case that asked for it: a loop iteration worked for 27 minutes, wrote a file and died on a
compaction that could not make progress. The chat showed the work, the file, and then the host's
"Context summary" card — and nothing said the run had failed or that there was no answer. The line
must be inside the turn, above that card, and say why the run stopped and where it gave out.
"""
from __future__ import annotations

import os

from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message, stub
from playwright.sync_api import expect, sync_playwright

OUTCOME = {
    "status": "failed",
    "cause": "context",
    "error_kind": "llm_context_window_exceeded",
    "detail": "reactive force_compaction exhausted retries",
    "steps": 176,
    "last_tool": "Write",
    "answered": False,
    "compaction": {"outcome": "at_floor", "summariser_failures": {"transport": 12}, "floor_dropped": 0},
}


def _conversation() -> list[dict]:
    return [
        message(201, "user", "[Loop iteration 189 — every 1h.] Continue the board work.", run_id="tick-189", origin="loop", internal=False),
        message(202, "assistant", "", run_id="tick-189", tool_calls=[{"id": "w1", "name": "Write", "arguments": {"path": "notes/board.md", "content": "…"}}]),
        message(203, "tool", "", run_id="tick-189", tool_results=[{"id": "w1", "content": "written", "is_error": False}]),
        message(204, "system", "The run ended without an answer.", run_id="tick-189", outcome=OUTCOME),
        message(205, "user", "## Goal\nKeep the board current.", summary=True, compaction={"reason": "auto", "messages": 304}),
    ]


def main() -> None:
    HOST.status = "idle"
    HOST.messages = _conversation()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang, title, cause in (
            ("en", "No answer — the run stopped", "compaction could not make it fit"),
            ("ru", "Ответа нет — запуск остановился", "сжатие не смогло его уместить"),
        ):
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents/{SESSION}?token=t&lang={lang}")
            line = page.locator(".run-outcome")
            expect(line).to_have_count(1)
            expect(line).to_contain_text(title)
            expect(line).to_contain_text(cause)
            expect(line).to_contain_text("reactive force_compaction exhausted retries")
            expect(line).to_contain_text("at_floor")
            expect(line).to_contain_text("transport 12")
            expect(line).to_contain_text("176")
            expect(page.locator(".summary")).to_have_count(1)
            # Inside the turn, and above the host's summary card.
            above = page.evaluate(
                """() => {
                  const line = document.querySelector('.run-outcome');
                  const card = document.querySelector('.summary');
                  return !!(line.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING) && !!line.closest('.turn');
                }"""
            )
            assert above, "the closing line must sit inside its turn, above the context summary"
            if os.environ.get("SHOTS"):
                page.screenshot(path=f"{os.environ['SHOTS']}/run-outcome-{lang}.png")
            page.close()
        browser.close()
    assert not UNHANDLED.report()


if __name__ == "__main__":
    main()
