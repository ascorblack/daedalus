"""Passkeys: the browser's own key vouches for the owner, with no Telegram and no password.

A credential is made once on a paired browser and then signs every later login, so the pairing link
is a one-off and not a thing to keep. Credentials are discoverable (resident): the authenticator
remembers which key belongs to this site, so the login screen asks for no name.

The relying party — the identity the authenticator binds a key to — is the public address when there
is one. Without it the app is opened on the machine itself, and the only name a browser will accept
as a relying party there is ``localhost``: an address bar showing ``127.0.0.1`` is a secure context
but not a domain, so it can hold no passkey. Both spellings stay valid origins for a login with a
credential already made.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from daedalus.stores.database import Database

RP_NAME = "Daedalus"
USER_NAME = "owner"


@dataclass(frozen=True)
class RelyingParty:
    """Who the authenticator thinks it is signing for, and which origins may ask it to."""

    rp_id: str
    origins: tuple[str, ...]

    @property
    def origin_list(self) -> list[str]:
        return list(self.origins)


def relying_party(public_url: str, port: int) -> RelyingParty:
    if public_url:
        parsed = urlparse(public_url if "://" in public_url else f"https://{public_url}")  # a bare host is an https one
        host = parsed.hostname or "localhost"
        netloc = parsed.netloc or host
        return RelyingParty(rp_id=host, origins=(f"{parsed.scheme or 'https'}://{netloc}",))
    return RelyingParty(rp_id="localhost", origins=(f"http://localhost:{port}", f"http://127.0.0.1:{port}"))


# -- storage -------------------------------------------------------------------------------


async def credentials(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT * FROM passkeys ORDER BY id")
    return [dict(r) for r in rows]


async def listing(db: Database) -> list[dict[str, Any]]:
    """What the Security section shows: never the key material, only what identifies a device."""
    return [
        {"id": row["id"], "name": row["name"], "created_at": row["created_at"], "last_used_at": row["last_used_at"], "transports": json.loads(row["transports"] or "[]")}
        for row in await credentials(db)
    ]


async def count(db: Database) -> int:
    row = await db.fetchone("SELECT count(*) c FROM passkeys")
    return int(row["c"]) if row else 0


async def store(db: Database, *, credential_id: bytes, public_key: bytes, sign_count: int, transports: list[str], name: str) -> None:
    await db.execute(
        "INSERT INTO passkeys(credential_id, public_key, sign_count, transports, name, created_at) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(credential_id) DO UPDATE SET public_key = excluded.public_key, sign_count = excluded.sign_count,"
        " transports = excluded.transports, name = excluded.name",
        (bytes_to_base64url(credential_id), bytes_to_base64url(public_key), sign_count, json.dumps(transports), name[:80], datetime.now(UTC).isoformat()),
    )


async def find(db: Database, credential_id: str) -> dict[str, Any] | None:
    row = await db.fetchone("SELECT * FROM passkeys WHERE credential_id = ?", (credential_id,))
    return dict(row) if row else None


async def used(db: Database, credential_id: str, sign_count: int) -> None:
    await db.execute("UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE credential_id = ?", (sign_count, datetime.now(UTC).isoformat(), credential_id))


async def remove(db: Database, passkey_id: int) -> bool:
    row = await db.fetchone("SELECT id FROM passkeys WHERE id = ?", (passkey_id,))
    if row is None:
        return False
    await db.execute("DELETE FROM passkeys WHERE id = ?", (passkey_id,))
    return True


# -- the ceremonies ------------------------------------------------------------------------


def _descriptors(rows: list[dict[str, Any]]) -> list[PublicKeyCredentialDescriptor]:
    out: list[PublicKeyCredentialDescriptor] = []
    for row in rows:
        transports = [AuthenticatorTransport(t) for t in json.loads(row["transports"] or "[]") if t in {e.value for e in AuthenticatorTransport}]
        out.append(PublicKeyCredentialDescriptor(id=base64url_to_bytes(row["credential_id"]), transports=transports or None))
    return out


async def registration_options(db: Database, rp: RelyingParty) -> dict[str, Any]:
    """Options for ``navigator.credentials.create``; the challenge is returned so the caller can hold it."""
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=RP_NAME,
        user_name=USER_NAME,
        user_display_name=RP_NAME,
        # The owner is one person: a device that already holds a key offers to replace it rather than
        # silently making a second one for the same site.
        exclude_credentials=_descriptors(await credentials(db)),
        # Required, not preferred: sign-in offers no list of credentials, so only a discoverable key
        # is ever offered by the authenticator; one that is not would be enrolled and never usable.
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.PREFERRED),
    )
    return json.loads(options_to_json(options))


def declined_resident_key(credential: dict[str, Any]) -> bool:
    """Whether the authenticator said, in the ``credProps`` extension, that the key is NOT discoverable."""
    props = (credential.get("clientExtensionResults") or {}).get("credProps") or {}
    return props.get("rk") is False


def verify_registration(credential: dict[str, Any], *, challenge: bytes, rp: RelyingParty) -> Any:
    return verify_registration_response(credential=credential, expected_challenge=challenge, expected_rp_id=rp.rp_id, expected_origin=rp.origin_list)


async def authentication_options(db: Database, rp: RelyingParty) -> dict[str, Any]:
    """Options for ``navigator.credentials.get``: no allow-list, so a discoverable key needs no username."""
    options = generate_authentication_options(rp_id=rp.rp_id, user_verification=UserVerificationRequirement.PREFERRED)
    return json.loads(options_to_json(options))


def verify_authentication(credential: dict[str, Any], *, challenge: bytes, rp: RelyingParty, public_key: bytes, sign_count: int) -> Any:
    return verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp.rp_id,
        expected_origin=rp.origin_list,
        credential_public_key=public_key,
        credential_current_sign_count=sign_count,
    )


__all__ = [
    "RelyingParty",
    "authentication_options",
    "count",
    "credentials",
    "find",
    "listing",
    "registration_options",
    "relying_party",
    "remove",
    "store",
    "used",
    "verify_authentication",
    "verify_registration",
]
