"""WebSocket framing for the fake Codex, both ends, standard library only.

The real app server answers on its unix socket with an HTTP upgrade to a WebSocket and then JSON
text messages, one per frame (recorded in ``recorded/codex``). The fake speaks the same, so the
adapter's client is tested against the framing it meets in production.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Closed(ConnectionError):
    pass


class Socket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, client: bool) -> None:
        self.reader = reader
        self.writer = writer
        self.client = client
        self.lock = asyncio.Lock()

    @classmethod
    async def accept(cls, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Socket | None:
        """The server's side of the upgrade; ``None`` (and the connection closed) for anything else,
        as the real server does with a bare JSON line."""
        try:
            first = await reader.readline()
            if not first.startswith(b"GET "):
                writer.close()
                return None
            head = first + await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return None
        key = ""
        for line in head.decode("latin-1").split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-key":
                key = value.strip()
        if not head.startswith(b"GET ") or not key:
            writer.close()
            return None
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        writer.write(f"HTTP/1.1 101 Switching Protocols\r\nconnection: Upgrade\r\nupgrade: websocket\r\nsec-websocket-accept: {accept}\r\n\r\n".encode())
        await writer.drain()
        return cls(reader, writer, client=False)

    @classmethod
    async def connect(cls, path: str) -> Socket:
        reader, writer = await asyncio.open_unix_connection(path, limit=1 << 22)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            raise Closed("the upgrade was refused")
        return cls(reader, writer, client=True)

    async def send(self, text: str) -> None:
        payload = text.encode()
        size = len(payload)
        head = bytes([0x81])
        flag = 0x80 if self.client else 0
        if size < 126:
            head += bytes([flag | size])
        elif size < 1 << 16:
            head += bytes([flag | 126]) + struct.pack(">H", size)
        else:
            head += bytes([flag | 127]) + struct.pack(">Q", size)
        if self.client:
            mask = os.urandom(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            head += mask
        async with self.lock:
            self.writer.write(head + payload)
            await self.writer.drain()

    async def receive(self) -> str | None:
        parts: list[bytes] = []
        while True:
            try:
                first, second = await self.reader.readexactly(2)
                size = second & 0x7F
                if size == 126:
                    size = struct.unpack(">H", await self.reader.readexactly(2))[0]
                elif size == 127:
                    size = struct.unpack(">Q", await self.reader.readexactly(8))[0]
                mask = await self.reader.readexactly(4) if second & 0x80 else b""
                payload = await self.reader.readexactly(size)
            except (asyncio.IncompleteReadError, ConnectionError):
                return None
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 0x8:
                return None
            if opcode in (0x9, 0xA):
                continue
            parts.append(payload)
            if first & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        self.writer.close()


__all__ = ["Closed", "Socket"]
