"""The git credential helper answers with the organisation's token for its repositories and the operator's for the rest."""

from __future__ import annotations

import subprocess
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2] / "launcher" / "git-credential-daedalus"


def _fill(path: str, env: dict[str, str]) -> str:
    return subprocess.run([str(HELPER), "get"], input=f"protocol=https\nhost=github.com\npath={path}\n", capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env}, check=True).stdout


def test_helper_picks_the_token_by_repository_owner() -> None:
    env = {"GH_TOKEN": "operator-token", "GH_ORG_TOKEN": "org-token", "DAEDALUS_GITHUB_ORG": "example-org"}
    assert "password=org-token" in _fill("example-org/tooling.git", env)
    assert "password=operator-token" in _fill("someone/daedalus.git", env)
    assert "username=x-access-token" in _fill("someone/daedalus.git", env)


def test_helper_without_an_organisation_uses_the_one_token() -> None:
    assert "password=operator-token" in _fill("example-org/x.git", {"GH_TOKEN": "operator-token"})
    assert _fill("x/y.git", {}) == ""
    assert subprocess.run([str(HELPER), "store"], input="", capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}).stdout == ""


def test_git_hands_the_repository_path_to_the_helper(tmp_path: Path, monkeypatch) -> None:
    """git only sends the owner/repo path to a helper with credential.useHttpPath on: the supervisor's git
    config must turn it on, or the helper never sees the owner and answers with the operator's token."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("supervisor", HELPER.parent / "supervisor.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    for key in ("GITHUB_TOKEN", "GITHUB_DAEDALUS_TOKEN", "DAEDALUS_GITHUB_ORG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "operator-token")
    monkeypatch.setenv("GITHUB_DAEDALUS_TOKEN", "org-token")
    monkeypatch.setenv("DAEDALUS_GITHUB_ORG", "example-org")
    spec.loader.exec_module(module)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    env.update({k: v for k, v in module.bot_env().items() if k.startswith(("GIT_CONFIG", "GH_", "DAEDALUS_GITHUB"))})
    fill = subprocess.run(["git", "credential", "fill"], input="url=https://github.com/example-org/tooling.git\n\n", capture_output=True, text=True, env=env, check=True).stdout
    assert "password=org-token" in fill
    fill = subprocess.run(["git", "credential", "fill"], input="url=https://github.com/someone/daedalus.git\n\n", capture_output=True, text=True, env=env, check=True).stdout
    assert "password=operator-token" in fill
