"""Signing in on the site: with Telegram's Login Widget, with a pairing link, or with a passkey."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cbor2
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import (
    SESSION_COOKIE,
    build_app,
    session_cookie_value,
    validate_login_widget,
    verify_session_cookie,
)
from daedalus.stores import pairing, passkeys
from daedalus.stores.database import Database

TOKEN = "123456:ABC-DEF"


def _signed(fields: dict[str, object]) -> dict[str, object]:
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(TOKEN.encode()).digest()
    return {**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()}


def test_login_widget_data_is_verified_against_the_bot_token() -> None:
    fields = {"id": 42, "first_name": "A", "username": "a", "auth_date": int(time.time())}
    assert validate_login_widget(_signed(fields), TOKEN)["id"] == 42
    with pytest.raises(ValueError, match="bad signature"):
        validate_login_widget({**_signed(fields), "id": 43}, TOKEN)
    with pytest.raises(ValueError, match="bad signature"):
        validate_login_widget(_signed(fields), "other-token")
    with pytest.raises(ValueError, match="expired"):
        validate_login_widget(_signed({**fields, "auth_date": int(time.time()) - 2 * 86400}), TOKEN)
    with pytest.raises(ValueError, match="incomplete"):
        validate_login_widget({"id": 42}, TOKEN)


def test_session_cookie_round_trip_and_forgery() -> None:
    secret = b"s" * 32
    value = session_cookie_value(secret, 42)
    assert verify_session_cookie(secret, value) == 42
    assert verify_session_cookie(b"t" * 32, value) is None
    assert verify_session_cookie(secret, value[:-1] + ("0" if value[-1] != "0" else "1")) is None
    assert verify_session_cookie(secret, "garbage") is None
    assert verify_session_cookie(secret, session_cookie_value(secret, 42, ttl=-1)) is None
    assert verify_session_cookie(secret, session_cookie_value(secret, 0)) == 0  # the owner of an installation with no Telegram


# -- pairing links and passkeys: signing in with no Telegram at all -----------------------------


class _Bot:
    def __init__(self, username: str) -> None:
        self.username = username

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(username=self.username)


def _app(settings: Settings, db: Database, *, bot: str | None = None) -> SimpleNamespace:
    """Stand-in for daedalus.app.Application covering what the auth routes read."""
    front = SimpleNamespace(bot=_Bot(bot)) if bot else None
    return SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=SimpleNamespace(), front=front, extensions={}, guard=None)


@pytest.fixture
async def client(settings: Settings, db: Database) -> Any:
    settings.telegram_bot_token = ""
    settings.owner_user_id = 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


H = {"X-Daedalus-Token": "tok"}


async def test_auth_config_names_the_ways_in_and_never_needs_a_bot(settings: Settings, db: Database) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        body = (await c.get("/api/auth/config")).json()
    assert body == {"telegram": None, "passkeys": 0, "pairing": False}
    await pairing.mint(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db, bot="daedalus_bot"), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        body = (await c.get("/api/auth/config")).json()
    assert body == {"telegram": {"bot_username": "daedalus_bot"}, "passkeys": 0, "pairing": True}


async def test_a_pairing_link_opens_the_app_once(client: httpx.AsyncClient, db: Database) -> None:
    code = await pairing.mint(db)
    response = await client.get("/api/auth/pair", params={"code": code})
    assert response.status_code == 303 and response.headers["location"] == "/app/"
    assert (await client.get("/api/auth/me")).json() == {"user_id": 1, "via": "cookie"}
    # Spent: the same link a second time is refused, and so is one that was never minted.
    assert (await client.get("/api/auth/pair", params={"code": code})).status_code == 403
    assert (await client.get("/api/auth/pair", params={"code": "not-a-code"})).status_code == 403


async def test_an_installation_with_no_telegram_account_still_holds_a_session(settings: Settings, db: Database) -> None:
    """Owner id zero is what an installation without Telegram has; a cookie must still mean the owner."""
    settings.telegram_bot_token = ""
    settings.owner_user_id = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        assert (await c.get("/api/auth/pair", params={"code": await pairing.mint(db)})).status_code == 303
        assert (await c.get("/api/auth/me")).json() == {"user_id": 0, "via": "cookie"}
        assert (await c.post("/api/auth/passkeys/register/begin")).status_code == 200


async def test_using_a_link_revokes_the_ones_still_outstanding(db: Database) -> None:
    stale = await pairing.mint(db)
    fresh = await pairing.mint(db)
    assert await pairing.outstanding(db) == 2
    assert await pairing.redeem(db, fresh)
    assert not await pairing.redeem(db, stale)
    assert await pairing.outstanding(db) == 0


async def test_a_code_expires(db: Database) -> None:
    code = await pairing.mint(db, ttl_minutes=-1)
    assert await pairing.outstanding(db) == 0
    assert not await pairing.redeem(db, code)


async def test_the_printed_link_is_readable_by_its_owner_only(db: Database, tmp_path: Path) -> None:
    url = await pairing.announce(db, tmp_path / "state", "https://example.org/")
    path = tmp_path / "state" / pairing.URL_FILE
    assert path.read_text(encoding="utf-8").strip() == url
    assert path.stat().st_mode & 0o777 == 0o600
    assert url.startswith("https://example.org/api/auth/pair?code=")


async def test_the_session_cookie_is_secure_only_when_the_browser_came_over_tls(client: httpx.AsyncClient, db: Database) -> None:
    plain = await client.get("/api/auth/pair", params={"code": await pairing.mint(db)})
    assert "secure" not in plain.headers["set-cookie"].lower()  # http on the machine itself would never get it back
    secure = await client.get("/api/auth/pair", params={"code": await pairing.mint(db)}, headers={"x-forwarded-proto": "https"})
    assert "secure" in secure.headers["set-cookie"].lower()


async def test_the_cookie_secret_survives_the_bot_token_and_changes_with_the_api_token(settings: Settings, db: Database) -> None:
    settings.telegram_bot_token = ""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        await c.get("/api/auth/pair", params={"code": await pairing.mint(db)})
        cookie = c.cookies[SESSION_COOKIE]
        assert (await c.get("/api/auth/me")).status_code == 200
    # A rotated API token invalidates what the old one signed, and the per-installation secret is kept.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "other")), base_url="http://test") as c:  # type: ignore[arg-type]
        c.cookies.set(SESSION_COOKIE, cookie)
        assert (await c.get("/api/auth/me")).status_code == 401
    assert await db.kv_get("session_secret")


class SoftAuthenticator:
    """What a security key does to a challenge, in a few lines and with no hardware.

    It is the only way to exercise both halves of a ceremony: the options the server generates are
    meaningless until something signs them and the server accepts the signature.
    """

    def __init__(self, rp_id: str, origin: str) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.sign_count = 0

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}).encode()

    def _authenticator_data(self, flags: int, attested: bytes = b"") -> bytes:
        return hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + self.sign_count.to_bytes(4, "big") + attested

    def create(self, options: dict[str, Any]) -> dict[str, Any]:
        client_data = self._client_data("webauthn.create", options["challenge"])
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
        attested = bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cose
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": self._authenticator_data(0x45, attested)})
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {"clientDataJSON": bytes_to_base64url(client_data), "attestationObject": bytes_to_base64url(attestation), "transports": ["internal"]},
        }

    def get(self, options: dict[str, Any]) -> dict[str, Any]:
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", options["challenge"])
        authenticator_data = self._authenticator_data(0x05)
        signature = self.key.sign(authenticator_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(authenticator_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(b"owner"),
            },
        }


def _soft(settings: Settings) -> SoftAuthenticator:
    rp = passkeys.relying_party(settings.miniapp_public_url, settings.api_port)
    return SoftAuthenticator(rp.rp_id, rp.origins[0])


def test_the_relying_party_follows_the_public_address() -> None:
    assert passkeys.relying_party("https://agent.example.org/app/", 8765) == passkeys.RelyingParty("agent.example.org", ("https://agent.example.org",))
    # No public address: the app is opened on the machine itself, where only "localhost" is a name a key can belong to.
    local = passkeys.relying_party("", 8765)
    assert local.rp_id == "localhost" and local.origins == ("http://localhost:8765", "http://127.0.0.1:8765")


async def test_a_passkey_is_enrolled_and_then_signs_the_browser_in(client: httpx.AsyncClient, settings: Settings) -> None:
    soft = _soft(settings)
    options = (await client.post("/api/auth/passkeys/register/begin", headers=H)).json()
    assert options["rp"]["id"] == "localhost" and options["authenticatorSelection"]["residentKey"] == "preferred"
    enrolled = await client.post("/api/auth/passkeys/register/finish", json={"credential": soft.create(options), "name": "a laptop"}, headers=H)
    assert enrolled.status_code == 200, enrolled.text
    assert [k["name"] for k in enrolled.json()["passkeys"]] == ["a laptop"]
    assert (await client.get("/api/auth/config")).json()["passkeys"] == 1

    # A login needs no username: the options carry no allow-list, and the key says which one it is.
    options = (await client.post("/api/auth/passkeys/login/begin")).json()
    assert not options.get("allowCredentials")
    done = await client.post("/api/auth/passkeys/login/finish", json={"credential": soft.get(options)})
    assert done.status_code == 200, done.text
    assert (await client.get("/api/auth/me")).json() == {"user_id": 1, "via": "cookie"}
    listed = (await client.get("/api/auth/passkeys", headers=H)).json()
    assert listed[0]["last_used_at"] and listed[0]["transports"] == ["internal"]

    assert (await client.delete(f"/api/auth/passkeys/{listed[0]['id']}", headers=H)).json()["passkeys"] == []
    assert (await client.delete(f"/api/auth/passkeys/{listed[0]['id']}", headers=H)).status_code == 404


async def test_a_signature_over_the_wrong_challenge_is_refused(client: httpx.AsyncClient, settings: Settings) -> None:
    soft = _soft(settings)
    options = (await client.post("/api/auth/passkeys/register/begin", headers=H)).json()
    await client.post("/api/auth/passkeys/register/finish", json={"credential": soft.create(options), "name": "a laptop"}, headers=H)
    await client.post("/api/auth/passkeys/login/begin")
    forged = soft.get({"challenge": bytes_to_base64url(b"a challenge nobody issued")})
    refused = await client.post("/api/auth/passkeys/login/finish", json={"credential": forged})
    assert refused.status_code == 403 and "refused" in refused.json()["detail"]
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_a_passkey_this_installation_never_saw_is_refused(client: httpx.AsyncClient, settings: Settings) -> None:
    soft = _soft(settings)
    other = _soft(settings)
    options = (await client.post("/api/auth/passkeys/register/begin", headers=H)).json()
    await client.post("/api/auth/passkeys/register/finish", json={"credential": soft.create(options), "name": "a laptop"}, headers=H)
    options = (await client.post("/api/auth/passkeys/login/begin")).json()
    assert (await client.post("/api/auth/passkeys/login/finish", json={"credential": other.get(options)})).status_code == 403


async def test_a_finish_without_a_begin_has_nothing_to_check_against(client: httpx.AsyncClient, settings: Settings) -> None:
    soft = _soft(settings)
    options = (await client.post("/api/auth/passkeys/register/begin", headers=H)).json()
    assert (await client.post("/api/auth/passkeys/register/finish", json={"credential": soft.create(options)}, headers=H)).status_code == 200
    # The challenge is spent with the call that used it; replaying the same credential finds none.
    assert (await client.post("/api/auth/passkeys/register/finish", json={"credential": soft.create(options)}, headers=H)).status_code == 400


async def test_registering_and_listing_need_a_signed_in_browser(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/auth/passkeys/register/begin")).status_code == 401
    assert (await client.get("/api/auth/passkeys")).status_code == 401
    assert (await client.delete("/api/auth/passkeys/1")).status_code == 401
    assert (await client.post("/api/auth/passkeys/login/begin")).status_code == 404  # public, but nothing is enrolled
