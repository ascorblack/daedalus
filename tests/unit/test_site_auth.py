"""Signing in on the site: Telegram's Login Widget data is verified, the session cookie is signed and expires."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from daedalus.extensions.api import session_cookie_value, validate_login_widget, verify_session_cookie

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
