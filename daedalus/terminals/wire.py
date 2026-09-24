"""The terminal daemon's wire format: socket frames, the JSON-RPC envelope, and the browser frames.

The contract is ``docs/architecture/terminals.md``. The daemon's Go codec and the app's TypeScript
codec are tested against the same golden files as this one, so a change here that is not a change
there is caught by the tests of whichever side was left behind.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass
from typing import Any

MAX_PAYLOAD = 1 << 20
"""The largest payload one frame may carry. A longer length prefix is a broken or hostile peer, and
reading it would allocate whatever it claims."""

CONTROL = 0
"""Channel 0 carries JSON-RPC; every other channel is an attachment or a byte stream."""

PROTOCOL = 1
"""The protocol this build speaks. A daemon announcing another is refused rather than half-used."""

# JSON-RPC codes, the standard ones and the daemon's own. The numbers are the contract.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL = -32603
NOT_FOUND = 1001
EXITED = 1002
LIMIT = 1003
FORBIDDEN = 1004
TIMEOUT = 1005
KEYBOARD_HELD = 1006
UNSUPPORTED = 1007
STALE_LAUNCH = 1008
INVALID_SIZE = 1009

# Browser frames: the first byte is the type.
OUTPUT = 0x01
SNAPSHOT = 0x02
EVENT = 0x03
INPUT = 0x10
RESIZE = 0x11
ACK = 0x12
ATTACH = 0x13

MAX_SEQ = (1 << 53) - 1
"""Offsets are kept within what a browser's numbers hold exactly."""


class FrameError(ValueError):
    """A frame that does not parse: too long, too short, or of an unknown kind."""


class RpcError(Exception):
    """An error object a daemon returned for a call."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message
        self.data = data


def encode_frame(channel: int, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise FrameError(f"a frame carries at most {MAX_PAYLOAD} bytes, not {len(payload)}")
    return struct.pack(">II", 4 + len(payload), channel) + payload


def decode_frame(data: bytes) -> tuple[int, bytes]:
    """One whole frame from its bytes; the codec the stream reader and the golden tests share."""
    if len(data) < 8:
        raise FrameError("a frame is at least eight bytes")
    length, channel = struct.unpack(">II", data[:8])
    if length < 4 or length - 4 > MAX_PAYLOAD:
        raise FrameError(f"a frame length of {length}")
    if len(data) != 4 + length:
        raise FrameError(f"the frame says {length} bytes and carries {len(data) - 4}")
    return channel, data[8:]


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = await reader.readexactly(8)
    length, channel = struct.unpack(">II", head)
    if length < 4 or length - 4 > MAX_PAYLOAD:
        raise FrameError(f"a frame length of {length}")
    return channel, await reader.readexactly(length - 4)


def request(call_id: int, method: str, params: dict[str, Any] | None) -> bytes:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": call_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()


# -- browser frames --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrowserFrame:
    """One decoded browser frame. Which fields mean something depends on ``kind``."""

    kind: str
    seq: int = 0
    data: bytes = b""
    cols: int = 0
    rows: int = 0
    px_w: int = 0
    px_h: int = 0
    json: dict[str, Any] | None = None


def _seq(value: int) -> int:
    if not 0 <= value <= MAX_SEQ:
        raise FrameError(f"an offset of {value} is outside 0..2^53-1")
    return value


def decode_browser(frame: bytes) -> BrowserFrame:
    """A browser frame from its bytes; raises ``FrameError`` on anything malformed."""
    if not frame:
        raise FrameError("an empty frame")
    kind, body = frame[0], frame[1:]
    if kind == OUTPUT:
        if len(body) < 8:
            raise FrameError("OUTPUT is shorter than its offset")
        return BrowserFrame("output", seq=_seq(struct.unpack(">Q", body[:8])[0]), data=body[8:])
    if kind == SNAPSHOT:
        if len(body) < 12:
            raise FrameError("SNAPSHOT is shorter than its header")
        cols, rows, seq = struct.unpack(">HHQ", body[:12])
        return BrowserFrame("snapshot", seq=_seq(seq), cols=cols, rows=rows, data=body[12:])
    if kind in (EVENT, ATTACH):
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FrameError(f"not JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise FrameError("the JSON of a frame is an object")
        if kind == EVENT and not isinstance(value.get("type"), str):
            raise FrameError("an EVENT names its type")
        return BrowserFrame("event" if kind == EVENT else "attach", json=value)
    if kind == INPUT:
        return BrowserFrame("input", data=body)
    if kind == RESIZE:
        if len(body) != 8:
            raise FrameError("RESIZE is four sizes")
        cols, rows, px_w, px_h = struct.unpack(">HHHH", body)
        return BrowserFrame("resize", cols=cols, rows=rows, px_w=px_w, px_h=px_h)
    if kind == ACK:
        if len(body) != 8:
            raise FrameError("ACK is one offset")
        return BrowserFrame("ack", seq=_seq(struct.unpack(">Q", body)[0]))
    raise FrameError(f"unknown frame type 0x{kind:02x}")


def encode_browser(frame: BrowserFrame) -> bytes:
    if frame.kind == "output":
        return bytes([OUTPUT]) + struct.pack(">Q", _seq(frame.seq)) + frame.data
    if frame.kind == "snapshot":
        return bytes([SNAPSHOT]) + struct.pack(">HHQ", frame.cols, frame.rows, _seq(frame.seq)) + frame.data
    if frame.kind in ("event", "attach"):
        body = json.dumps(frame.json or {}, separators=(",", ":"), ensure_ascii=False).encode()
        return bytes([EVENT if frame.kind == "event" else ATTACH]) + body
    if frame.kind == "input":
        return bytes([INPUT]) + frame.data
    if frame.kind == "resize":
        return bytes([RESIZE]) + struct.pack(">HHHH", frame.cols, frame.rows, frame.px_w, frame.px_h)
    if frame.kind == "ack":
        return bytes([ACK]) + struct.pack(">Q", _seq(frame.seq))
    raise FrameError(f"unknown frame kind {frame.kind!r}")


__all__ = [
    "ACK",
    "ATTACH",
    "CONTROL",
    "EVENT",
    "INPUT",
    "MAX_PAYLOAD",
    "OUTPUT",
    "PROTOCOL",
    "RESIZE",
    "SNAPSHOT",
    "BrowserFrame",
    "FrameError",
    "RpcError",
    "decode_browser",
    "decode_frame",
    "encode_browser",
    "encode_frame",
    "read_frame",
    "request",
]
