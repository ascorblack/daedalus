"""Restart-loop guard around boot-time recovery.

If the work a boot does to recover (resuming runs from snapshots, re-sending answers)
is itself what crashes the process, a supervised deployment enters a tight crash and
respawn loop. The guard counts *unclean* boots — the previous process died without
clearing its running marker — inside a short window; at the threshold it tells the boot
to skip recovery for this once, so the service still starts and serves live traffic.
Operator restarts never trip it (a clean shutdown clears the marker) and every failure
of the guard itself fails open.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

WINDOW_MINUTES = 10
THRESHOLD = 3


class BootGuard:
    def __init__(self, state_dir: Path, *, window_minutes: int = WINDOW_MINUTES, threshold: int = THRESHOLD) -> None:
        self.marker = state_dir / "RUNNING"
        self.history = state_dir / "boot-history.json"
        self.window_minutes = window_minutes
        self.threshold = threshold
        self.unclean_boots = 0
        self.skip_recovery = False

    def on_boot(self) -> None:
        """Call first thing at start: records this boot, decides whether recovery is safe."""
        try:
            now = datetime.now(UTC)
            unclean = self.marker.exists()
            self.history.parent.mkdir(parents=True, exist_ok=True)
            self.marker.write_text(now.isoformat(), encoding="utf-8")  # armed first: a failure below must not disarm the next boot
            times: list[datetime] = []
            if self.history.exists():
                try:
                    times = [datetime.fromisoformat(t) for t in json.loads(self.history.read_text(encoding="utf-8")) if isinstance(t, str)]
                except (OSError, ValueError, TypeError):
                    times = []
            cutoff = now - timedelta(minutes=self.window_minutes)
            recent = [t for t in times if t >= cutoff]
            if unclean:
                recent.append(now)
            self.unclean_boots = len(recent)
            self.skip_recovery = self.unclean_boots >= self.threshold
            self.history.write_text(json.dumps([t.isoformat() for t in recent]), encoding="utf-8")
            if unclean:
                logger.warning("unclean boot %d/%d within %d min%s", self.unclean_boots, self.threshold, self.window_minutes, " — skipping boot recovery" if self.skip_recovery else "")
        except Exception:  # noqa: BLE001 — the guard must never keep the bot from starting
            logger.warning("boot guard failed open", exc_info=True)
            self.skip_recovery = False

    def on_clean_shutdown(self) -> None:
        try:
            self.marker.unlink(missing_ok=True)
            self.history.write_text("[]", encoding="utf-8")
        except OSError:
            pass


__all__ = ["THRESHOLD", "WINDOW_MINUTES", "BootGuard"]
