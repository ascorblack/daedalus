"""Self-development: worktrees, pull requests, approval cards, rebuild and rollback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus import supervisor_client
from daedalus.host import reachability
from daedalus.security import redact

# A command that runs Python tests. Such a run prints how many it executed, so a receipt for one that
# says nothing, or says zero, is not evidence that anything was checked. Recognising a runner is not
# guessing from a word in the string: see `_is_test_run` in daedalus.tools.verify.
from daedalus.tools.verify import _is_test_run as PYTHON_TEST_RUN
from daedalus.tools.verify import file_digest as FILE_DIGEST

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message

    from daedalus.app import Application

logger = logging.getLogger(__name__)

REPOS = ("bot", "core")
RECEIPT_COMMAND_CHARS = 160



@dataclass(slots=True)
class RepoSpec:
    name: str
    checkout: Path
    worktrees: Path


class GitError(RuntimeError):
    pass


_TOKEN_RE = re.compile(r"(https?://)[^/@\s]+@")
_PRIVATE_LINES = re.compile(r"(?im)^\s*(session|run|operator|owner|claude-session|co-authored-by|generated[- ]with|signed-off-by)\s*:.*(?:\n|$)")
_PRIVATE_ADDRESSES = re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}(?:\.\d{1,3}){1,2}\b|/home/[A-Za-z0-9_-]+|/srv/state/[^\s`'\"]*")
_PRIVATE_PROSE = re.compile(
    r"(?i)\b(?:board\s+thread|review\s+(?:thread|round)|defect\s+catalogue|defect\s+#\d+|\(?seq\s+\d{3,}\)?|session\s+id\s+[0-9a-f]{6,}|run\s+id\s+[0-9a-f]{6,})\b"
)
"""Coordination references written as prose rather than as trailers: a board thread, a message
sequence number, a review round or a defect number describe how the change came about, not the
change, and name places the public repository must not point at."""


def public_references(text: str) -> list[str]:
    """The prose references in ``text`` that ``public_text`` cannot simply strip (they sit mid-sentence)."""
    return sorted({m.group(0).strip() for m in _PRIVATE_PROSE.finditer(text)})


def public_text(text: str) -> str:
    """Strip what a pull request or commit must not carry into a public repository.

    Session and run identifiers, tooling trailers and co-author lines go; private network
    addresses and paths on the operator's machine are replaced with a placeholder. The
    redactor handles credentials before this runs.
    """
    text = _PRIVATE_LINES.sub("", redact.shared().redact(text))
    text = _PRIVATE_ADDRESSES.sub("<redacted>", text)
    return text.strip()



async def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: float = 600) -> str:
    """Run a git/gh command; returns stdout only, stderr goes into the error message."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        raise GitError(f"timed out: {' '.join(cmd)}") from exc
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        detail = _TOKEN_RE.sub(r"\1***@", (err.decode("utf-8", "replace") + text)[-1500:])
        raise GitError(f"{' '.join(cmd[:3])} failed ({proc.returncode}):\n{detail}")
    return text


ADDED_LINES_CAP = 200
NEW_MODULE_LINES_CAP = 150
_REPLACEMENT_WORDS = re.compile(r"\b(replaces?|replacing|removes?|removing|deletes?|deleting|supersedes?|superseding|folds? into)\b", re.IGNORECASE)


class ProposalRefused(GitError):
    """A proposal gate said no; the message tells the agent what to do instead."""


# The modules a process may start in, written here rather than read from the tree being audited.
# These three are the boot set: "python -m daedalus", the app object the supervisor loads, and the
# benchmark harbor. A new entry point is not a new process start — it is a module reached from one
# of these — so widening this tuple is a deliberate change to the gate itself, not something a
# proposed tree can do to itself. ``test_the_gate_carries_its_own_boot_set`` holds the two lists
# together so drift is a failing test rather than a quiet widening.
SELF_NAMABLE = ("daedalus.__main__", "daedalus.app", "daedalus.bench.harbor")


