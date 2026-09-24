"""Web Push, sent by the host itself: message encryption (RFC 8291) and the sender's identity (RFC 8292).

A browser that subscribes hands over three things: an endpoint at its vendor's push service, a
P-256 public key and a 16-byte authentication secret. A message is encrypted end to end for that
key, so the push service carries bytes it cannot read, and the request is signed with this
installation's own key pair (VAPID), so the push service knows every message comes from the one
server the subscription was made for.

Both halves are small enough to write here on ``cryptography`` and ``httpx`` rather than import
``pywebpush``: that library pulls three more packages including ``requests``, and sends
synchronously, which in this host would mean a thread per push. The encryption is checked byte
for byte against the worked example in RFC 8291.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RECORD_SIZE = 4096
"""One record carries the whole message. A push service accepts at least 4096 bytes of body, and
the header (86 bytes) plus the tag (16) plus the delimiter leave 3993 bytes of plaintext."""
MAX_PLAINTEXT = RECORD_SIZE - 86 - 16 - 1
JWT_LIFETIME = 12 * 3600
"""RFC 8292 allows up to 24 hours; Apple refuses a token whose expiry is too far out, so half of it."""
JWT_REUSE = 3600
"""A signed token is reused for an hour per push service, then signed afresh."""

Urgency = Literal["very-low", "low", "normal", "high"]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    text = text.strip().replace("+", "-").replace("/", "_").rstrip("=")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def load_public_key(raw: bytes) -> ec.EllipticCurvePublicKey:
    """A P-256 public key in the uncompressed form a browser hands out (65 bytes, first byte 4)."""
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("a subscription key is an uncompressed P-256 point of 65 bytes")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)


def check_subscription_keys(p256dh: str, auth: str) -> None:
    """Refuse keys no message could ever be encrypted for, at the moment the browser hands them over."""
    try:
        load_public_key(b64url_decode(p256dh))
        secret = b64url_decode(auth)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"the subscription keys cannot be read: {exc}") from exc
    if len(secret) != 16:
        raise ValueError("the subscription's authentication secret is 16 bytes")


@dataclass(frozen=True)
class VapidKeys:
    """This installation's identity towards every push service. Kept for good: new keys orphan every subscription."""

    private_key: ec.EllipticCurvePrivateKey

    @classmethod
    def generate(cls) -> VapidKeys:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_private_b64(cls, text: str) -> VapidKeys:
        value = int.from_bytes(b64url_decode(text), "big")
        return cls(ec.derive_private_key(value, ec.SECP256R1()))

    @property
    def private_b64(self) -> str:
        return b64url(self.private_key.private_numbers().private_value.to_bytes(32, "big"))

    @property
    def public_bytes(self) -> bytes:
        return _point(self.private_key.public_key())

    @property
    def public_b64(self) -> str:
        """What the browser passes as ``applicationServerKey`` when it subscribes."""
        return b64url(self.public_bytes)


def audience(endpoint: str) -> str:
    """The origin of a push endpoint, which is what a VAPID token is issued for."""
    parts = urlsplit(endpoint)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"not an absolute URL: {endpoint!r}")
    return f"{parts.scheme}://{parts.netloc}"


