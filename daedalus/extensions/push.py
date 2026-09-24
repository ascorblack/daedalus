"""Web Push: the notification channel that reaches a phone or a browser with the app closed.

The router decides *whether* a notification is pushed (presence, quiet hours, the matrix, Telegram
having delivered it already); this module decides nothing of that. It keeps the devices, turns a
notification into the small encrypted message every one of them receives, and sends it without the
router ever waiting for a push service.

Three things here are easy to get wrong, and each is written down where it is done:

- **The lock-screen buttons.** A message carries at most two actions, only the ones the router
  marked quick (a host-level permission never is), and with them a token that lets the service
  worker take exactly those actions on exactly that notification for a day. The service worker has
  no other credential: it cannot read the app's token, and a cookie is not guaranteed.
- **Withdrawing.** When a request that was pushed is answered anywhere else, the devices are told
  to close it, so the phone does not keep offering "Allow" for a request that is gone. Every push
  must show something (``userVisibleOnly``); a withdrawal that finds nothing to close shows
  nothing, which Chrome and Firefox tolerate now and then and Safari punishes by cancelling the
  subscription after a few. So Apple's push service is never sent a withdrawal: an iPhone shows no
  buttons anyway, and tapping a stale notification opens the app, which shows the answer.
- **The keys.** The VAPID key pair is made once and kept; new keys would orphan every subscription.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict
from urllib.parse import urlsplit

import httpx

from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.notify_text import render
from daedalus.host.webpush import (
    PushResult,
    PushSender,
    PushTarget,
    Urgency,
    VapidKeys,
    audience,
    b64url,
    b64url_decode,
    check_subscription_keys,
    topic_for,
)

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.extensions.notifications import NotificationService, NotificationView
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

PAYLOAD_MAX = 3000
"""Bytes of JSON before encryption. Every push service takes this much; the body is cut to fit."""
TTL_NORMAL = 12 * 3600
TTL_URGENT = 24 * 3600
"""How long a push service keeps a message for a device that is off. An urgent request is still
worth hearing about the next morning; a finished run twelve hours late is not news."""
PUSH_ACTIONS_MAX = 2
"""Android and desktop Chrome show two buttons. A question with more options shows none rather
than two of them, which would read as if those were the only answers."""
ACTION_TOKEN_TTL = 24 * 3600
FAILING_FOR = timedelta(days=7)
"""A device that has not taken a message for this long is gone (a wiped phone, an old browser profile)."""
DEFAULT_BACKOFF = 60.0
"""Seconds to leave a push service alone after a 429 that did not say for how long."""
SEND_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
ENDPOINT_MAX = 1024
APPLE_PUSH_SUFFIX = ".push.apple.com"
SETTLE_WAIT = 2.0
"""How long the outcome of a send waits for the router to finish writing the notification's delivery record."""


class SubscriptionView(TypedDict):
    id: int
    endpoint: str
    device: str
    user_agent: str
    created_at: str
    last_ok_at: str | None
    failures: int
    last_error: str | None
    apple: bool


