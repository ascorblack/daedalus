"""The browser never answers a terminal query: the daemon does, once, for every client.

A program asks its terminal where the cursor is, what it is, what colour the background is, and reads
the reply from its input. If the browser answered as well, the program would get two replies, or one
seconds late from a background tab, and a cursor report is byte for byte a Shift+F3 keypress. So the
page swallows the queries. Here the stream carries DA1, DA2, DSR 5 and 6, DECXCPR, OSC 10/11 `?`,
DECRQM, XTVERSION and the kitty keyboard query, and the page must send no INPUT at all — while a key
typed afterwards still arrives, so silence is not a dead socket.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_queries.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, dock_state, open_session, text, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ID = "queryaaaaaaa"
QUERIES = {
    "DA1": "\x1b[c",
    "DA2": "\x1b[>c",
    "DSR 5": "\x1b[5n",
    "DSR 6": "\x1b[6n",
    "DECXCPR": "\x1b[?6n",
    "colour scheme": "\x1b[?996n",
    "OSC 10": "\x1b]10;?\x07",
    "OSC 11": "\x1b]11;?\x1b\\",
    "DECRQM": "\x1b[?2004$p",
    "XTVERSION": "\x1b[>q",
    "kitty keyboard": "\x1b[?u",
    "window size": "\x1b[18t",
}


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    term = TerminalStub(S1)
    term.add(ID, title="queries")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.add_init_script(DEBUG)
        context.add_init_script(dock_state(S1, [ID]))
        page = open_session(context, term, stub, BASE, S1)
        wait_live(page, ID)
        page.locator(f".term-view[data-terminal-view='{ID}'] .term-screen").click()
        page.wait_for_timeout(300)
        before = term.inputs(ID)
        for name, query in QUERIES.items():
            term.emit(ID, f"{name}: {query}\r\n")
        term.emit(ID, "done\r\n")
        for _ in range(50):
            if any("done" in line for line in text(page, ID)):
                break
            page.wait_for_timeout(100)
        page.wait_for_timeout(500)
        replies = term.inputs(ID)[len(before):]
        print("replies from the page:", replies)
        if replies:
            problems.append(f"the page answered queries itself: {replies!r}")
        page.keyboard.type("ok")
        page.wait_for_timeout(300)
        typed = term.inputs(ID)[len(before):]
        if typed != b"ok":
            problems.append(f"typing after the queries did not arrive as typed: {typed!r}")
        context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
