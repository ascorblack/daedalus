"""Self-development: worktrees, pull requests, approval cards, rebuild and rollback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from daedalus import supervisor_client

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

REPOS = ("bot", "core")


@dataclass(slots=True)
class RepoSpec:
    name: str
    checkout: Path
    worktrees: Path


class GitError(RuntimeError):
    pass


async def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: float = 600) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        raise GitError(f"timed out: {' '.join(cmd)}") from exc
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise GitError(f"{' '.join(cmd[:3])} failed ({proc.returncode}):\n{text[-1500:]}")
    return text


class SelfDevelopment:
    def __init__(self, app: Application) -> None:
        self.app = app
        s = app.settings
        self.repos = {
            "bot": RepoSpec("bot", s.bot_repo_dir, s.state_dir / "worktrees" / "bot"),
            "core": RepoSpec("core", s.core_repo_dir, s.state_dir / "worktrees" / "core"),
        }
        self._reason_waits: dict[int, str] = {}  # chat_id -> proposal id awaiting a reason

    # -- git plumbing ---------------------------------------------------------------

    def _git_env(self) -> dict[str, str]:
        token = self.app.settings.github_token
        env = {"GIT_TERMINAL_PROMPT": "0"}
        if token:
            env["GH_TOKEN"] = token
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "credential.helper"
            env["GIT_CONFIG_VALUE_0"] = "!f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f"
        return env

    async def git(self, repo: RepoSpec, *args: str, cwd: Path | None = None) -> str:
        return await _run(["git", "-C", str(cwd or repo.checkout), *args], env=self._git_env())

    async def gh(self, *args: str, cwd: Path) -> str:
        return await _run(["gh", *args], cwd=cwd, env=self._git_env())

    def repo(self, name: str) -> RepoSpec:
        if name not in self.repos:
            raise GitError(f"unknown repo {name!r}; use one of {REPOS}")
        return self.repos[name]

    async def workspace(self, repo_name: str, branch: str) -> Path:
        """Create (or reuse) a worktree for ``agent/<branch>`` off ``origin/main``."""
        repo = self.repo(repo_name)
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", branch).strip("-") or uuid.uuid4().hex[:8]
        full_branch = slug if slug.startswith("agent/") else f"agent/{slug}"
        target = repo.worktrees / slug
        if target.exists():
            return target
        repo.worktrees.mkdir(parents=True, exist_ok=True)
        await self.git(repo, "fetch", "--prune", "origin")
        try:
            await self.git(repo, "worktree", "add", "-B", full_branch, str(target), "origin/main")
        except GitError:
            await self.git(repo, "worktree", "prune")
            await self.git(repo, "worktree", "add", "-B", full_branch, str(target), "origin/main")
        await self.git(repo, "config", "user.name", "daedalus", cwd=target)
        await self.git(repo, "config", "user.email", "daedalus@localhost", cwd=target)
        return target

    async def _worktree_for(self, repo: RepoSpec, branch: str | None) -> Path:
        if branch:
            slug = branch.removeprefix("agent/")
            path = repo.worktrees / slug
            if path.exists():
                return path
            raise GitError(f"no worktree for branch {branch!r}; call self_workspace first")
        candidates = [p for p in repo.worktrees.iterdir() if p.is_dir()] if repo.worktrees.exists() else []
        if not candidates:
            raise GitError("no worktree exists; call self_workspace first")
        return max(candidates, key=lambda p: p.stat().st_mtime)

    # -- proposals ------------------------------------------------------------------

    async def propose(self, *, repo: str, title: str, summary: str, session_id: str | None, branch: str | None = None) -> str:
        spec = self.repo(repo)
        worktree = await self._worktree_for(spec, branch)
        status = await self.git(spec, "status", "--porcelain", cwd=worktree)
        if status.strip():
            raise GitError("the worktree has uncommitted changes; commit them first")
        head_branch = (await self.git(spec, "rev-parse", "--abbrev-ref", "HEAD", cwd=worktree)).strip()
        ahead = (await self.git(spec, "rev-list", "--count", "origin/main..HEAD", cwd=worktree)).strip()
        if ahead == "0":
            raise GitError("the branch has no commits beyond origin/main")
        await self.git(spec, "push", "-u", "origin", head_branch, "--force-with-lease", cwd=worktree)
        body = summary + "\n\n" + (f"Session: {session_id}" if session_id else "")
        existing = (await self.gh("pr", "list", "--head", head_branch, "--json", "number,url", cwd=worktree)).strip()
        prs = json.loads(existing or "[]")
        if prs:
            pr_url = prs[0]["url"]
            pr_number = int(prs[0]["number"])
            await self.gh("pr", "edit", str(pr_number), "--title", title, "--body", body, cwd=worktree)
        else:
            out = await self.gh("pr", "create", "--base", "main", "--head", head_branch, "--title", title, "--body", body, cwd=worktree)
            pr_url = out.strip().splitlines()[-1]
            pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        proposal_id = uuid.uuid4().hex[:10]
        diffstat = (await self.git(spec, "diff", "--stat", "origin/main...HEAD", cwd=worktree)).strip()
        await self.app.db.execute(
            "INSERT INTO change_proposals(id, repo, branch, pr_number, pr_url, title, summary, session_id, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (proposal_id, repo, head_branch, pr_number, pr_url, title, summary, session_id, "pending", datetime.now(UTC).isoformat()),
        )
        if self.app.config.self_change.approval == "auto":
            result = await self.decide(proposal_id, "approve", reason="auto-approval mode")
            return f"PR #{pr_number} {pr_url} — {result}"
        await self._send_card(proposal_id, repo, title, summary, pr_url, diffstat)
        return f"PR #{pr_number} opened: {pr_url}. Waiting for the operator's decision in chat."

    async def _send_card(self, proposal_id: str, repo: str, title: str, summary: str, pr_url: str, diffstat: str) -> None:
        front = self.app.front
        if front is None:
            return
        outbox = front._general_outbox()
        if outbox is None:
            return
        text = f"🛠 Change proposal ({repo}): {title}\n\n{summary[:1500]}\n\n{diffstat[-800:]}\n\n{pr_url}"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Approve", callback_data=f"cp:{proposal_id}:approve"),
                    InlineKeyboardButton(text="❌ Reject", callback_data=f"cp:{proposal_id}:reject"),
                ],
                [InlineKeyboardButton(text="✍️ Reject with reason", callback_data=f"cp:{proposal_id}:reason")],
            ]
        )
        msg = await front.bot.send_message(outbox.chat_id, text, message_thread_id=outbox.thread_id, reply_markup=keyboard)
        await self.app.db.execute("UPDATE change_proposals SET message_id = ? WHERE id = ?", (msg.message_id, proposal_id))

    async def decide(self, proposal_id: str, decision: str, *, reason: str = "") -> str:
        row = await self.app.db.fetchone("SELECT * FROM change_proposals WHERE id = ?", (proposal_id,))
        if row is None:
            return "unknown proposal"
        if row["status"] != "pending":
            return f"already {row['status']}"
        spec = self.repo(row["repo"])
        worktree = spec.worktrees / row["branch"].removeprefix("agent/")
        cwd = worktree if worktree.exists() else spec.checkout
        if decision == "approve":
            try:
                # Merge from the main checkout: gh would otherwise try to switch the worktree's branch.
                await self.gh("pr", "merge", str(row["pr_number"]), "--squash", cwd=spec.checkout)
            except GitError as exc:
                return f"merge failed: {exc}"
            await self._cleanup_branch(spec, row["branch"], worktree)
            await self.app.db.execute(
                "UPDATE change_proposals SET status = 'merged', decided_at = ?, reason = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), reason, proposal_id),
            )
            result = f"merged PR #{row['pr_number']}"
            if self.app.config.self_change.auto_rebuild:
                result += "; " + await self.rebuild(f"merged PR #{row['pr_number']}: {row['title']}")
            await self._notify_session(row["session_id"], f"Your change proposal '{row['title']}' was approved and merged. {result}")
            return result
        try:
            await self.gh("pr", "close", str(row["pr_number"]), "--comment", reason or "Rejected by the operator.", cwd=cwd)
        except GitError as exc:
            logger.warning("closing PR failed: %s", exc)
        await self.app.db.execute(
            "UPDATE change_proposals SET status = 'rejected', decided_at = ?, reason = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), reason, proposal_id),
        )
        await self._notify_session(
            row["session_id"],
            f"Your change proposal '{row['title']}' was rejected." + (f" Reason: {reason}" if reason else "") + " Revise it or ask for clarification.",
        )
        return f"rejected PR #{row['pr_number']}" + (f": {reason}" if reason else "")

    async def _cleanup_branch(self, spec: RepoSpec, branch: str, worktree: Path) -> None:
        """Remove the merged branch's worktree and its remote ref; failures are not fatal."""
        try:
            if worktree.exists():
                await self.git(spec, "worktree", "remove", "--force", str(worktree))
            await self.git(spec, "branch", "-D", branch)
        except GitError as exc:
            logger.warning("worktree cleanup: %s", exc)
        try:
            await self.git(spec, "push", "origin", "--delete", branch)
        except GitError as exc:
            logger.warning("remote branch cleanup: %s", exc)

    async def _notify_session(self, session_id: str | None, text: str) -> None:
        if not session_id or self.app.manager is None:
            return
        try:
            await self.app.manager.submit(session_id, text)
        except Exception:  # noqa: BLE001
            logger.exception("could not deliver the decision to session %s", session_id)

    # -- supervisor ---------------------------------------------------------------------

    async def rebuild(self, reason: str) -> str:
        try:
            return str(await supervisor_client.call(self.app.settings.supervisor_socket, "rebuild", reason=reason))
        except supervisor_client.SupervisorUnavailable as exc:
            return f"supervisor unavailable ({exc}); pull main and restart by hand"

    async def rollback(self, steps_back: int, reason: str = "") -> str:
        try:
            return str(await supervisor_client.call(self.app.settings.supervisor_socket, "rollback", steps_back=steps_back))
        except supervisor_client.SupervisorUnavailable as exc:
            return f"supervisor unavailable ({exc})"

    async def panic(self) -> str:
        try:
            return str(await supervisor_client.call(self.app.settings.supervisor_socket, "panic"))
        except supervisor_client.SupervisorUnavailable as exc:
            return f"supervisor unavailable ({exc})"

    async def supervisor_status(self) -> dict[str, Any] | None:
        try:
            return dict(await supervisor_client.call(self.app.settings.supervisor_socket, "status"))
        except (supervisor_client.SupervisorUnavailable, RuntimeError):
            return None

    # -- telegram wiring ----------------------------------------------------------------

    async def on_callback(self, query: CallbackQuery, data: list[str]) -> None:
        _, proposal_id, action = data
        if action == "reason":
            await query.answer()
            if query.message is not None:
                self._reason_waits[query.message.chat.id] = proposal_id
                await self.app.front.bot.send_message(  # type: ignore[union-attr]
                    query.message.chat.id,
                    "Why is it rejected? (reply in one message)",
                    message_thread_id=query.message.message_thread_id if query.message.is_topic_message else None,
                    reply_markup=ForceReply(selective=True),
                )
            return
        result = await self.decide(proposal_id, "approve" if action == "approve" else "reject")
        await query.answer(result[:200])
        if query.message is not None:
            try:
                await query.message.edit_text(f"{query.message.text}\n\n→ {result}", reply_markup=None)
            except Exception:  # noqa: BLE001
                pass

    async def intercept_message(self, message: Message) -> bool:
        proposal_id = self._reason_waits.pop(message.chat.id, None)
        if proposal_id is None:
            return False
        result = await self.decide(proposal_id, "reject", reason=message.text or message.caption or "")
        await message.answer(result)
        return True


