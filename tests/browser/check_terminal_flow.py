"""A flood reaches the end without the browser drowning in it.

The old web terminal had no flow control: output went out as fast as the program wrote it and piled
up in the page. Here the stub plays the daemon's side of the window — it sends only while less than
256 KiB is unacknowledged — and the page acknowledges what xterm.js has *parsed*. Sixteen megabytes of
numbered lines go through; the unacknowledged amount never passes the window, and the last line the
terminal shows is the last line sent.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_flow.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, WINDOW, TerminalStub, dock_state, open_session, text, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
FLOOD = "flowaaaaaaaa"
TOTAL = 16 * 1024 * 1024


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    term = TerminalStub(S1)
    term.add(FLOOD, title="yes")
    # Short enough not to wrap in the dock, so the last row is the last line whole.
    line = 64
    count = TOTAL // line
    body = "".join(f"{i:08d} {'x' * (line - 11)}\r\n" for i in range(count)).encode()
    last = f"{count - 1:08d}"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.add_init_script(DEBUG)
        context.add_init_script(dock_state(S1, [FLOOD]))
        page = open_session(context, term, stub, BASE, S1)
        wait_live(page, FLOOD)
        page.wait_for_timeout(300)
        started = time.monotonic()
        term.emit(FLOOD, body)
        client = term.live(FLOOD)[0]
        deadline = started + 240
        while client.acked < len(body) and time.monotonic() < deadline:
            page.wait_for_timeout(200)
        took = time.monotonic() - started
        print(f"flood: {len(body)} bytes, acknowledged {client.acked} in {took:.1f} s ({len(body) / took / 1e6:.1f} MB/s); most unacknowledged {client.max_unacked}")
        if client.acked < len(body):
            problems.append(f"the flood did not finish: {client.acked} of {len(body)} acknowledged")
        if client.max_unacked > WINDOW:
            problems.append(f"{client.max_unacked} bytes went unacknowledged, past the {WINDOW}-byte window")
        page.wait_for_timeout(300)
        shown = [x for x in text(page, FLOOD) if x.strip()]
        print("last line shown:", shown[-1][:20] if shown else None)
        if not shown or not shown[-1].startswith(last):
            problems.append(f"the last line shown is not the last line sent ({last}): {shown[-1][:20] if shown else None}")
        # The page is still answering: the dock's search opens during and after a flood.
        page.locator(".term-tools button[aria-label='Find']").click()
        page.wait_for_selector(".term-search", timeout=3000)
        context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
