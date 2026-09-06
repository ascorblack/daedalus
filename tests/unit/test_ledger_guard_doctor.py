"""Delivery ledger, restart-loop guard and doctor checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from daedalus.config import RuntimeConfig, Settings
from daedalus.doctor import DoctorContext, render_text, run_checks, summarize
from daedalus.host.boot_guard import THRESHOLD, BootGuard
from daedalus.stores.database import Database
from daedalus.stores.sqlite import DeliveryLedger


async def test_ledger_tracks_attempts_and_recovers_only_fresh_rows(db: Database) -> None:
    ledger = DeliveryLedger(db)
    await ledger.begin("r1", "s1", "the answer")
    await ledger.begin("r2", "s1", "another")
    await ledger.settle("r2", delivered=True)
    rows = await ledger.recoverable()
    assert [r["run_id"] for r in rows] == ["r1"] and rows[0]["attempts"] == 1
    for _ in range(DeliveryLedger.MAX_ATTEMPTS):
        await ledger.begin("r1", "s1", "the answer")
        await ledger.settle("r1", delivered=False, error="boom")
    assert await ledger.recoverable() == []  # a poison row cannot spin
    old = (datetime.now(UTC) - timedelta(hours=DeliveryLedger.MAX_AGE_HOURS + 1)).isoformat()
    await db.execute("INSERT INTO deliveries(run_id, session_id, text, status, attempts, created_at, updated_at) VALUES ('r3', 's1', 'stale', 'attempting', 0, ?, ?)", (old, old))
    assert await ledger.recoverable() == []
    await ledger.prune()


def test_boot_guard_counts_unclean_boots_and_fails_open(tmp_path: Path) -> None:
    guard = BootGuard(tmp_path)
    guard.on_boot()
    assert guard.unclean_boots == 0 and not guard.skip_recovery and guard.marker.exists()
    guard.on_clean_shutdown()
    assert not guard.marker.exists()
    guard = BootGuard(tmp_path)
    guard.on_boot()  # clean start; from here on no shutdown ever clears the marker
    for n in range(1, THRESHOLD + 1):
        guard = BootGuard(tmp_path)
        guard.on_boot()  # the marker was left behind: an unclean boot
        assert guard.unclean_boots == n
    assert guard.skip_recovery
    guard.on_clean_shutdown()
    guard = BootGuard(tmp_path)
    guard.on_boot()
    assert guard.unclean_boots == 0 and not guard.skip_recovery
    # a corrupt history file never blocks the boot
    guard.history.write_text("not json", encoding="utf-8")
    guard = BootGuard(tmp_path)
    guard.on_boot()
    assert not guard.skip_recovery


async def test_doctor_runs_without_a_bot_and_fixes_stale_snapshots(settings: Settings, db: Database) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await db.execute("INSERT INTO runs(id, tenant_id, session_id, status, created_at, updated_at) VALUES ('r9', 'daedalus', 's9', 'running', ?, ?)", (old, old))
    await db.execute("INSERT INTO snapshots(run_id, tenant_id, session_id, state, snapshot, updated_at) VALUES ('r9', 'daedalus', 's9', 'running', '{}', ?)", (old,))
    ctx = DoctorContext(settings=settings, config=RuntimeConfig(), db=db, guard=SimpleNamespace(unclean_boots=0, skip_recovery=False))
    checks = await run_checks(ctx)
    by_name = {c.name: c for c in checks}
    assert not by_name["stale run snapshots"].ok and by_name["stale run snapshots"].fixable
    assert "telegram credentials" in by_name and "session hub" in by_name
    assert by_name["boot health"].ok
    text = render_text(checks)
    assert "stale run snapshots" in text and "failures" in text
    ctx.fix = True
    checks = await run_checks(ctx)
    by_name = {c.name: c for c in checks}
    assert by_name["stale run snapshots"].fixed and summarize(checks)["fixed"] >= 1
    assert (await db.fetchone("SELECT status FROM runs WHERE id = 'r9'"))["status"] == "cancelled"
    assert json.loads(json.dumps([c.as_dict() for c in checks]))
