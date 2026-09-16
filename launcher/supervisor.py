#!/usr/bin/env python3
"""Daedalus supervisor — PID 1 in the container, never edited by the agent.

Responsibilities, and nothing more:

* start the bot and restart it when it dies (with backoff);
* on ``rebuild``: preflight ``origin/main`` on a candidate checkout while the bot keeps
  running, then move both repositories to it and restart; a revision that fails the
  preflight never touches the running bot;
* on ``restart``: apply what the checkout already holds. Where the checkout is the only
  place a change lives (no remote to pull from), the same preflight runs on a detached
  worktree of the commit that is there, and the bot restarts only if it passes;
* on ``rollback``: check out an earlier known-good revision and restart;
* on ``panic``: kill the whole process tree immediately;
* roll back by itself when a revision cannot boot: three short-lived starts inside ten
  minutes put the last known-good commit back;
* enforce the daily spend cap by reading the bot's usage table.

Commands arrive as JSON lines on a unix socket. Standard library only.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BOT_REPO = Path(os.environ.get("DAEDALUS_BOT_REPO", "/srv/daedalus"))
CORE_REPO = Path(os.environ.get("DAEDALUS_CORE_REPO", "/srv/protocore-exp"))
STATE = Path(os.environ.get("DAEDALUS_STATE", "/srv/state"))
WORKSPACES = Path(os.environ.get("DAEDALUS_WORKSPACES", "/srv/workspaces"))
SOCKET = Path(os.environ.get("DAEDALUS_SUPERVISOR_SOCKET", "/run/daedalus/supervisor.sock"))
BOT_CMD = os.environ.get("DAEDALUS_BOT_CMD", "uv run --frozen python -m daedalus serve")
REBUILD_TRIGGER_DIR = Path(os.environ.get("DAEDALUS_REBUILD_TRIGGER_DIR", "/run/daedalus-rebuild"))
"""Shared with the rebuilder sidecar (the only container that holds the docker socket)."""
USD_PER_DAY = float(os.environ.get("USD_PER_DAY", "0") or 0)
"""Daily spend cap, from the supervisor's own environment — never from a file the bot can edit."""
GOOD_DIR = STATE / "good"
HISTORY = GOOD_DIR / "history.json"
FAILED = GOOD_DIR / "FAILED"
LAST_REBUILD = GOOD_DIR / "LAST_REBUILD"
LIMIT_FLAG = STATE / "BUDGET_EXCEEDED"
LOG = STATE / "supervisor.log"

REBUILD_TRIGGER_FILES = ("deploy/Dockerfile", "deploy/compose.yaml", "deploy/apt-packages.txt")
DEPENDENCY_FILES = ("uv.lock", "pyproject.toml")
"""A change to one of these is the only reason to sync the virtualenv: it lives on a volume that outlives the
container, so syncing it when nothing declared a dependency is minutes of waiting for no difference."""
CANDIDATE = STATE / "preflight"
"""Detached checkouts of the revision about to run, one per repository under the names the running checkouts
have, so the bot repository's ``../protocore-exp`` path dependency resolves to the candidate core. The
preflight runs here while the bot keeps serving on the old revision; the running checkouts move only once it
passes."""
VENV = Path(os.environ.get("UV_PROJECT_ENVIRONMENT", "/srv/venv"))
SELFDEV_DIR = STATE / "selfdev"
APPLY_RESULT = SELFDEV_DIR / "result.json"
"""What the last apply attempt did, for the bot to read after it comes back up and show in the app. The
supervisor writes it when it refuses or reverses a change; the bot writes it when the change is live."""
CONFIGURED_MODE = os.environ.get("DAEDALUS_SELFDEV_MODE", "auto")
"""``server``, ``local``, ``off`` or ``auto``; the same value the bot resolves for itself. The supervisor
resolves it again from what it can see, because it decides before the bot is running."""

HEALTHY_SECONDS = 120
"""A start that lasts this long is what ``record_good`` calls healthy; one that does not is a failed boot."""
BOOT_WINDOW_SECONDS = 600
BOOT_THRESHOLD = 3
"""Three failed boots inside ten minutes — the boot guard's own numbers — mean the revision cannot run."""


