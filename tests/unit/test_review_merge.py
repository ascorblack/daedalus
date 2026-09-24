"""Review and merge of staff branches, with real git in temporary repositories."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.board import Board
from daedalus.extensions.review import Review, ReviewRefused
from daedalus.extensions.staff import Team
from daedalus.host.session_runner import SessionManager
from daedalus.staff_runtime import FakeStaffRuntime, StartRequest
from daedalus.stores.database import Database
from daedalus.stores.projects import Project
from daedalus.stores.staff import Staff
from tests.unit.test_session_runner import ScriptedProvider, _manager
from tests.unit.test_staff_runtime import board_task, events, git, project_with, repository, task_row, team_for

HEADERS = {"X-Daedalus-Token": "tok"}


class Rig:
    def __init__(self, manager: SessionManager, team: Team, runtime: FakeStaffRuntime, project: Project, board: Board, review: Review) -> None:
        self.manager, self.team, self.runtime, self.project, self.board, self.review = manager, team, runtime, project, board, review

    @property
    def folder(self) -> Path:
        assert self.project.primary is not None
        return self.project.primary.path

    async def hire(self, name: str = "Ada") -> Staff:
        return await self.manager.staff.hire(self.project.id, name=name, isolation="worktree")

    async def reviewed(self, member: Staff, title: str, files: dict[str, str]) -> tuple[str, StartRequest]:
        """A task the member finished: its files committed on the staff branch and reported done."""
        task_id = await board_task(self.manager, self.project, title)
        await self.team.assign(member, task_id)
        req = next(r for r in reversed(self.runtime.started) if r.task.id == task_id)
        assert req.worktree is not None
        for name, text in files.items():
            (req.worktree.cwd / name).write_text(text)
        git(req.worktree.path, "add", "-A")
        git(req.worktree.path, "commit", "-qm", f"{title} by {member.name}")
        live = await self.team.live_of(member)
        assert live is not None
        await self.team.ingress.report(live, "done", "finished")
        await self.team.ingress.status(live, "turn_done_unseen")
        assert (await task_row(self.manager, task_id))["status"] == "review"
        return task_id, req


async def rig(settings: Settings, db: Database, tmp_path: Path) -> Rig:
    manager = await _manager(settings, db, ScriptedProvider([]))
    team = await team_for(settings, manager)
    runtime = FakeStaffRuntime(kind="daedalus")
    team.runtimes["daedalus"] = runtime
    project = await project_with(manager, repository(tmp_path))
    app = team.app
    app.config = manager.config  # type: ignore[attr-defined]
    board = Board(app)  # type: ignore[arg-type]
    app.extensions["board"] = board  # type: ignore[attr-defined]
    review = Review(app, team)  # type: ignore[arg-type]
    team.review = review
    return Rig(manager, team, runtime, project, board, review)


def head(folder: Path) -> str:
    return git(folder, "rev-parse", "HEAD").strip()


async def test_a_clean_branch_merges_as_a_merge_commit_and_the_task_is_done(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        ada = await r.hire()
        task_id, req = await r.reviewed(ada, "Menu page", {"menu.md": "bread\ncake\n", "prices.md": "bread 3\n"})
        seen = await r.review.review(task_id)
        assert seen["can_merge"] and seen["blockers"] == [] and seen["conflicts"] == []
        assert seen["branch"] == req.worktree.branch and seen["base"] == "main" and seen["current"] == "main" and seen["on_base"]  # type: ignore[union-attr]
        assert [c["subject"] for c in seen["commits"]] == ["Menu page by Ada"] and seen["commits"][0]["author"] == "daedalus"
        assert sorted(f["path"] for f in seen["files"]) == ["menu.md", "prices.md"] and (seen["added"], seen["removed"]) == (3, 0)
        assert "+cake" in seen["patch"] and seen["patch_complete"]
        before = head(r.folder)

        # Accept on a branch task is Merge.
        done = await r.board.accept(task_id)
        assert done["status"] == "done" and done["merge_state"] == "merged" and done["merge"]["commit"] == head(r.folder)
        parents = git(r.folder, "rev-list", "--parents", "-n", "1", "HEAD").split()
        assert len(parents) == 3 and parents[1] == before, "a merge commit even though a fast-forward was possible"
        assert git(r.folder, "log", "-1", "--format=%s").strip() == f"Merge {req.worktree.branch}: Menu page"  # type: ignore[union-attr]
        assert (r.folder / "menu.md").read_text() == "bread\ncake\n" and git(r.folder, "status", "--porcelain") == ""
        # Nothing else in this folder for Ada: her idle session ends, the worktree goes, the merged branch goes.
        assert done["merge"]["worktree_removed"] and done["merge"]["branch_deleted"]
        assert not req.worktree.path.exists() and req.worktree.branch not in git(r.folder, "branch", "--list")  # type: ignore[union-attr]
        assert await r.team.live_of(ada) is None and r.runtime.stopped
        accepted = await events(r.manager, "task.accepted")
        assert [(e.payload["task_id"], e.payload["actor"], e.payload["merge"]) for e in accepted] == [(task_id, "operator", done["merge"]["commit"])]
        journal = [e.text for e in await r.manager.projects.journal(r.project.id)]
        assert any(text.startswith(f"Merged {req.worktree.branch} into main (1 commits, +3 −0)") for text in journal)  # type: ignore[union-attr]
        with pytest.raises(ValueError, match="only a task in review"):
            await r.board.accept(task_id)
    finally:
        await r.manager.close()


async def test_a_conflict_is_refused_the_folder_untouched_and_the_orchestrator_told(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        ada = await r.hire()
        task_id, req = await r.reviewed(ada, "Readme", {"README.md": "bakery by ada\n"})
        (r.folder / "README.md").write_text("bakery by the operator\n")
        git(r.folder, "commit", "-qam", "operator edit")
        before = head(r.folder)
        seen = await r.review.review(task_id)
        assert not seen["can_merge"] and seen["conflicts"] == ["README.md"] and [b["code"] for b in seen["blockers"]] == ["conflicts"]
        # Merge is disabled on a conflict, so the review that sees it is what tells the orchestrator.
        assert seen["merge_state"] == "conflict" and len(await events(r.manager, "task.merge_failed")) == 1
        assert (await r.review.review(task_id))["merge_state"] == "conflict" and len(await events(r.manager, "task.merge_failed")) == 1

        with pytest.raises(ReviewRefused, match="conflict in README.md"):
            await r.review.merge(task_id)
        assert head(r.folder) == before and git(r.folder, "status", "--porcelain") == "" and not (r.folder / ".git" / "MERGE_HEAD").exists()
        row = await task_row(r.manager, task_id)
        assert (row["status"], row["merge_state"]) == ("review", "conflict")
        [failed] = await events(r.manager, "task.merge_failed")
        assert failed.payload["task_id"] == task_id and failed.payload["conflicts"] == ["README.md"] and failed.project_id == r.project.id
        # The branch is the only copy of the work: it stays.
        assert req.worktree.branch in git(r.folder, "branch", "--list")  # type: ignore[union-attr]
    finally:
        await r.manager.close()


async def test_a_merge_that_stops_on_a_conflict_is_aborted(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dry run can be stale by the time Merge is pressed; the merge itself undoes what it started."""
    r = await rig(settings, db, tmp_path)
    try:
        ada = await r.hire()
        task_id, _req = await r.reviewed(ada, "Readme", {"README.md": "bakery by ada\n"})
        (r.folder / "README.md").write_text("bakery by the operator\n")
        git(r.folder, "commit", "-qam", "operator edit")
        before = head(r.folder)
        real = r.team.worktrees.compare

        async def stale(*args: Any, **kwargs: Any) -> Any:
            found = await real(*args, **kwargs)
            object.__setattr__(found, "conflicts", [])
            return found

        monkeypatch.setattr(r.team.worktrees, "compare", stale)
        with pytest.raises(ReviewRefused, match="did not go through and was undone"):
            await r.review.merge(task_id)
        assert head(r.folder) == before and git(r.folder, "status", "--porcelain") == "" and (r.folder / "README.md").read_text() == "bakery by the operator\n"
        assert (await task_row(r.manager, task_id))["merge_state"] == "conflict" and len(await events(r.manager, "task.merge_failed")) == 1
    finally:
        await r.manager.close()


