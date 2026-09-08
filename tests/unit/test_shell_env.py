"""The Exec/Verify subprocess environment is a strict allowlist.

Regression test for the env-hermeticity fix (SAR-006, shared with
xChuCx/agent-memory): the old denylist only filtered a fixed list of
secret names, so any *new* secret the bot's environment gained later
(e.g. ``GITHUB_TOKEN``, ``GH_TOKEN``, ``SUPERVISOR_SOCKET``) leaked into
every tool subprocess. The allowlist inverts that: only known-safe names
are inherited; everything else stays in the bot's process unless passed
explicitly through the tool's ``env`` parameter.
"""

from __future__ import annotations

import pytest

from daedalus.tools.shell import shell_environment


@pytest.fixture
def secret_env(monkeypatch):
    """Seed the bot's environment with secrets and internal values the old
    denylist missed, drawn from the real production environment."""
    secrets = {
        # the git token the old denylist let through
        "GITHUB_TOKEN": "ghp_should_not_leak",
        "GH_TOKEN": "ghp_should_not_leak",
        # internal / operator-facing values from the real env
        "SUPERVISOR_SOCKET": "/run/daedalus/supervisor.sock",
        "DAEDALUS_SUPERVISOR_SOCKET": "/run/daedalus/supervisor.sock",
        "OWNER_USER_ID": "123456789",
        "USD_PER_DAY": "50",
        "API_HOST": "127.0.0.1",
        "API_PORT": "8080",
        # internal path / repo vars from the real env
        "DAEDALUS_STATE": "/srv/state",
        "DAEDALUS_WORKSPACES": "/srv/workspaces",
        "DAEDALUS_REBUILD_TRIGGER_DIR": "/srv/state/rebuild",
        "STATE_DIR": "/srv/state",
        "WORKSPACES_DIR": "/srv/workspaces",
        "BOT_REPO_DIR": "/srv/daedalus",
        "CORE_REPO_DIR": "/srv/protocore-exp",
        # a brand-new secret name the old denylist would never have matched
        "POSTINGBOARD_KEY": "pb_should_not_leak",
        # the git-config triplet: values can carry secrets (http.*.extraheader)
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": "Authorization: Bearer should_not_leak",
        # names the old denylist *did* match (kept as a sanity check)
        "TELEGRAM_BOT_TOKEN": "tg_should_not_leak",
        "SOME_API_KEY": "ak_should_not_leak",
        "SOME_SECRET": "sec_should_not_leak",
        "SOME_PASSWORD": "pw_should_not_leak",
    }
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    return secrets


def test_secrets_do_not_reach_subprocess(secret_env):
    env = shell_environment("sess-1")
    for name in secret_env:
        assert name not in env, f"{name} leaked into the subprocess environment"


def test_git_token_is_excluded(secret_env):
    # the specific defect from the review thread: the git token stayed in
    env = shell_environment("sess-1")
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_git_config_triplet_is_excluded(secret_env):
    # the reviewer's residual-leak finding: the GIT_CONFIG_VALUE_* prefix
    # could carry a secret (http.*.extraheader), so the whole triplet is
    # dropped; identity is covered by GIT_AUTHOR_*/GIT_COMMITTER_*.
    env = shell_environment("sess-1")
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env


def test_safe_base_is_inherited(secret_env, monkeypatch):
    # make sure the safe base is present even if the host env is minimal
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/daedalus")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "daedalus")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "daedalus@localhost")
    env = shell_environment("sess-1")
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/daedalus"
    assert env["GIT_AUTHOR_NAME"] == "daedalus"
    assert env["GIT_AUTHOR_EMAIL"] == "daedalus@localhost"


def test_explicit_extra_passes_through(secret_env):
    # a caller that genuinely needs a value passes it explicitly
    env = shell_environment("sess-1", extra={"GITHUB_TOKEN": "explicit", "MY_VAR": "x"})
    assert env["GITHUB_TOKEN"] == "explicit"
    assert env["MY_VAR"] == "x"


def test_session_id_cannot_be_overridden_by_extra(secret_env):
    # DAEDALUS_SESSION_ID is assigned after extra, so a caller cannot spoof it
    env = shell_environment("sess-42", extra={"DAEDALUS_SESSION_ID": "spoofed"})
    assert env["DAEDALUS_SESSION_ID"] == "sess-42"


def test_session_id_is_always_set(secret_env):
    env = shell_environment("sess-42")
    assert env["DAEDALUS_SESSION_ID"] == "sess-42"


def test_extra_overrides_inherited(secret_env, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    env = shell_environment("sess-1", extra={"PATH": "/custom/bin"})
    assert env["PATH"] == "/custom/bin"
