"""The browser daemon's wire: the terminal daemon's framing and JSON-RPC, and its own codes and frames.

The contract is ``docs/architecture/browser.md``. The socket framing, the handshake and the error
envelope are ``daedalus.terminals.wire``'s, byte for byte; what is the browser's own is below.
"""

from __future__ import annotations

from daedalus.terminals.wire import (
    FORBIDDEN,
    INTERNAL,
    INVALID_PARAMS,
    LIMIT,
    METHOD_NOT_FOUND,
    NOT_FOUND,
    TIMEOUT,
    UNSUPPORTED,
    RpcError,
)

PROTOCOL = 1

HUMAN_DRIVING = 1101
BLOCKED = 1102
STALE_REF = 1103
NO_SUCH_TAB = 1104
FIELD_FORBIDDEN = 1105
PAUSED = 1106
DIALOG_OPEN = 1107
BROWSER_GONE = 1108

# View frames: the first byte is the type, apart from the terminal's (0x01–0x13) so a frame sent down
# the wrong kind of channel is refused rather than misread.
FRAME = 0x21
EVENT = 0x22
ATTACH = 0x30
ACK = 0x31
VIEW = 0x32
INPUT = 0x33

MAX_VIEW_JSON = 4 << 10
"""ATTACH and VIEW carry a JSON object of at most this many bytes."""
MAX_INPUT_JSON = 4 << 10
"""INPUT carries a JSON object of at most this many bytes, so the frame is at most this plus one."""
ACK_SIZE = 5
"""ACK is its type and a u32 frame number, exactly."""

LOCK_FILE = "browserd.lock"

__all__ = [
    "ACK",
    "ACK_SIZE",
    "ATTACH",
    "BLOCKED",
    "BROWSER_GONE",
    "DIALOG_OPEN",
    "EVENT",
    "FIELD_FORBIDDEN",
    "FORBIDDEN",
    "FRAME",
    "HUMAN_DRIVING",
    "INPUT",
    "INTERNAL",
    "INVALID_PARAMS",
    "LIMIT",
    "LOCK_FILE",
    "MAX_INPUT_JSON",
    "MAX_VIEW_JSON",
    "METHOD_NOT_FOUND",
    "NOT_FOUND",
    "NO_SUCH_TAB",
    "PAUSED",
    "PROTOCOL",
    "RpcError",
    "STALE_REF",
    "TIMEOUT",
    "UNSUPPORTED",
    "VIEW",
]
