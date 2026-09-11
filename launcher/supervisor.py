#!/usr/bin/env python3
"""Daedalus supervisor — PID 1 in the container, never edited by the agent.

Responsibilities, and nothing more:

* start the bot and restart it when it dies (with backoff);
* on ``rebuild``: preflight ``origin/main`` on a candidate checkout while the bot keeps
  running, then move both repositories to it and restart; a revision that fails the
  preflight never touches the running bot;
* on ``rollback``: check out an earlier known-good revision and restart;
* on ``panic``: kill the whole process tree immediately;
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
CANDIDATE = STATE / "preflight"
"""Detached checkouts of origin/main, one per repository under the names the running checkouts have, so the
bot repository's ``../protocore-exp`` path dependency resolves to the candidate core. The preflight runs here
while the bot keeps serving on the old revision; the running checkouts move only once it passes."""


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


def candidate_dir(repo: Path) -> Path:
    return CANDIDATE / repo.name


def prepare_candidate(repo: Path) -> tuple[bool, str]:
    """Put a detached checkout of origin/main next to the others under CANDIDATE, reusing its venv and
    node_modules from last time; the object store is the repository's own (a worktree), so this is a checkout,
    not a clone."""
    target = candidate_dir(repo)
    CANDIDATE.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        git(repo, "worktree", "prune")
        code, out = git(repo, "worktree", "add", "--detach", "--force", str(target), "origin/main")
        return code == 0, out
    code, out = git(target, "checkout", "--detach", "--force", "origin/main")
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


def preflight(repo: Path) -> tuple[bool, str]:
    """Dependencies, Mini App build, import, config and smoke tests in the tree about to run."""
    steps: list[tuple[list[str], Path]] = [
        (["uv", "sync", "--frozen", "--extra", "dev"], repo),
        (["uv", "run", "--frozen", "python", "-m", "compileall", "-q", "daedalus"], repo),
        (["uv", "run", "--frozen", "python", "-m", "daedalus", "check"], repo),
        (["uv", "run", "--frozen", "python", "-m", "pytest", "-q", "-x", "tests/smoke", "tests/unit/test_boundaries.py", "tests/unit/test_redact.py", "tests/unit/test_reachability.py"], repo),
    ]
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

    # -- child lifecycle ------------------------------------------------------------

    async def start_child(self) -> None:
        install_ssh()
        self.child = await asyncio.create_subprocess_exec(
            "bash", "-lc", BOT_CMD, cwd=str(BOT_REPO), env=bot_env(), start_new_session=True
        )
        log(f"bot started pid={self.child.pid} bot={head(BOT_REPO)[:10]} core={head(CORE_REPO)[:10]}")

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
            if uptime > 120:
                self.backoff = 2.0
            else:
                self.backoff = min(self.backoff * 2, 120.0)
            await asyncio.sleep(self.backoff)

    # -- operations -----------------------------------------------------------------

    async def rebuild(self, reason: str) -> str:
        """Acknowledge at once; the work runs in the background and reports through LAST_REBUILD."""
        if self.lock.locked():
            return "a rebuild or rollback is already in progress"
        asyncio.create_task(self._rebuild(reason))
        return "rebuild started: the bot stops, main is pulled and preflighted, then it restarts (rolled back on failure)"

    async def _rebuild(self, reason: str) -> None:
        """Fetch, preflight the new revision on a candidate checkout while the bot keeps serving, and only then
        stop it, move the running checkouts and restart. A revision that fails the preflight never touches the
        running bot: the failure is recorded and the bot goes on as it was."""
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
                self.restart_requested.set()
                result = "restarting"
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