async def install(app: Application) -> list[asyncio.Task[None]]:
    selfdev = SelfDevelopment(app)
    app.extensions["selfdev"] = selfdev
    assert app.manager is not None

    async def self_workspace(*, repo: str, branch: str, **_: Any) -> str:
        path = await selfdev.workspace(repo, branch)
        return f"worktree ready at {path} (branch agent/{branch.removeprefix('agent/')}, based on origin/main)"

    async def self_propose(*, repo: str, title: str, summary: str, session_id: str | None = None, branch: str | None = None, **_: Any) -> str:
        try:
            return await selfdev.propose(repo=repo, title=title, summary=summary, session_id=session_id, branch=branch)
        except GitError as exc:
            return f"proposal failed: {exc}"

    async def self_rebuild(*, reason: str, **_: Any) -> str:
        return await selfdev.rebuild(reason)

    async def self_rollback(*, steps_back: int = 0, reason: str = "", **_: Any) -> str:
        return await selfdev.rollback(steps_back, reason)

    app.manager.service_hooks.update(
        {
            "self_workspace": self_workspace,
            "self_propose": self_propose,
            "self_rebuild": self_rebuild,
            "self_rollback": self_rollback,
        }
    )
    front = app.front
    if front is not None:
        front.callback_hooks["cp"] = selfdev.on_callback
        front.message_interceptors.append(selfdev.intercept_message)

        async def op_rebuild(args: str) -> str:
            return await selfdev.rebuild(args or "operator request")

        async def op_rollback(args: str) -> str:
            try:
                steps = int(args.strip()) if args.strip() else 0
            except ValueError:
                return "usage: /rollback [steps_back]"
            return await selfdev.rollback(steps)

        async def op_panic(args: str) -> str:
            if app.manager is not None:
                for state in list(app.manager._states.values()):
                    await app.manager.stop(state.session.id)
            return await selfdev.panic()

        front.operator_hooks.update({"rebuild": op_rebuild, "rollback": op_rollback, "panic": op_panic})
    return []


__all__ = ["GitError", "SelfDevelopment", "install"]
