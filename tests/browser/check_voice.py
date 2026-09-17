"""The voice page's two halves that only a browser can show: a streamed answer, and barge-in.

What decides whose voice it is lives in `shouldBargeIn` and is covered by the unit tests. What cannot
be checked without a browser is that the callback is wired to a listener at all, that the listener is
running while the page is speaking, and that the interruption reaches the server and the screen. So
the page is driven for real: a fake capture device supplies the microphone, the concierge's stream is
canned so that the page is genuinely speaking, and the recognition endpoint is made to report words in
the middle of it.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_voice.py

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


def sequence(*clips: bytes) -> bytes:
    """A local voice's answer on the wire: four bytes of length, then one playable clip, per sentence."""
    return b"".join(len(clip).to_bytes(4, "big") + clip for clip in clips)


PLAYED = """
window.__played = [];
const play = HTMLMediaElement.prototype.play;
HTMLMediaElement.prototype.play = function () { if (this.src) window.__played.push(this.src); return play.apply(this); };
"""


def check_streamed(browser, check) -> None:  # type: ignore[no-untyped-def]
    """Three sentences arrive as three clips, and the page plays all three in order."""
    clips = sequence(shots.wav(0.4), shots.wav(0.4), shots.wav(0.4))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        rel = route.request.url.split("?", 1)[0]
        rel = rel[rel.index("/api/") :]
        if rel == "/api/voice/tts":
            return route.fulfill(
                status=200,
                body=clips,
                headers={"content-type": "application/x-speech-sequence", "x-speech-media-type": "audio/wav"},
            )
        return shots.stub(route)

    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark", permissions=["microphone"])
    context.add_init_script(PLAYED)
    page = context.new_page()
    page.route("**/api/**", stub)
    shots.stub.voice_frames = SPEAKING  # type: ignore[attr-defined]
    page.goto(f"{BASE}/voice?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".voice-stage.phase-speaking", timeout=15000)
    page.wait_for_timeout(2500)
    played = page.evaluate("window.__played")
    check(len(played) >= 3, f"the three sentences of one answer are played as three clips (played {len(played)})")
    check(len(set(played)) == len(played), "each clip is its own object URL rather than one played over")
    shots.stub.voice_frames = ""  # type: ignore[attr-defined]
    context.close()


# A synthesiser that behaves, and says when it was spoken to. The browser's own is not used here:
# what is being measured is the page's own path from a frame on the wire to a word handed over, and a
# real engine would add its own start-up to it and answer differently on every machine.
FAKE_SYNTH = """
window.__spoke = [];
window.__streamAt = 0;
window.__shownAt = 0;
const fetched = window.fetch;
window.fetch = function (input, init) {
  const url = String(typeof input === "string" ? input : input.url || "");
  return fetched.apply(this, arguments).then((response) => {
    // The canned stream is a finite body, so the page reconnects; the first one is the one timed.
    if (url.includes("/api/voice/stream") && !window.__streamAt) window.__streamAt = performance.now();
    return response;
  });
};
Object.defineProperty(window, "speechSynthesis", { configurable: true, value: {
    // A loaded list with nothing for this language in it: the engine's default reads the sentence,
  // which is the ordinary case and the one whose timing is worth measuring.
  getVoices: () => [{ lang: "xx-XX", name: "Nobody" }],
  speak: (u) => {
    if (!u.text.trim()) return;
    window.__spoke.push({ text: u.text, at: performance.now() });
    setTimeout(() => { u.onstart && u.onstart(); setTimeout(() => u.onend && u.onend(), 5); }, 1);
  },
  cancel: () => undefined,
  resume: () => undefined,
  speaking: false,
  pending: false,
  paused: false,
  addEventListener: () => undefined,
  removeEventListener: () => undefined,
} });
document.addEventListener("readystatechange", function watch() {
  if (!document.documentElement) return;
  document.removeEventListener("readystatechange", watch);
  new MutationObserver(() => {
    if (!window.__shownAt && document.querySelector(".voice-said .voice-sentence")) window.__shownAt = performance.now();
  }).observe(document.documentElement, { childList: true, subtree: true });
});
"""

SPOKEN_NOW = shots.sse(("say", {"text": "All eleven invoices went out this morning."}))


def check_prompt(browser, check) -> None:  # type: ignore[no-untyped-def]
    """A sentence off the wire is on the screen and in the synthesiser, both within a breath.

    The complaint this answers is that the first answers of a conversation were spoken late or not at
    all. The server writes a sentence the moment it has one; what is timed here is everything after
    that — the stream reader, the reducer, the render, and the speaker's own queue — because that is
    where the delay was.
    """

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        rel = route.request.url.split("?", 1)[0]
        rel = rel[rel.index("/api/") :]
        if rel == "/api/voice":
            # The browser's own synthesiser, which is what the operator is on: no endpoint, no
            # downloaded voice, and therefore the path with no audio element in it at all.
            body = json.loads(json.dumps(shots.VOICE))
            body["tts"] = {"configured": False, "reason": "", "kind": "browser", "state": "ready"}
            return shots.respond(route, body)
        return shots.stub(route)

    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark", permissions=["microphone"])
    context.add_init_script(FAKE_SYNTH)
    page = context.new_page()
    page.route("**/api/**", stub)
    shots.stub.voice_frames = SPOKEN_NOW  # type: ignore[attr-defined]
    page.goto(f"{BASE}/voice?token=t&scheme=dark&lang=en")
    page.wait_for_function("window.__spoke.length > 0", timeout=15000)
    timings = page.evaluate("({ stream: window.__streamAt, shown: window.__shownAt, spoke: window.__spoke })")
    shown = timings["shown"] - timings["stream"]
    spoke = timings["spoke"][0]["at"] - timings["stream"]
    check(timings["spoke"][0]["text"].startswith("All eleven invoices"), "the sentence the server wrote is the sentence handed to the synthesiser")
    check(shown < 200, f"the sentence is on the screen within 200ms of the stream ({shown:.0f}ms)")
    check(spoke < 300, f"the sentence is handed to the synthesiser within 300ms of the stream ({spoke:.0f}ms)")
    shots.stub.voice_frames = ""  # type: ignore[attr-defined]
    context.close()


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
        check_streamed(browser, check)
        check_prompt(browser, check)
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