def log(message: str) -> None:
    line = f"{datetime.now(UTC).isoformat()} {message}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def bot_env() -> dict[str, str]:
    """Environment the bot (and its preflight) runs with: paths owned by the supervisor."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Commits the agent makes in its workspace carry its own identity; the repos' local config covers the self-development checkouts.
    for key, value in (("GIT_AUTHOR_NAME", "Daedalus"), ("GIT_AUTHOR_EMAIL", "daedalus@localhost"), ("GIT_COMMITTER_NAME", "Daedalus"), ("GIT_COMMITTER_EMAIL", "daedalus@localhost")):
        env.setdefault(key, value)
    token = env.get("GITHUB_TOKEN")
    org_token = env.get("GITHUB_DAEDALUS_TOKEN", "")
    if token or org_token:
        # git authenticates with a token, never with a stored password. Two tokens when the agent has an
        # organisation of its own: the helper answers with the organisation's token for that owner's
        # repositories and with the operator's token for everything else.
        env["GH_TOKEN"] = token or org_token
        if org_token:
            env["GH_ORG_TOKEN"] = org_token
        # The helper picks the token by the repository owner, which git only sends when useHttpPath is on:
        # without it every push goes out with the operator's token and the organisation answers 403.
        env["GIT_CONFIG_COUNT"] = "2"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = f"!{Path(__file__).parent / 'git-credential-daedalus'}"
        env["GIT_CONFIG_KEY_1"] = "credential.useHttpPath"
        env["GIT_CONFIG_VALUE_1"] = "true"
    env.update(
        {
            "BOT_REPO_DIR": str(BOT_REPO),
            "CORE_REPO_DIR": str(CORE_REPO),
            "STATE_DIR": str(STATE),
            "WORKSPACES_DIR": str(WORKSPACES),
            "SUPERVISOR_SOCKET": str(SOCKET),
        }
    )
    return env


SSH_SOURCE = Path(os.environ.get("DAEDALUS_SSH_SOURCE", "/srv/ssh"))
SSH_HOME = Path(os.environ.get("DAEDALUS_SSH_HOME", str(Path.home() / ".ssh")))


def install_ssh() -> None:
    """Copy the mounted ssh material into ~/.ssh: the mount is read-only and owned by the host user,
    and ssh refuses a private key it can read too widely, so the copy carries the modes ssh wants."""
    if not SSH_SOURCE.is_dir():
        return
    try:
        SSH_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(SSH_HOME, 0o700)
        for src in sorted(p for p in SSH_SOURCE.iterdir() if p.is_file()):
            target = SSH_HOME / src.name
            target.write_bytes(src.read_bytes())
            os.chmod(target, 0o644 if src.name.endswith(".pub") or src.name in ("config", "known_hosts") else 0o600)
    except OSError as exc:
        log(f"ssh material not installed: {exc}")


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 1800) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout, env=bot_env()
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    return proc.returncode, (proc.stdout + proc.stderr)[-20000:]


def git(repo: Path, *args: str) -> tuple[int, str]:
    code, out = run(["git", "-C", str(repo), *args], timeout=600)
    if args and args[0] in ("reset", "checkout", "fetch"):
        restore_owner(repo)
    return code, out


def restore_owner(repo: Path) -> None:
    """Keep the checkout owned by whoever owns its root: the supervisor runs as root on a
    host-mounted repository, and files it rewrites must stay editable from the host."""
    try:
        st = repo.stat()
    except OSError:
        return
    if st.st_uid == os.getuid():
        return
    run(["chown", "-R", f"{st.st_uid}:{st.st_gid}", str(repo)], timeout=600)


def head(repo: Path) -> str:
    code, out = git(repo, "rev-parse", "HEAD")
    return out.strip() if code == 0 else "unknown"


def load_history() -> list[dict[str, Any]]:
    if HISTORY.exists():
        try:
            return json.loads(HISTORY.read_text())
        except json.JSONDecodeError:
            return []
    return []


def record_good() -> None:
    GOOD_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()
    entry = {"bot": head(BOT_REPO), "core": head(CORE_REPO), "at": datetime.now(UTC).isoformat()}
    if history and history[-1]["bot"] == entry["bot"] and history[-1]["core"] == entry["core"]:
        return
    history.append(entry)
    HISTORY.write_text(json.dumps(history[-50:], indent=2))
    log(f"recorded known-good bot={entry['bot'][:10]} core={entry['core'][:10]}")


def has_origin(repo: Path) -> bool:
    """Whether the checkout has somewhere to push to. Read from the git configuration rather than asked of
    git: this decides how a restart behaves and must not depend on a subprocess that can hang."""
    config = repo / ".git" / "config"
    try:
        return '[remote "origin"]' in config.read_text(encoding="utf-8")
    except OSError:
        return False


def resolve_mode(configured: str, *, repo: Path, token: str) -> str:
    """Which self-development this installation does, decided the way the bot decides it.

    ``server`` needs a remote to pull from and a token to reach it; ``local`` needs only the checkout,
    because the change is already in it. An explicit setting is taken as given — the operator's word is
    what the bot obeys too, and the two must not disagree about which preflight runs.
    """
    if configured in ("off", "local", "server"):
        return configured
    if has_origin(repo) and token.strip():
        return "server"
    return "local" if (repo / ".git").exists() else "off"


def needs_new_image(changed: set[str]) -> bool:
    """Whether the change rewrites the environment rather than the code that runs in it."""
    return any(path in changed for path in REBUILD_TRIGGER_FILES) or any(path.startswith("launcher/") for path in changed)


def venv_ready(venv: Path) -> bool:
    """Whether the virtualenv can run the preflight.

    Not merely whether it exists: the preflight's last step is the test suite, and a virtualenv synced
    without the ``dev`` extra — which is what ``uv run --frozen`` leaves behind when it starts the bot —
    has no test runner in it. Such an environment fails the preflight on the runner rather than on the
    change, which reads as a broken change and is not one.
    """
    return any((venv / d / name).exists() for d in ("bin", "Scripts") for name in ("pytest", "pytest.exe"))


def needs_dependency_sync(changed: set[str], *, venv: Path | None = None) -> bool:
    """Whether the virtualenv has to be synced before this revision can run.

    Two reasons, and no others: the revision declares different dependencies, or the virtualenv is not
    one the preflight can run in — a volume seeded from an image without the development extra, or a
    fresh one. Otherwise the environment on the volume is left exactly as it is, which is the point of
    keeping it on a volume at all.
    """
    if any(path in changed for path in DEPENDENCY_FILES):
        return True
    return not venv_ready(venv or VENV)


def unhealthy_boots(history: list[float], now: float, *, window: float = BOOT_WINDOW_SECONDS) -> list[float]:
    """The failed boots still inside the window, newest last. A record from the future is a clock step,
    not evidence, and would otherwise never age out."""
    return [t for t in history if now - window <= t <= now]


def write_result(status: str, commit: str, detail: str) -> None:
    """Record what the last apply attempt did where the bot and the launcher can both read it."""
    try:
        SELFDEV_DIR.mkdir(parents=True, exist_ok=True)
        APPLY_RESULT.write_text(json.dumps({"status": status, "commit": commit, "detail": detail[-2000:], "at": datetime.now(UTC).isoformat()}, indent=2))
    except OSError as exc:
        log(f"could not record the apply result: {exc}")


def last_change() -> dict[str, Any] | None:
    """What the last apply attempt did, or ``None`` when nothing has been applied here."""
    try:
        return dict(json.loads(APPLY_RESULT.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def candidate_dir(repo: Path) -> Path:
    return CANDIDATE / repo.name


def prepare_candidate(repo: Path, ref: str = "origin/main") -> tuple[bool, str]:
    """Put a detached checkout of ``ref`` next to the others under CANDIDATE, reusing its venv and
    node_modules from last time; the object store is the repository's own (a worktree), so this is a checkout,
    not a clone.

    ``ref`` is ``origin/main`` when the revision comes from a remote and a commit sha when it comes from the
    checkout itself — the only difference between preflighting a merge and preflighting a local change.
    """
    target = candidate_dir(repo)
    CANDIDATE.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        git(repo, "worktree", "prune")
        code, out = git(repo, "worktree", "add", "--detach", "--force", str(target), ref)
        return code == 0, out
    code, out = git(target, "checkout", "--detach", "--force", ref)
    if code != 0:
        return False, out
    code, out = git(target, "clean", "-fd", "--exclude=.venv", "--exclude=node_modules", "--exclude=miniapp/node_modules")
    return code == 0, out


def install_from_candidate(repo: Path) -> tuple[bool, str]:
    """After the preflight passed on the candidate: the running checkout gets its dependencies synced (a warm
    cache, seconds) and the app build copied over rather than built again; this is the whole downtime."""
    code, out = run(["uv", "sync", "--frozen", "--extra", "dev"], cwd=repo, timeout=1200)
    if code != 0:
        return False, out
    built = candidate_dir(repo) / "miniapp" / "dist"
    if built.is_dir():
        target = repo / "miniapp" / "dist"
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(built, target)
    restore_owner(repo)
    return True, out


def build_app_if_missing(repo: Path) -> None:
    """A fresh checkout has no built app: the bundle is not in git, and a rebuild builds it on the candidate.

    The first start of an installation — a clone the desktop launcher or a setup script just made —
    would otherwise serve 404 at /app until the first merged pull request. Built once here, in the
    tree about to run; a failure is logged and the bot still starts (the API and Telegram work
    without the app)."""
    miniapp = repo / "miniapp"
    if (miniapp / "dist" / "index.html").is_file() or not (miniapp / "package.json").exists() or not shutil.which("npm"):
        return
    log("no built app in the checkout; building it before the first start")
    try:
        for step in (["npm", "ci", "--no-audit", "--no-fund"], ["npm", "run", "build"]):
            code, out = run(step, cwd=miniapp, timeout=1200)
            if code != 0:
                log(f"app build failed ({' '.join(step)}): {out[-800:]}")
                return
        log("app built")
    finally:
        restore_owner(repo)


def reap_zombies(keep: set[int]) -> int:
    """Collect children the bot left behind (sandbox wrappers reparented to PID 1) — by pid, never with a
    wait on any child, so the bot process asyncio itself waits on is not taken from under it."""
    reaped = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in keep:
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if fields[0] != "Z" or int(fields[1]) != os.getpid():
            continue
        try:
            pid, _status = os.waitpid(int(entry), os.WNOHANG)
        except ChildProcessError:
            continue
        reaped += pid == int(entry)
    return reaped


def preflight(repo: Path, *, sync: bool = True) -> tuple[bool, str]:
    """Dependencies, Mini App build, import, config and smoke tests in the tree about to run.

    ``sync`` is what decides whether the virtualenv is rebuilt. It is on for a revision pulled from a remote,
    where anything may have changed; a local change passes it only when the change touched the files that
    declare a dependency, so the one sync that is needed happens here and nowhere else.
    """
    steps: list[tuple[list[str], Path]] = [
        (["uv", "run", "--frozen", "python", "-m", "compileall", "-q", "daedalus"], repo),
        (["uv", "run", "--frozen", "python", "-m", "daedalus", "check"], repo),
        (["uv", "run", "--frozen", "python", "-m", "pytest", "-q", "-x", "tests/smoke", "tests/unit/test_boundaries.py", "tests/unit/test_redact.py", "tests/unit/test_reachability.py"], repo),
    ]
    if sync:
        steps.insert(0, (["uv", "sync", "--frozen", "--extra", "dev"], repo))
    miniapp = repo / "miniapp"
    if (miniapp / "package.json").exists() and shutil.which("npm"):
        steps.insert(1, (["npm", "ci", "--no-audit", "--no-fund"], miniapp))
        steps.insert(2, (["npm", "run", "build"], miniapp))
    transcript: list[str] = []
    try:
        for step, cwd in steps:
            code, out = run(step, cwd=cwd, timeout=1200)
            transcript.append(f"$ {' '.join(step)}\n{out}")
            if code != 0:
                return False, "\n".join(transcript)
        return True, "\n".join(transcript)
    finally:
        restore_owner(repo)  # npm/uv wrote node_modules, dist and caches as root


class Supervisor:
    def __init__(self) -> None:
        self.child: asyncio.subprocess.Process | None = None
        self.want_running = True
        self.restart_requested = asyncio.Event()
        self.backoff = 2.0
        self.lock = asyncio.Lock()
        self.last_result = "startup"
        self.health_task: asyncio.Task[None] | None = None
        self.rebuild_task: asyncio.Task[None] | None = None
        self.queued_rebuild: str | None = None
        """The reason of a rebuild asked for while another was running: it runs right after, so a pull
        request merged during a rebuild is not left undeployed until someone asks again."""
        self.mode = resolve_mode(CONFIGURED_MODE, repo=BOT_REPO, token=os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GITHUB_DAEDALUS_TOKEN", ""))
        self.restart_task: asyncio.Task[None] | None = None
        self.running_revision = ""
        """The commit the child was started on. Not the last known-good one: a revision becomes known-good
        only after two healthy minutes, and a change refused before then would otherwise be taken back out
        past a change that is already running."""
        self.failed_boots: list[float] = []
        """When each of the recent starts died before it was healthy. Three inside the window and the
        revision goes back to the last known-good one by itself: a change that cannot boot cannot be
        undone from the app, because the app is what is not coming up."""
        log(f"self-development mode: {self.mode} (configured {CONFIGURED_MODE})")

    # -- child lifecycle ------------------------------------------------------------

    async def start_child(self) -> None:
        install_ssh()
        self.child = await asyncio.create_subprocess_exec(
            "bash", "-lc", BOT_CMD, cwd=str(BOT_REPO), env=bot_env(), start_new_session=True
        )
        self.running_revision = head(BOT_REPO)
        log(f"bot started pid={self.child.pid} bot={self.running_revision[:10]} core={head(CORE_REPO)[:10]}")

    async def stop_child(self, *, graceful_seconds: float = 25.0) -> None:
        child = self.child
        if child is None or child.returncode is not None:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(child.wait(), timeout=graceful_seconds)
        except TimeoutError:
            log("bot did not exit in time; killing")
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await child.wait()

    async def _record_good_when_healthy(self, child: asyncio.subprocess.Process) -> None:
        await asyncio.sleep(120)
        if child.returncode is None:
            record_good()

    async def run_loop(self) -> None:
        while self.want_running:
            await self.start_child()
            assert self.child is not None
            if self.health_task is not None:
                self.health_task.cancel()
            self.health_task = asyncio.create_task(self._record_good_when_healthy(self.child))
            started = time.monotonic()
            waiter = asyncio.create_task(self.child.wait())
            restart = asyncio.create_task(self.restart_requested.wait())
            done, _ = await asyncio.wait({waiter, restart}, return_when=asyncio.FIRST_COMPLETED)
            if restart in done:
                self.restart_requested.clear()
                await self.stop_child()
                waiter.cancel()
                continue
            restart.cancel()
            code = self.child.returncode
            uptime = time.monotonic() - started
            log(f"bot exited code={code} after {uptime:.0f}s")
            if not self.want_running:
                break
            if self.lock.locked():
                # A rebuild or rollback stopped the child on purpose; it restarts the bot itself.
                await self.restart_requested.wait()
                self.restart_requested.clear()
                continue
            if uptime > HEALTHY_SECONDS:
                self.backoff = 2.0
                self.failed_boots.clear()
            else:
                self.backoff = min(self.backoff * 2, 120.0)
                if await self._note_failed_boot():
                    continue  # the checkout moved back; start the old revision at once rather than after the backoff
            await asyncio.sleep(self.backoff)

    async def _note_failed_boot(self) -> bool:
        """Record a start that died before it was healthy; roll back at the threshold.

        Only where the change lives in the checkout: with a remote, the revision that cannot boot was
        merged and pulled, and putting it back is the operator's ``rollback``, not the supervisor's guess.
        Returns whether the checkout was moved.
        """
        self.failed_boots = unhealthy_boots([*self.failed_boots, time.time()], time.time())
        if self.mode != "local" or len(self.failed_boots) < BOOT_THRESHOLD:
            return False
        log(f"{len(self.failed_boots)} failed boots within {BOOT_WINDOW_SECONDS // 60} minutes; going back to the last known-good revision")
        self.failed_boots.clear()
        return await self._auto_rollback()

    async def _auto_rollback(self) -> bool:
        """Put the last known-good commit back, so the next start is one that has run before."""
        async with self.lock:
            broken = head(BOT_REPO)
            history = load_history()
            target = next((h for h in reversed(history) if h["bot"] != broken), None)
            if target is None:
                write_result("no_rollback", broken, f"{BOOT_THRESHOLD} failed boots and no earlier known-good revision to go back to")
                log("no earlier known-good revision recorded; staying where we are")
                return False
            self._checkout(target["bot"], target["core"])
            changed = self._changed_files(target["bot"], broken)
            if needs_dependency_sync(changed):
                await asyncio.to_thread(lambda: run(["uv", "sync", "--frozen", "--extra", "dev"], cwd=BOT_REPO, timeout=1200))
            write_result(
                "rolled_back",
                broken,
                f"{BOOT_THRESHOLD} starts in a row died within {HEALTHY_SECONDS}s, so the change was reversed: "
                f"bot {broken[:10]} → {target['bot'][:10]} (known good since {target['at']}). The commit is still in the checkout's history.",
            )
            log(f"rolled back to bot={target['bot'][:10]} core={target['core'][:10]}")
            return True

    # -- operations -----------------------------------------------------------------

    async def restart(self, reason: str) -> str:
        """Apply what the checkout holds.

        With a remote there is nothing to check — the revision running is the revision that was merged and
        preflighted — so the bot simply goes round again. Where the change lives only in the checkout, the
        commit that is there has been seen by nothing but the agent that wrote it: it is preflighted on a
        detached worktree of itself first, and the bot is stopped only once that passes.
        """
        if self.mode != "local":
            self.restart_requested.set()
            return "restarting"
        if self.lock.locked() or (self.restart_task is not None and not self.restart_task.done()):
            return "a restart, rebuild or rollback is already running; this one is not started twice"
        self.restart_task = asyncio.create_task(self._apply_local(reason))
        return "checking the change and restarting: it applies if the checks pass, and the running version is kept if they do not"

    async def _apply_local(self, reason: str) -> None:
        """Preflight the commit in the checkout and restart on it; keep the running one if it fails."""
        async with self.lock:
            target = head(BOT_REPO)
            history = load_history()
            previous = self.running_revision or (history[-1]["bot"] if history else target)
            log(f"restart requested: {reason} (bot {previous[:10]} → {target[:10]})")
            changed = self._changed_files(previous, target)
            stopped = False
            try:
                for repo in (BOT_REPO, CORE_REPO):
                    ok, out = await asyncio.to_thread(prepare_candidate, repo, head(repo))
                    if not ok:
                        write_result("preflight_failed", target, f"a checkout of the change could not be made: {out[-800:]}")
                        return
                sync = needs_dependency_sync(changed)
                ok, transcript = await asyncio.to_thread(preflight, candidate_dir(BOT_REPO), sync=sync)
                if not ok:
                    log("preflight failed on the local change; the running bot is untouched")
                    FAILED.parent.mkdir(parents=True, exist_ok=True)
                    FAILED.write_text(f"restart ({reason}) failed preflight at {datetime.now(UTC).isoformat()}\nthe bot kept running on bot={previous[:10]}\n\n{transcript[-6000:]}")
                    await self._revert_to(previous, target, transcript)
                    return
                FAILED.unlink(missing_ok=True)
                detail = "the change is live"
                if needs_new_image(changed):
                    # The environment itself changed, and nothing here can replace it: say so rather than
                    # asking a rebuilder that a local installation does not have.
                    detail = "the code change is live, but this change also rewrites the image (the Dockerfile, the packages or the supervisor). Run an update to build a new image; until then the old environment is what runs."
                    log("the change touches the image; no rebuild is attempted in local mode")
                await self.stop_child()
                stopped = True
                write_result("applied", target, detail)
            finally:
                # Only a change that passed gets the bot restarted. A refused one leaves the process
                # running the code it already has, which is the code the checkout was put back to.
                if stopped:
                    self.restart_requested.set()

    async def _revert_to(self, running: str, broken: str, transcript: str) -> None:
        """Put the checkout back to what the running process is on.

        The preflight ran on a copy, so nothing that failed has run — but the commit is in the checkout,
        and the next start would pick it up without anyone asking for it. The target is the revision the
        child was started on, which is the state the refusal has to restore; the last known-good commit
        is a different thing and may be older than a change that is already running. The refused commit
        itself is not lost: the branch the agent committed on still points at it.
        """
        if running == broken:
            write_result("preflight_failed", broken, f"the checks did not pass, so the change was not applied:\n{transcript[-1500:]}")
            return
        code, out = git(BOT_REPO, "reset", "--hard", running)
        note = "" if code == 0 else f" The checkout could not be moved back ({out[-300:]}); it still holds the change."
        write_result("preflight_failed", broken, f"the checks did not pass, so the change was not applied and the checkout was put back to {running[:10]}, which is what is running.{note}\n\n{transcript[-1500:]}")

    async def rebuild(self, reason: str) -> str:
        """Acknowledge at once; the work runs in the background and reports through LAST_REBUILD.

        A request that lands while a rebuild or rollback holds the lock is not dropped: it is kept (one
        slot — a second request during the same wait replaces the reason, the outcome is the same
        origin/main either way) and starts as soon as the lock is free.
        """
        if self.lock.locked() or (self.rebuild_task is not None and not self.rebuild_task.done()):
            self.queued_rebuild = reason
            return "a rebuild or rollback is in progress; this one is queued and starts right after it"
        self.rebuild_task = asyncio.create_task(self._rebuild(reason))
        return "rebuild started: the bot stops, main is pulled and preflighted, then it restarts (rolled back on failure)"

    def _run_queued_rebuild(self) -> None:
        if self.queued_rebuild is None:
            return
        reason, self.queued_rebuild = self.queued_rebuild, None
        try:
            self.rebuild_task = asyncio.create_task(self._rebuild(reason))
        except RuntimeError:
            log(f"queued rebuild dropped, the loop is closing: {reason}")
            return
        log(f"queued rebuild starts: {reason}")

    async def _rebuild(self, reason: str) -> None:
        """Fetch, preflight the new revision on a candidate checkout while the bot keeps serving, and only then
        stop it, move the running checkouts and restart. A revision that fails the preflight never touches the
        running bot: the failure is recorded and the bot goes on as it was."""
        try:
            await self._rebuild_locked(reason)
        finally:
            self._run_queued_rebuild()  # whatever happened above, a request that waited is not lost

    async def _rebuild_locked(self, reason: str) -> None:
        async with self.lock:
            log(f"rebuild requested: {reason}")
            previous = {"bot": head(BOT_REPO), "core": head(CORE_REPO)}
            outcome = "unknown"
            stopped = False
            try:
                for repo in (BOT_REPO, CORE_REPO):
                    code, out = git(repo, "fetch", "--prune", "origin")
                    if code != 0:
                        outcome = f"fetch failed in {repo}: {out[-500:]}"
                        return
                code, out = git(BOT_REPO, "rev-parse", "origin/main")
                incoming = out.strip() if code == 0 else "unknown"
                changed = self._changed_files(previous["bot"], incoming)
                if any(path in changed for path in REBUILD_TRIGGER_FILES) or any(path.startswith("launcher/") for path in changed):
                    # The image itself changes: the rebuilder replaces the container from origin/main.
                    await self.stop_child()
                    stopped = True
                    for repo in (BOT_REPO, CORE_REPO):
                        git(repo, "reset", "--hard", "origin/main")
                    if self._request_image_rebuild():
                        self.queued_rebuild = None  # this container is going away; the new one starts from origin/main
                        outcome = "image rebuild requested; the container will be replaced by the rebuilder"
                        return
                    log("image rebuild needed but no rebuilder is configured; continuing in place")
                    self._checkout(previous["bot"], previous["core"])
                for repo in (BOT_REPO, CORE_REPO):
                    ok, out = await asyncio.to_thread(prepare_candidate, repo)
                    if not ok:
                        outcome = f"candidate checkout failed for {repo.name}: {out[-500:]}"
                        return
                ok, transcript = await asyncio.to_thread(preflight, candidate_dir(BOT_REPO))
                if not ok:
                    log("preflight failed on the candidate; the running bot is untouched")
                    FAILED.parent.mkdir(parents=True, exist_ok=True)
                    FAILED.write_text(
                        f"rebuild ({reason}) failed preflight at {datetime.now(UTC).isoformat()}\n"
                        f"the bot kept running on bot={previous['bot'][:10]} core={previous['core'][:10]}\n\n{transcript[-6000:]}"
                    )
                    outcome = "preflight failed; the running revision was kept"
                    return
                await self.stop_child()
                stopped = True
                for repo in (BOT_REPO, CORE_REPO):
                    code, out = git(repo, "reset", "--hard", "origin/main")
                    if code != 0:
                        outcome = f"checkout failed in {repo}: {out[-500:]}"
                        self._checkout(previous["bot"], previous["core"])
                        return
                ok, out = await asyncio.to_thread(install_from_candidate, BOT_REPO)
                if not ok:
                    log("dependency sync failed on the new revision; rolling back")
                    self._checkout(previous["bot"], previous["core"])
                    await asyncio.to_thread(install_from_candidate, BOT_REPO)
                    outcome = f"dependency sync failed; rolled back: {out[-500:]}"
                    return
                FAILED.unlink(missing_ok=True)
                outcome = f"rebuilt: bot {previous['bot'][:10]}→{head(BOT_REPO)[:10]}, core {previous['core'][:10]}→{head(CORE_REPO)[:10]}"
            finally:
                log(f"rebuild outcome: {outcome}")
                LAST_REBUILD.parent.mkdir(parents=True, exist_ok=True)
                LAST_REBUILD.write_text(f"{datetime.now(UTC).isoformat()} {reason}: {outcome}\n")
                if stopped:
                    self.restart_requested.set()

    def _changed_files(self, old: str, new: str) -> set[str]:
        if old == new or "unknown" in (old, new):
            return set()
        code, out = git(BOT_REPO, "diff", "--name-only", old, new)
        return set(out.split()) if code == 0 else set(REBUILD_TRIGGER_FILES)

    def _request_image_rebuild(self) -> bool:
        """Hand the image rebuild to the rebuilder sidecar through the shared trigger directory."""
        if self.mode != "server":
            # Only a server installation has a rebuilder; anywhere else the trigger file would sit there
            # unread and the operator would be told a rebuild was under way that nothing is doing.
            return False
        if not REBUILD_TRIGGER_DIR.is_dir():
            return False
        (REBUILD_TRIGGER_DIR / "rebuild").write_text(datetime.now(UTC).isoformat() + "\n")
        log("image rebuild requested from the rebuilder sidecar; this container will be replaced")
        return True

    def _checkout(self, bot_sha: str, core_sha: str) -> None:
        git(BOT_REPO, "reset", "--hard", bot_sha)
        git(CORE_REPO, "reset", "--hard", core_sha)

    async def rollback(self, steps_back: int) -> str:
        if self.lock.locked():
            return "a rebuild or rollback is already in progress"
        result = "not rolled back"
        try:
            result = await self._rollback(steps_back)
            return result
        finally:
            if self.queued_rebuild is not None and result.startswith("rolled back"):
                # The operator just went back on purpose; a rebuild asked for meanwhile would undo that.
                log(f"queued rebuild dropped after the rollback: {self.queued_rebuild}")
                self.queued_rebuild = None
            else:
                self._run_queued_rebuild()  # nothing was rolled back: the request stands

    async def _rollback(self, steps_back: int) -> str:
        async with self.lock:
            await self.stop_child()
            history = load_history()
            current = {"bot": head(BOT_REPO), "core": head(CORE_REPO)}
            candidates = [h for h in history if h["bot"] != current["bot"] or h["core"] != current["core"]]
            if not candidates:
                self.restart_requested.set()
                return "no earlier known-good revision recorded"
            index = max(0, len(candidates) - 1 - steps_back)
            target = candidates[index]
            self._checkout(target["bot"], target["core"])
            ok, transcript = await asyncio.to_thread(preflight, BOT_REPO)
            if not ok:
                self._checkout(current["bot"], current["core"])
                self.restart_requested.set()
                return f"rollback target failed preflight; staying on the current revision\n{transcript[-800:]}"
            self.restart_requested.set()
            return f"rolled back to bot={target['bot'][:10]} core={target['core'][:10]} (from {target['at']}); restarting"

    async def panic(self) -> str:
        child = self.child
        if child is not None and child.returncode is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        run(["pkill", "-9", "-f", "python[0-9.]* -m daedalus"], timeout=10)
        log("PANIC: process tree killed")
        return "killed everything; the bot restarts in a few seconds"

    def status(self) -> dict[str, Any]:
        return {
            "bot": head(BOT_REPO),
            "core": head(CORE_REPO),
            "child_pid": self.child.pid if self.child else None,
            "child_running": bool(self.child and self.child.returncode is None),
            "known_good": load_history()[-3:],
            "failed": FAILED.read_text()[:2000] if FAILED.exists() else None,
            "last_rebuild": LAST_REBUILD.read_text()[-500:] if LAST_REBUILD.exists() else None,
            "usd_per_day": USD_PER_DAY,
            "budget_exceeded": LIMIT_FLAG.exists(),
            "selfdev_mode": self.mode,
            "last_change": last_change(),
        }

    # -- socket ---------------------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30)
            request = json.loads(raw.decode("utf-8") or "{}")
            op = request.get("op")
            if op == "status":
                result: Any = self.status()
            elif op == "rebuild":
                result = await self.rebuild(str(request.get("reason") or ""))
            elif op == "rollback":
                result = await self.rollback(int(request.get("steps_back") or 0))
            elif op == "panic":
                result = await self.panic()
            elif op == "restart":
                result = await self.restart(str(request.get("reason") or "operator request"))
            else:
                result = f"unknown op {op!r}"
            writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            writer.write((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode("utf-8"))
        finally:
            await writer.drain()
            writer.close()

    async def serve_socket(self) -> None:
        SOCKET.parent.mkdir(parents=True, exist_ok=True)
        SOCKET.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self.handle, path=str(SOCKET))
        os.chmod(SOCKET, 0o660)
        async with server:
            await server.serve_forever()

    # -- spend cap ------------------------------------------------------------------

    async def reap_loop(self) -> None:
        """The sandbox wrappers the bot starts die with it and land on PID 1; collect them so they do not pile up."""
        while True:
            await asyncio.sleep(30)
            try:
                keep = {self.child.pid} if self.child is not None and self.child.returncode is None else set()
                reap_zombies(keep)
            except Exception as exc:  # noqa: BLE001
                log(f"reaping failed: {exc}")

    async def budget_loop(self) -> None:
        db_path = STATE / "daedalus.sqlite"
        while True:
            await asyncio.sleep(60)
            try:
                cap = USD_PER_DAY
                if cap <= 0 or not db_path.exists():
                    continue
                spent = _spent_today(db_path)
                if spent > cap and not LIMIT_FLAG.exists():
                    LIMIT_FLAG.write_text(f"{spent:.4f} > {cap:.2f} at {datetime.now(UTC).isoformat()}\n")
                    log(f"daily budget exceeded: ${spent:.4f} > ${cap:.2f}; new runs are refused by the bot")
                elif spent <= cap and LIMIT_FLAG.exists():
                    LIMIT_FLAG.unlink()
            except Exception as exc:  # noqa: BLE001
                log(f"budget check failed: {exc}")


def _spent_today(db_path: Path) -> float:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT sum(cost_usd) FROM usage_events WHERE at >= ?", (today,)).fetchone()
    finally:
        conn.close()
    return float(row[0] or 0.0)


async def main() -> int:
    supervisor = Supervisor()
    loop = asyncio.get_running_loop()

    def _terminate() -> None:
        supervisor.want_running = False
        asyncio.ensure_future(supervisor.stop_child())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _terminate)
    GOOD_DIR.mkdir(parents=True, exist_ok=True)
    if not load_history():
        record_good()
    await asyncio.to_thread(build_app_if_missing, BOT_REPO)
    tasks = [
        asyncio.create_task(supervisor.serve_socket()),
        asyncio.create_task(supervisor.budget_loop()),
        asyncio.create_task(supervisor.reap_loop()),
    ]
    await supervisor.run_loop()
    for task in tasks:
        task.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