def relevance_gate(root: Path, changed_files: list[str], execution_path: str | None) -> None:
    """A changed host module must sit on an execution path the agent can name.

    ``execution_path`` is ``pkg.module`` or ``pkg.module:symbol``; the module must exist in the
    proposed tree and, through the static import graph, reach every changed module. A change that
    touches no host module (tests, docs, skills, the Mini App, deploy files) needs no path.

    Naming the changed module itself is refused, because a module is not the reason it runs. The
    exception is a boot module: it is where the process starts, so naming it is naming the runner
    rather than the module under it. That set is ``SELF_NAMABLE``, a constant of this module and not
    the audited tree's copy of it: a rule a proposal can extend is not a rule. The exception applies
    to every boot module, not only to one nothing imports. For ``daedalus/__main__.py`` it is also
    the only name there is — itself is refused, nothing else reaches it, and no path at all is
    refused as well; for ``daedalus.app`` something does reach it, but the reason it runs is still
    that the process starts there.
    """
    present = [f for f in changed_files if (root / f).is_file()]  # a deleted module needs no path: it is gone
    modules = reachability.modules_for_files(root, present)
    if not modules:
        return
    named = execution_path.split(":", 1)[0].strip() if execution_path else ""
    if named in modules and named not in SELF_NAMABLE:
        raise ProposalRefused(
            f"execution_path {execution_path!r} is the changed module itself. Name the code that runs it — the tool, "
            "hook, extension or startup step that imports it — not the module being changed."
        )
    if not execution_path or not execution_path.strip():
        raise ProposalRefused(
            "the change touches host modules (" + ", ".join(modules) + ") but names no execution_path. "
            "Pass execution_path='pkg.module' or 'pkg.module:symbol' for the path that runs the changed code "
            "(a tool, a hook, an extension's install, a startup step). A module nothing reaches is an "
            "experiment: keep it in your own repository."
        )
    ok, missing = reachability.path_reaches(root, execution_path, modules)
    if not ok:
        raise ProposalRefused(
            f"execution_path {execution_path!r} does not reach: " + ", ".join(missing) + ". "
            "Either the path is wrong or the module is not imported by anything that runs; wire it "
            "(import it from the tool, hook or extension that uses it) or drop it."
        )
    dead = [m for m in reachability.unreachable_modules(root) if m in modules]
    if dead:
        raise ProposalRefused(
            "these changed modules are not reachable from the process entry points (daedalus.__main__, daedalus.app): "
            + ", ".join(dead) + ". Tests do not count as importers."
        )


def _receipt_time(row: dict[str, Any]) -> float | None:
    """When the receipt was recorded, as a POSIX timestamp, or ``None`` if the row will not say."""
    raw = row.get("at")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


def _names(command: str, choices: set[str]) -> bool:
    """Whether ``command`` contains one of ``choices`` as a whole word.

    ``test_boot_guard`` counts as part of ``tests/unit/test_boot_guard.py``; ``config`` does not count
    as part of ``config.toml`` because that token is the whole path ``daedalus/config.py``.
    """
    return any(re.search(r"(?<![A-Za-z0-9_-])" + re.escape(tok) + r"(?![A-Za-z0-9_])", command) for tok in choices)


def _recorded_digests(row: dict[str, Any]) -> dict[str, str]:
    """The content the receipt says it ran against, keyed by path from the repo root.

    A row written before this column existed, an empty one, and one holding something that is not an
    object all read as "this receipt makes no content claim" — which is what they are. The gate then
    falls back to the timestamp rule for such a row rather than refusing it: an old receipt is not a
    wrong one.
    """
    raw = row.get("file_digests")
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _ran_no_tests(row: dict[str, Any]) -> str | None:
    """Why this passing receipt is worth nothing, or ``None`` when it is worth what it says.

    A Python test run is evidence only if it executed at least one test: a run that collected nothing,
    or that the receipt cannot account for, proves the runner started, not that the change was checked.
    Commands that are not Python test runs are not judged by this rule — the gate does not guess what
    another runner's output means.
    """
    if not PYTHON_TEST_RUN(str(row.get("command") or "")):
        return None
    count = row.get("tests_run")
    if count is None:
        return "the receipt does not say how many tests it ran"
    try:
        ran = int(count)
    except (TypeError, ValueError):
        return "the receipt's test count is not a number"
    if ran < 1:
        return "the run executed no tests"
    return None


