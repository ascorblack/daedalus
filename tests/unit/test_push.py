"""Web Push as a notification channel: the devices, what a message carries, the lock-screen token,
withdrawing an answered request, and the routes. Every push service here is a fake on an
``httpx.MockTransport``; each message is opened with the browser's own key, as a phone would."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from daedalus.config import Settings
from daedalus.extensions import push as push_module
from daedalus.extensions.api import build_app
from daedalus.extensions.notifications import Action, Draft, NotificationService, NotificationView
from daedalus.extensions.push import PAYLOAD_MAX, PushRefused, PushService, check_endpoint, message
from daedalus.host.events import EventFilter
from daedalus.host.presence import PresenceReport
from daedalus.stores.database import Database
from tests.support.waiting import until_await
from tests.support.webpush_receiver import Subscriber
from tests.unit.test_notification_router import HEAD, Host, entries, permission_payload
from tests.unit.test_session_runner import ScriptedProvider, _manager

PUBLIC = "https://daedalus.example.com/app/"
ANDROID = "https://fcm.example.com/fcm/send/android-1"
LAPTOP = "https://push.example.com/wpush/v2/laptop-1"
IPHONE = "https://web.push.apple.com/QGx1bmNo"


class FakePushService:
    """Every push service at once: records what it was sent, answers what each endpoint is told to."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, Callable[[], httpx.Response]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answers.get(str(request.url), lambda: httpx.Response(201))()

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def to(self, endpoint: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == endpoint]


def view(**overrides: Any) -> NotificationView:
    base: dict[str, Any] = {
        "id": 7, "at": "2026-09-24T12:00:00+00:00", "updated_at": "2026-09-24T12:00:00+00:00", "category": "permission",
        "kind": "policy", "level": "urgent", "tone": "warning", "title": "Bakery is waiting for permission",
        "body": "Exec: curl https://other.example/", "link": "/app/agents/a1b2c3d4e5f6", "session_id": "a1b2c3d4e5f6",
        "run_id": None, "project_id": None, "staff_id": None, "terminal_id": None, "source": "policy",
        "dedupe_key": "policy:a1b2c3d4e5f6:0123456789ab", "request_ref": "policy:a1b2c3d4e5f6:0123456789ab", "count": 1,
        "actions": [{"id": "allow", "label": "Allow", "style": "primary", "quick": True},
                    {"id": "deny", "label": "Deny", "style": "default", "quick": True},
                    {"id": "open", "label": "Open", "style": "ghost", "quick": False}],
        "seen": False, "resolved": None, "needs_you": True, "delivered": {},
    }
    return NotificationView(**{**base, **overrides})  # type: ignore[typeddict-item]


async def service_with(db: Database, fake: FakePushService, *, url: str = PUBLIC, notifications: NotificationService | None = None) -> PushService:
    service = PushService(db, public_url=lambda: url, notifications=notifications, client=fake.client())
    await service.load()
    return service


# -- what a message carries --------------------------------------------------------------------


def test_a_message_carries_only_quick_actions_and_the_token_only_with_them() -> None:
    data = message(view(), lang="en", token="tok.1", unseen=3)
    assert [a["id"] for a in data["actions"]] == ["allow", "deny"] and data["token"] == "tok.1"
    assert (data["tag"], data["link"], data["level"], data["unseen"], data["kind"]) == ("n7", "/app/agents/a1b2c3d4e5f6", "urgent", 3, "show")
    # A host-level permission: no quick action, so no button and no token, only a tap into the app.
    elevated = [dict(a, quick=False) for a in view()["actions"]]
    data = message(view(actions=elevated), lang="en", token="tok.1")
    assert data["actions"] == [] and "token" not in data
    # Four options would show as two, which reads as if there were only two answers.
    four = [{"id": f"answer:{n}", "label": f"option {n}", "style": "default", "quick": True} for n in range(4)]
    assert message(view(actions=four), lang="en", token="t")["actions"] == []