async def test_a_dirty_folder_or_one_on_another_branch_is_refused(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        ada = await r.hire()
        task_id, _req = await r.reviewed(ada, "Menu", {"menu.md": "bread\n"})
        (r.folder / "README.md").write_text("half-edited\n")
        seen = await r.review.review(task_id)
        assert [b["code"] for b in seen["blockers"]] == ["dirty"] and not seen["folder_clean"]
        with pytest.raises(ReviewRefused, match="uncommitted changes"):
            await r.review.merge(task_id)
        assert (r.folder / "README.md").read_text() == "half-edited\n", "the operator's edit is never touched"
        git(r.folder, "checkout", "--", "README.md")

        git(r.folder, "switch", "-qc", "experiment")
        seen = await r.review.review(task_id)
        assert [b["code"] for b in seen["blockers"]] == ["moved"] and not seen["on_base"] and seen["current"] == "experiment"
        with pytest.raises(ReviewRefused, match="on experiment, not on main"):
            await r.review.merge(task_id)
        assert await events(r.manager, "task.merge_failed") == [], "the operator's own folder is theirs to put right; nobody is woken"
        assert (await task_row(r.manager, task_id))["status"] == "review"
        git(r.folder, "switch", "-q", "main")
        assert (await r.review.merge(task_id))["status"] == "done"
    finally:
        await r.manager.close()


async def test_the_patch_is_bounded(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        ada = await r.hire()
        files = {f"f{i:02d}.txt": "".join(f"line {n}\n" for n in range(40)) for i in range(6)}
        task_id, _req = await r.reviewed(ada, "Many", files)
        assert r.project.primary is not None
        branch = (await task_row(r.manager, task_id))["branch"]
        few = await r.team.worktrees.compare(r.project.primary, branch, max_files=2)
        assert len(few.files) == 6 and few.added == 240 and not few.patch_complete
        assert few.patch.count("diff --git") == 2
        short = await r.team.worktrees.compare(r.project.primary, branch, max_lines=50)
        assert short.patch.count("diff --git") == 1 and not short.patch_complete, "at least one file is shown, then the line budget stops"
        clipped = await r.team.worktrees.compare(r.project.primary, branch, max_chars=100)
        assert len(clipped.patch) == 100 and not clipped.patch_complete
        whole = await r.team.worktrees.compare(r.project.primary, branch)
        assert whole.patch.count("diff --git") == 6 and whole.patch_complete
    finally:
        await r.manager.close()


async def test_the_orchestrator_cannot_finish_or_merge_the_work(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        ada = await r.hire()
        task_id, _req = await r.reviewed(ada, "Menu", {"menu.md": "bread\n"})
        orchestrator = await r.manager.create_session(title="Orchestrator · Bakery", metadata={"orchestrator_of": r.project.id}, project_id=r.project.id)
        await r.manager.projects.set_orchestrator(r.project.id, expect="", value=orchestrator.session.id)
        with pytest.raises(ValueError, match="unmerged work"):
            await r.board.update(task_id, status="done", actor=orchestrator.session.id)
        with pytest.raises(ReviewRefused, match="only the operator merges"):
            await r.board.accept(task_id, by="orchestrator")
        assert (await task_row(r.manager, task_id))["status"] == "review"
    finally:
        await r.manager.close()


async def test_a_next_task_in_the_folder_keeps_the_worktree(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        ada = await r.hire()
        task_id, req = await r.reviewed(ada, "Menu", {"menu.md": "bread\n"})
        await board_task(r.manager, r.project, "Prices")
        nxt = await r.manager.db.fetchone("SELECT id FROM board_tasks WHERE title = 'Prices'")
        await r.manager.db.execute("UPDATE board_tasks SET assignee_staff_id = ? WHERE id = ?", (ada.id, nxt["id"]))
        done = await r.review.merge(task_id)
        assert not done["merge"]["worktree_removed"] and req.worktree.path.exists()  # type: ignore[union-attr]
        # Still checked out in the kept worktree, so git keeps the (merged) branch; nothing is forced.
        assert not done["merge"]["branch_deleted"]
    finally:
        await r.manager.close()


async def test_a_folder_with_no_identity_merges_under_the_agents_name(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        ada = await r.hire()
        task_id, _req = await r.reviewed(ada, "Menu", {"menu.md": "bread\n"})
        git(r.folder, "config", "--unset", "user.name")
        git(r.folder, "config", "--unset", "user.email")
        git(r.folder, "config", "user.useConfigOnly", "true")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        await r.review.merge(task_id)
        assert git(r.folder, "log", "-1", "--format=%cn <%ce>").strip() == "daedalus <daedalus@localhost>"
    finally:
        await r.manager.close()


async def test_a_rejection_goes_back_to_the_member_with_the_note(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        ada = await r.hire()
        task_id, _req = await r.reviewed(ada, "Menu", {"menu.md": "bread\n"})
        with pytest.raises(ReviewRefused, match="needs a note"):
            await r.review.reject(task_id, "  ")
        back = await r.review.reject(task_id, "Prices\nin euros")
        assert back["told"] and back["status"] == "doing" and back["merge_state"] == "rejected"
        [(_, message)] = r.runtime.sent
        assert "Prices\nin euros" in message.text and "report done again" in message.text
        assert "sent back by the operator: Prices in euros" in back["notes"]
        moved = [(e.payload["from"], e.payload["to"], e.payload["actor"]) for e in await events(r.manager, "task.moved")]
        assert moved[-1] == ("review", "doing", "operator")

        # With the session gone, the task goes back to the queue and the next session starts from the note.
        live = await r.team.live_of(ada)
        assert live is not None
        await r.team.ingress.report(live, "done", "again")
        await r.team.release(ada)
        await r.manager.db.execute("UPDATE board_tasks SET status = 'review', assignee_staff_id = ? WHERE id = ?", (ada.id, task_id))
        again = await r.review.reject(task_id, "Add the cakes too")
        assert not again["told"] and again["status"] == "doing"
        assert "The operator sent this work back from review: Add the cakes too" in r.runtime.started[-1].first_message
    finally:
        await r.manager.close()


async def test_the_review_routes(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        ada = await r.hire()
        task_id, _req = await r.reviewed(ada, "Menu", {"menu.md": "bread\n"})
        plain = await board_task(r.manager, r.project, "Plain")
        app = SimpleNamespace(settings=settings, config=r.manager.config, db=db, manager=r.manager, front=None, extensions={"staff": r.team, "board": r.board}, guard=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            assert (await client.get(f"/api/board/{task_id}/review")).status_code == 401
            seen = await client.get(f"/api/board/{task_id}/review", headers=HEADERS)
            assert seen.status_code == 200 and seen.json()["can_merge"] and seen.json()["files"] == [{"path": "menu.md", "added": 1, "removed": 0}]
            assert (await client.get("/api/board/nope/review", headers=HEADERS)).status_code == 404
            assert (await client.get(f"/api/board/{plain}/review", headers=HEADERS)).status_code == 409
            assert (await client.post(f"/api/board/{task_id}/reject", headers=HEADERS, json={"note": ""})).status_code == 422
            assert (await client.post(f"/api/board/{task_id}/reject", headers=HEADERS, json={"note": "x", "more": 1})).status_code == 422
            (r.folder / "README.md").write_text("dirty\n")
            refused = await client.post(f"/api/board/{task_id}/merge", headers=HEADERS)
            assert refused.status_code == 409 and "uncommitted" in refused.json()["detail"]
            git(r.folder, "checkout", "--", "README.md")
            merged = await client.post(f"/api/board/{task_id}/merge", headers=HEADERS)
            assert merged.status_code == 200 and merged.json()["status"] == "done"
            assert (await client.post(f"/api/board/{task_id}/reject", headers=HEADERS, json={"note": "late"})).status_code == 409
    finally:
        await r.manager.close()
