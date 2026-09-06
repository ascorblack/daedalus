"""OAuth 2.1 client support for remote (Streamable HTTP) MCP servers.

Implements the pieces the ``mcp`` SDK leaves to the host: RFC 8414 discovery,
RFC 7591 dynamic client registration, RFC 7636 PKCE, the authorization-code
exchange and refresh-token renewal.

The authorization step is interactive by design: a human owner must open the
authorization URL and approve/link the account on the server's own page. There
is no device flow and no shared secret here, so the client only ever holds
short-lived access tokens plus a refresh token, stored on the state volume with
mode 0600. Nothing is written to chat, logs or tool arguments.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from daedalus.config import McpOAuthConfig

logger = logging.getLogger(__name__)

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8931/callback"
_TOKEN_GRACE_SECONDS = 60.0

_TOKEN_KEYS = ("access_token", "refresh_token", "expires_at", "scope")


class OAuthError(Exception):
    """Transport-level OAuth failure (server error, bad metadata, ...)."""


class NeedsAuthorization(OAuthError):
    """No usable tokens: the owner must complete the interactive link first."""


class PendingMismatch(OAuthError):
    """The pasted redirect URL does not match the pending authorization request."""


@dataclass
class OAuthDiscovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    code_challenge_methods: list[str]


class OAuthTokenStore:
    """Small atomic JSON file holding client registration + tokens for one server."""

    def __init__(self, path: Any) -> None:
        self.path = path
        self._data: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        if self._data:
            return self._data
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically and fsync before the rename so a crash cannot leave a
        # truncated token file (a lost refresh token means a manual re-link).
        data = json.dumps(self._data, indent=2).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover — the temp file was already renamed
                pass
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover — filesystems without chmod
            pass
        self._data = dict(self._data)

    def get(self, key: str) -> Any:
        return self.load().get(key)

    def set(self, key: str, value: Any) -> None:
        self.load()[key] = value
        self.save()

    def pop(self, key: str) -> Any:
        value = self.load().pop(key, None)
        if value is not None:
            self.save()
        return value

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        self._data = {}


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _safe_origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise OAuthError(f"cannot derive origin from url {url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


class MCPOAuthClient:
    """One OAuth client per MCP server; all HTTP goes through the injected httpx client."""

    def __init__(self, server: str, config: McpOAuthConfig, server_url: str, token_path: Any, http: httpx.AsyncClient) -> None:
        self.server = server
        self.config = config
        self.server_url = server_url
        self.store = OAuthTokenStore(token_path)
        self.http = http
        self._discovery: OAuthDiscovery | None = None
        self._refresh_lock = asyncio.Lock()

    # -- metadata ------------------------------------------------------------------

    @property
    def scopes(self) -> list[str]:
        return list(self.config.scopes)

    @property
    def scope_string(self) -> str:
        return " ".join(self.scopes)

    @property
    def redirect_uri(self) -> str:
        return self.config.redirect_uri or DEFAULT_REDIRECT_URI

    def _issuer(self) -> str:
        return self.config.issuer or _safe_origin(self.server_url)

    async def discovery(self) -> OAuthDiscovery:
        if self._discovery is None:
            doc = await self._get_json(f"{self._issuer()}/.well-known/oauth-authorization-server")
            auth = doc.get("authorization_endpoint")
            token = doc.get("token_endpoint")
            reg = doc.get("registration_endpoint")
            if not auth or not token or not reg:
                raise OAuthError(f"{self.server}: oauth discovery document is missing endpoints: {sorted(doc)}")
            self._discovery = OAuthDiscovery(
                issuer=str(doc.get("issuer") or self._issuer()),
                authorization_endpoint=str(auth),
                token_endpoint=str(token),
                registration_endpoint=str(reg),
                code_challenge_methods=[str(m) for m in doc.get("code_challenge_methods_supported") or []],
            )
        return self._discovery

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self.http.get(url)
        except httpx.HTTPError as exc:
            raise OAuthError(f"{self.server}: oauth discovery request failed: {exc}") from exc
        if response.status_code != 200:
            raise OAuthError(f"{self.server}: oauth discovery {url} returned {response.status_code}")
        return response.json()

    # -- registration (RFC 7591) ----------------------------------------------------

    def client_id(self) -> str | None:
        value = self.store.get("client_id")
        return str(value) if value else None

    async def register_client(self) -> str:
        existing = self.client_id()
        if existing:
            return existing
        doc = await self.discovery()
        metadata = {
            "client_name": self.config.client_name or f"{self.server} MCP client",
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": self.scope_string,
        }
        try:
            response = await self.http.post(doc.registration_endpoint, json=metadata)
        except httpx.HTTPError as exc:
            raise OAuthError(f"{self.server}: dynamic client registration failed: {exc}") from exc
        if response.status_code not in (200, 201):
            raise OAuthError(
                f"{self.server}: dynamic client registration rejected ({response.status_code}): {_brief(response.text)}"
            )
        registered = response.json()
        client_id = registered.get("client_id")
        if not client_id:
            raise OAuthError(f"{self.server}: registration response has no client_id: {_brief(response.text)}")
        self.store.set("client_id", str(client_id))
        return str(client_id)

    # -- interactive authorization (RFC 7636) ---------------------------------------

    async def authorization_url(self) -> str:
        """Register the client and return the URL the owner must open to link the account."""
        doc = await self.discovery()
        client_id = await self.register_client()
        if self.scopes and "S256" not in doc.code_challenge_methods:
            raise OAuthError(f"{self.server}: authorization server does not support PKCE S256")
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(32)
        self.store.set(
            "pending",
            {"state": state, "code_verifier": verifier, "auth_url": "", "created_at": int(time.time())},
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scope_string,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        url = f"{doc.authorization_endpoint}?{query}"
        pending = self.store.get("pending") or {}
        pending["auth_url"] = url
        self.store.set("pending", pending)
        return url

    @staticmethod
    def extract_code(redirect_url_or_code: str) -> dict[str, str]:
        """Pull ``code``/``state`` out of the pasted redirect URL (or a bare code)."""
        if "?" not in redirect_url_or_code and "=" not in redirect_url_or_code:
            return {"code": redirect_url_or_code, "state": ""}
        query = parse_qs(urlparse(redirect_url_or_code).query)
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code:
            raise OAuthError("no 'code' found in the pasted redirect URL")
        return {"code": code, "state": state}

    async def finish_authorization(self, redirect_url_or_code: str) -> dict[str, Any]:
        """Exchange the authorization code for tokens and store them."""
        doc = await self.discovery()
        client_id = await self.register_client()
        pending = self.store.get("pending")
        if not pending:
            raise PendingMismatch(
                f"{self.server}: no pending authorization. Run McpOAuthBegin again and paste the URL it produced."
            )
        extracted = self.extract_code(redirect_url_or_code)
        if extracted["state"] and pending.get("state") and extracted["state"] != pending.get("state"):
            raise PendingMismatch(
                f"{self.server}: state mismatch — the pasted URL does not match the last McpOAuthBegin. Run it again."
            )
        verifier = pending.get("code_verifier")
        if not verifier:
            raise PendingMismatch(f"{self.server}: pending authorization is missing its code verifier; run McpOAuthBegin again.")
        form = {
            "grant_type": "authorization_code",
            "code": extracted["code"],
            "redirect_uri": self.redirect_uri,
            "client_id": client_id,
            "code_verifier": str(verifier),
        }
        try:
            response = await self.http.post(doc.token_endpoint, data=form)
        except httpx.HTTPError as exc:
            raise OAuthError(f"{self.server}: token request failed: {exc}") from exc
        if response.status_code != 200:
            raise OAuthError(f"{self.server}: token exchange failed ({response.status_code}): {_brief(response.text)}")
        tokens = response.json()
        self.store.pop("pending")
        self._save_tokens(tokens)
        return {"scope": tokens.get("scope", self.scope_string), "expires_in": tokens.get("expires_in")}

    # -- tokens ---------------------------------------------------------------------

    def _save_tokens(self, tokens: dict[str, Any], *, preserve_refresh: bool = False) -> None:
        expires_in = tokens.get("expires_in")
        expires_at = int(time.time()) + int(expires_in) - int(_TOKEN_GRACE_SECONDS) if expires_in else None
        refresh_token = tokens.get("refresh_token")
        if refresh_token is None and preserve_refresh:
            # RFC 6749 §5.1: a refresh response may omit refresh_token, meaning the
            # previously issued one stays valid. Keep it instead of wiping the link.
            refresh_token = self._tokens().get("refresh_token")
        saved = {
            "access_token": tokens.get("access_token"),
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "scope": tokens.get("scope", self.scope_string),
        }
        self.store.set("tokens", saved)

    def _tokens(self) -> dict[str, Any]:
        tokens = self.store.get("tokens") or {}
        return {key: tokens.get(key) for key in _TOKEN_KEYS}

    async def access_token(self) -> str:
        """Return a usable access token, refreshing first when needed."""
        tokens = self._tokens()
        if tokens.get("access_token") and tokens.get("expires_at") and int(tokens["expires_at"]) > time.time():
            return str(tokens["access_token"])
        if tokens.get("refresh_token"):
            await self.refresh()
            tokens = self._tokens()
            if tokens.get("access_token"):
                return str(tokens["access_token"])
        raise NeedsAuthorization(
            f"{self.server}: not linked yet. Ask the owner to run McpOAuthBegin and paste back the redirect URL."
        )

    async def refresh(self) -> None:
        async with self._refresh_lock:
            tokens = self._tokens()
            # Another caller may have refreshed while we waited for the lock; if the
            # stored token is already usable there is nothing left to do.
            if tokens.get("access_token") and tokens.get("expires_at") and int(tokens["expires_at"]) > time.time():
                return
            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                raise NeedsAuthorization(f"{self.server}: no refresh token; link again with McpOAuthBegin.")
            doc = await self.discovery()
            client_id = await self.register_client()
            form = {
                "grant_type": "refresh_token",
                "refresh_token": str(refresh_token),
                "client_id": client_id,
            }
            if self.scope_string:
                form["scope"] = self.scope_string
            try:
                response = await self.http.post(doc.token_endpoint, data=form)
            except httpx.HTTPError as exc:
                raise OAuthError(f"{self.server}: refresh request failed: {exc}") from exc
            if response.status_code != 200:
                self.store.set("tokens", {})  # a rejected refresh token is dead; force a fresh link
                raise OAuthError(f"{self.server}: token refresh failed ({response.status_code}): {_brief(response.text)}")
            self._save_tokens(response.json(), preserve_refresh=True)

    def linked(self) -> bool:
        tokens = self._tokens()
        return bool(tokens.get("access_token") or tokens.get("refresh_token"))

    def status(self) -> dict[str, Any]:
        tokens = self._tokens()
        return {
            "configured": True,
            "scopes": self.scopes,
            "redirect_uri": self.redirect_uri,
            "client_registered": bool(self.client_id()),
            "linked": self.linked(),
            "expires_at": tokens.get("expires_at"),
        }

    def disconnect(self) -> None:
        self.store.delete()


def _brief(text: str, limit: int = 400) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return cleaned[:limit] if len(cleaned) > limit else cleaned
