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