def test_a_message_never_exceeds_its_size_and_says_how_many_more_there_were() -> None:
    for body in ("ж" * 20_000, "x" * 20_000, '"\n' * 5_000, "🍉" * 5_000):
        data = message(view(body=body), lang="ru", token="t" * 60, unseen=10)
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        assert len(encoded) <= PAYLOAD_MAX and data["body"].endswith("…") and len(encoded) > PAYLOAD_MAX - 16
    assert message(view(body="done"), lang="ru", more=3)["body"] == "done\nи ещё 3"
    assert message(view(link=""), lang="en")["link"] == "/app/inbox"


# -- devices ----------------------------------------------------------------------------------


def test_an_endpoint_must_be_a_push_service_on_the_internet() -> None:
    assert check_endpoint(f" {ANDROID} ") == ANDROID
    for bad in ("http://push.example.com/x", "https://localhost/x", "https://127.0.0.1/x", "https://10.0.0.8/x", "https://[::1]/x", "https://svc.localhost/x", "push.example.com/x"):
        with pytest.raises(PushRefused):
            check_endpoint(bad)


async def test_devices_are_added_refreshed_and_removed(db: Database) -> None:
    fake = FakePushService()
    service = await service_with(db, fake)
    phone = Subscriber()
    assert not service.available()
    first = await service.subscribe(ANDROID, phone.p256dh, phone.auth_b64, device="Pixel", user_agent="Mozilla/5.0 (Linux; Android 15)")
    assert service.available() and first["device"] == "Pixel" and not first["apple"]
    renewed = Subscriber()
    again = await service.subscribe(ANDROID, renewed.p256dh, renewed.auth_b64)
    assert again["id"] == first["id"] and again["device"] == "Pixel"  # an upsert by endpoint keeps the name
    assert len(await service.devices()) == 1
    with pytest.raises(PushRefused):
        await service.subscribe(LAPTOP, "AAAA", phone.auth_b64)
    assert (await service.subscribe(IPHONE, phone.p256dh, phone.auth_b64))["apple"]
    assert await service.remove(first["id"]) and not await service.remove(first["id"])
    assert [d["endpoint"] for d in await service.devices()] == [IPHONE]


async def test_push_needs_the_public_https_address(db: Database) -> None:
    fake = FakePushService()
    service = await service_with(db, fake, url="http://127.0.0.1:8080/app/")
    phone = Subscriber()
    await service.subscribe(ANDROID, phone.p256dh, phone.auth_b64)
    assert await service.config() == {"available": False, "reason": "no_https_url", "public_key": ""}
    assert not service.available() and await service.deliver(view(), more=0) == "skipped: no device"
    assert fake.requests == []


async def test_the_keys_are_made_once_and_kept(db: Database) -> None:
    first = (await (await service_with(db, FakePushService())).config())["public_key"]
    second = (await (await service_with(db, FakePushService())).config())["public_key"]
    assert first and first == second and (await db.kv_get("vapid_keys"))["public"] == first


async def test_a_renewal_needs_the_old_subscriptions_secret(db: Database) -> None:
    service = await service_with(db, FakePushService())
    old, new = Subscriber(), Subscriber()
    await service.subscribe(ANDROID, old.p256dh, old.auth_b64, device="Pixel")
    with pytest.raises(PermissionError):
        await service.renew(ANDROID, new.auth_b64, ANDROID + "-2", new.p256dh, new.auth_b64)
    with pytest.raises(PermissionError):
        await service.renew(LAPTOP, old.auth_b64, ANDROID + "-2", new.p256dh, new.auth_b64)
    done = await service.renew(ANDROID, old.auth_b64, ANDROID + "-2", new.p256dh, new.auth_b64)
    assert done["device"] == "Pixel" and [d["endpoint"] for d in await service.devices()] == [ANDROID + "-2"]


# -- the lock-screen token --------------------------------------------------------------------