class PushRefused(ValueError):
    """A subscription that cannot be accepted (a malformed key, an endpoint that is not a push service)."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def is_apple(endpoint: str) -> bool:
    host = (urlsplit(endpoint).hostname or "").lower()
    return host == APPLE_PUSH_SUFFIX[1:] or host.endswith(APPLE_PUSH_SUFFIX)


def check_endpoint(endpoint: str) -> str:
    """An endpoint must be an https URL on a named host.

    The operator hands it over, but the host then posts to it on its own schedule; a loopback or
    private address would turn the push channel into a way to reach the machine's own services.
    """
    endpoint = endpoint.strip()
    if len(endpoint) > ENDPOINT_MAX:
        raise PushRefused("the endpoint is too long")
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise PushRefused("a push endpoint is an https URL")
    if host == "localhost" or host.endswith(".localhost"):
        raise PushRefused("a push endpoint is on the internet, not this machine")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return endpoint
    if not address.is_global:
        raise PushRefused("a push endpoint is on the internet, not this network")
    return endpoint


def _row_view(row: Any) -> SubscriptionView:
    return SubscriptionView(
        id=int(row["id"]),
        endpoint=row["endpoint"],
        device=row["device"],
        user_agent=row["user_agent"],
        created_at=row["created_at"],
        last_ok_at=row["last_ok_at"],
        failures=int(row["failures"]),
        last_error=row["last_error"],
        apple=is_apple(row["endpoint"]),
    )


def message(
    view: NotificationView, *, lang: str, more: int = 0, token: str | None = None, unseen: int = 0,
) -> dict[str, Any]:
    """What the service worker receives for one notification, cut to :data:`PAYLOAD_MAX` bytes of JSON."""
    quick = [a for a in view["actions"] if a.get("quick")]
    actions = [{"id": str(a["id"]), "label": str(a.get("label") or a["id"])[:40]} for a in quick] if len(quick) <= PUSH_ACTIONS_MAX else []
    body = view["body"].strip()
    if more:
        body = f"{body}\n{render('burst', lang, count=more)}".strip()
    data: dict[str, Any] = {
        "v": 1,
        "kind": "show",
        "id": view["id"],
        "title": view["title"],
        "body": body,
        "link": view["link"] or "/app/inbox",
        "tag": f"n{view['id']}",
        "level": view["level"],
        "at": view["updated_at"],
        "actions": actions,
        "unseen": unseen,
    }
    if actions and token:
        data["token"] = token
    encoded = _encode(data)
    if len(encoded) > PAYLOAD_MAX and data["body"]:
        # Measured per character as JSON writes it (a quote is two bytes, a Cyrillic letter two, an
        # emoji four), so the body keeps as much as fits rather than a guess at it.
        text, data["body"] = data["body"], ""
        room = PAYLOAD_MAX - len(_encode(data)) - len("…".encode())
        used = kept = 0
        for char in text:
            cost = len(json.dumps(char, ensure_ascii=False)[1:-1].encode("utf-8"))
            if used + cost > room:
                break
            used += cost
            kept += 1
        data["body"] = text[:kept].rstrip() + "…" if kept else ""
        encoded = _encode(data)
    if len(encoded) > PAYLOAD_MAX:
        data["title"] = data["title"][:120]
    return data


def _encode(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class PushService:
    """The devices, the keys and the sending. Registered with the notifications as the ``push`` channel."""

    name = "push"

    def __init__(
        self,
        db: Database,
        *,
        public_url: Callable[[], str],
        notifications: NotificationService | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.db = db
        self._public_url = public_url
        self.notifications = notifications
        self._client = client
        """Tests hand one in; otherwise each batch opens its own and closes it when done."""
        self._count = 0
        self._keys: VapidKeys | None = None
        self._action_key: bytes | None = None
        self._lock = asyncio.Lock()
        self._not_before: dict[str, float] = {}
        self._actors: dict[int, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- state ------------------------------------------------------------------------------

    async def load(self) -> None:
        row = await self.db.fetchone("SELECT count(*) AS n FROM push_subscriptions")
        self._count = int(row["n"]) if row else 0

    def reason(self) -> str:
        """Why push cannot work here, or "" when it can. A browser subscribes only from a secure
        origin, and a push service links the notification back to the public address."""
        return "" if self._public_url().strip().lower().startswith("https://") else "no_https_url"

    def available(self) -> bool:
        return not self.reason() and self._count > 0

    def subject(self) -> str:
        return audience(self._public_url().strip())

    async def keys(self) -> VapidKeys:
        async with self._lock:
            if self._keys is None:
                stored = await self.db.kv_get("vapid_keys")
                if stored and stored.get("private"):
                    self._keys = VapidKeys.from_private_b64(stored["private"])
                else:
                    self._keys = VapidKeys.generate()
                    await self.db.kv_set("vapid_keys", {"private": self._keys.private_b64, "public": self._keys.public_b64, "created_at": _now()})
            return self._keys

    async def config(self) -> dict[str, Any]:
        reason = self.reason()
        return {"available": not reason, "reason": reason, "public_key": (await self.keys()).public_b64 if not reason else ""}

    # -- the lock-screen token ----------------------------------------------------------------

    async def _key(self) -> bytes:
        async with self._lock:
            if self._action_key is None:
                stored = await self.db.kv_get("notify_action_key")
                if not stored:
                    stored = b64url(secrets.token_bytes(32))
                    await self.db.kv_set("notify_action_key", stored)
                self._action_key = b64url_decode(stored)
            return self._action_key

    async def token_for(self, entry_id: int, now: float | None = None) -> str:
        expiry = int((time.time() if now is None else now) + ACTION_TOKEN_TTL)
        mac = hmac.new(await self._key(), f"{entry_id}.{expiry}".encode(), hashlib.sha256).digest()
        return f"{b64url(mac)}.{expiry}"

    async def verify_token(self, entry_id: int, token: str, now: float | None = None) -> bool:
        """Whether ``token`` was minted for this notification and has not expired. It authorises the
        actions marked quick on that one notification and nothing else; the caller enforces the rest."""
        mac_text, _, expiry_text = (token or "").partition(".")
        if not mac_text or not expiry_text.isdigit():
            return False
        expiry = int(expiry_text)
        if expiry < (time.time() if now is None else now):
            return False
        expected = b64url(hmac.new(await self._key(), f"{entry_id}.{expiry}".encode(), hashlib.sha256).digest())
        return hmac.compare_digest(expected, mac_text)

    def note_actor(self, entry_id: int, endpoint: str | None) -> None:
        """The device that answered from its lock screen closed its own notification; the withdrawal skips it."""
        if endpoint:
            self._actors[entry_id] = endpoint
            while len(self._actors) > 256:
                self._actors.pop(next(iter(self._actors)))

    # -- devices ------------------------------------------------------------------------------

    async def subscribe(self, endpoint: str, p256dh: str, auth: str, *, device: str = "", user_agent: str = "") -> SubscriptionView:
        """Add a device, or refresh the keys of one already known by its endpoint."""
        endpoint = check_endpoint(endpoint)
        try:
            check_subscription_keys(p256dh, auth)
        except ValueError as exc:
            raise PushRefused(str(exc)) from exc
        await self.db.execute(
            "INSERT INTO push_subscriptions(endpoint, p256dh, auth, user_agent, device, created_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth,"
            " user_agent = excluded.user_agent, device = CASE WHEN excluded.device != '' THEN excluded.device ELSE device END,"
            " failures = 0, last_error = NULL",
            (endpoint, p256dh.strip(), auth.strip(), user_agent[:300], device.strip()[:80], _now()),
        )
        await self.load()
        row = await self.db.fetchone("SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        assert row is not None
        return _row_view(row)

    async def renew(self, old_endpoint: str, old_auth: str, endpoint: str, p256dh: str, auth: str) -> SubscriptionView:
        """Replace a subscription the browser renewed on its own, while no page was open to sign in.

        The service worker has no credential, so the proof is the old subscription's authentication
        secret: sixteen random bytes only that browser and this host ever held.
        """
        row = await self.db.fetchone("SELECT * FROM push_subscriptions WHERE endpoint = ?", (old_endpoint.strip(),))
        if row is None or not hmac.compare_digest(str(row["auth"]), old_auth.strip()):
            raise PermissionError("no such subscription")
        view = await self.subscribe(endpoint, p256dh, auth, device=row["device"], user_agent=row["user_agent"])
        if old_endpoint.strip() != view["endpoint"]:
            await self.db.execute("DELETE FROM push_subscriptions WHERE id = ?", (int(row["id"]),))
            await self.load()
        return view

    async def devices(self) -> list[SubscriptionView]:
        return [_row_view(r) for r in await self.db.fetchall("SELECT * FROM push_subscriptions ORDER BY id")]

    async def remove(self, subscription_id: int) -> bool:
        async with self.db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (subscription_id,))
            removed = cursor.rowcount > 0
            await cursor.close()
        await self.load()
        return removed

    # -- sending ------------------------------------------------------------------------------

    async def deliver(self, view: NotificationView, *, more: int) -> Any:
        """The channel's send: start it and return at once, so a slow push service never holds the router.

        The outcome is written into the notification's ``delivered`` when the sends come back. The
        test notification is the exception: the Settings button is there to say which device got it.
        """
        rows = await self.db.fetchall("SELECT * FROM push_subscriptions ORDER BY id")
        if not rows or self.reason():
            return "skipped: no device"
        lang = self.notifications.language() if self.notifications is not None else ""
        unseen = (await self.notifications.summary())["unseen"] if self.notifications is not None else 0
        has_quick = any(a.get("quick") for a in view["actions"])
        token = await self.token_for(view["id"]) if has_quick else None
        payload = _encode(message(view, lang=lang, more=more, token=token, unseen=unseen))
        urgent = view["level"] == "urgent"
        topic = topic_for(view["dedupe_key"] or f"n{view['id']}")
        send = self._send_all(rows, payload, ttl=TTL_URGENT if urgent else TTL_NORMAL, urgency="high" if urgent else "normal", topic=topic)
        if view["source"] == "test":
            results = await send
            return {"devices": [{"id": int(r["id"]), "device": r["device"] or _short(r["endpoint"]), "outcome": _outcome(res)} for r, res in results]}
        self._spawn(self._settle(view["id"], send), f"push:{view['id']}")
        return {"queued": len(rows)}

    async def withdraw(self, entry_id: int) -> int:
        """Tell the devices a pushed request was answered elsewhere, so they close it. Returns how many were told."""
        if self.notifications is None or self.reason():
            return 0
        view = await self.notifications.get(entry_id)
        pushed = view["delivered"].get("push") if view is not None else None
        if view is None or not isinstance(pushed, dict) or not (pushed.get("queued") or pushed.get("sent")):
            return 0
        actor = self._actors.pop(entry_id, None)
        rows = [r for r in await self.db.fetchall("SELECT * FROM push_subscriptions ORDER BY id") if not is_apple(r["endpoint"]) and r["endpoint"] != actor]
        if not rows:
            return 0
        unseen = (await self.notifications.summary())["unseen"]
        payload = _encode({"v": 1, "kind": "withdraw", "id": entry_id, "tag": f"n{entry_id}", "unseen": unseen})
        # The same topic as the message it withdraws: a device that has not received that message
        # yet gets this in its place, instead of a buzz for something already answered.
        topic = topic_for(view["dedupe_key"] or f"n{entry_id}")
        self._spawn(self._send_all(rows, payload, ttl=TTL_NORMAL, urgency="normal", topic=topic), f"push-withdraw:{entry_id}")
        return len(rows)

    async def on_resolved(self, event: AppEvent) -> None:
        if event.type == "notify.resolved":
            await self.withdraw(int(event.payload["id"]))

    def _spawn(self, work: Any, name: str) -> None:
        task = asyncio.create_task(work, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for the sends in flight; tests use it, and so does a clean shutdown."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _send_all(self, rows: Sequence[Any], payload: bytes, *, ttl: int, urgency: Urgency, topic: str) -> list[tuple[Any, PushResult]]:
        keys = await self.keys()
        now = time.monotonic()
        client = self._client or httpx.AsyncClient(timeout=SEND_TIMEOUT, follow_redirects=False)
        try:
            sender = PushSender(client, keys, self.subject())

            async def one(row: Any) -> tuple[Any, PushResult]:
                service = audience(row["endpoint"])
                if self._not_before.get(service, 0.0) > now:
                    return row, PushResult(429, retry_after=self._not_before[service] - now, error="waiting: the push service asked for a pause")
                try:
                    result = await sender.send(PushTarget(row["endpoint"], row["p256dh"], row["auth"]), payload, ttl=ttl, urgency=urgency, topic=topic)
                except Exception as exc:  # noqa: BLE001 — one device's failure is its own, the rest still get the message
                    logger.warning("push to subscription %s failed", row["id"], exc_info=True)
                    result = PushResult(0, error=f"{type(exc).__name__}: {exc}"[:300])
                if result.status == 429:
                    self._not_before[service] = time.monotonic() + (result.retry_after if result.retry_after is not None else DEFAULT_BACKOFF)
                return row, result

            results = await asyncio.gather(*(one(r) for r in rows))
        finally:
            if self._client is None:
                await client.aclose()
        await self._record(results)
        return list(results)

    async def _record(self, results: Sequence[tuple[Any, PushResult]]) -> None:
        now = _now()
        cutoff = (datetime.now(UTC) - FAILING_FOR).isoformat()
        async with self.db.transaction() as conn:
            for row, result in results:
                if result.ok:
                    await conn.execute("UPDATE push_subscriptions SET last_ok_at = ?, failures = 0, last_error = NULL WHERE id = ?", (now, row["id"]))
                elif result.gone:
                    await conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (row["id"],))
                elif result.error.startswith("waiting:"):
                    continue  # not sent at all, so not a failure of the device
                else:
                    await conn.execute("UPDATE push_subscriptions SET failures = failures + 1, last_error = ? WHERE id = ?", (result.error[:300], row["id"]))
            await conn.execute("DELETE FROM push_subscriptions WHERE failures > 0 AND COALESCE(last_ok_at, created_at) < ?", (cutoff,))
        await self.load()

    async def _settle(self, entry_id: int, send: Any) -> None:
        """Write how the sends went into the notification, once the router has written its own record.

        The router stores ``delivered`` after this channel returns, and it stores the whole object;
        written before that, the outcome would be overwritten with "queued".
        """
        results = await send
        outcome = {"sent": sum(1 for _, r in results if r.ok)}
        failed = [r for _, r in results if not r.ok]
        if failed:
            outcome["failed"] = len(failed)
        deadline = time.monotonic() + SETTLE_WAIT
        while True:
            row = await self.db.fetchone("SELECT json_extract(delivered_json, '$.push.queued') AS queued FROM notifications WHERE id = ?", (entry_id,))
            if row is None:
                return
            if row["queued"] is not None or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        await self.db.execute(
            "UPDATE notifications SET delivered_json = json_set(delivered_json, '$.push', json(?)) WHERE id = ?",
            (json.dumps(outcome), entry_id),
        )

    async def status(self) -> dict[str, Any]:
        """For the doctor: keys, address, devices and the failing ones."""
        stored = await self.db.kv_get("vapid_keys")
        devices = await self.devices()
        return {
            "keys": bool(stored and stored.get("private")),
            "reason": self.reason(),
            "devices": len(devices),
            "failing": sum(1 for d in devices if d["failures"]),
        }


def _short(endpoint: str) -> str:
    return urlsplit(endpoint).hostname or "device"


def _outcome(result: PushResult) -> str:
    if result.ok:
        return "sent"
    if result.gone:
        return "gone: removed"
    return f"failed: {result.error or result.status}"


async def install(app: Application) -> list[asyncio.Task[None]]:
    assert app.manager is not None
    service = PushService(app.db, public_url=lambda: app.settings.miniapp_public_url, notifications=app.notifications)
    await service.load()
    app.extensions["push"] = service
    if app.notifications is not None:
        app.notifications.register_channel(service)
    return [app.manager.bus.on(EventFilter(types=("notify.resolved",)), service.on_resolved, name="push-withdraw")]


__all__ = [
    "PAYLOAD_MAX",
    "PushRefused",
    "PushService",
    "SubscriptionView",
    "check_endpoint",
    "install",
    "is_apple",
    "message",
]
