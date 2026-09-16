"""A restart that applies what the checkout already holds, and undoes it when it cannot boot.

Where a change lives only in the checkout there is no merged revision to pull and no reviewer who
saw it: the commit the agent left behind is preflighted on a detached copy of itself, and the bot is
stopped only once that passes. Three starts that die young put the last known-good commit back,
because at that point the app the operator would have used to undo it is the thing not coming up.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "launcher" / "supervisor.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("supervisor_local_under_test", LAUNCHER)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["supervisor_local_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# -- the decisions, as pure functions ------------------------------------------------------


def test_the_mode_is_read_from_what_is_there_and_from_what_was_asked_for(tmp_path: Path) -> None:
    sup = _load()
    repo = tmp_path / "daedalus"
    (repo / ".git").mkdir(parents=True)
    assert sup.resolve_mode("auto", repo=repo, token="") == "local", "no remote and no token is a local install"

    (repo / ".git" / "config").write_text('[remote "origin"]\n\turl = https://example.invalid/x.git\n')
    assert sup.resolve_mode("auto", repo=repo, token="t") == "server"
    assert sup.resolve_mode("auto", repo=repo, token="  ") == "local", "a remote with nothing to authenticate with is not a server"
    assert sup.resolve_mode("auto", repo=tmp_path / "nothing", token="t") == "off"
    # The operator's word wins: the bot obeys it too, and the two must not disagree about the preflight.
    assert sup.resolve_mode("local", repo=repo, token="t") == "local"
    assert sup.resolve_mode("off", repo=repo, token="t") == "off"


def test_only_the_image_files_ask_for_a_new_image() -> None:
    sup = _load()
    assert sup.needs_new_image({"deploy/Dockerfile"})
    assert sup.needs_new_image({"deploy/apt-packages.txt"})
    assert sup.needs_new_image({"launcher/supervisor.py"})
    assert not sup.needs_new_image({"daedalus/host/greeting.py", "tests/unit/test_greeting.py"})
    assert not sup.needs_new_image(set())


def test_the_virtualenv_is_synced_only_when_the_dependencies_changed_or_it_is_empty(tmp_path: Path) -> None:
    sup = _load()
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    assert not sup.needs_dependency_sync({"daedalus/host/greeting.py"}, venv=venv)
    assert sup.needs_dependency_sync({"uv.lock"}, venv=venv)
    assert sup.needs_dependency_sync({"pyproject.toml"}, venv=venv)
    # A volume that has just been created has nothing in it; the first start fills it once.
    assert sup.needs_dependency_sync(set(), venv=tmp_path / "fresh")


def test_the_boot_window_forgets_old_failures_and_ignores_impossible_ones() -> None:
    sup = _load()
    now = 10_000.0
    inside = [now - 100, now - 10, now]
    assert sup.unhealthy_boots([now - 5000, *inside], now) == inside
    # A record from the future is a clock step back, not a failed boot, and would never age out.
    assert sup.unhealthy_boots([now + 5000, now], now) == [now]


def test_the_preflight_skips_the_sync_when_it_is_not_needed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one sync a local change may cost happens inside the preflight, or not at all."""
    sup = _load()
    ran: list[list[str]] = []
    monkeypatch.setattr(sup, "run", lambda cmd, cwd=None, timeout=0: (ran.append(cmd), (0, ""))[1])
    monkeypatch.setattr(sup, "restore_owner", lambda repo: None)
    monkeypatch.setattr(sup.shutil, "which", lambda name: None)

    sup.preflight(tmp_path, sync=False)
    assert not any("sync" in cmd for cmd in ran)
    assert ["uv", "run", "--frozen", "python", "-m", "compileall", "-q", "daedalus"] in ran
    ran.clear()
    sup.preflight(tmp_path, sync=True)
    assert [cmd for cmd in ran if "sync" in cmd] == [["uv", "sync", "--frozen", "--extra", "dev"]]


# -- the restart ----------------------------------------------------------------------------


def _harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, preflight_ok: bool, changed: set[str] | None = None) -> tuple[Any, Any, list[str]]:
    sup = _load()
    calls: list[str] = []
    monkeypatch.setattr(sup, "BOT_REPO", tmp_path / "daedalus")
    monkeypatch.setattr(sup, "CORE_REPO", tmp_path / "protocore-exp")
    monkeypatch.setattr(sup, "CANDIDATE", tmp_path / "preflight")
    monkeypatch.setattr(sup, "FAILED", tmp_path / "good" / "FAILED")
    monkeypatch.setattr(sup, "SELFDEV_DIR", tmp_path / "selfdev")
    monkeypatch.setattr(sup, "APPLY_RESULT", tmp_path / "selfdev" / "result.json")
    monkeypatch.setattr(sup, "LOG", tmp_path / "supervisor.log")
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr\n")  # a virtualenv that is already there, so only a dependency change syncs
    monkeypatch.setattr(sup, "VENV", venv)
    monkeypatch.setattr(sup, "run", lambda cmd, cwd=None, timeout=0: (calls.append(f"run {' '.join(cmd)}"), (0, ""))[1])
    monkeypatch.setattr(sup, "load_history", lambda: [{"bot": "good000000", "core": "core000000", "at": "2026-09-17T00:00:00+00:00"}])
    monkeypatch.setattr(sup, "git", lambda repo, *args: (calls.append(f"git {repo.name} {' '.join(args)}"), (0, "new1111111\n"))[1])
    monkeypatch.setattr(sup, "head", lambda repo: "new1111111")
    monkeypatch.setattr(sup, "prepare_candidate", lambda repo, ref="origin/main": (calls.append(f"candidate {repo.name} {ref}"), (True, ""))[1])
    monkeypatch.setattr(sup, "preflight", lambda repo, sync=True: (calls.append(f"preflight {repo} sync={sync}"), (preflight_ok, "transcript"))[1])
    supervisor = sup.Supervisor()
    supervisor.mode = "local"
    supervisor._changed_files = lambda old, new: set(changed or {"daedalus/host/greeting.py"})  # type: ignore[method-assign]

    async def stop_child() -> None:
        calls.append("stop")

    supervisor.stop_child = stop_child  # type: ignore[method-assign]
    return sup, supervisor, calls