async def test_the_token_opens_one_notification_until_it_expires(db: Database) -> None:
    service = await service_with(db, FakePushService())
    token = await service.token_for(7, now=1000.0)
    assert await service.verify_token(7, token, now=1000.0 + 3600)
    assert not await service.verify_token(8, token, now=1000.0)
    assert not await service.verify_token(7, token, now=1000.0 + 24 * 3600 + 1)
    mac, expiry = token.split(".")
    assert not await service.verify_token(7, f"{mac}.{int(expiry) + 3600}", now=1000.0)  # a longer life is not for the asking
    assert not await service.verify_token(7, "", now=1000.0) and not await service.verify_token(7, "garbage", now=1000.0)
    other = await service_with(db, FakePushService())
    assert await other.verify_token(7, token, now=1000.0)  # the key is the installation's, not the process's


# -- sending ----------------------------------------------------------------------------------


async def test_every_device_gets_its_own_message_and_the_outcome_is_recorded(db: Database) -> None:
    fake = FakePushService()
    service = await service_with(db, fake)
    phone, laptop = Subscriber(), Subscriber()
    await service.subscribe(ANDROID, phone.p256dh, phone.auth_b64)
    await service.subscribe(LAPTOP, laptop.p256dh, laptop.auth_b64)
    notes = NotificationService(db)
    entry = await notes.post(Draft("permission", "Bakery is waiting for permission", "Exec: curl", request_ref="policy:s1:0123456789ab",
                                   actions=(Action("allow", "Allow", "primary", quick=True), Action("deny", "Deny", quick=True))))
    assert entry is not None
    assert await service.deliver(entry, more=0) == {"queued": 2}
    await db.execute("UPDATE notifications SET delivered_json = ? WHERE id = ?", (json.dumps({"push": {"queued": 2}}), entry["id"]))
    await service.drain()
    [to_phone], [to_laptop] = fake.to(ANDROID), fake.to(LAPTOP)
    shown = json.loads(phone.open(to_phone.content))
    assert json.loads(laptop.open(to_laptop.content))["id"] == shown["id"] == entry["id"]
    assert [a["id"] for a in shown["actions"]] == ["allow", "deny"] and await service.verify_token(entry["id"], shown["token"])
    assert to_phone.headers["Urgency"] == "high" and to_phone.headers["TTL"] == str(24 * 3600) and len(to_phone.headers["Topic"]) == 32
    assert (await notes.get(entry["id"]))["delivered"]["push"] == {"sent": 2}  # type: ignore[index]
    assert all(d["last_ok_at"] for d in await service.devices())


async def test_a_gone_device_is_forgotten_a_failing_one_counted_and_a_week_of_failures_ends_it(db: Database) -> None:
    fake = FakePushService()
    service = await service_with(db, fake)
    a, b, c = Subscriber(), Subscriber(), Subscriber()
    for endpoint, who in ((ANDROID, a), (LAPTOP, b), (IPHONE, c)):
        await service.subscribe(endpoint, who.p256dh, who.auth_b64)
    fake.answers[ANDROID] = lambda: httpx.Response(410)
    fake.answers[LAPTOP] = lambda: httpx.Response(500, text="try later")
    note = view(level="normal", actions=[], request_ref=None)
    await service.deliver(note, more=0)
    await service.drain()
    devices = {d["endpoint"]: d for d in await service.devices()}
    assert ANDROID not in devices and devices[LAPTOP]["failures"] == 1 and "500" in (devices[LAPTOP]["last_error"] or "")
    assert devices[IPHONE]["failures"] == 0
    assert fake.to(LAPTOP)[0].headers["Urgency"] == "normal" and fake.to(LAPTOP)[0].headers["TTL"] == str(12 * 3600)
    await db.execute("UPDATE push_subscriptions SET created_at = '2026-01-01T00:00:00+00:00' WHERE endpoint = ?", (LAPTOP,))
    await service.deliver(note, more=0)
    await service.drain()
    assert [d["endpoint"] for d in await service.devices()] == [IPHONE]


