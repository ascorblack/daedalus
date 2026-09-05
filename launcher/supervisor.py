#!/usr/bin/env python3
"""Daedalus supervisor — PID 1 in the container, never edited by the agent.

Responsibilities, and nothing more:

* start the bot and restart it when it dies (with backoff);
* on ``rebuild``: move both repositories to ``origin/main``, run preflight in the
  new tree, and only then restart; roll back to the last known-good revision when
  preflight fails;
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
COMPOSE_FILE = os.environ.get("DAEDALUS_COMPOSE_FILE", "")
COMPOSE_PROJECT_DIR = os.environ.get("DAEDALUS_COMPOSE_PROJECT_DIR", "")
COMPOSE_SERVICE = os.environ.get("DAEDALUS_COMPOSE_SERVICE", "daedalus")
GOOD_DIR = STATE / "good"
HISTORY = GOOD_DIR / "history.json"
FAILED = GOOD_DIR / "FAILED"
LIMIT_FLAG = STATE / "BUDGET_EXCEEDED"
LOG = STATE / "supervisor.log"

REBUILD_TRIGGER_FILES = ("deploy/Dockerfile", "deploy/compose.yaml", "deploy/apt-packages.txt")


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


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 1800) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout, env=bot_env()
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    return proc.returncode, (proc.stdout + proc.stderr)[-20000:]


def git(repo: Path, *args: str) -> tuple[int, str]:
    return run(["git", "-C", str(repo), *args], timeout=600)


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


def preflight(repo: Path) -> tuple[bool, str]:
    """Import, config and smoke tests in the tree that is about to run."""
    steps = [
        ["uv", "sync", "--frozen", "--extra", "dev"],
        ["uv", "run", "--frozen", "python", "-m", "compileall", "-q", "daedalus"],
        ["uv", "run", "--frozen", "python", "-m", "daedalus", "check"],
        ["uv", "run", "--frozen", "python", "-m", "pytest", "-q", "-x", "tests/smoke"],
    ]
    transcript: list[str] = []
    for step in steps:
        code, out = run(step, cwd=repo, timeout=1200)
        transcript.append(f"$ {' '.join(step)}\n{out}")
        if code != 0:
            return False, "\n".join(transcript)
    return True, "\n".join(transcript)


class Supervisor:
    def __init__(self) -> None:
        self.child: asyncio.subprocess.Process | None = None
        self.want_running = True
        self.restart_requested = asyncio.Event()
        self.backoff = 2.0
        self.lock = asyncio.Lock()
        self.last_result = "startup"

    # -- child lifecycle ------------------------------------------------------------

    async def start_child(self) -> None:
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

    async def run_loop(self) -> None:
        while self.want_running:
            await self.start_child()
            assert self.child is not None
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
            if uptime > 120:
                self.backoff = 2.0
                record_good()
            else:
                self.backoff = min(self.backoff * 2, 120.0)
            await asyncio.sleep(self.backoff)

    # -- operations -----------------------------------------------------------------

    async def rebuild(self, reason: str) -> str:
        async with self.lock:
            log(f"rebuild requested: {reason}")
            previous = {"bot": head(BOT_REPO), "core": head(CORE_REPO)}
            for repo in (BOT_REPO, CORE_REPO):
                code, out = git(repo, "fetch", "--prune", "origin")
                if code != 0:
                    return f"fetch failed in {repo}: {out[-500:]}"
                code, out = git(repo, "reset", "--hard", "origin/main")
                if code != 0:
                    return f"checkout failed in {repo}: {out[-500:]}"
            changed = self._changed_files(previous["bot"], head(BOT_REPO))
            if any(path in changed for path in REBUILD_TRIGGER_FILES):
                message = self._rebuild_image()
                if message is not None:
                    return message
            ok, transcript = await asyncio.to_thread(preflight, BOT_REPO)
            if not ok:
                log("preflight failed; rolling back")
                self._checkout(previous["bot"], previous["core"])
                FAILED.parent.mkdir(parents=True, exist_ok=True)
                FAILED.write_text(
                    f"rebuild ({reason}) failed preflight at {datetime.now(UTC).isoformat()}\n"
                    f"target bot={head(BOT_REPO)[:10]} rolled back to {previous['bot'][:10]}\n\n{transcript[-6000:]}"
                )
                self.restart_requested.set()
                return "preflight failed; rolled back to the previous revision (details posted on restart)"
            FAILED.unlink(missing_ok=True)
            self.restart_requested.set()
            return f"rebuilt: bot {previous['bot'][:10]}→{head(BOT_REPO)[:10]}, core {previous['core'][:10]}→{head(CORE_REPO)[:10]}; restarting"

    def _changed_files(self, old: str, new: str) -> set[str]:
        if old == new or "unknown" in (old, new):
            return set()
        code, out = git(BOT_REPO, "diff", "--name-only", old, new)
        return set(out.split()) if code == 0 else set(REBUILD_TRIGGER_FILES)

    def _rebuild_image(self) -> str | None:
        """Ask the Docker daemon to rebuild and replace this container.

        The work runs in a throw-away helper container so it survives this one
        being replaced half-way through.
        """
        if not COMPOSE_FILE or not shutil.which("docker"):
            log("image rebuild needed but docker/compose is not configured; continuing in place")
            return None
        cmd = [
            "docker", "run", "-d", "--rm",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{COMPOSE_PROJECT_DIR}:{COMPOSE_PROJECT_DIR}",
            "-w", COMPOSE_PROJECT_DIR,
            "docker:cli",
            "sh", "-c",
            f"docker compose -f {COMPOSE_FILE} up -d --build {COMPOSE_SERVICE}",
        ]
        code, out = run(cmd, timeout=120)
        if code != 0:
            return f"could not start the image rebuild helper: {out[-500:]}"
        log("image rebuild helper started; this container will be replaced")
        return "image rebuild started; the container will be replaced when the build finishes"

    def _checkout(self, bot_sha: str, core_sha: str) -> None:
        git(BOT_REPO, "reset", "--hard", bot_sha)
        git(CORE_REPO, "reset", "--hard", core_sha)

    async def rollback(self, steps_back: int) -> str:
        async with self.lock:
            history = load_history()
            current = {"bot": head(BOT_REPO), "core": head(CORE_REPO)}
            candidates = [h for h in history if h["bot"] != current["bot"] or h["core"] != current["core"]]
            if not candidates:
                return "no earlier known-good revision recorded"
            index = max(0, len(candidates) - 1 - steps_back)
            target = candidates[index]
            self._checkout(target["bot"], target["core"])
            ok, transcript = await asyncio.to_thread(preflight, BOT_REPO)
            if not ok:
                self._checkout(current["bot"], current["core"])
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
        run(["pkill", "-9", "-u", str(os.getuid()), "-f", "daedalus"], timeout=10)
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

    async def budget_loop(self) -> None:
        db_path = STATE / "daedalus.sqlite"
        config_path = STATE / "config.toml"
        while True:
            await asyncio.sleep(60)
            try:
                cap = _read_daily_cap(config_path)
                if cap <= 0 or not db_path.exists():
                    continue
                spent = _spent_today(db_path)
                if spent > cap and not LIMIT_FLAG.exists():
                    LIMIT_FLAG.write_text(f"{spent:.4f} > {cap:.2f} at {datetime.now(UTC).isoformat()}\n")
                    log(f"daily budget exceeded: ${spent:.4f} > ${cap:.2f}; restarting bot in read-only mode")
                    self.restart_requested.set()
                elif spent <= cap and LIMIT_FLAG.exists():
                    LIMIT_FLAG.unlink()
            except Exception as exc:  # noqa: BLE001
                log(f"budget check failed: {exc}")


def _read_daily_cap(config_path: Path) -> float:
    if not config_path.exists():
        return 0.0
    import tomllib

    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    return float((data.get("limits") or {}).get("usd_per_day") or 0.0)


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
    ]
    await supervisor.run_loop()
    for task in tasks:
        task.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
