"""The browser daemon and the app hold the golden view frames in two copies that must not drift apart.

Each side tests its own codec against its own copy, because each is built and tested alone (the app
by vitest, the daemon in a Go container that sees only its own module). The copies are what make the
two codecs agree with each other, so a change to the view format changes both files in one commit.
The host relays these frames unchanged; this test also reads each golden frame with the layout
docs/architecture/browser.md gives, so the host's own reading of them is held to the same bytes.
"""

import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_COPY = ROOT / "miniapp" / "src" / "browser" / "testdata" / "frames.json"
DAEMON_COPY = ROOT / "browserd" / "internal" / "wire" / "testdata" / "frames.json"


def test_the_app_and_the_daemon_hold_the_same_golden_view_frames() -> None:
    assert APP_COPY.read_bytes() == DAEMON_COPY.read_bytes()


def _read(raw: bytes) -> dict:
    kind = raw[0]
    body = raw[1:]
    if kind == 0x21:
        frame_no, meta_len = struct.unpack(">IH", body[:6])
        assert frame_no > 0
        meta = json.loads(body[6 : 6 + meta_len])
        image = body[6 + meta_len :]
        assert image
        return {"kind": "frame", "frame_no": frame_no, "meta": meta, "image_hex": image.hex()}
    if kind == 0x22:
        return {"kind": "event", "event": json.loads(body)}
    if kind == 0x30:
        return {"kind": "attach", "attach": json.loads(body)}
    if kind == 0x31:
        assert len(body) == 4
        return {"kind": "ack", "frame_no": struct.unpack(">I", body)[0]}
    if kind == 0x32:
        return {"kind": "view", "view": json.loads(body)}
    if kind == 0x33:
        return {"kind": "input", "input": json.loads(body)}
    raise AssertionError(f"unknown view frame type {kind:#x}")


def test_every_golden_view_frame_reads_as_its_value() -> None:
    golden = json.loads(DAEMON_COPY.read_text())
    assert golden["frames"]
    for frame in golden["frames"]:
        assert _read(bytes.fromhex(frame["hex"])) == frame["value"], frame["name"]
        # The JSON inside a frame is compact, in the order the contract lists its keys.
        value = frame["value"]
        for key in ("meta", "event", "attach", "view", "input"):
            if key in value:
                compact = json.dumps(value[key], separators=(",", ":"), ensure_ascii=False).encode()
                assert compact in bytes.fromhex(frame["hex"]), frame["name"]
