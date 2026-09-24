"""The receiving side of Web Push, written independently of ``daedalus.host.webpush`` for the tests.

A sender checked only against itself proves nothing: an encryption with the wrong info string
decrypts perfectly with the same wrong info string. This file follows the receiver's steps of RFC
8188 and RFC 8291 from the documents themselves, with ``cryptography``'s HKDF rather than the
hand-written HMAC steps the sender uses, and is itself checked against the RFCs' examples.
"""

from __future__ import annotations

import base64
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def unb64(text: str) -> bytes:
    text = "".join(text.split())
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def open_aes128gcm(body: bytes, ikm: bytes) -> tuple[bytes, bytes]:
    """RFC 8188 decryption of a single-record body; returns (plaintext, keyid)."""
    salt, record_size, id_length = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
    keyid = body[21 : 21 + id_length]
    record = body[21 + id_length :]
    assert len(record) <= record_size, "one record only"
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    content = AESGCM(cek).decrypt(nonce, record, None).rstrip(b"\x00")
    assert content.endswith(b"\x02"), "the last record's delimiter is 2"
    return content[:-1], keyid


def webpush_ikm(ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes, as_public: bytes) -> bytes:
    """RFC 8291 section 3.3 from the receiver's end: the key material RFC 8188 then uses."""
    ua_public = ua_private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    return _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)


def open_webpush(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """Decrypt a Web Push message as the browser would."""
    as_public = body[21 : 21 + body[20]]
    plaintext, keyid = open_aes128gcm(body, webpush_ikm(ua_private, auth_secret, as_public))
    assert keyid == as_public
    return plaintext


class Subscriber:
    """A browser's side of one subscription: its key pair and secret, and the keys it hands the server."""

    def __init__(self) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        public = self.private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.p256dh = b64(public)
        self.auth_b64 = b64(self.auth)

    def open(self, body: bytes) -> bytes:
        return open_webpush(body, self.private, self.auth)


__all__ = ["Subscriber", "b64", "open_aes128gcm", "open_webpush", "unb64", "webpush_ikm"]
