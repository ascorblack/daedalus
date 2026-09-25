"""Talking to a CLI's own server over a stream the terminal daemon relays.

A CLI's server listens where the CLI runs (a unix socket in the launch's dial directory, a loopback
port), which the host reaches only through the daemon's ``net.dial``: a plain byte stream. So the two
protocols the adapters need are spoken here over any such stream, with nothing but the standard
library:

- ``WebSocket``: the client side of RFC 6455, text messages only. Codex's app server answers on its
  unix socket with an HTTP upgrade to a WebSocket (measured: a JSON line sent without the upgrade is
  met with a closed connection).
- ``http_request`` and ``EventStream``: HTTP/1.1 requests, each on a stream of its own, and a
  server-sent event stream, for OpenCode's embedded server. Both read a body framed by length, by
  chunks, or by the end of the connection.
"""

from __future__ import annotations

import base64
import json
import os
import struct
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from daedalus.harness.contract import ByteStreamPort

HEADERS_MAX = 64 << 10
"""The most an HTTP head may take before the answer is treated as broken."""
MESSAGE_MAX = 64 << 20
"""The largest WebSocket message or HTTP body accepted: a thread's history page is large, but a
server sending more than this is not answering the question asked."""


class StreamClosed(ConnectionError):
    """The other side closed the stream, or the daemon did."""


class ProtocolError(ConnectionError):
    """The other side answered in a way the protocol does not allow."""


class Reader:
    """Buffered reads over a ``ByteStreamPort``, whose ``read`` gives whatever arrived."""

    def __init__(self, stream: ByteStreamPort) -> None:
        self.stream = stream
        self.buffer = bytearray()
        self.ended = False

    async def _more(self) -> None:
        chunk = await self.stream.read()
        if not chunk:
            self.ended = True
            raise StreamClosed("the stream closed")
        self.buffer += chunk

    async def exactly(self, count: int) -> bytes:
        while len(self.buffer) < count:
            await self._more()
        out = bytes(self.buffer[:count])
        del self.buffer[:count]
        return out

    async def until(self, marker: bytes, limit: int = HEADERS_MAX) -> bytes:
        while (index := self.buffer.find(marker)) < 0:
            if len(self.buffer) > limit:
                raise ProtocolError("an answer's head was too long")
            await self._more()
        out = bytes(self.buffer[: index + len(marker)])
        del self.buffer[: index + len(marker)]
        return out

    async def rest(self, limit: int = MESSAGE_MAX) -> bytes:
        while not self.ended:
            if len(self.buffer) > limit:
                raise ProtocolError("an answer was too long")
            try:
                await self._more()
            except StreamClosed:
                break
        out = bytes(self.buffer)
        self.buffer.clear()
        return out


class WebSocket:
    """A WebSocket client over a relayed stream. ``receive`` returns one text message at a time and
    ``None`` once the connection closed; pings are answered on the way."""

    def __init__(self, stream: ByteStreamPort) -> None:
        self.stream = stream
        self.reader = Reader(stream)
        self.closed = False

    @classmethod
    async def connect(cls, stream: ByteStreamPort, *, host: str = "localhost", path: str = "/") -> WebSocket:
        socket = cls(stream)
        key = base64.b64encode(os.urandom(16)).decode()
        request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        await stream.write(request.encode())
        head = await socket.reader.until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].split(b" ")
        if len(status) < 2 or status[1] != b"101":
            raise ProtocolError(f"the server refused the WebSocket upgrade: {head.split(b'\r\n', 1)[0].decode('latin-1')}")
        return socket

    async def send_text(self, text: str) -> None:
        await self._frame(0x1, text.encode())

    async def _frame(self, opcode: int, payload: bytes) -> None:
        # A client masks every frame (RFC 6455 5.3); a server closes on an unmasked one.
        mask = os.urandom(4)
        size = len(payload)
        head = bytes([0x80 | opcode])
        if size < 126:
            head += bytes([0x80 | size])
        elif size < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", size)
        await self.stream.write(head + mask + _mask(payload, mask))

    async def receive(self) -> str | None:
        parts: list[bytes] = []
        total = 0
        while True:
            try:
                first, second = await self.reader.exactly(2)
                size = second & 0x7F
                if size == 126:
                    size = struct.unpack(">H", await self.reader.exactly(2))[0]
                elif size == 127:
                    size = struct.unpack(">Q", await self.reader.exactly(8))[0]
                mask = await self.reader.exactly(4) if second & 0x80 else b""
                total += size
                if total > MESSAGE_MAX:
                    raise ProtocolError("a message was too long")
                payload = await self.reader.exactly(size)
            except StreamClosed:
                self.closed = True
                return None
            if mask:
                payload = _mask(payload, mask)
            opcode = first & 0x0F
            if opcode == 0x8:
                self.closed = True
                return None
            if opcode == 0x9:
                await self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if first & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self._frame(0x8, struct.pack(">H", 1000))
            except Exception:  # noqa: BLE001 — closing a stream that is already gone is done
                pass
        await self.stream.close()


