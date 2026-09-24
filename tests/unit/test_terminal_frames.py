"""The app and the terminal daemon hold shared data that must not drift apart: the golden wire frames
and the width grids byte for byte, and the fallback palette and xterm.js version by value.

Each side tests its own codec against its own copy, because each is built and tested alone (the app
by vitest, the daemon in a Go container that sees only its own module). The copies are what make
the two codecs agree with each other, so they must never drift apart: a change to the wire format
changes both files in the same commit.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_COPY = ROOT / "miniapp" / "src" / "terminal" / "testdata" / "frames.json"
DAEMON_COPY = ROOT / "ptyd" / "internal" / "wire" / "testdata" / "frames.json"


def test_the_app_and_the_daemon_hold_the_same_golden_frames() -> None:
    assert APP_COPY.read_bytes() == DAEMON_COPY.read_bytes()


WIDTHS_APP = ROOT / "miniapp" / "src" / "terminal" / "testdata" / "ghostty-widths.json"
WIDTHS_DAEMON = ROOT / "ptyd" / "internal" / "emulator" / "ghostty" / "testdata" / "ghostty-widths.json"


def test_the_app_and_the_daemon_measure_text_from_the_same_grids() -> None:
    # The app's width provider is tested against these grids and the daemon's emulator replays them;
    # one copy moving without the other would let the two disagree about where a character sits.
    assert WIDTHS_APP.read_bytes() == WIDTHS_DAEMON.read_bytes()


def _hex_colours(text: str) -> list[str]:
    return [c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", text)]


def test_the_daemon_answers_colour_queries_with_the_apps_fallback_palette() -> None:
    theme_ts = (ROOT / "miniapp" / "src" / "terminal" / "theme.ts").read_text()
    fallback = theme_ts[theme_ts.index("export const FALLBACK") :]
    fallback = fallback[: fallback.index("};")]
    emulator_go = (ROOT / "ptyd" / "internal" / "emulator" / "emulator.go").read_text()
    default = emulator_go[emulator_go.index("var DefaultTheme") :]
    default = default[: default.index("\n}\n")]
    go_colours = [
        "#" + "".join(f"{int(v, 16):02x}" for v in m)
        for m in re.findall(r"RGB\{(0x[0-9a-f]{2}), (0x[0-9a-f]{2}), (0x[0-9a-f]{2})\}", default)
    ]
    go_colours += [
        "#" + "".join(f"{int(v, 16):02x}" for v in m)
        for m in re.findall(r"\{(0x[0-9a-f]{2}), (0x[0-9a-f]{2}), (0x[0-9a-f]{2})\}", default.split("Palette:")[1])
    ]
    # background, foreground, cursor, then the sixteen; the Go order is foreground, background, cursor.
    app = _hex_colours(fallback)
    assert len(app) == 19
    assert go_colours[:3] == [app[1], app[0], app[2]]
    assert go_colours[3:] == app[3:]


def test_the_daemon_reports_the_xterm_version_the_app_loads() -> None:
    package = json.loads((ROOT / "miniapp" / "package.json").read_text())
    answer_go = (ROOT / "ptyd" / "internal" / "answer" / "answer.go").read_text()
    reported = re.search(r'const XtermVersion = "([^"]+)"', answer_go)
    assert reported is not None
    assert reported.group(1) == package["dependencies"]["@xterm/xterm"]
