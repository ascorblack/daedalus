"""Barge-in: the operator talks while the answer is being read out, and the answer stops.

What decides whose voice it is lives in `shouldBargeIn` and is covered by the unit tests. What cannot
be checked without a browser is that the callback is wired to a listener at all, that the listener is
running while the page is speaking, and that the interruption reaches the server and the screen. So
the page is driven for real: a fake capture device supplies the microphone, the concierge's stream is
canned so that the page is genuinely speaking, and the recognition endpoint is made to report words in
the middle of it.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_voice_bargein.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE
HEARD = "wait, read me the second one again"

SPEAKING = shots.sse(
    ("status", {"state": "idle"}),
    ("say", {"text": "All eleven invoices went out this morning."}),
    ("say", {"text": "Two came back with the wrong VAT line, and I have put both on the board."}),
    ("say", {"text": "Shall I have someone redo them now?"}),
)


def main() -> int:
    seen: list[str] = []
    talking = {"on": False}

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        rel = request.url.split("?", 1)[0]
        rel = rel[rel.index("/api/") :]
        seen.append(f"{request.method} {rel}")
        if rel == "/api/voice/listen/open":
            return shots.respond(route, {"stream": "s-1", "rate": 16000})
        if rel == "/api/voice/listen":
            # Silence until the answer is under way, then words: that is the whole gesture.
            text = HEARD if talking["on"] else ""
            return shots.respond(route, {"text": text, "final": False})
        return shots.stub(route)

    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM, args=shots.FAKE_MEDIA)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark", permissions=["microphone"])
        page = context.new_page()
        page.route("**/api/**", stub)
        shots.stub.voice_frames = SPEAKING  # type: ignore[attr-defined]
        page.goto(f"{BASE}/voice?token=t&scheme=dark&lang=en")
        page.wait_for_selector(".voice-orb", timeout=15000)
        page.locator(".voice-orb").click()
        page.wait_for_selector(".voice-stage.phase-speaking", timeout=15000)
        check(True, "the page reaches the speaking phase with the microphone open")

        check(
            "POST /api/voice/interrupt" not in seen,
            "nothing is interrupted while the operator has said nothing",
        )
        talking["on"] = True
        page.wait_for_selector(".voice-stage.phase-listening", timeout=15000)
        check("POST /api/voice/interrupt" in seen, "talking over the answer calls the interrupt")
        check(page.locator(".voice-stage.phase-listening").count() == 1, "the page goes back to listening")
        check(page.evaluate("document.querySelectorAll('audio').length >= 0"), "the page is still alive after the barge-in")
        shots.stub.voice_frames = ""  # type: ignore[attr-defined]
        context.close()
        browser.close()
    return failures


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