def _mask(payload: bytes, mask: bytes) -> bytes:
    key = int.from_bytes((mask * (len(payload) // 4 + 1))[: len(payload)], "big") if payload else 0
    return (int.from_bytes(payload, "big") ^ key).to_bytes(len(payload), "big") if payload else b""


# -- HTTP -----------------------------------------------------------------------------------------


Dial = Callable[[], Awaitable[ByteStreamPort]]


class HttpAnswer:
    def __init__(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body) if self.body.strip() else None


async def _head(reader: Reader) -> tuple[int, dict[str, str]]:
    raw = await reader.until(b"\r\n\r\n")
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2 or not parts[1].isdigit():
        raise ProtocolError(f"not an HTTP answer: {lines[0][:80]}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return int(parts[1]), headers


def _request_bytes(method: str, path: str, headers: Mapping[str, str], body: bytes | None) -> bytes:
    lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    if body is not None:
        lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b"")


async def http_request(dial: Dial, method: str, path: str, *, body: Any = None, headers: Mapping[str, str] | None = None) -> HttpAnswer:
    """One request on a stream of its own, closed after the answer: a server that keeps connections
    alive is never left holding one of the daemon's channels."""
    stream = await dial()
    try:
        data = None if body is None else json.dumps(body).encode()
        await stream.write(_request_bytes(method, path, headers or {}, data))
        reader = Reader(stream)
        status, answer_headers = await _head(reader)
        if answer_headers.get("transfer-encoding", "").lower() == "chunked":
            content = bytearray()
            async for chunk in _chunks(reader):
                content += chunk
                if len(content) > MESSAGE_MAX:
                    raise ProtocolError("an answer was too long")
            payload = bytes(content)
        elif "content-length" in answer_headers:
            payload = await reader.exactly(int(answer_headers["content-length"]))
        elif status in (204, 304) or method == "HEAD":
            payload = b""
        else:
            payload = await reader.rest()
        return HttpAnswer(status, answer_headers, payload)
    finally:
        await stream.close()


async def _chunks(reader: Reader) -> AsyncIterator[bytes]:
    while True:
        line = (await reader.until(b"\r\n")).strip()
        size = int(line.split(b";", 1)[0] or b"0", 16)
        if size == 0:
            return
        data = await reader.exactly(size)
        await reader.exactly(2)
        yield data


class EventStream:
    """``GET <path>`` as server-sent events: each ``data:`` payload, parsed as JSON, until the server
    or the daemon closes the stream."""

    def __init__(self, stream: ByteStreamPort, reader: Reader, chunked: bool) -> None:
        self.stream = stream
        self.reader = reader
        self.chunked = chunked

    @classmethod
    async def open(cls, dial: Dial, path: str, *, headers: Mapping[str, str] | None = None) -> EventStream:
        stream = await dial()
        await stream.write(_request_bytes("GET", path, {"Accept": "text/event-stream", **(headers or {})}, None))
        reader = Reader(stream)
        status, answer_headers = await _head(reader)
        if status != 200:
            await stream.close()
            raise ProtocolError(f"the event stream answered {status}")
        return cls(stream, reader, answer_headers.get("transfer-encoding", "").lower() == "chunked")

    async def events(self) -> AsyncIterator[Any]:
        text = ""
        data: list[str] = []
        source = _chunks(self.reader) if self.chunked else self._plain()
        try:
            async for chunk in source:
                text += chunk.decode("utf-8", "replace")
                while "\n" in text:
                    line, text = text.split("\n", 1)
                    line = line.rstrip("\r")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif not line and data:
                        joined = "\n".join(data)
                        data = []
                        try:
                            yield json.loads(joined)
                        except ValueError:
                            continue
        except StreamClosed:
            return

    async def _plain(self) -> AsyncIterator[bytes]:
        if self.reader.buffer:
            yield bytes(self.reader.buffer)
            self.reader.buffer.clear()
        while chunk := await self.stream.read():
            yield chunk

    async def close(self) -> None:
        await self.stream.close()


__all__ = ["EventStream", "HttpAnswer", "ProtocolError", "Reader", "StreamClosed", "WebSocket", "http_request"]