def evidence_gate(root: Path, changed_files: list[str], receipts: list[dict[str, Any]], execution_path: str | None) -> None:
    """Every changed host module must be named by a passing Verify receipt that postdates it.

    ``root`` is the worktree the change lives in: the files are judged as they are there, not as they
    are in whatever checkout the process happens to be running from.

    A receipt names a changed file when its command mentions the file, the module by its dotted name,
    ``test_<stem>``, or the module named by ``execution_path``. ``pytest tests`` alone does not count
    for a host-module change: it proves the tree is green, not that the changed path ran. Every changed
    host module needs a receipt of its own — one receipt that names one of them says nothing about the
    rest, and a receipt for a differently named test does not reach the module it tests.

    Naming the change is not enough on its own: the receipt must also have been recorded at or after
    the last write to the file it names, or it proves something about bytes that are no longer in the
    tree. A row whose timestamp is missing or unreadable is no evidence at all, not an exemption —
    ``receipt_rows`` selects the column, so the host always supplies it.

    A green test run that executed nothing is a specific hazard, and the window is judged for it as a
    whole rather than row by row. If every passing receipt is such a run, the change has no check at all.
    And while such a run is in the window, naming a module with a command that only reads it is not
    enough: each changed host module then needs a run that counted tests and postdates it, because
    otherwise any unrelated green run launders the empty one and the count rule is undone by arrangement.

    Four limits are known and are named rather than hidden. The receipt's ``tree`` field — the commit it
    ran against — is shown to a reader and is not compared here, because a commit is a name and a rebase
    renames identical content; what a receipt can be held to is the ``file_digests`` it carries, and the
    comparison against the proposed worktree is made on those. A row without digests (one written before
    the column existed) is still judged by modification time: an edit followed by a timestamp of an
    earlier date defeats that rule, and such a row is the only case where a content-preserving rewrite
    (a ``touch``, a formatter, a rebase) blocks the change. A deleted file has no bytes left to cover, so
    a receipt that names it is enough for it — but a symlink whose target is gone is refused, because a
    path no checkout can read is not a deletion. And a command that runs the tests through a wrapper this
    module does not recognise is treated as no test run at all, so its count is not read: a runner the
    receipt cannot parse is not counted, and the cost is that such a receipt has to name the code some
    other way.
    """
    root = root.resolve()
    green = [r for r in receipts if r.get("passed")]
    passed = [r for r in green if _ran_no_tests(r) is None]
    empty = [r for r in green if _ran_no_tests(r) is not None]
    if not green:
        raise ProposalRefused(
            "no passing Verify receipt recorded since the branch started. Run the tests and the changed "
            "path through Verify (not Exec) so the receipts are on the card, then propose again."
        )
    modules = reachability.modules_for_files(root, changed_files)
    if not modules:
        return

    tokens_for: dict[str, set[str]] = {}
    must_be_named: set[str] = set()
    for f in changed_files:
        path = Path(f)
        if path.suffix != ".py" or not path.parts:
            continue
        if path.parts[0] == reachability.PACKAGE:
            tokens_for[f] = {f, f"test_{path.stem}", ".".join(path.with_suffix("").parts)}
            must_be_named.add(f)
        else:
            # A test that was changed along with the code is part of what the receipt claims: a
            # receipt naming it must also postdate it, or an edit to the covering test slips past.
            tokens_for[f] = {f}
    path_tokens: set[str] = set()
    if execution_path:
        module = execution_path.split(":", 1)[0].strip()
        path_tokens = {module, module.replace(".", "/") + ".py"}
        if module.endswith(".__main__"):
            path_tokens.add("-m " + module.removesuffix(".__main__"))  # `python -m package` runs package.__main__

    def naming(f: str) -> list[dict[str, Any]]:
        """The passing receipts whose command names this file."""
        mine = tokens_for[f]
        found = []
        for row in passed:
            command = str(row.get("command") or "")
            # the declared execution path runs the host modules of the proposal, so it names them too
            if _names(command, mine) or (f in must_be_named and path_tokens and _names(command, path_tokens)):
                found.append(row)
        return found

    def _covers(row: dict[str, Any], f: str, path: Path, newest: float) -> bool:
        """Whether this receipt covers the file: by its recorded bytes when it has them, else by time.

        A receipt that fingerprinted this file is judged by content alone — the bytes are the claim — and
        a receipt that did not is judged by the clock, as every receipt was before the column existed.
        The two are not mixed inside one row: a digest that matches is not rescued by a fresh timestamp,
        and a row with no digest is not refused for lacking one.
        """
        recorded = _recorded_digests(row)
        if recorded and f in recorded:
            return recorded[f] == FILE_DIGEST(path)
        return _postdates(row, newest)

    def _why_stale(f: str, rows: list[dict[str, Any]], path: Path, newest: float) -> str:
        """Why the receipts that name this file do not cover it: content or clock, said plainly."""
        if any(f in _recorded_digests(r) and _recorded_digests(r)[f] != FILE_DIGEST(path) for r in rows):
            return "the receipts ran against content that is not what the file holds now"
        dates = ", ".join(sorted({str(r.get("at") or "no timestamp") for r in rows}))
        return f"last written {datetime.fromtimestamp(newest, tz=UTC).isoformat()}; receipts at {dates}"

    def _postdates(row: dict[str, Any], newest: float) -> bool:
        when = _receipt_time(row)
        return when is not None and when >= newest

    def counted_tests(row: dict[str, Any]) -> bool:
        """Whether the receipt is a test run that says it executed at least one test.

        The same reading as ``_ran_no_tests``, so the two cannot drift: the row is a counted test run
        exactly when it is a test run and that function has no complaint about it.
        """
        return PYTHON_TEST_RUN(str(row.get("command") or "")) and _ran_no_tests(row) is None

    # A symlink whose target is gone is not a deleted file. The deletion exemption says "no bytes left to
    # cover"; a broken link leaves a path in the tree that no checkout can read, and exempting it would
    # turn "the file is not there" into a reason to accept an old receipt.
    broken = [f for f in sorted(tokens_for) if (root / f).is_symlink() and not (root / f).exists()]
    if broken:
        raise ProposalRefused(
            "a changed path is a symlink whose target is gone: " + ", ".join(broken)
            + ". The deletion exemption does not apply to a link that points nowhere — no receipt can "
            "have covered that file. Repair or remove the link, then propose again."
        )

    unnamed: list[str] = []
    stale: list[str] = []
    unaudited: list[str] = []
    for f in sorted(tokens_for):
        rows = naming(f)
        if not rows:
            if f in must_be_named:
                unnamed.append(f)
            continue
        path = root / f
        deleted = not path.exists() and not path.is_symlink()
        newest = None if deleted else path.stat().st_mtime
        covered = deleted or any(_covers(r, f, path, newest) for r in rows)
        if not covered:
            stale.append(f"{f} ({_why_stale(f, rows, path, newest)})")
            continue
        # A green test run that executed nothing must not be what stands behind a changed module: while
        # such a row is in the window, naming the module is not enough — a run that counted tests has to
        # name it. Without this, any unrelated counted run launders an empty one, and a command that only
        # reads the file reopens the hole the count rule is meant to close.
        if empty and f in must_be_named and not any(
            counted_tests(r) and (deleted or _covers(r, f, path, newest)) for r in rows
        ):
            unaudited.append(f)

    if not passed and empty:
        raise ProposalRefused(
            "the only passing receipts prove nothing about the change: "
            + "; ".join(f"receipt {r.get('id')} — {_ran_no_tests(r)} ({str(r.get('command') or '')[:RECEIPT_COMMAND_CHARS]})" for r in empty)
            + ". Collect-only, a run with no matching tests, or a receipt too old to carry the count is not a check: run the "
            "tests the change actually exercises with Verify, after the last edit."
        )
    # A green test run that executed nothing must not sit in the window while the change rides on a
    # receipt that only reads a file: that is the count rule undone by arrangement. If the window holds
    # such a row and no run that really counted tests, the proposal is refused for what it is.
    real_run = any(counted_tests(r) for r in passed)
    if empty and not real_run:
        raise ProposalRefused(
            "the window holds a test run that proves nothing ("
            + "; ".join(f"receipt {r.get('id')} — {_ran_no_tests(r)} ({str(r.get('command') or '')[:RECEIPT_COMMAND_CHARS]})" for r in empty)
            + ") and no run that counted any tests, so what names the changed code is a command that only read a file. "
            "Run the tests the change exercises with Verify, after the last edit."
        )
    if unaudited:
        raise ProposalRefused(
            "a test run that executed nothing is in the window, so naming a changed module with a command "
            "that only reads it is not enough: " + ", ".join(unaudited) + ". While such a row is on the "
            "receipts, every changed host module needs a run that counted tests and postdates it. Run those "
            "tests with Verify, after the last edit."
        )
    if unnamed:
        raise ProposalRefused(
            "no passing Verify receipt names the changed code: " + ", ".join(unnamed) + ". Every changed host module "
            "needs its own: Verify a command that runs it — its test file, its dotted module (python -m pkg.module or "
            "an import), or the file path itself."
        )
    if stale:
        raise ProposalRefused(
            "the passing receipt names the changed code but does not cover the bytes now in the tree: "
            + "; ".join(stale)
            + ". A green run does not cover an edit made after it, and a receipt that fingerprinted the file is "
            "read against its content. Run the check again with Verify — after the last edit — so the receipt "
            "covers the bytes that are in the branch."
        )


