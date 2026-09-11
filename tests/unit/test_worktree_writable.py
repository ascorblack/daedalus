"""A worktree a session opens is writable for its sandboxed commands from the moment it opens, and after a restart."""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


@pytest.mark.asyncio
async def test_open_writable_reaches_the_live_services_and_the_stored_session(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("self-change")
        repo = tmp_path / "repo"
        (repo / ".git" / "worktrees" / "fix").mkdir(parents=True)
        (repo / ".git" / "objects").mkdir()
        wt = tmp_path / "worktrees" / "bot" / "fix"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'fix'}\n")

        await manager.open_writable(state.session.id, wt)

        # The live services object the tools resolve sees it at once — no new turn needed.
        live = manager.locator_services(state.session.id)
        assert live is not None and wt in live.writable and repo / ".git" / "objects" in live.writable
        # And it survives a rebuild of the services: the session remembers the path, not a message about it.
        assert state.session.metadata["worktrees"] == [str(wt)]
        manager.register_services(state)
        again = manager.locator_services(state.session.id)
        assert again is not None and wt in again.writable
    finally:
        await manager.close()