def vapid_jwt(aud: str, keys: VapidKeys, subject: str, now: float, lifetime: int = JWT_LIFETIME) -> str:
    """An ES256 JSON Web Token for one push service. ES256 wants the raw ``r‖s`` signature, not the DER that ``cryptography`` returns."""
    header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64url(json.dumps({"aud": aud, "exp": int(now) + lifetime, "sub": subject}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    r, s = decode_dss_signature(keys.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    return f"{header}.{claims}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def vapid_header(endpoint: str, keys: VapidKeys, subject: str, now: float | None = None) -> str:
    """The ``Authorization`` value RFC 8292 defines: ``vapid t=<token>, k=<public key>``."""
    token = vapid_jwt(audience(endpoint), keys, subject, time.time() if now is None else now)
    return f"vapid t={token}, k={keys.public_b64}"


def encrypt(
    plaintext: bytes,
    p256dh: str,
    auth: str,
    *,
    salt: bytes | None = None,
    server_key: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """The ``aes128gcm`` body for one subscription (RFC 8291 over RFC 8188), in a single record.

    ``salt`` and ``server_key`` are fresh for every message; they are parameters only so that the
    RFC's worked example can be reproduced exactly. Reusing either for a real message would reuse
    a key and nonce pair under AES-GCM, which gives the plaintext away.
    """
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError(f"a push message carries at most {MAX_PLAINTEXT} bytes, not {len(plaintext)}")
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    ua_key = load_public_key(ua_public)
    salt = salt if salt is not None else os.urandom(16)
    if len(salt) != 16:
        raise ValueError("the salt is 16 bytes")
    server_key = server_key if server_key is not None else ec.generate_private_key(ec.SECP256R1())
    as_public = _point(server_key.public_key())
    ecdh_secret = server_key.exchange(ec.ECDH(), ua_key)
    # HKDF written out as its HMAC steps, as RFC 8291 section 3.4 does: every output here is at
    # most one hash long, so each expand is a single HMAC with the counter byte 1.
    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    # The last (and only) record ends with the delimiter 2 and no padding.
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


def topic_for(key: str) -> str:
    """A ``Topic`` header for a dedupe key: at most 32 characters of the base64url alphabet, as RFC 8030 requires.

    A push service keeps only the newest undelivered message per topic, so a phone that was off
    receives the latest word on a thing rather than every step of it.
    """
    return b64url(hashlib.sha256(key.encode("utf-8")).digest())[:32]


@dataclass(frozen=True)
class PushTarget:
    endpoint: str
    p256dh: str
    auth: str


@dataclass(frozen=True)
class PushResult:
    status: int
    """The push service's HTTP status; 0 when no answer came back at all."""
    gone: bool = False
    """The subscription no longer exists (404 or 410): forget it."""
    retry_after: float | None = None
    """Seconds to leave this push service alone after a 429."""
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _retry_after(value: str | None, now: float) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - now)
    except (TypeError, ValueError):
        return None


@dataclass
class PushSender:
    """Sends encrypted messages with a signed identity. One HTTP client for all of them."""

    client: httpx.AsyncClient
    keys: VapidKeys
    subject: str
    """The ``sub`` claim: how a push service reaches whoever runs this server (the public https address)."""
    _tokens: dict[str, tuple[str, float]] = field(default_factory=dict)

    def authorization(self, endpoint: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        aud = audience(endpoint)
        cached = self._tokens.get(aud)
        if cached is None or now - cached[1] >= JWT_REUSE:
            cached = (vapid_jwt(aud, self.keys, self.subject, now), now)
            self._tokens[aud] = cached
        return f"vapid t={cached[0]}, k={self.keys.public_b64}"

    async def send(self, target: PushTarget, payload: bytes, *, ttl: int, urgency: Urgency = "normal", topic: str | None = None) -> PushResult:
        body = encrypt(payload, target.p256dh, target.auth)
        headers = {
            "TTL": str(max(0, int(ttl))),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "Urgency": urgency,
            "Authorization": self.authorization(target.endpoint),
        }
        if topic:
            headers["Topic"] = topic
        try:
            response = await self.client.post(target.endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            return PushResult(0, error=f"{type(exc).__name__}: {exc}"[:300])
        status = response.status_code
        if 200 <= status < 300:
            return PushResult(status)
        detail = f"{status} {response.reason_phrase}".strip()
        text = response.text.strip()[:200] if response.content else ""
        error = f"{detail}: {text}" if text else detail
        if status in (404, 410):
            return PushResult(status, gone=True, error=error)
        if status == 429:
            return PushResult(status, retry_after=_retry_after(response.headers.get("retry-after"), time.time()), error=error)
        return PushResult(status, error=error)


__all__ = [
    "JWT_LIFETIME",
    "MAX_PLAINTEXT",
    "RECORD_SIZE",
    "PushResult",
    "PushSender",
    "PushTarget",
    "VapidKeys",
    "audience",
    "b64url",
    "b64url_decode",
    "check_subscription_keys",
    "encrypt",
    "load_public_key",
    "topic_for",
    "vapid_header",
    "vapid_jwt",
]
