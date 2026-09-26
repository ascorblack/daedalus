"""The Origin rule: whether a WebSocket comes from the app itself."""

from __future__ import annotations

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _authority(scheme: str, host: str, port: int | None) -> str:
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"  # an IPv6 literal, bracketed as it is written in an origin
    return host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"


def _origin(url: str) -> tuple[str, str] | None:
    """``(scheme, authority)`` of an http(s) URL with its default port left out; ``None`` otherwise."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    return scheme, _authority(scheme, parts.hostname, port)


def allowed_origin(origin: str | None, host_header: str | None, public_url: str) -> bool:
    """Whether a WebSocket from ``origin`` is the app's own.

    The same origin as the ``Host`` it was sent to (the app opened on the machine, or through a proxy
    that keeps ``Host``), or exactly the origin of ``MINIAPP_PUBLIC_URL`` (a proxy that rewrites
    ``Host``, and Telegram's webview, which sends the Mini App's origin). A missing Origin, or
    ``null`` (a sandboxed frame, a file), is refused: every browser sends one on a WebSocket, so its
    absence means something that is not the app.

    The ticket is what proves the operator; this check is what stops a page elsewhere from using a
    signed-in browser's cookie to fetch one and open a terminal or a browser with it.
    """
    if not origin or origin.strip().lower() == "null":
        return False
    parsed = _origin(origin)
    if parsed is None:
        return False
    scheme, authority = parsed
    if host_header:
        host = _origin(f"{scheme}://{host_header.strip()}")
        if host is not None and host[1] == authority:
            return True
    public = _origin(public_url) if public_url else None
    return public is not None and public == parsed


__all__ = ["allowed_origin"]