async def test_a_passing_change_is_preflighted_on_a_copy_of_itself_and_then_applied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True)
    await supervisor._apply_local("the operator asked the app to apply the change")
    assert "candidate daedalus new1111111" in calls, "the preflight must run on the local commit, not on a remote"
    order = [c for c in calls if c.startswith(("preflight", "stop"))]
    assert order[0].startswith("preflight") and order[1] == "stop"
    assert "sync=False" in order[0], "nothing declared a dependency, so nothing is synced"
    assert supervisor.restart_requested.is_set()
    result = sup.last_change()
    assert result["status"] == "applied" and result["commit"] == "new1111111"


async def test_a_failing_change_never_stops_the_bot_and_is_taken_back_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=False)
    await supervisor._apply_local("the operator asked the app to apply the change")
    assert "stop" not in calls
    assert not supervisor.restart_requested.is_set()
    assert "git daedalus reset --hard good000000" in calls, "the refused commit was left in the running checkout"
    result = sup.last_change()
    assert result["status"] == "preflight_failed" and "not applied" in result["detail"]
    assert "the bot kept running" in sup.FAILED.read_text()


async def test_a_change_to_the_image_is_applied_without_asking_for_a_rebuild(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True, changed={"deploy/Dockerfile"})
    triggered: list[str] = []
    supervisor._request_image_rebuild = lambda: (triggered.append("asked"), True)[1]  # type: ignore[method-assign]
    await supervisor._apply_local("a new package")
    assert triggered == [], "a local installation has no rebuilder to ask"
    assert "needs a new image" not in sup.last_change()["detail"]
    assert "run an update" in sup.last_change()["detail"].lower()
    assert supervisor.restart_requested.is_set(), "the code part of the change still applies"


async def test_a_dependency_change_buys_exactly_one_sync(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True, changed={"uv.lock", "pyproject.toml"})
    await supervisor._apply_local("a new dependency")
    assert [c for c in calls if c.startswith("preflight")] == [f"preflight {sup.candidate_dir(sup.BOT_REPO)} sync=True"]


async def test_the_rebuilder_is_never_asked_outside_server_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, _ = _harness(monkeypatch, tmp_path, preflight_ok=True)
    monkeypatch.setattr(sup, "REBUILD_TRIGGER_DIR", tmp_path / "trigger")
    (tmp_path / "trigger").mkdir()
    assert supervisor._request_image_rebuild() is False
    assert not (tmp_path / "trigger" / "rebuild").exists()
    supervisor.mode = "server"
    assert supervisor._request_image_rebuild() is True


async def test_a_restart_is_not_started_twice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, _ = _harness(monkeypatch, tmp_path, preflight_ok=True)
    async with supervisor.lock:
        assert "already running" in await supervisor.restart("again")


async def test_a_server_restart_just_goes_round_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With a remote the running revision was preflighted when it was merged; there is nothing to check."""
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=False)
    supervisor.mode = "server"
    assert await supervisor.restart("after a merge") == "restarting"
    assert supervisor.restart_requested.is_set()
    assert not any(c.startswith("preflight") for c in calls)


# -- the automatic rollback ------------------------------------------------------------------


async def test_three_boots_that_die_young_put_the_last_known_good_commit_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True)
    assert await supervisor._note_failed_boot() is False
    assert await supervisor._note_failed_boot() is False
    assert not any("reset" in c for c in calls)
    assert await supervisor._note_failed_boot() is True

    assert "git daedalus reset --hard good000000" in calls
    assert "git protocore-exp reset --hard core000000" in calls
    result = sup.last_change()
    assert result["status"] == "rolled_back" and result["commit"] == "new1111111"
    assert "still in the checkout's history" in result["detail"]
    assert supervisor.failed_boots == [], "the count starts again from the revision that is running now"


async def test_a_server_install_is_not_rolled_back_behind_the_operators_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True)
    supervisor.mode = "server"
    for _ in range(5):
        assert await supervisor._note_failed_boot() is False
    assert not any("reset" in c for c in calls)


async def test_a_first_revision_that_cannot_boot_says_so_rather_than_guessing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True)
    monkeypatch.setattr(sup, "load_history", lambda: [{"bot": "new1111111", "core": "core000000", "at": "2026-09-17T00:00:00+00:00"}])
    for _ in range(3):
        await supervisor._note_failed_boot()
    assert not any("reset" in c for c in calls)
    assert sup.last_change()["status"] == "no_rollback"
