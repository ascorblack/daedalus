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
SKIP_DIRS = {DIR_NAME, "node_modules", ".venv", "__pycache__"}
NESTED_SCAN_DEPTH = 6


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
        if not (self.git_dir / "HEAD").exists():
            self.workspace.mkdir(parents=True, exist_ok=True)
            await self._git("init", "-q")
            await self.relocate()
        await self._write_excludes()

    async def relocate(self) -> None:
        """Forget the work-tree path ``git init`` recorded, so a copied ``.checkpoints`` never points back at its origin."""
        try:
            await self._git("config", "--unset", "core.worktree")
        except CheckpointError:
            pass

    async def _write_excludes(self) -> list[str]:
        """Exclude derived directories and every nested git repository (a snapshot cannot hold those)."""
        nested = await asyncio.to_thread(nested_repos, self.workspace)
        exclude = self.git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lines = [*EXCLUDES, *(f"/{path}/" for path in nested)]
        text = "\n".join(lines) + "\n"
        if not exclude.exists() or exclude.read_text(encoding="utf-8") != text:
            exclude.write_text(text, encoding="utf-8")
        return nested

    async def snapshot(self, label: str) -> str:
        """Commit the current state of the workspace; returns the commit id."""
        await self.ensure()
        await self._git("add", "-A", "--", ".")
        await self._git("commit", "-q", "--allow-empty", "-m", label[:200])
        return (await self._git("rev-parse", "HEAD")).strip()

    async def restore(self, sha: str) -> list[str]:
        """Put the work tree back to ``sha``: tracked files reset, untracked files removed.

        The branch only ever moves forward: the state before the restore is committed first
        and the restored tree is committed after, so every earlier snapshot stays reachable
        and the restore itself can be undone. Returns the nested repositories the restore
        could not touch (their contents stay as they are).
        """
        await self.ensure()
        await self._git("add", "-A", "--", ".")
        await self._git("commit", "-q", "--allow-empty", "-m", f"before restore to {sha[:12]}")
        await self._git("read-tree", "-u", "--reset", sha)
        await self._git("clean", "-qfd")
        nested = await self._write_excludes()
        await self._git("add", "-A", "--", ".")
        await self._git("commit", "-q", "--allow-empty", "-m", f"restored to {sha[:12]}")
        return nested

    async def head(self) -> str | None:
        if not (self.git_dir / "HEAD").exists():
            return None
        try:
            return (await self._git("rev-parse", "HEAD")).strip()
        except CheckpointError:
            return None


def nested_repos(path: Path) -> list[str]:
    """Relative paths of git repositories inside the workspace (clones, worktrees), a bounded walk."""
    found: list[str] = []
    base_depth = len(path.parts)
    for root, dirs, files in os.walk(path):
        depth = len(Path(root).parts) - base_depth
        if ".git" in dirs or ".git" in files:
            if depth > 0:
                found.append(Path(root).relative_to(path).as_posix())
                dirs[:] = []
                continue
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d != ".git"] if depth < NESTED_SCAN_DEPTH else []
    return sorted(found)


def workspace_size(path: Path) -> int:
    """Bytes a snapshot would have to hold: derived directories and nested repositories are skipped."""
    total = 0
    nested = {os.path.join(path, p) for p in nested_repos(path)}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and os.path.join(root, d) not in nested]
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


__all__ = ["DIR_NAME", "CheckpointError", "Checkpoints", "nested_repos", "workspace_size"]
