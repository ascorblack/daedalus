"""One connection to one terminal daemon: the handshake, calls, events and channels.

The reader never waits for anybody. Replies go to the futures that asked, event notifications to a
queue a separate task drains, and channel frames to their channel's queue. So a subscriber that is
slow to handle an event, or a browser that is slow to take its output, can never stall a reply or
another terminal's stream on the same socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from daedalus.terminals import wire
from daedalus.terminals.endpoint import EndpointMissing, read_endpoint

logger = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 5.0
CALL_TIMEOUT = 10.0
CHANNEL_QUEUE_BYTES = 4 << 20
"""What one channel may hold unread before it is closed as too slow. Attachments are flow-controlled
end to end and never come near it; it bounds a byte stream whose consumer stopped reading."""


class Unavailable(Exception):
    """The environment's daemon cannot be used now; ``reason`` is a code the app shows as text."""

    def __init__(self, env: str, reason: str, detail: str) -> None:
        super().__init__(f"{env}: {detail}")
        self.env = env
        self.reason = reason
        self.detail = detail


class Channel:
    """One channel other than 0: an attachment or a byte stream, opened by the daemon's reply."""

    def __init__(self, client: PtydClient, channel_id: int) -> None:
        self.client = client
        self.id = channel_id
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._queued = 0
        self.limit = CHANNEL_QUEUE_BYTES
        """What the channel may hold unread; a byte stream sets its own, smaller one."""
        self.closed = False
        self.reason = ""
        self.claimed = False
        """Whether the caller that asked for this channel has taken it. Until then a close must leave
        it in the table, or the caller would find no channel and make a fresh one that never ends."""

    def _deliver(self, payload: bytes) -> None:
        if self.closed:
            return
        if not payload:
            self._finish("closed by the terminal service")
            return
        self._queued += len(payload)
        if self._queued > self.limit:
            self._finish("consumer too slow")
            self.client._send_nowait(self.id, b"")
            return
        self._queue.put_nowait(payload)

    def _finish(self, reason: str) -> None:
        if not self.closed:
            self.closed = True
            self.reason = reason
            self._queue.put_nowait(None)

    async def recv(self) -> bytes | None:
        """The next payload, or ``None`` once the channel is closed from either side."""
        item = await self._queue.get()
        if item is None:
            self._queue.put_nowait(None)  # every later recv sees the close as well
            return None
        self._queued -= len(item)
        return item

    async def send(self, payload: bytes) -> None:
        if self.closed:
            raise Unavailable(self.client.env, "closed", self.reason or "the channel is closed")
        if not payload:
            raise ValueError("an empty payload is the close; use close()")
        await self.client._send(self.id, payload)

    async def close(self) -> None:
        if not self.closed:
            self._finish("closed by the host")
            self.client._channels.pop(self.id, None)
            with contextlib.suppress(Unavailable, OSError):
                await self.client._send(self.id, b"")


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
"""``(method, params)`` for every notification after the handshake: ``event`` and ``events.resync``."""


