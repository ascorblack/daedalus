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
"""

from __future__ import annotations

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


__all__ = ["GATES", "Unhandled"]
