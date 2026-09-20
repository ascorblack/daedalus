"""A new stream cannot turn the preceding run's final answer into an activity note."""
from __future__ import annotations

import json
import os

from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message, stub
from playwright.sync_api import expect, sync_playwright


def main() -> None:
    HOST.status = "idle"
    HOST.messages = [message(101, "user", "First question", run_id="old"), message(102, "assistant", "First final answer", run_id="old")]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.add_init_script("""const originalFetch = window.fetch;
          window.fetch = (input, init) => String(input).includes('/stream')
            ? Promise.resolve(new Response(new ReadableStream({start(controller) {
                window.sendEvent = (name, payload) => controller.enqueue(new TextEncoder().encode(
                  'event: ' + name + '\\ndata: ' + JSON.stringify(payload) + '\\n\\n'));
              }}), {headers: {'Content-Type': 'text/event-stream'}}))
            : originalFetch(input, init);""")
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents/{SESSION}?token=t&lang=en")
        expect(page.locator(".answer")).to_have_text("First final answer")
        page.wait_for_function("typeof window.sendEvent === 'function'")

        def event(name: str, payload: dict) -> None:
            page.evaluate(f"window.sendEvent({json.dumps(name)}, {json.dumps({'run_id': 'r1', **payload})})")

        HOST.status = "running"
        event("message_start", {})
        event("content_block_delta", {"delta": {"type": "text_delta", "text": "Second final answer"}})
        expect(page.locator(".answer")).to_have_count(2)
        expect(page.locator(".answer").first).to_have_text("First final answer")
        expect(page.locator(".answer").last).to_have_text("Second final answer")
        event("run_settled", {"run_id": "old", "status": "completed", "housekeeping": False})
        page.wait_for_timeout(250)
        expect(page.locator(".answer.streaming")).to_have_text("Second final answer")
        HOST.messages += [message(103, "user", "Second question", run_id="r1"), message(104, "assistant", "Second final answer", run_id="r1")]
        event("message_stop", {"stop_reason": "end_turn"})
        HOST.status = "idle"
        event("run_settled", {"status": "completed"})
        expect(page.locator(".msg.user")).to_have_count(2)
        expect(page.locator(".answer")).to_have_count(2)
        expect(page.locator(".answer").first).to_have_text("First final answer")
        expect(page.locator(".answer").last).to_have_text("Second final answer")
        if os.environ.get("SHOTS"):
            page.screenshot(path=f"{os.environ['SHOTS']}/run-boundaries.png")
        browser.close()
    assert not UNHANDLED.report()


if __name__ == "__main__":
    main()
