"""Web Push on the wire: the encryption against RFC 8291's worked example and an independent
receiver, the VAPID identity RFC 8292 asks for, and what the sender makes of each push service answer."""

from __future__ import annotations

import json
import string
import time

import httpx
import pytest
import respx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from daedalus.host import webpush
from daedalus.host.webpush import PushSender, PushTarget, VapidKeys, b64url_decode, encrypt, topic_for, vapid_header
from tests.support.webpush_receiver import Subscriber, open_aes128gcm, open_webpush, unb64

# RFC 8291, section 5 and appendix A.
PLAINTEXT = b"When I grow up, I want to be a watermelon"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
MESSAGE = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)
ENDPOINT = "https://push.example.com/send/abc123"


def _private(text: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64(text), "big"), ec.SECP256R1())


def test_the_rfc_example_is_reproduced_byte_for_byte() -> None:
    body = encrypt(PLAINTEXT, UA_PUBLIC, AUTH, salt=unb64(SALT), server_key=_private(AS_PRIVATE))
    assert body == unb64(MESSAGE)
    assert len(body) == 144  # the 86-byte header, 41 bytes of text, the delimiter and the 16-byte tag


def test_the_independent_receiver_reads_the_rfc_examples() -> None:
    # The receiver the other tests trust is itself checked against both documents first.
    assert open_webpush(unb64(MESSAGE), _private(UA_PRIVATE), unb64(AUTH)) == PLAINTEXT
    walrus = unb64("I1BsxtFttlv3u_Oo94xnmwAAEAAA-NAVub2qFgBEuQKRapoZu-IxkIva3MEB1PD-ly8Thjg")
    assert open_aes128gcm(walrus, unb64("yqdlZ-tYemfogSmv7Ws5PQ")) == (b"I am the walrus", b"")


def test_a_fresh_message_round_trips_and_never_repeats_its_salt_or_key() -> None:
    browser = Subscriber()
    text = json.dumps({"title": "Ная ждёт разрешения", "body": "npm install grammy"}, ensure_ascii=False).encode()
    first, second = encrypt(text, browser.p256dh, browser.auth_b64), encrypt(text, browser.p256dh, browser.auth_b64)
    assert browser.open(first) == text and browser.open(second) == text
    assert first[:16] != second[:16] and first[21:86] != second[21:86]
    other = Subscriber()
    with pytest.raises(Exception):  # noqa: B017 — any failure will do: a stranger's key must not open it
        other.open(first)


def test_what_cannot_be_encrypted_is_refused() -> None:
    browser = Subscriber()
    with pytest.raises(ValueError, match="at most"):
        encrypt(b"x" * (webpush.MAX_PLAINTEXT + 1), browser.p256dh, browser.auth_b64)
    assert len(encrypt(b"x" * webpush.MAX_PLAINTEXT, browser.p256dh, browser.auth_b64)) == 4096
    with pytest.raises(ValueError):
        webpush.check_subscription_keys("AAAA", browser.auth_b64)
    with pytest.raises(ValueError, match="16 bytes"):
        webpush.check_subscription_keys(browser.p256dh, "c2hvcnQ")
    webpush.check_subscription_keys(browser.p256dh, browser.auth_b64)


def _claims(header: str, keys: VapidKeys) -> dict[str, object]:
    scheme, _, rest = header.partition(" ")
    params = dict(part.strip().split("=", 1) for part in rest.split(","))
    assert scheme == "vapid" and params["k"] == keys.public_b64
    head, claims, signature = params["t"].split(".")
    assert json.loads(b64url_decode(head)) == {"typ": "JWT", "alg": "ES256"}
    raw = b64url_decode(signature)
    assert len(raw) == 64  # r and s, 32 bytes each, as JWS wants them
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(params["k"]))
    public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    return json.loads(b64url_decode(claims))


