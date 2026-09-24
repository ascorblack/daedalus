"""Where a session in a project may read and where it may write.

A project has several folders, and a session is not free in all of them the same way: a folder the
operator marked read-only is read-only to every tool and to the sandbox, a session with a directory
of its own stays inside that directory, and a staff member working in a worktree writes there rather
than in the checkout it was made from. One pure function answers the question for every kind of
session, so the file tools, the file browser, ``Exec`` and the services all ask the same thing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from daedalus.stores.projects import Project, ProjectFolder

ISOLATIONS = ("shared", "worktree", "readonly")
"""How a staff member works in a git folder: in the checkout itself, in a worktree of its own, or
only reading. An ordinary session is ``shared``."""


@dataclass(frozen=True, slots=True)
class Walls:
    """The folders a session may read, and the ones it may write. Every writable path is readable."""

    readable: tuple[Path, ...]
    writable: tuple[Path, ...]

    def root_of(self, path: Path, *, write: bool = False) -> Path | None:
        """The wall ``path`` lies inside, judged on real paths; ``None`` when it is outside all of them.

        ``os.path.realpath`` resolves the symlinks it can and leaves a not-yet-created tail alone, so
        a file about to be written is judged by where it would land, and a link that points out of a
        folder is judged by where it points.
        """
        real = Path(os.path.realpath(path))
        for root in self.writable if write else self.readable:
            base = Path(os.path.realpath(root))
            if real == base or base in real.parents:
                return root
        return None


def worktree_writable_paths(worktree: Path) -> list[Path]:
    """The worktree itself and the parts of its repository a commit there writes.

    A worktree keeps its own HEAD, index and logs under the main repository's ``.git/worktrees/<name>``, and
    shares that repository's object store and the refs of its branch. A commit writes to all of them, so a
    session that may write its worktree gets those too — the shared object store (append-only by nature),
    the ``agent/`` branch refs and their reflogs — and not the rest of the repository's state.
    """
    paths = [worktree]
    dotgit = worktree / ".git"
    try:
        text = dotgit.read_text(encoding="utf-8") if dotgit.is_file() else ""
    except OSError:
        text = ""
    if not text.startswith("gitdir:"):
        return paths
    gitdir = Path(text.split(":", 1)[1].strip())
    if gitdir.parent.name != "worktrees":
        return paths + [gitdir]
    common = gitdir.parent.parent
    for extra in (gitdir, common / "objects", common / "refs" / "heads" / "agent", common / "logs" / "refs" / "heads" / "agent"):
        try:
            extra.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        paths.append(extra)
    return paths


def _inside(path: Path, folder: ProjectFolder) -> bool:
    return path == folder.path or folder.path in path.parents


def walls_for(
    project: Project,
    *,
    folder_id: str | None,
    directory: str | None,
    local_env: str,
    isolation: str = "shared",
    worktree: Path | None = None,
) -> Walls:
    """The walls of a session that works in ``folder_id`` (the primary when empty) of ``project``.

    - A session with a ``directory`` of its own (``.agents/<id>``) keeps the wall it always had: that
      directory alone, for reading and for writing.
    - Otherwise it reads every local folder of the project and writes every one not marked
      read-only. Only folders of this process's environment count: a host folder's path, seen from
      the container, names nothing the tools can reach, or worse an unrelated directory of the same
      name. A folder that is not reachable is left out as well, because a write under an unmounted
      mount point creates the path and the empty directory then shadows the real one when it comes
      back. The session's own folder is the exception: it is where the session works, and whether it
      is there is the run's question to answer, with a message naming the path.
    - ``isolation="worktree"`` writes ``worktree`` and what a commit in it touches instead of the
      folder the worktree was made from (nothing of it when that folder is read-only); every other
      writable folder stays writable.
    - ``isolation="readonly"`` writes nothing.

    Every writable folder is also readable. The function reads the filesystem only to ask whether a
    folder is there and where a worktree keeps its git metadata; it changes nothing but the
    directories a worktree commit needs to exist.
    """
    if isolation not in ISOLATIONS:
        raise ValueError(f"unknown isolation {isolation!r}; one of {', '.join(ISOLATIONS)}")
    home = project.folder(folder_id) if folder_id else project.primary
    if home is None:
        raise ValueError(f"{folder_id} is not a folder of {project.name}")
    if directory:
        own = Path(os.path.normpath(home.path / directory))
        # A read-only folder is read-only all the way down, the agent's own directory in it included.
        writes = () if home.readonly or isolation == "readonly" else (own,)
        return Walls(readable=(own,), writable=writes)
    local = [f for f in project.folders if f.local(local_env) and (f is home or f.reachable)]
    readable = [f.path for f in local]
    if isolation == "readonly":
        return Walls(readable=tuple(readable), writable=())
    writable = [f.path for f in local if not f.readonly]
    if isolation == "worktree":
        if worktree is None:
            raise ValueError("a worktree session needs the worktree it works in")
        # The folder the worktree belongs to is the one it lives in (``<folder>/.agents/worktrees/…``);
        # a worktree kept anywhere else is taken to be of the folder the session works in.
        source = next((f for f in local if _inside(worktree, f)), home)
        writable = [p for p in writable if p != source.path]
        if not source.readonly:
            # A worktree of a read-only folder lives in that folder and commits into its ``.git``, so
            # writing it would be writing the folder the operator closed.
            writable = [*worktree_writable_paths(worktree), *writable]
        if not any(_inside(worktree, f) for f in local):
            readable.append(worktree)
    return Walls(readable=tuple(readable), writable=tuple(writable))


__all__ = ["ISOLATIONS", "Walls", "walls_for", "worktree_writable_paths"]
