"""The API answers every browser harness in this directory has to agree on.

Each script here invents an installation of its own — a session of three thousand synthetic
messages, a small studio's agents — but none of them reaches a screen at all until a handful of
routes answer sensibly. The app asks whether a model is configured before it draws anything, and
what this installation is allowed to do before it draws the navigation; a stub that answers ``{}``
to those sits on the "Add a model" screen until its selector times out, which is exactly what
happened to ``perf_session.py`` when the onboarding gate landed in the app and only
``screenshots.py`` was taught about it.

So the gates live here, once. A script answers what it has invented and falls back to this table
for the rest, and records every ``/api/`` path it did not recognise so the run can fail with the
list: a gate added to the app cannot be answered by one harness and silently missed by another.

``expect_app`` is here for the same reason one level down: before any of this matters, the address
a harness was pointed at has to be *this* build and not something else that happens to hold the
port. It is checked once, in a sentence, rather than discovered as a selector that never appears.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

# Outside services_port_range (8100-8119), the range this product hands to an agent's own preview
# servers: a harness that serves its build into that range competes with the installation running
# beside it, and loses silently — the browser is pointed at the address either way.
DEFAULT_PORT = 8163
DEFAULT_APP = f"http://127.0.0.1:{DEFAULT_PORT}/app"

GATES: dict[str, object] = {
    # Drawn before any screen: no model means the whole app is the "Add a model" flow.
    "/api/onboarding": {"has_model": True, "presets": 1, "default_preset": "p", "providers": [], "needs": [], "message": ""},
    # Decides which screens the navigation has at all.
    "/api/capabilities": {"selfdev": {"mode": "off", "configured": "off", "reasons": [], "missing": [], "tools": []}},
    "/api/auth/me": {"user": "operator"},
    "/api/auth/config": {"passkeys": 1},
    "/api/status": {"ok": True},
    "/api/inbox/unread": {"unread": 0},
    "/api/modes": {},
    "/api/commands": [],
    "/api/asr": {"configured": False, "reason": "", "provider": "", "model": "", "max_seconds": 120, "autosend": False},
    "/api/proposals": [],
    "/api/schedules": [],
    "/api/sessions": [],
    # The shell asks which projects there are before it draws the rail.
    "/api/projects": [],
}


class Unhandled:
    """Every ``/api/`` path a stub had no answer for, so a run can end by saying so."""

    def __init__(self) -> None:
        self.paths: set[str] = set()

    def record(self, path: str) -> None:
        self.paths.add(path)

    def report(self) -> int:
        """Print what was missed. Non-zero when something was, which is the run's exit code."""
        if not self.paths:
            return 0
        print("\nthe stub had no answer for these API paths (add them here or to tests/browser/api_stub.py):")
        for path in sorted(self.paths):
            print(f"  {path}")
        return 1


def expect_app(base: str) -> None:
    """Refuse to drive a browser at an address that is not the built Mini App.

    ``serve_app.py`` is normally started in the background, and a port it could not bind leaves
    something else answering there — an agent's own preview server, a stale run. The browser loads
    that page quite happily, and the failure arrives much later as a selector timeout on ``.screen``
    that reads like a broken component. One request up front says what actually happened.
    """
    try:
        with urllib.request.urlopen(f"{base}/index.html", timeout=5) as answer:  # noqa: S310
            body = answer.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        print(f"nothing is serving the app at {base} ({exc}); build it and start tests/browser/serve_app.py", file=sys.stderr)
        raise SystemExit(1) from None
    if "/app/assets/" not in body:
        print(f"{base} answers, but not with the built Mini App — something else holds that port", file=sys.stderr)
        raise SystemExit(1)


__all__ = ["DEFAULT_APP", "DEFAULT_PORT", "GATES", "Unhandled", "expect_app"]
