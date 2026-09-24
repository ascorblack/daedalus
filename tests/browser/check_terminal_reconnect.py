"""A dropped connection comes back without garbage: a tail when the screen is still right, a snapshot
after a full reset when it is not.

The old web terminal replayed raw bytes from a buffer on every reconnect, drawn for whatever size the
screen had then and cut in the middle of escape sequences; and a backend that switched mouse modes
behind the application's back left the prompt full of `[<64;…M`. Two cases:

- **Tail.** A stream of numbered lines, a dropped socket, more lines while it is down. The page
  reattaches asking for the tail (`haveState`, the right `lastSeq`), and its buffer holds every line
  exactly once, in order.
- **Snapshot.** The stream turns on mouse reporting (1000) and bracketed paste, the socket drops, and
  the PTY is resized while it is down, so the daemon sends a snapshot instead of a tail. After it, a
  click sends no mouse report, a paste arrives without the bracketed-paste markers, and the terminal
  holds the snapshot's text and nothing of the old screen. (Before the drop the same click did send a
  report, so the check is not passing for want of a working mouse.)

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_reconnect.py
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
TAIL, SNAP = "rtailaaaaaaa", "rsnapbbbbbbb"


def wait_for(page: Page, predicate, timeout_ms: int = 10000) -> bool:  # type: ignore[no-untyped-def]
    for _ in range(timeout_ms // 100):
        if predicate():
            return True
        page.wait_for_timeout(100)
    return predicate()


def attaches(term: TerminalStub, id_: str) -> list[dict]:
    return [f["json"] for c in term.of(id_) for f in c.of("attach")]


def click_terminal(page: Page, id_: str) -> None:
    box = page.locator(f".term-view[data-terminal-view='{id_}'] .term-screen").bounding_box()
    assert box
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def paste(page: Page, id_: str, data: str) -> None:
    """A paste event on the terminal's text field, the way the browser delivers Ctrl+V."""
    page.evaluate(
        """([id, data]) => {
          const field = document.querySelector(`.term-view[data-terminal-view='${id}'] .xterm-helper-textarea`);
          const dt = new DataTransfer();
          dt.setData('text/plain', data);
          field.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
        }""",
        [id_, data],
    )


def tail_case(page: Page, term: TerminalStub, problems: list[str]) -> None:
    lines = [f"line {i:03d}" for i in range(1, 251)]
    term.emit(TAIL, "".join(f"{line}\r\n" for line in lines[:200]))
    if not wait_for(page, lambda: "line 200" in text(page, TAIL)):
        problems.append("tail: the first 200 lines never arrived")
        return
    held = len(term.terms[TAIL].stream)
    term.drop(TAIL)
    page.wait_for_timeout(100)
    term.emit(TAIL, "".join(f"{line}\r\n" for line in lines[200:]))
    if not wait_for(page, lambda: len(attaches(term, TAIL)) >= 2):
        problems.append("tail: the page never reattached")
        return
    again = attaches(term, TAIL)[-1]
    print("tail: reattach", again, "held", held)
    if not again.get("haveState") or again.get("lastSeq") != held:
        problems.append(f"tail: the reattach did not ask for the tail after {held}: {again}")
    wait_for(page, lambda: "line 250" in text(page, TAIL))
    got = [line for line in text(page, TAIL) if line.startswith("line ")]
    if got != lines:
        dupes = sorted({x for x in got if got.count(x) > 1})
        missing = [x for x in lines if x not in got]
        problems.append(f"tail: the buffer is not the 250 lines once each (duplicated {dupes[:5]}, missing {missing[:5]})")


def snapshot_case(page: Page, term: TerminalStub, problems: list[str]) -> None:
    page.locator(f".term-tab[data-tab='{SNAP}']").click()
    wait_live(page, SNAP)
    page.wait_for_timeout(300)
    term.emit(SNAP, "old screen line 1\r\nold screen line 2\r\n\x1b[?1000h\x1b[?2004h")
    wait_for(page, lambda: "old screen line 2" in text(page, SNAP))
    page.wait_for_timeout(200)
    click_terminal(page, SNAP)
    page.wait_for_timeout(300)
    before = term.inputs(SNAP)
    print("snapshot: a click with mouse reporting on sent", before)
    if b"\x1b[M" not in before and b"\x1b[<" not in before:
        problems.append("snapshot: with mouse reporting on, a click sent no report — the check proves nothing")
    # The PTY is resized while the page is away: its screen no longer matches, so a snapshot comes.
    term.drop(SNAP)
    stream = term.terms[SNAP]
    stream.snapshot = b"\x1b[H\x1b[2Jrestored line A\r\nrestored line B\r\n$ "
    term.emit(SNAP, "drawn for another size\r\n")
    stream.last_resize_seq = len(stream.stream)
    if not wait_for(page, lambda: len(attaches(term, SNAP)) >= 2):
        problems.append("snapshot: the page never reattached")
        return
    wait_for(page, lambda: "restored line A" in text(page, SNAP))
    page.wait_for_timeout(300)
    shown = [line.rstrip() for line in text(page, SNAP) if line.strip()]
    print("snapshot: the terminal holds", shown)
    if shown != ["restored line A", "restored line B", "$"]:
        problems.append(f"snapshot: the terminal does not hold exactly the snapshot: {shown}")
    mark = len(term.inputs(SNAP))
    click_terminal(page, SNAP)
    page.wait_for_timeout(300)
    after_click = term.inputs(SNAP)[mark:]
    if b"\x1b[M" in after_click or b"\x1b[<" in after_click:
        problems.append(f"snapshot: mouse reporting survived the snapshot: {after_click!r}")
    mark = len(term.inputs(SNAP))
    paste(page, SNAP, "hello")
    page.wait_for_timeout(300)
    pasted = term.inputs(SNAP)[mark:]
    print("snapshot: a paste sent", pasted)
    if pasted != b"hello":
        problems.append(f"snapshot: a paste after the snapshot was not plain: {pasted!r}")


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    term = TerminalStub(S1)
    term.add(TAIL, title="tail")
    term.add(SNAP, title="snapshot")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.add_init_script(DEBUG)
        context.add_init_script(dock_state(S1, [TAIL, SNAP], height=420))
        page = open_session(context, term, stub, BASE, S1)
        wait_live(page, TAIL)
        page.wait_for_timeout(300)
        tail_case(page, term, problems)
        snapshot_case(page, term, problems)
        context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