async def test_a_push_service_that_asks_for_a_pause_is_left_alone(db: Database) -> None:
    fake = FakePushService()
    service = await service_with(db, fake)
    phone = Subscriber()
    await service.subscribe(ANDROID, phone.p256dh, phone.auth_b64)
    fake.answers[ANDROID] = lambda: httpx.Response(429, headers={"Retry-After": "600"})
    note = view(level="normal", actions=[], request_ref=None)
    await service.deliver(note, more=0)
    await service.drain()
    await service.deliver(note, more=0)
    await service.drain()
    assert len(fake.to(ANDROID)) == 1  # the second message waited out the pause instead of asking again
    assert (await service.devices())[0]["failures"] == 1


# -- through the router ---------------------------------------------------------------------------


async def routed(settings: Settings, db: Database) -> tuple[Any, NotificationService, PushService, FakePushService]:
    manager = await _manager(settings, db, ScriptedProvider([]))
    notes = NotificationService(db, manager.bus, presence=manager.presence)
    fake = FakePushService()
    service = await service_with(db, fake, notifications=notes)
    notes.register_channel(service)
    return manager, notes, service, fake


async def test_a_permission_request_reaches_the_phone_with_its_buttons_and_nothing_telegram_carried_is_pushed(settings: Settings, db: Database) -> None:
    manager, notes, service, fake = await routed(settings, db)
    phone = Subscriber()
    try:
        await service.subscribe(ANDROID, phone.p256dh, phone.auth_b64)
        entry = await notes.post(Draft("permission", "Bakery is waiting for permission", "Exec: curl", session_id="s1", request_ref="policy:s1:0123456789ab",
                                       actions=(Action("allow", "Allow", "primary", quick=True), Action("deny", "Deny", quick=True), Action("open", "Open", "ghost"))))
        assert entry is not None and entry["delivered"]["push"] == {"queued": 1}
        await service.drain()
        shown = json.loads(phone.open(fake.to(ANDROID)[0].content))
        assert [a["id"] for a in shown["actions"]] == ["allow", "deny"] and shown["token"]
        assert (await notes.get(entry["id"]))["delivered"]["push"] == {"sent": 1}  # type: ignore[index]
        # The same kind of request, already delivered by Telegram: no second buzz on the phone.
        handled = await notes.post(Draft("permission", "Again", session_id="s2", request_ref="policy:s2:0123456789ab", handled=frozenset({"telegram"})))
        assert handled is not None and handled["delivered"]["push"] == "skipped: sent to Telegram"
        # With a window in front of the operator the phone stays quiet too.
        await manager.presence.report(PresenceReport(client="tab", visible=True, focused=True, sessions=("other",)))
        present = await notes.post(Draft("permission", "Third", session_id="s3", request_ref="policy:s3:0123456789ab"))
        assert present is not None and present["delivered"]["push"] == "skipped: present"
        await service.drain()
        assert len(fake.requests) == 1
    finally:
        await manager.close()


async def test_an_answered_request_is_withdrawn_from_every_device_but_the_one_that_answered_and_never_from_apple(settings: Settings, db: Database) -> None:
    manager, notes, service, fake = await routed(settings, db)
    phone, laptop, iphone = Subscriber(), Subscriber(), Subscriber()
    try:
        for endpoint, who in ((ANDROID, phone), (LAPTOP, laptop), (IPHONE, iphone)):
            await service.subscribe(endpoint, who.p256dh, who.auth_b64)
        ref = "policy:s1:0123456789ab"
        entry = await notes.post(Draft("permission", "Bakery is waiting for permission", session_id="s1", request_ref=ref, dedupe_key=ref,
                                       actions=(Action("allow", "Allow", quick=True), Action("deny", "Deny", quick=True))))
        assert entry is not None
        await service.drain()
        original_topic = fake.to(ANDROID)[0].headers["Topic"]
        fake.requests.clear()
        service.note_actor(entry["id"], ANDROID)  # answered from the Android lock screen
        await notes.resolve(ref, "allow", via="push")
        [resolved] = await manager.bus.replay(0, EventFilter(types=("notify.resolved",)), limit=5)
        assert await service.withdraw(int(resolved.payload["id"])) == 1
        await service.drain()
        assert fake.to(ANDROID) == [] and fake.to(IPHONE) == []
        [to_laptop] = fake.to(LAPTOP)
        assert json.loads(laptop.open(to_laptop.content)) == {"v": 1, "kind": "withdraw", "id": entry["id"], "tag": f"n{entry['id']}", "unseen": 1}
        assert to_laptop.headers["Topic"] == original_topic  # replaces the original if it is still waiting at the push service
        # A request that was never pushed has nothing to withdraw.
        quiet = await notes.post(Draft("permission", "Held back", session_id="s2", request_ref="policy:s2:0123456789ab", handled=frozenset({"telegram"})))
        assert quiet is not None and await service.withdraw(quiet["id"]) == 0
    finally:
        await manager.close()


