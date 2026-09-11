"""The environment a tool's subprocess inherits: the toolchain's variables and git's token, never the bot's own credentials."""

from __future__ import annotations

import pytest

from daedalus.tools.shell import shell_environment


@pytest.fixture
def host_env(monkeypatch):  # type: ignore[no-untyped-def]
    values = {
        # the bot's own credentials: never inherited
        "TELEGRAM_BOT_TOKEN": "tg_should_not_leak",
        "TELEGRAM_API_HASH": "hash_should_not_leak",
        "KEYPROXY_UPSTREAMS": "should_not_leak",
        "EXAMPLE_API_KEY": "ak_should_not_leak",
        "EXAMPLE_SECRET": "sec_should_not_leak",
        "EXAMPLE_PASSWORD": "pw_should_not_leak",
        "GITHUB_TOKEN": "raw_should_not_leak",
        # unrelated host variables: not inherited either
        "OWNER_USER_ID": "1",
        # the browser store the skills drive: inherited
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/pw-browsers",
        "CHROME_PATH": "/usr/local/bin/chromium",
        "SUPERVISOR_SOCKET": "/tmp/example.sock",
        # what a shell needs
        "PATH": "/usr/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "HTTPS_PROXY": "http://proxy.example:3128",
        "SSL_CERT_FILE": "/etc/ssl/example.pem",
        "UV_PROJECT_ENVIRONMENT": "/tmp/venv",
        "GIT_AUTHOR_NAME": "Example",
        "GIT_TERMINAL_PROMPT": "0",
        # git authentication, kept on purpose
        "GH_TOKEN": "github_pat_example",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "!f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return values


def test_credentials_stay_out_and_the_toolchain_gets_in(host_env) -> None:  # type: ignore[no-untyped-def]
    env = shell_environment("sess-1")
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_API_HASH", "KEYPROXY_UPSTREAMS", "EXAMPLE_API_KEY", "EXAMPLE_SECRET", "EXAMPLE_PASSWORD", "GITHUB_TOKEN", "OWNER_USER_ID", "SUPERVISOR_SOCKET"):
        assert name not in env, name
    for name in ("PATH", "HOME", "LANG", "HTTPS_PROXY", "SSL_CERT_FILE", "UV_PROJECT_ENVIRONMENT", "GIT_AUTHOR_NAME", "GIT_TERMINAL_PROMPT"):
        assert env[name] == host_env[name], name
    assert env["DAEDALUS_SESSION_ID"] == "sess-1"


def test_git_authentication_is_inherited(host_env) -> None:  # type: ignore[no-untyped-def]
    env = shell_environment("sess-1")
    assert env["GH_TOKEN"] == "github_pat_example"


def test_the_browser_store_is_inherited(host_env) -> None:  # type: ignore[no-untyped-def]
    env = shell_environment("sess-1")
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/pw-browsers" and env["CHROME_PATH"] == "/usr/local/bin/chromium"
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_KEY_0"] == "credential.helper" and "GH_TOKEN" in env["GIT_CONFIG_VALUE_0"]


def test_explicit_extra_wins(host_env) -> None:  # type: ignore[no-untyped-def]
    env = shell_environment("sess-1", {"EXAMPLE_API_KEY": "given", "PATH": "/opt/bin"})
    assert env["EXAMPLE_API_KEY"] == "given" and env["PATH"] == "/opt/bin"


@pytest.mark.asyncio
async def test_the_sandbox_opens_the_paths_the_host_named_for_the_session(tmp_path, monkeypatch) -> None:
    """A worktree the session opened is writable inside the sandbox; a path that does not exist is not bound."""
    from types import SimpleNamespace

    from daedalus.tools import shell

    monkeypatch.setattr(shell, "bwrap_status", lambda: "ok")
    monkeypatch.setattr(shell.shutil, "which", lambda name: "/usr/bin/bwrap")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    worktree = tmp_path / "worktrees" / "bot" / "fix"
    worktree.mkdir(parents=True)
    exec_config = SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[])
    link = tmp_path / "worktrees" / "bot" / "elsewhere"
    link.symlink_to(tmp_path)
    argv, sandboxed = await shell.sandbox_argv("git commit", worktree, workspace, exec_config, writable=[worktree, tmp_path / "missing", link, worktree])
    assert sandboxed
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    # The missing path, the symlink and the duplicate are left out; the sandbox still runs.
    assert binds == [str(workspace), str(worktree)]
