"""Git-backed workspace checkpoints.

Every session workspace keeps a hidden repository (``.checkpoints``) whose work tree is
the workspace itself. A snapshot is taken before every operator turn and after every run,
so "undo the last three turns" can put the files back as well as the history. The
repository is derived data: deleting ``.checkpoints`` loses nothing but the undo.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DIR_NAME = ".checkpoints"
EXCLUDES = (DIR_NAME + "/", "node_modules/", ".venv/", "__pycache__/", "*.pyc")


class CheckpointError(RuntimeError):
    pass


class Checkpoints:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.git_dir = workspace / DIR_NAME

    async def _git(self, *args: str) -> str:
        env = {
            **os.environ,
            "GIT_DIR": str(self.git_dir),
            "GIT_WORK_TREE": str(self.workspace),
            "GIT_AUTHOR_NAME": "daedalus",
            "GIT_AUTHOR_EMAIL": "daedalus@localhost",
            "GIT_COMMITTER_NAME": "daedalus",
            "GIT_COMMITTER_EMAIL": "daedalus@localhost",
        }
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(self.workspace), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            raise CheckpointError(f"git {' '.join(args[:2])} failed: {err.decode('utf-8', 'replace').strip()[:300]}")
        return out.decode("utf-8", "replace")

    async def ensure(self) -> None:
        if (self.git_dir / "HEAD").exists():
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        await self._git("init", "-q")
        exclude = self.git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join(EXCLUDES) + "\n", encoding="utf-8")

    async def snapshot(self, label: str) -> str:
        """Commit the current state of the workspace; returns the commit id."""
        await self.ensure()
        await self._git("add", "-A", "--", ".")
        await self._git("commit", "-q", "--allow-empty", "-m", label[:200])
        return (await self._git("rev-parse", "HEAD")).strip()

    async def restore(self, sha: str) -> None:
        """Put the work tree back to ``sha``: tracked files reset, untracked files removed."""
        await self.ensure()
        await self._git("reset", "-q", "--hard", sha)
        await self._git("clean", "-qfd")

    async def head(self) -> str | None:
        if not (self.git_dir / "HEAD").exists():
            return None
        try:
            return (await self._git("rev-parse", "HEAD")).strip()
        except CheckpointError:
            return None


def workspace_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != DIR_NAME]
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


__all__ = ["DIR_NAME", "CheckpointError", "Checkpoints", "workspace_size"]