async def test_the_withdrawal_runs_on_the_bus(settings: Settings, db: Database) -> None:
    manager, notes, service, fake = await routed(settings, db)
    laptop = Subscriber()
    task = manager.bus.on(EventFilter(types=("notify.resolved",)), service.on_resolved, name="push-withdraw")
    try:
        await service.subscribe(LAPTOP, laptop.p256dh, laptop.auth_b64)
        ref = "ask:s1:call-1"
        await notes.post(Draft("question", "Bakery asks you", session_id="s1", request_ref=ref, actions=(Action("answer:0", "Blue", quick=True),)))
        await service.drain()
        await notes.resolve(ref, "answered", via="telegram")

        async def withdrawn() -> bool:
            await service.drain()
            return len(fake.to(LAPTOP)) == 2

        await until_await(withdrawn, "the answered question was withdrawn from the laptop")
        assert json.loads(laptop.open(fake.to(LAPTOP)[1].content))["kind"] == "withdraw"
    finally:
        task.cancel()
        await manager.close()


# -- the routes ---------------------------------------------------------------------------------


async def test_the_routes(settings: Settings, db: Database) -> None:
    settings = settings.model_copy(update={"miniapp_public_url": PUBLIC})
    manager = await _manager(settings, db, ScriptedProvider([]))
    host = Host(settings, db, manager)
    await host.install()
    host.tasks += await push_module.install(host)  # type: ignore[arg-type]
    push: PushService = host.extensions["push"]
    fake = FakePushService()
    push._client = fake.client()
    phone = Subscriber()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(host, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            assert (await client.get("/api/push/config")).status_code == 401
            config = (await client.get("/api/push/config", headers=HEAD)).json()
            assert config["available"] and config["reason"] == "" and len(config["public_key"]) == 87

            body = {"endpoint": ANDROID, "keys": {"p256dh": phone.p256dh, "auth": phone.auth_b64}, "expirationTime": None, "device": "Pixel"}
            assert (await client.post("/api/push/subscriptions", json=body)).status_code == 401
            added = await client.post("/api/push/subscriptions", json=body, headers={**HEAD, "User-Agent": "Mozilla/5.0 (Linux; Android 15)"})
            assert added.status_code == 200 and added.json()["subscription"]["device"] == "Pixel"
            bad = await client.post("/api/push/subscriptions", json={**body, "endpoint": "https://127.0.0.1/x"}, headers=HEAD)
            assert bad.status_code == 400
            [device] = (await client.get("/api/push/subscriptions", headers=HEAD)).json()["subscriptions"]
            assert device["user_agent"].startswith("Mozilla") and "p256dh" not in device and "auth" not in device

            # The browser renewed the subscription with no page open: the old secret is the proof.
            fresh = Subscriber()
            renewal = {"old_endpoint": ANDROID, "old_auth": fresh.auth_b64, "endpoint": ANDROID + "-2", "keys": {"p256dh": fresh.p256dh, "auth": fresh.auth_b64}}
            assert (await client.post("/api/push/subscriptions/renew", json=renewal)).status_code == 403
            renewed = await client.post("/api/push/subscriptions/renew", json={**renewal, "old_auth": phone.auth_b64})
            assert renewed.status_code == 200
            [device] = (await client.get("/api/push/subscriptions", headers=HEAD)).json()["subscriptions"]
            assert device["endpoint"] == ANDROID + "-2" and device["device"] == "Pixel"
            phone = fresh

            # A refused tool call becomes a notification that is pushed with its token.
            state = await manager.create_session("policy", metadata={"telegram_detached": True})
            sid = state.session.id
            manager.config.policy.egress_allow = ["github.com"]
            refused = manager.policy_gate(sid, "run-1").decide("Exec", {"command": "curl https://other.example/"})

            async def pushed() -> bool:
                await push.drain()
                return bool(fake.requests)

            await until_await(pushed, "the refusal was pushed")
            shown = json.loads(phone.open(fake.requests[0].content))
            [entry] = await entries(host.notifications)  # type: ignore[arg-type]
            assert shown["id"] == entry["id"] and [a["id"] for a in shown["actions"]] == ["allow", "deny"]
            url = f"/api/notifications/{entry['id']}/act"
            assert (await client.post(url, json={"action": "allow", "via": "push", "token": "nope.1"})).status_code == 401
            other_token = await push.token_for(entry["id"] + 1)
            assert (await client.post(url, json={"action": "allow", "via": "push", "token": other_token})).status_code == 401
            # The token carries the quick actions and nothing else.
            assert (await client.post(url, json={"action": "open", "via": "push", "token": shown["token"]})).status_code == 400
            done = await client.post(url, json={"action": "allow", "via": "push", "token": shown["token"], "endpoint": ANDROID + "-2"})
            assert done.status_code == 200 and done.json()["resolution"] == "allow"
            assert refused.key in state.metadata["policy_grants"]
            [resolved] = await manager.bus.replay(0, EventFilter(types=("notify.resolved",)), limit=5)
            assert resolved.payload["via"] == "push"
            await push.drain()
            assert len(fake.requests) == 1  # the device that answered is not sent a withdrawal of its own answer

            tested = (await client.post("/api/notifications/test", headers=HEAD)).json()["delivered"]
            assert tested["push"] == {"devices": [{"id": device["id"], "device": "Pixel", "outcome": "sent"}]}

            assert (await client.delete(f"/api/push/subscriptions/{device['id']}", headers=HEAD)).json() == {"deleted": device["id"]}
            assert (await client.delete(f"/api/push/subscriptions/{device['id']}", headers=HEAD)).status_code == 404
    finally:
        await host.close()


async def test_a_host_level_permission_is_never_answerable_from_the_lock_screen(settings: Settings, db: Database) -> None:
    settings = settings.model_copy(update={"miniapp_public_url": PUBLIC})
    manager = await _manager(settings, db, ScriptedProvider([]))
    host = Host(settings, db, manager)
    await host.install()
    host.tasks += await push_module.install(host)  # type: ignore[arg-type]
    push: PushService = host.extensions["push"]
    fake = FakePushService()
    push._client = fake.client()
    phone = Subscriber()
    try:
        await push.subscribe(ANDROID, phone.p256dh, phone.auth_b64)
        await manager.bus.publish("permission.pending", permission_payload("s1", quick=False), session_id="s1")

        async def pushed() -> bool:
            await push.drain()
            return bool(fake.requests)

        await until_await(pushed, "the host-level request was pushed")
        shown = json.loads(phone.open(fake.requests[0].content))
        assert shown["actions"] == [] and "token" not in shown  # a tap opens the app, where it is answered
        [entry] = await entries(host.notifications)  # type: ignore[arg-type]
        forged = await push.token_for(entry["id"])  # even a valid token for it does not reach "allow"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(host, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            answer = await client.post(f"/api/notifications/{entry['id']}/act", json={"action": "allow", "via": "push", "token": forged})
        assert answer.status_code == 400
    finally:
        await host.close()
