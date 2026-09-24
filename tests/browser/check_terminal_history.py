"""Codex's transcript stays in the terminal's history in the browser, as it does in the daemon.

Codex, like every ratatui "inline" program, keeps its composer at the bottom of the screen and pushes
finished lines above it into the history by scrolling a region anchored at the top row (`CSI 1;n r`,
then `CSI n S`). Stock xterm.js deletes those lines; the daemon's emulator keeps them, so the live
view lost what a reconnect would bring back. Here thirty transcript lines are written that way, in
the shipped build, and every one must be in the buffer in order with the composer rows untouched.
Then the socket drops and the page gets a snapshot (a full reset of the terminal), and it is done
again: the fix must survive the reset.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_history.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, dock_state, open_session, text, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ID = "codexaaaaaaa"
CSI = "\x1b["


def inline_history(term: TerminalStub, rows: int, lines: list[str]) -> None:
    """What Codex writes: a live area in the bottom four rows, history inserted above it two lines at a time."""
    top = rows - 4
    term.emit(ID, f"{CSI}{top + 1};1H{CSI}2K› composer{CSI}{top + 3};1H{CSI}2Kfooter")
    for i in range(0, len(lines), 2):
        term.emit(ID, f"{CSI}1;{top}r{CSI}2S{CSI}{top - 1};1H{lines[i]}{CSI}{top};1H{lines[i + 1]}{CSI}r{CSI}{top + 1};11H")


def check(page: Page, lines: list[str], label: str, problems: list[str]) -> None:
    for _ in range(50):
        if lines[-1] in text(page, ID):
            break
        page.wait_for_timeout(100)
    page.wait_for_timeout(200)
    buffer = text(page, ID)
    kept = [x for x in buffer if x.startswith(lines[0].split(" ")[0])]
    print(f"{label}: {len(kept)} of {len(lines)} transcript lines in the buffer, {len(buffer)} rows")
    if kept != lines:
        missing = [x for x in lines if x not in kept]
        problems.append(f"{label}: the transcript lost lines ({len(missing)} missing, first {missing[:3]})")
    if "› composer" not in buffer or "footer" not in buffer:
        problems.append(f"{label}: the live area was disturbed")


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    term = TerminalStub(S1)
    rows = 20
    stream = term.add(ID, title="codex", cols=80, rows=rows)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.add_init_script(DEBUG)
        context.add_init_script(dock_state(S1, [ID], height=420))
        page = open_session(context, term, stub, BASE, S1)
        wait_live(page, ID)
        page.wait_for_timeout(500)
        rows = page.evaluate("(id) => window.__terminals.size(id).rows", ID)
        print("terminal rows:", rows)
        first = [f"turn-one {i:02d}" for i in range(30)]
        inline_history(term, rows, first)
        check(page, first, "live", problems)

        # A snapshot resets the terminal; the fix must still be in force afterwards.
        term.drop(ID)
        stream.snapshot = f"{CSI}H{CSI}2J".encode()
        term.emit(ID, "\r\n")
        stream.last_resize_seq = len(stream.stream)
        for _ in range(100):
            if len([c for c in term.of(ID) if c.of("attach")]) >= 2 and term.live(ID):
                break
            page.wait_for_timeout(100)
        page.wait_for_timeout(500)
        second = [f"turn-two {i:02d}" for i in range(30)]
        inline_history(term, rows, second)
        check(page, second, "after a snapshot", problems)
        context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
