"""Whether the host still hears a staff member, and whether a CLI's version is one to trust.

The first is one verdict read by the staff card, the staff view and the orchestrator's ``Team``; the
second warns a hire while an updated CLI has not yet passed a self-check on its new version.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from daedalus.config import RuntimeConfig, Settings
from daedalus.harness.contract import CheckResult, CheckStep, InstallInfo, LoginState
from daedalus.harness.health import channel_health
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.unit.test_harnesses_api import HEADERS, _client, _harness

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def at(**back: float) -> str:
    return (NOW - timedelta(**back)).isoformat()


@dataclass
class Msg:
    id: str
    state: str
    updated_at: str
    created_at: str = ""


def verdict(status: str = "working", team_tools: str = "mcp", channel: dict | None = None, signal: str | None = None, messages: list[Msg] | None = None, silence: int = 300):  # type: ignore[no-untyped-def]
    return channel_health(status=status, team_tools=team_tools, channel=channel or {}, last_signal_at=signal, messages=messages or [], now=NOW, silence_after_s=silence)


def test_a_member_that_is_heard_is_well() -> None:
    health = verdict(channel={"team_tools": "connected", "last_hook_at": at(seconds=20), "last_team_call_at": at(minutes=3)}, signal=at(minutes=2), messages=[Msg("m2", "acknowledged", at(minutes=1)), Msg("m1", "acknowledged", at(minutes=9))])
    assert (health.team_tools, health.level, health.problems, health.silent) == ("connected", "ok", (), False)
    # The newest signal of any channel counts: the hook twenty seconds ago, not the status two minutes ago.
    assert (health.last_signal_at, health.silent_s) == (at(seconds=20), 20)
    assert (health.last_message_state, health.last_acknowledged_at) == ("acknowledged", at(minutes=1))
    line = health.line(NOW)
    assert line.startswith("channel: team tools connected, last hook 20s ago, last team call 3 min ago") and "silent 0 min" in line


def test_what_is_wrong_is_named_in_the_order_it_matters() -> None:
    health = verdict(channel={"team_tools": "missing"}, signal=at(minutes=7), messages=[Msg("m3", "failed", at(minutes=1)), Msg("m2", "acknowledged", at(minutes=20))])
    assert health.problems == ("team_tools_missing", "silent", "message_failed") and health.level == "warn"
    assert health.silent_s == 420 and health.last_acknowledged_at == at(minutes=20)
    assert "silent 7 min (past 5 min)" in health.line(NOW)


def test_silence_counts_only_while_a_member_is_expected_to_speak() -> None:
    # A member between turns is quiet by right; a member the reconcile could not read is silent however recent.
    assert verdict(status="idle", signal=at(hours=3)).silent_s is None
    assert verdict(status="turn_done_unseen", signal=at(hours=3)).problems == ()
    assert verdict(status="no_signal", signal=at(seconds=5)).silent is True
    # The team tools are waited for until the runtime says otherwise; a Daedalus member and a CLI
    # without them have nothing to wait for.
    assert verdict(channel={}).team_tools == "waiting"
    assert verdict(team_tools="builtin", channel={"team_tools": "missing"}).team_tools == "builtin"
    assert verdict(team_tools="none").problems == ()


async def test_a_version_past_the_tested_range_is_trusted_once_its_self_check_passed(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    harness = _harness(db, config, reachable=True)
    install = InstallInfo(True, "/home/operator/.local/bin/claude", "2.1.281", "native")
    await harness.store.record_check("container", "claude", install=install, login=LoginState("yes"))
    try:
        assert (await harness.entry("container", "claude"))["version_guard"] == ""
        assert await harness.hire_warning("container", "claude") == ""
        # The CLI moved past what the adapter was tested with, and nothing has checked it since.
        await harness.store.record_check("container", "claude", install=InstallInfo(True, install.path, "2.4.0", "native"), login=LoginState("yes"))
        assert (await harness.entry("container", "claude"))["version_guard"] == "unverified"
        warning = await harness.hire_warning("container", "claude")
        assert warning.startswith("Claude Code 2.4.0 in the container environment is outside the versions the adapter was tested with (2.1.281 to below 2.2.0)"), warning
        repo = tmp_path / "bakery"
        repo.mkdir()
        async with _client(settings, config, db, manager, harness) as client:
            form = (await client.get("/api/harnesses/catalog?env=container", headers=HEADERS)).json()
            assert form["claude"]["version_guard"] == "unverified" and form["claude"]["tested_versions"] == ["2.1.281", "2.2.0"]
            pid = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(repo)}]})).json()["id"]
            hired = await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Cc", "harness": "claude"})
            assert hired.status_code == 201 and hired.json()["warning"] == warning
            daedalus = await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Ada"})
            assert daedalus.json()["warning"] == ""
        # A self-check that passed on an older version proves nothing about this one.
        await harness.store.record_self_check("container", "claude", CheckResult(True, (CheckStep("version", True),), "2.1.281", 10))
        assert (await harness.entry("container", "claude"))["version_guard"] == "unverified"
        await harness.store.record_self_check("container", "claude", CheckResult(True, (CheckStep("version", True),), "2.4.0", 10))
        assert (await harness.entry("container", "claude"))["version_guard"] == "verified"
        assert await harness.hire_warning("container", "claude") == ""
    finally:
        await manager.close()
