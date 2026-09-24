"""The app and the terminal daemon hold one set of golden wire frames, byte for byte.

Each side tests its own codec against its own copy, because each is built and tested alone (the app
by vitest, the daemon in a Go container that sees only its own module). The copies are what make
the two codecs agree with each other, so they must never drift apart: a change to the wire format
changes both files in the same commit.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_COPY = ROOT / "miniapp" / "src" / "terminal" / "testdata" / "frames.json"
DAEMON_COPY = ROOT / "ptyd" / "internal" / "wire" / "testdata" / "frames.json"


def test_the_app_and_the_daemon_hold_the_same_golden_frames() -> None:
    assert APP_COPY.read_bytes() == DAEMON_COPY.read_bytes()