def size_gate(added_lines: int, new_modules: dict[str, int], summary: str) -> None:
    """A large or net-new change must say what it replaces; net-additive self-modification is the pathology."""
    big_new = {m: n for m, n in new_modules.items() if n > NEW_MODULE_LINES_CAP}
    if added_lines <= ADDED_LINES_CAP and not big_new:
        return
    if _REPLACEMENT_WORDS.search(summary):
        return
    what = f"{added_lines} added lines" + (f"; new modules over {NEW_MODULE_LINES_CAP} lines: " + ", ".join(big_new) if big_new else "")
    raise ProposalRefused(
        f"large change ({what}) whose summary does not say what it replaces or removes. State the code, "
        "behaviour or workaround this supersedes (or removes), or split the change; a self-change that only adds is suspect."
    )


class SelfDevelopment:
    def __init__(self, app: Application) -> None:
        self.app = app
        s = app.settings
        self.repos = {
            "bot": RepoSpec("bot", s.bot_repo_dir, s.state_dir / "worktrees" / "bot"),
            "core": RepoSpec("core", s.core_repo_dir, s.state_dir / "worktrees" / "core"),
        }
        self._reason_waits: dict[tuple[int, int], str] = {}  # (chat_id, thread_id) -> proposal id
        self._background: set[asyncio.Task[None]] = set()

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
        slug = self.slug_of(branch) or uuid.uuid4().hex[:8]
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

    @staticmethod
    def slug_of(branch: str) -> str:
        return re.sub(r"[^a-zA-Z0-9._-]+", "-", branch.removeprefix("agent/")).strip("-")

    async def _worktree_for(self, repo: RepoSpec, branch: str | None) -> Path:
        if branch:
            slug = self.slug_of(branch)
            path = repo.worktrees / slug
            if path.exists():
                return path
            raise GitError(f"no worktree for branch {branch!r}; call self_workspace first")
        candidates = [p for p in repo.worktrees.iterdir() if p.is_dir()] if repo.worktrees.exists() else []
        if not candidates:
            raise GitError("no worktree exists; call self_workspace first")
        return max(candidates, key=lambda p: p.stat().st_mtime)

    # -- proposals ------------------------------------------------------------------

    async def propose(self, *, repo: str, title: str, summary: str, session_id: str | None, branch: str | None = None, execution_path: str | None = None) -> str:
        spec = self.repo(repo)
        worktree = await self._worktree_for(spec, branch)
        status = await self.git(spec, "status", "--porcelain", cwd=worktree)
        if status.strip():
            raise GitError("the worktree has uncommitted changes; commit them first")
        head_branch = (await self.git(spec, "rev-parse", "--abbrev-ref", "HEAD", cwd=worktree)).strip()
        ahead = (await self.git(spec, "rev-list", "--count", "origin/main..HEAD", cwd=worktree)).strip()
        messages = await self.git(spec, "log", "--format=%H%x00%B%x1e", "origin/main..HEAD", cwd=worktree)
        for record in messages.split("\x1e"):
            sha, _, body = record.strip("\n").partition("\x00")
            # The agent may sign its own work; everything else public_text() strips has no place in a public history.
            signed = re.sub(r"(?im)^\s*co-authored-by:.*(?:\n|$)", "", body)
            if signed.strip() and (public_text(signed).strip() != signed.strip() or public_references(signed)):
                raise GitError(
                    f"commit {sha[:8]} carries a reference the public repository must not: "
                    "no session or run ids, board threads, message sequence numbers, review rounds or defect numbers "
                    f"(a Co-authored-by line is fine); found: {', '.join(public_references(signed)) or 'a private line'}. "
                    "Amend the message (git commit --amend / rebase) and propose again."
                )
        if ahead == "0":
            raise GitError("the branch has no commits beyond origin/main")
        # The same rule for what the change itself says: docstrings, comments and tests are public text too.
        added = await self.git(spec, "diff", "origin/main...HEAD", "--unified=0", "--no-color", cwd=worktree)
        added_lines = "\n".join(line[1:] for line in added.splitlines() if line.startswith("+") and not line.startswith("+++"))
        leaks = public_references(added_lines) + ([m.group(0) for m in _PRIVATE_ADDRESSES.finditer(added_lines)][:3])
        if redact.shared().redact(added_lines) != added_lines:
            leaks.append("a credential-looking value (masked here)")
        if leaks:
            raise GitError(
                "the diff carries a reference the public repository must not (in a docstring, comment or test): "
                f"{', '.join(dict.fromkeys(leaks))}. Describe the change on its own terms and propose again."
            )
        since = await self._branch_started(spec, worktree)
        changed_files = [f for f in (await self.git(spec, "diff", "--name-only", "origin/main...HEAD", cwd=worktree)).split("\n") if f.strip()]
        if repo == "bot":
            relevance_gate(worktree, changed_files, execution_path)
            evidence_gate(worktree, changed_files, await self.receipt_rows(session_id, since=since) if session_id else [], execution_path)
        numstat = await self.git(spec, "diff", "--numstat", "origin/main...HEAD", cwd=worktree)
        added_total = sum(int(a) for a, _, _ in (line.split("\t", 2) for line in numstat.splitlines() if "\t" in line) if a.isdigit())
        new_files = [f for f in (await self.git(spec, "diff", "--name-only", "--diff-filter=A", "origin/main...HEAD", cwd=worktree)).split("\n") if f.endswith(".py") and not f.startswith("tests/")]
        new_modules = {f: sum(1 for _ in (worktree / f).open(encoding="utf-8")) if (worktree / f).is_file() else 0 for f in new_files}
        size_gate(added_total, new_modules, summary)
        await self.git(spec, "push", "-u", "origin", head_branch, "--force-with-lease", cwd=worktree)
        receipts = await self.receipts_for(session_id, since=since) if session_id else ""
        if execution_path:
            receipts = f"\n\nExecution path: `{execution_path}`" + receipts
        body = public_text(summary) + public_text(receipts)
        existing = (await self.gh("pr", "list", "--head", head_branch, "--json", "number,url", cwd=worktree)).strip()
        try:
            prs = json.loads(existing or "[]")
        except json.JSONDecodeError as exc:
            raise GitError(f"unexpected gh output: {existing[:300]}") from exc
        if prs:
            pr_url = prs[0]["url"]
            pr_number = int(prs[0]["number"])
            await self.gh("pr", "edit", str(pr_number), "--title", title, "--body", body, cwd=worktree)
        else:
            await self.gh("pr", "create", "--base", "main", "--head", head_branch, "--title", title, "--body", body, cwd=worktree)
            created = (await self.gh("pr", "view", head_branch, "--json", "number,url", cwd=worktree)).strip()
            try:
                info = json.loads(created)
            except json.JSONDecodeError as exc:
                raise GitError(f"unexpected gh output: {created[:300]}") from exc
            pr_url = info["url"]
            pr_number = int(info["number"])
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
        await self._send_card(proposal_id, repo, title, summary + receipts, pr_url, diffstat)
        return f"PR #{pr_number} opened: {pr_url}. Waiting for the operator's decision in chat."

    async def _branch_started(self, spec: RepoSpec, worktree: Path) -> str | None:
        """When the branch diverged from main: receipts older than that are not evidence for it.

        The merge base's commit date, not the branch's first commit: a Verify run before the first
        commit still counts, and an amend or rebase (which the leak gate itself asks for) does not
        move the window.
        """
        try:
            base = (await self.git(spec, "merge-base", "origin/main", "HEAD", cwd=worktree)).strip()
            stamp = (await self.git(spec, "log", "-1", "--format=%cI", base, cwd=worktree)).strip() if base else ""
        except GitError:
            return None
        return datetime.fromisoformat(stamp).astimezone(UTC).isoformat() if stamp else None

    async def receipt_rows(self, session_id: str, *, since: str | None = None, hours: int = 24) -> list[dict[str, Any]]:
        since = since or (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        rows = await self.app.db.fetchall("SELECT id, criterion, command, exit_code, passed, at, tree, tests_run, tests_skipped, file_digests FROM verifications WHERE session_id = ? AND at >= ? ORDER BY id DESC LIMIT 50", (session_id, since))
        return [dict(r) for r in rows]

    async def receipts_for(self, session_id: str, *, since: str | None = None, hours: int = 24) -> str:
        """Verification receipts the proposing session recorded for this work, as evidence on the card.

        The command is shown whole (clipped with an explicit mark) and a shell fallback that can
        turn a failure into exit 0 is pointed out, so a green mark is read for what it is.
        """
        since = since or (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        rows = await self.app.db.fetchall(
            "SELECT id, criterion, command, exit_code, passed, sandboxed, dependencies, tree, tests_run, tests_skipped, file_digests FROM verifications WHERE session_id = ? AND at >= ? ORDER BY id DESC LIMIT 12", (session_id, since)
        )
        if not rows:
            return "\n\nVerification receipts: none — nothing in this proposal was checked with Verify."
        r = redact.shared()
        lines = []
        for row in rows:
            command = r.redact(row["command"])
            shown = command if len(command) <= RECEIPT_COMMAND_CHARS else command[:RECEIPT_COMMAND_CHARS] + "…"
            caveats = []
            if re.search(r"\|\||;\s*(true|exit 0)\b|set \+e", command):
                caveats.append("has a shell fallback that can mask a failure")
            if re.search(r"\|\s*(?:tail|head|grep|wc)\b", command):
                caveats.append("pipes the output into a filter; read the receipt's output, not only its exit code")
            if not row["sandboxed"]:
                caveats.append("unsandboxed")
            if PYTHON_TEST_RUN(command):
                try:
                    ran = int(row["tests_run"]) if row["tests_run"] is not None else None
                except (TypeError, ValueError):
                    ran = None
                if ran is None:
                    caveats.append("a test run whose receipt does not say how many tests it executed")
                elif ran < 1:
                    caveats.append("a test run that executed no tests")
            suffix = f" ⚠ {'; '.join(caveats)}" if caveats else ""
            deps = r.redact((row["dependencies"] or "").strip())
            deps_note = f" · deps: {deps}" if deps else ""
            # The dirty marker is the part a reader must not lose: a bare hash looks like a clean tree.
            tree = row["tree"] or ""
            tree_note = f", tree {tree[:12]}{'+dirty' if tree.endswith('+worktree') else ''}" if tree else ""
            run_note = f", {row['tests_run']} tests run" if row["tests_run"] is not None else ""
            covers = len(_recorded_digests(dict(row)))
            bytes_note = f", covers {covers} changed file" + ("s" if covers != 1 else "") if covers else ""
            lines.append(f"- {'✅' if row['passed'] else '❌'} {r.redact(row['criterion'])} — `{shown}` (exit {row['exit_code']}, receipt v{row['id']}{tree_note}{run_note}{bytes_note}){deps_note}{suffix}")
        return "\n\nVerification receipts:\n" + "\n".join(lines)

    async def _send_card(self, proposal_id: str, repo: str, title: str, summary: str, pr_url: str, diffstat: str) -> None:
        front = self.app.front
        if front is None:
            return
        outbox = front._general_outbox()
        if outbox is None:
            return
        text = f"🛠 Change proposal ({repo}): {title}\n\n{summary[:1500]}\n\n{diffstat[-800:]}\n\n{pr_url}"
        message_id = await front.send_choice(
            outbox,
            text,
            [
                [("✅ Approve", f"cp:{proposal_id}:approve"), ("❌ Reject", f"cp:{proposal_id}:reject")],
                [("✍️ Reject with reason", f"cp:{proposal_id}:reason")],
            ],
        )
        await self.app.db.execute("UPDATE change_proposals SET message_id = ? WHERE id = ?", (message_id, proposal_id))

    async def decide(self, proposal_id: str, decision: str, *, reason: str = "") -> str:
        row = await self.app.db.fetchone("SELECT * FROM change_proposals WHERE id = ?", (proposal_id,))
        if row is None:
            return "unknown proposal"
        if row["status"] != "pending":
            return f"already {row['status']}"
        spec = self.repo(row["repo"])
        worktree = spec.worktrees / self.slug_of(row["branch"])
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
                result += "; " + await self.rebuild_when_idle(f"merged PR #{row['pr_number']}: {row['title']}")
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
            await self.app.manager.submit(session_id, text, as_answer=False, origin="selfdev")
        except Exception:  # noqa: BLE001
            logger.exception("could not deliver the decision to session %s", session_id)

    # -- supervisor ---------------------------------------------------------------------

    async def rebuild_when_idle(self, reason: str) -> str:
        """Rebuild once no run is active: a restart mid-run kills the run that proposed the change."""
        manager = self.app.manager
        if manager is None or not manager.running_run_ids():
            return await self.rebuild(reason)

        async def _wait_then_rebuild() -> None:
            deadline = asyncio.get_running_loop().time() + 60 * self.app.config.self_change.rebuild_wait_minutes
            while manager.running_run_ids() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(5)
            outcome = await self.rebuild(reason)
            inbox = self.app.extensions.get("inbox")
            if inbox is not None:
                await inbox.post("rebuild", f"Rebuild: {reason}", outcome, severity="notice")

        task = asyncio.create_task(_wait_then_rebuild(), name="rebuild-when-idle")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return "rebuild scheduled for when no run is active (the running ones finish first)"

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
        if len(data) != 3:
            await query.answer("stale button")
            return
        _, proposal_id, action = data
        if action == "reason":
            await query.answer()
            if query.message is not None:
                thread = query.message.message_thread_id if query.message.is_topic_message else 0
                self._reason_waits[(query.message.chat.id, thread or 0)] = proposal_id
                await self.app.front.send_force_reply(  # type: ignore[union-attr]
                    query.message.chat.id,
                    query.message.message_thread_id if query.message.is_topic_message else None,
                    "Why is it rejected? (reply in one message)",
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
        thread = message.message_thread_id if message.is_topic_message else 0
        proposal_id = self._reason_waits.pop((message.chat.id, thread or 0), None)
        if proposal_id is None:
            return False
        result = await self.decide(proposal_id, "reject", reason=message.text or message.caption or "")
        await message.answer(result)
        return True


async def install(app: Application) -> list[asyncio.Task[None]]:
    selfdev = SelfDevelopment(app)
    app.extensions["selfdev"] = selfdev
    assert app.manager is not None

    async def self_workspace(*, repo: str, branch: str, session_id: str | None = None, **_: Any) -> str:
        path = await selfdev.workspace(repo, branch)
        if session_id and app.manager is not None:
            # The worktree is where this session's changes go: its Exec, Verify and services may write there.
            await app.manager.open_writable(session_id, path)
        return f"worktree ready at {path} (branch agent/{branch.removeprefix('agent/')}, based on origin/main)"

    async def self_propose(*, repo: str, title: str, summary: str, session_id: str | None = None, branch: str | None = None, execution_path: str | None = None, **_: Any) -> str:
        try:
            return await selfdev.propose(repo=repo, title=title, summary=summary, session_id=session_id, branch=branch, execution_path=execution_path)
        except GitError as exc:
            raise RuntimeError(f"proposal failed: {exc}") from exc  # the tool turns it into an error result, not a success that reads like one

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
