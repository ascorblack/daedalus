"""The agent can work in the worktree it was given, and still cannot touch the installation.

Self-development hands the model a directory and tells it to edit, test and commit there. Sealing the
state directory whole sealed that directory too when the worktrees were cut inside it: every ``Read``,
``Write``, ``Find`` and ``Exec`` in the agent's own worktree was refused as part of the installation,
in a container as well as on a machine. These tests hold both halves at once — the worktree is open
and the installation beside it is not — in the two modes self-development runs in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.selfdev import SelfDevelopment
from daedalus.host.policy import ALLOW, DENY
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.files import find_files, read_file, search_files, write_file


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _checkout(root: Path, *, with_origin: bool) -> Path:
    """A real checkout of the shape self-development works on, with a remote where the mode needs one."""
    repo = root / "daedalus"
    (repo / "daedalus").mkdir(parents=True)
    (repo / "daedalus" / "app.py").write_text("HELLO = 'hello'\n", encoding="utf-8")
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@localhost")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the first commit")
    if with_origin:
        origin = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        _git(repo, "remote", "add", "origin", str(origin))
        _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _settings(tmp_path: Path, repo: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        state_dir=tmp_path / "state",
        workspaces_dir=tmp_path / "workspaces",
        bot_repo_dir=repo,
        core_repo_dir=repo,
        telegram_bot_token="",
        owner_user_id=0,
        api_port=0,
        native=True,
    )


class _App:
    """What the extension asks its application for, and nothing else."""

    def __init__(self, settings: Settings, db: Database, manager: SessionManager) -> None:
        self.settings = settings
        self.config = RuntimeConfig()
        self.db = db
        self.manager = manager
        self.front = None
        self.extensions: dict[str, object] = {}
        self.guard = None


async def _sealed_installation(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The three files the state directory is sealed for, on disk so a tool could open them."""
    state = tmp_path / "state"
    (state / "sessions").mkdir(parents=True, exist_ok=True)
    config = state / "config.toml"
    config.write_text("[models]\n", encoding="utf-8")
    journal = state / "daedalus.sqlite-journal"
    journal.write_text("rows", encoding="utf-8")
    link = state / "pairing-url"
    link.write_text("https://example.invalid/pair/abc", encoding="utf-8")
    return config, journal, link


@pytest.mark.parametrize("mode", ["server", "local"])
async def test_a_session_works_in_its_worktree_while_the_installation_stays_sealed(tmp_path: Path, mode: str) -> None:
    repo = _checkout(tmp_path, with_origin=mode == "server")
    settings = _settings(tmp_path, repo)
    # The worktrees are the agent's work, so they are beside the installation rather than inside it.
    assert settings.worktrees_dir == tmp_path / "worktrees"
    assert settings.state_dir not in settings.worktrees_dir.parents

    db = Database(settings.db_path)
    await db.open()
    try:
        manager = SessionManager(settings, RuntimeConfig(), db=db)
        await manager.start()
        state = await manager.create_session("self-change")
        selfdev = SelfDevelopment(_App(settings, db, manager), mode)  # type: ignore[arg-type]
        worktree = await selfdev.workspace("bot", "greeting")
        assert worktree == settings.worktrees_dir / "bot" / "greeting" and (worktree / "daedalus" / "app.py").is_file()
        await manager.open_writable(state.session.id, worktree)

        config, journal, _link = await _sealed_installation(tmp_path)
        policy = manager.policy(base_dir=state.workspace)
        for command in (
            f"cat {worktree}/daedalus/app.py",
            f"git -C {worktree} status",
            f"rg HELLO {worktree}",
        ):
            assert policy.evaluate("Exec", {"command": command}).action == ALLOW, command
        # The directory the work runs in is a path of its own, and a test run there is the point of the worktree.
        assert policy.evaluate("Exec", {"command": "uv run pytest -q", "cwd": str(worktree)}).action == ALLOW
        assert policy.evaluate("Read", {"path": str(worktree / "daedalus" / "app.py")}).action == ALLOW
        assert policy.evaluate("Write", {"path": str(worktree / "daedalus" / "app.py"), "content": "x"}).action == ALLOW
        # And nothing of the installation moved with it.
        for command in (f"cat {config}", f"cat {journal}", f"cat {tmp_path}/state/pairing-url"):
            assert policy.evaluate("Exec", {"command": command}).action == DENY, command
        assert policy.evaluate("Read", {"path": str(config)}).action == DENY

        services = manager.locator_services(state.session.id)
        assert services is not None and not services.is_protected(worktree / "daedalus" / "app.py")
        assert services.is_protected(config) and services.is_protected(journal) and services.is_protected(settings.db_path)

        context = ToolContext(tenant_id="t", run_id="r", session_id=state.session.id, metadata={"tool_call_id": "c"})
        assert "HELLO" in (await read_file().invoke(context, {"path": str(worktree / "daedalus" / "app.py")})).content
        written = await write_file().invoke(context, {"path": str(worktree / "daedalus" / "greeting.py"), "content": "def greet():\n    return 'hi'\n"})
        assert not written.is_error and (worktree / "daedalus" / "greeting.py").is_file()
        assert not (await find_files().invoke(context, {"path": str(worktree), "pattern": "*.py"})).is_error
        assert not (await search_files().invoke(context, {"pattern": "HELLO", "path": str(worktree)})).is_error
        # The commit the agent makes there is git's business and the tools do not stand in its way.
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-qm", "A greeting of its own")
        assert (await read_file().invoke(context, {"path": str(config)})).is_error
        assert (await find_files().invoke(context, {"path": str(settings.state_dir), "pattern": "*"})).is_error
    finally:
        await db.close()


async def test_the_environment_file_the_launcher_owns_is_refused_by_both_layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It holds the Telegram token, which is one of the two front doors.

    The policy refused it and the tools did not, because each layer had its own list of what the
    installation is. There is one list now, so the two cannot drift apart again.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=123:abc\n", encoding="utf-8")
    monkeypatch.setenv("DAEDALUS_ENV_FILE", str(env_file))
    repo = _checkout(tmp_path, with_origin=False)
    settings = _settings(tmp_path, repo)
    assert settings.env_file_path == env_file and env_file in set(settings.sealed_paths)

    db = Database(settings.db_path)
    await db.open()
    try:
        manager = SessionManager(settings, RuntimeConfig(), db=db)
        await manager.start()
        assert env_file in set(manager.protected_paths())
        state = await manager.create_session("curious")
        assert manager.policy(base_dir=state.workspace).evaluate("Exec", {"command": f"cat {env_file}"}).action == DENY
        services = manager.locator_services(state.session.id)
        assert services is not None and services.is_protected(env_file)
        context = ToolContext(tenant_id="t", run_id="r", session_id=state.session.id, metadata={"tool_call_id": "c"})
        result = await read_file().invoke(context, {"path": str(env_file)})
        assert result.is_error and "protected" in result.content and "123:abc" not in result.content
    finally:
        await db.close()