class PtydClient:
    def __init__(self, env: str, run_dir: Path, *, on_notification: EventHandler | None = None) -> None:
        self.env = env
        self.run_dir = run_dir
        self.on_notification = on_notification
        self.hello: dict[str, Any] = {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._channels: dict[int, Channel] = {}
        self._next_id = 0
        self._notifications: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = asyncio.Event()
        self.lost_reason = ""

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._closed.is_set()

    @property
    def instance(self) -> str:
        return str(self.hello.get("instance") or "")

    async def connect(self) -> dict[str, Any]:
        """Open the socket, present the token and wait for ``hello``; raises ``Unavailable``."""
        try:
            endpoint = read_endpoint(self.run_dir)
        except EndpointMissing as exc:
            raise Unavailable(self.env, exc.reason, exc.detail) from None
        try:
            if endpoint.kind == "unix":
                reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(endpoint.path), limit=wire.MAX_PAYLOAD + 64), HANDSHAKE_TIMEOUT)
            else:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(endpoint.host, endpoint.port, limit=wire.MAX_PAYLOAD + 64), HANDSHAKE_TIMEOUT)
        except (OSError, TimeoutError) as exc:
            # The endpoint file says a daemon listens and nothing answers: it died without its
            # shutdown (a killed container), and the next one will rewrite the file.
            raise Unavailable(self.env, "not_running", f"the terminal service does not answer at {self.run_dir}: {exc}") from None
        try:
            writer.write(wire.encode_frame(wire.CONTROL, endpoint.token))
            await writer.drain()
            channel, payload = await asyncio.wait_for(wire.read_frame(reader), HANDSHAKE_TIMEOUT)
            message = json.loads(payload) if channel == wire.CONTROL else {}
        except asyncio.IncompleteReadError:
            writer.close()
            # A wrong token is answered by a close after a second. The usual cause is a token file
            # read in the moment a restarting daemon replaced it; the next attempt reads the new one.
            raise Unavailable(self.env, "refused", "the terminal service refused this host's token") from None
        except (OSError, TimeoutError, ValueError, wire.FrameError) as exc:
            writer.close()
            raise Unavailable(self.env, "unreachable", f"no greeting from the terminal service: {exc}") from None
        hello = message.get("params") if isinstance(message, dict) and message.get("method") == "hello" else None
        if not isinstance(hello, dict):
            writer.close()
            raise Unavailable(self.env, "unreachable", "the terminal service did not greet this host")
        if hello.get("protocol") != wire.PROTOCOL:
            writer.close()
            newer = isinstance(hello.get("protocol"), int) and hello["protocol"] > wire.PROTOCOL
            raise Unavailable(
                self.env,
                "protocol_mismatch",
                f"the terminal service speaks protocol {hello.get('protocol')} and this build speaks {wire.PROTOCOL}: "
                + ("update Daedalus" if newer else "recreate the terminals service, which ends its terminals"),
            )
        self.hello = hello
        self._reader, self._writer = reader, writer
        self._tasks = [
            asyncio.create_task(self._read_loop(), name=f"ptyd-{self.env}-reader"),
            asyncio.create_task(self._notify_loop(), name=f"ptyd-{self.env}-events"),
        ]
        return hello

    async def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = CALL_TIMEOUT) -> Any:
        """One JSON-RPC call; raises ``wire.RpcError`` for the daemon's errors, ``Unavailable`` when gone."""
        if not self.connected:
            raise Unavailable(self.env, "unreachable", self.lost_reason or "not connected to the terminal service")
        self._next_id += 1
        call_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        try:
            await self._send(wire.CONTROL, wire.request(call_id, method, params))
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise Unavailable(self.env, "timeout", f"{method} took longer than {timeout:g} s") from None
        finally:
            self._pending.pop(call_id, None)

    def channel(self, channel_id: int) -> Channel:
        """The channel a reply named; frames that arrived before it was asked for are already in it."""
        existing = self._channels.get(channel_id)
        if existing is None:
            existing = self._channels[channel_id] = Channel(self, channel_id)
        existing.claimed = True
        if existing.closed:
            self._channels.pop(channel_id, None)  # closed before it was claimed: it is the caller's now
        return existing

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        self._lose("closed by the host")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- internals ------------------------------------------------------------------------

    async def _send(self, channel: int, payload: bytes) -> None:
        writer = self._writer
        if writer is None or self._closed.is_set():
            raise Unavailable(self.env, "unreachable", self.lost_reason or "not connected")
        frame = wire.encode_frame(channel, payload)
        async with self._write_lock:
            try:
                writer.write(frame)
                await writer.drain()
            except (OSError, RuntimeError) as exc:
                self._lose(f"write failed: {exc}")
                raise Unavailable(self.env, "unreachable", f"the terminal service went away: {exc}") from None

    def _send_nowait(self, channel: int, payload: bytes) -> None:
        if self._writer is not None and not self._closed.is_set():
            self._writer.write(wire.encode_frame(channel, payload))

    async def _read_loop(self) -> None:
        assert self._reader is not None
        reason = "the terminal service closed the connection"
        try:
            while True:
                channel, payload = await wire.read_frame(self._reader)
                if channel == wire.CONTROL:
                    self._control(payload)
                    continue
                target = self._channels.get(channel)
                if target is None:
                    if not payload:
                        continue  # the answer to a close this side already sent
                    target = self._channels[channel] = Channel(self, channel)  # kept for whoever asked for it
                target._deliver(payload)
                if not payload:
                    if target.claimed:
                        self._channels.pop(channel, None)
                    self._send_nowait(channel, b"")  # the close is answered with a close
        except asyncio.CancelledError:
            reason = "closed by the host"
            raise
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except (OSError, wire.FrameError) as exc:
            reason = f"the connection broke: {exc}"
        finally:
            self._lose(reason)

    def _control(self, payload: bytes) -> None:
        try:
            message = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("terminal service %s sent a frame that is not JSON", self.env)
            return
        if not isinstance(message, dict):
            return
        if "id" in message and message.get("id") is not None and "method" not in message:
            future = self._pending.get(message["id"]) if isinstance(message["id"], int) else None
            if future is None or future.done():
                return
            error = message.get("error")
            if isinstance(error, dict):
                future.set_exception(wire.RpcError(int(error.get("code") or wire.INTERNAL), str(error.get("message") or ""), error.get("data")))
            else:
                future.set_result(message.get("result"))
            return
        method = message.get("method")
        if isinstance(method, str):
            params = message.get("params")
            self._notifications.put_nowait((method, params if isinstance(params, dict) else {}))

    async def _notify_loop(self) -> None:
        while True:
            item = await self._notifications.get()
            if item is None:
                return
            if self.on_notification is None:
                continue
            try:
                await self.on_notification(*item)
            except Exception:  # noqa: BLE001 — one event handled badly must not stop the next
                logger.exception("terminal service %s: handling %s failed", self.env, item[0])

    def _lose(self, reason: str) -> None:
        if self._closed.is_set():
            return
        self.lost_reason = reason
        self._closed.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(Unavailable(self.env, "unreachable", reason))
        for channel in list(self._channels.values()):
            channel._finish(reason)
        self._channels.clear()
        self._notifications.put_nowait(None)
        if self._writer is not None:
            self._writer.close()


__all__ = ["Channel", "PtydClient", "Unavailable"]