def test_the_vapid_header_verifies_names_the_push_service_and_expires_soon() -> None:
    keys = VapidKeys.generate()
    now = 1_790_000_000.0
    claims = _claims(vapid_header(ENDPOINT, keys, "https://daedalus.example.com", now), keys)
    assert claims["aud"] == "https://push.example.com" and claims["sub"] == "https://daedalus.example.com"
    assert now < int(claims["exp"]) <= now + 24 * 3600  # type: ignore[call-overload]
    forged = VapidKeys.generate()
    header = vapid_header(ENDPOINT, keys, "https://daedalus.example.com", now).replace(keys.public_b64, forged.public_b64)
    with pytest.raises(InvalidSignature):
        _claims(header, forged)


def test_the_keys_survive_being_stored() -> None:
    keys = VapidKeys.generate()
    again = VapidKeys.from_private_b64(keys.private_b64)
    assert again.public_b64 == keys.public_b64 and len(b64url_decode(keys.public_b64)) == 65


def test_a_topic_is_short_and_safe_and_stable() -> None:
    topic = topic_for("policy:a1b2c3d4e5f6:0123456789ab")
    assert len(topic) == 32 and set(topic) <= set(string.ascii_letters + string.digits + "-_")
    assert topic == topic_for("policy:a1b2c3d4e5f6:0123456789ab") != topic_for("policy:a1b2c3d4e5f6:0123456789ac")


def test_a_signed_token_is_reused_for_an_hour_per_push_service() -> None:
    sender = PushSender(httpx.AsyncClient(), VapidKeys.generate(), "https://daedalus.example.com")
    first = sender.authorization(ENDPOINT, now=1000.0)
    assert sender.authorization("https://push.example.com/other", now=1000.0 + 3599) == first
    assert sender.authorization(ENDPOINT, now=1000.0 + 3600) != first
    assert sender.authorization("https://other.example.com/x", now=1000.0) != first


@respx.mock
async def test_the_sender_posts_an_encrypted_body_with_the_headers_a_push_service_reads() -> None:
    browser = Subscriber()
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(201))
    async with httpx.AsyncClient() as client:
        sender = PushSender(client, VapidKeys.generate(), "https://daedalus.example.com")
        result = await sender.send(PushTarget(ENDPOINT, browser.p256dh, browser.auth_b64), b'{"hello":1}', ttl=43200, urgency="high", topic="abc")
    assert result.ok and not result.gone
    request = route.calls[0].request
    assert request.headers["TTL"] == "43200" and request.headers["Urgency"] == "high" and request.headers["Topic"] == "abc"
    assert request.headers["Content-Encoding"] == "aes128gcm" and request.headers["Authorization"].startswith("vapid t=")
    assert browser.open(request.content) == b'{"hello":1}'


@respx.mock
async def test_what_the_sender_makes_of_each_answer() -> None:
    browser = Subscriber()
    target = PushTarget(ENDPOINT, browser.p256dh, browser.auth_b64)
    async with httpx.AsyncClient() as client:
        sender = PushSender(client, VapidKeys.generate(), "https://daedalus.example.com")

        async def answer(response: httpx.Response | Exception) -> webpush.PushResult:
            respx.post(ENDPOINT).mock(side_effect=[response])
            return await sender.send(target, b"{}", ttl=60)

        gone = await answer(httpx.Response(410, text="expired"))
        assert gone.gone and gone.status == 410 and "expired" in gone.error
        assert (await answer(httpx.Response(404))).gone
        slow = await answer(httpx.Response(429, headers={"Retry-After": "120"}))
        assert (slow.status, slow.retry_after, slow.gone) == (429, 120.0, False)
        later = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 300))
        dated = await answer(httpx.Response(429, headers={"Retry-After": later}))
        assert dated.retry_after is not None and 290 <= dated.retry_after <= 301
        big = await answer(httpx.Response(413, text="payload too large"))
        assert (big.status, big.ok, big.gone) == (413, False, False) and "413" in big.error
        refused = await answer(httpx.ConnectError("refused"))
        assert (refused.status, refused.ok) == (0, False) and "ConnectError" in refused.error
