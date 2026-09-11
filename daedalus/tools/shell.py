"""``exec`` — run a shell command in the session workspace."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import clip, error, ok, services_for, tool_config

_warned_missing_bwrap = False
_bwrap_state: str | None = None
"""Cached result of :func:`bwrap_status`: "ok", or the reason the sandbox cannot run here."""
_bwrap_probed_at = 0.0
PROBE_RETRY_SECONDS = 300.0
"""A failed probe is repeated after this long; a successful one is kept for the life of the process."""


class SandboxUnavailable(RuntimeError):
    """The configured sandbox cannot be created here; commands do not run unsandboxed instead."""


def bwrap_status() -> str:
    """Whether bubblewrap can create namespaces in this container (Docker's default seccomp profile forbids it).

    Blocking (it runs a subprocess): call it through ``asyncio.to_thread`` from the event loop.
    """
    global _bwrap_state, _bwrap_probed_at
    if _bwrap_state == "ok" or (_bwrap_state is not None and time.monotonic() - _bwrap_probed_at < PROBE_RETRY_SECONDS):
        return _bwrap_state
    _bwrap_probed_at = time.monotonic()
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        _bwrap_state = "bwrap is not installed"
        return _bwrap_state
    try:
        probe = subprocess.run([bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid", "true"], capture_output=True, text=True, timeout=20)
        _bwrap_state = "ok" if probe.returncode == 0 else f"bwrap cannot create namespaces here: {(probe.stderr or probe.stdout).strip()[:120]} (the container needs cap_add SYS_ADMIN and an unconfined seccomp profile)"
    except (OSError, subprocess.TimeoutExpired) as exc:
        _bwrap_state = f"bwrap probe failed: {type(exc).__name__}"
    return _bwrap_state


async def sandbox_argv(command: str, workdir: Path, workspace: Path, exec_config: Any, *, writable: Sequence[Path] = ()) -> tuple[list[str], bool]:
    """The argv to run ``command`` with: plain bash, or bash inside bubblewrap when the sandbox is on.

    The sandbox binds the whole filesystem read-only, makes the session workspace (any
    configured extra path, and the paths the host opened for this session — its own
    worktrees) writable, gives the command a private /tmp and PID namespace, and dies with
    the parent so a timeout kill cannot leave it behind. Everything else is entered read-only.
    """
    global _warned_missing_bwrap
    plain = ["bash", "-lc", command]
    if getattr(exec_config, "sandbox", "off") != "workspace":
        return plain, False
    status = await asyncio.to_thread(bwrap_status)
    if status != "ok":
        if not _warned_missing_bwrap:
            logging.getLogger(__name__).warning("tools.exec.sandbox=workspace but the sandbox is unavailable (%s); commands are refused until it is", status)
            _warned_missing_bwrap = True
        # Fail closed: a sandbox the operator asked for and did not get is not a warning, it is a missing wall.
        raise SandboxUnavailable(f"the sandbox is configured (tools.exec.sandbox=workspace) but unavailable: {status}. The operator can switch it off in Settings → Tools or enable namespaces for the container.")
    bwrap = shutil.which("bwrap") or "bwrap"
    argv = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--unshare-pid", "--die-with-parent", "--new-session"]
    paths = [workspace, *[Path(p) for p in getattr(exec_config, "sandbox_extra_writable", [])], *writable]
    for path in paths:
        if path.exists():
            argv += ["--bind", str(path), str(path)]
    return argv + ["bash", "-lc", command], True


# Environment names a tool's subprocess may inherit. Everything else —
# including any secret name the bot's environment gains later — stays in
# the bot's process unless the caller passes it explicitly through the
# tool's ``env`` parameter.
_SAFE_ENV_BASE = frozenset({
    # process basics
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "HOSTNAME", "PWD", "SHLVL",
    "TMPDIR", "TEMP", "TMP",
    # locale / timezone
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
    # editor / pager preferences (harmless)
    "EDITOR", "VISUAL", "PAGER",
    # apt in the sandbox
    "DEBIAN_FRONTEND",
    # python / uv runtime (the bot's toolchain)
    "PYTHONUNBUFFERED", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
    # network: a proxy or a private CA the container was given must reach every client
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    # git over ssh
    "SSH_AUTH_SOCK",
    # git authentication: the supervisor's credential helper reads GH_TOKEN, and gh reads it too.
    # Kept on purpose: the operator wants the shell able to fetch, push and use gh; the token is
    # the agent's own (a fine-grained PAT scoped to its repositories).
    "GH_TOKEN", "GH_ORG_TOKEN",
    # The headless Chromium the browser skills drive (Playwright's browser store, Lighthouse's binary).
    "PLAYWRIGHT_BROWSERS_PATH", "CHROME_PATH",
})
"""Names inherited by a tool's subprocess. Prefixes in :data:`_SAFE_ENV_PREFIXES` are inherited as well."""

_SAFE_ENV_PREFIXES = ("GIT_", "UV_", "PIP_", "NPM_CONFIG_", "NODE_", "LC_", "XDG_", "DAEDALUS_", "CARGO_", "GOPATH", "GOFLAGS", "JAVA_")
"""Variable families a toolchain reads; none of them carries the bot's own credentials."""

_SECRET_ENV = re.compile(r"^(TELEGRAM_.*|KEYPROXY_.*|.*_API_KEY|.*_SECRET|.*_PASSWORD|GITHUB_TOKEN)$")
"""Never inherited even when a prefix would admit them."""


def shell_environment(session_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a tool's subprocess gets: what a shell and its toolchains need, without the bot's own credentials.

    This is hygiene, not containment: the bot's Telegram and provider credentials do not
    propagate into child processes and their logs, but a shell in the same container can still
    read the parent's environment through ``/proc``. A caller that needs a specific value passes
    it through the tool's ``env`` parameter.
    """
    env = {k: v for k, v in os.environ.items() if (k in _SAFE_ENV_BASE or k.startswith(_SAFE_ENV_PREFIXES)) and not _SECRET_ENV.match(k)}
    env.update(extra or {})
    env["DAEDALUS_SESSION_ID"] = session_id
    return env


@tool(
    name="Exec",
    description=(
        "Run a shell command with bash. The working directory defaults to the session "
        "workspace. Output (stdout and stderr, interleaved) is returned; very long output "
        "is clipped to its head and tail and the whole of it is kept in a file the result names. "
        "background=true starts the command as a job and returns at once with a job id: "
        "JobOutput reads its output, JobKill stops it, JobList shows the jobs; use it for servers, "
        "builds and anything longer than a few minutes instead of holding this call open."
    ),
)
async def exec_command(
    context: ToolContext,
    command: str,
    cwd: str | None = None,
    timeout_seconds: int | None = None,
    env: dict[str, str] | None = None,
    background: bool = False,
) -> ToolResult:
    services = services_for(context)
    workdir = services.resolve(cwd)
    limit = float(timeout_seconds or services.tool_timeout_seconds)
    started = time.monotonic()
    if background:
        if services.exec_backend is not None:
            return error(context, "background jobs are not available when commands run on another machine; start the process with nohup and redirect its output")
        if not workdir.exists():
            return error(context, f"working directory does not exist: {workdir}")
        return await _start_job(context, services, command, workdir, env)
    if services.exec_backend is not None:
        outcome = await services.exec_backend.run(command, cwd=str(workdir), env=env, timeout=limit)
        elapsed = time.monotonic() - started
        body = clip(outcome.output, services.max_tool_output_chars, note="write to a file for the full output")
        header = f"exit_code={outcome.exit_code} elapsed={elapsed:.1f}s cwd={workdir}" + (f" TIMED OUT after {limit:.0f}s" if outcome.timed_out else "")
        if outcome.timed_out:
            header += REMOTE_TIMEOUT_HINT
        text = f"{header}\n{body}" if body else header
        if outcome.timed_out or outcome.exit_code != 0:
            return error(context, text, exit_code=outcome.exit_code, timed_out=outcome.timed_out)
        return ok(context, text, exit_code=outcome.exit_code)
    if not workdir.exists():
        return error(context, f"working directory does not exist: {workdir}")
    environment = shell_environment(context.session_id, env)
    try:
        argv, sandboxed = await sandbox_argv(command, workdir, services.workspace_dir, tool_config(context).exec, writable=getattr(services, "writable", ()))
    except SandboxUnavailable as exc:
        return error(context, str(exc))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    limit_chars = services.max_tool_output_chars
    head_cap = int(limit_chars * 0.7) * 4  # bytes; the model sees at most limit_chars characters, UTF-8 needs up to 4 bytes each
    tail_cap = int(limit_chars * 0.25) * 4
    chunks: list[bytes] = []
    tail_chunks: deque[bytes] = deque()
    tail_size = 0
    total = 0
    spill = _spill_path(services, context)
    spill_fh = None
    spill_written = 0

    async def _pump() -> None:
        nonlocal total, tail_size, spill_fh, spill_written
        assert proc.stdout is not None
        last_progress = time.monotonic()
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            if total + len(chunk) > head_cap:
                # Past the head: the whole stream goes to a file (up to a cap) and a ring keeps the tail for the answer.
                if spill_fh is None and spill_written == 0:
                    try:
                        spill.parent.mkdir(parents=True, exist_ok=True)
                        _prune_spills(spill.parent)
                        spill_fh = spill.open("wb")
                        spill_fh.write(b"".join(chunks))
                        spill_written = sum(len(c) for c in chunks)
                    except OSError:
                        spill_fh = None
                if spill_fh is not None:
                    if spill_written + len(chunk) <= SPILL_MAX_BYTES:
                        try:
                            spill_fh.write(chunk)
                            spill_written += len(chunk)
                        except OSError:
                            spill_fh.close()
                            spill_fh = None
                    else:
                        spill_fh.write(b"\n[... spill capped at %d bytes ...]\n" % SPILL_MAX_BYTES)
                        spill_fh.close()
                        spill_fh = None
            if total < head_cap:
                chunks.append(chunk[: head_cap - total])
            else:
                tail_chunks.append(chunk)
                tail_size += len(chunk)
                while tail_size > tail_cap and len(tail_chunks) > 1:
                    tail_size -= len(tail_chunks.popleft())
            total += len(chunk)
            if services.progress is not None and time.monotonic() - last_progress > 2.0:
                last_progress = time.monotonic()
                tail = chunk.decode("utf-8", "replace").strip().splitlines()
                if tail:
                    await services.progress(tail[-1][:160])

    timed_out = False
    try:
        try:
            await asyncio.wait_for(_pump(), timeout=limit)
            remaining = max(1.0, limit - (time.monotonic() - started))
            await asyncio.wait_for(proc.wait(), timeout=remaining)
        except TimeoutError:
            timed_out = True
    finally:
        if proc.returncode is None:
            # A timeout, a cancelled run or a failed write: the process group never outlives the call.
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
            await proc.wait()
        if spill_fh is not None:
            spill_fh.close()
    head_text = b"".join(chunks).decode("utf-8", "replace")
    elapsed = time.monotonic() - started
    kept = sum(len(c) for c in chunks) + tail_size
    if total > kept:
        tail_text = b"".join(tail_chunks).decode("utf-8", "replace")
        where = f"full output in {spill}" if spill_written else "the rest was not kept; write the output to a file"
        if spill_written and spill_written < total:
            where = f"the first {spill_written} bytes are in {spill}; write the output to a file for the rest"
        body = head_text + f"\n\n[... {total - kept} of {total} bytes omitted — {where} ...]\n\n" + tail_text
    else:
        body = head_text
    header = f"exit_code={proc.returncode} elapsed={elapsed:.1f}s cwd={workdir}" + (" sandbox=workspace" if sandboxed else "")
    if timed_out:
        header += f" TIMED OUT after {limit:.0f}s (process group killed)" + LOCAL_TIMEOUT_HINT
    text = f"{header}\n{body}" if body else header
    if timed_out or (proc.returncode or 0) != 0:
        return error(context, text, exit_code=proc.returncode, timed_out=timed_out)
    return ok(context, text, exit_code=proc.returncode)


LOCAL_TIMEOUT_HINT = " — waiting this long in the foreground is the mistake, not the command: start it again with background=true and read it with JobOutput, or pass a larger timeout_seconds if it must block"
REMOTE_TIMEOUT_HINT = " — waiting this long in the foreground is the mistake, not the command: start it again with `nohup … > /tmp/job.log 2>&1 &` and poll the log with later calls, or pass a larger timeout_seconds if it must block"

SPILL_MAX_BYTES = 20 * 1024 * 1024
"""The most of one command's output kept on disk; beyond it the file says it was capped."""
SPILL_KEEP_FILES = 30
"""How many spill files a workspace keeps; older ones go when a new one is opened."""


def _prune_spills(directory: Path) -> None:
    try:
        files = sorted((p for p in directory.iterdir() if p.suffix == ".log"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - SPILL_KEEP_FILES + 1)]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _spill_path(services: Any, context: ToolContext) -> Path:
    call = re.sub(r"[^A-Za-z0-9_-]", "", str(context.metadata.get("tool_call_id") or "")) or f"{int(time.time())}"
    return services.workspace_dir / ".exec" / f"{call}.log"


@dataclass(slots=True)
class Job:
    id: str
    command: str
    cwd: Path
    log: Path
    process: asyncio.subprocess.Process
    started: float
    sandboxed: bool = False

    @property
    def running(self) -> bool:
        return self.process.returncode is None


def _jobs(services: Any) -> dict[str, Job]:
    return services.extra.setdefault("jobs", {})


async def _start_job(context: ToolContext, services: Any, command: str, workdir: Path, env: dict[str, str] | None) -> ToolResult:
    jobs = _jobs(services)
    job_id = f"job-{len(jobs) + 1}-{int(time.time() * 1000) % 1000000}"
    log = services.workspace_dir / ".jobs" / f"{job_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    _prune_spills(log.parent)  # the same bound as the spill directory: the newest logs stay
    try:
        argv, sandboxed = await sandbox_argv(command, workdir, services.workspace_dir, tool_config(context).exec, writable=getattr(services, "writable", ()))
    except SandboxUnavailable as exc:
        return error(context, str(exc))
    fh = log.open("wb")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(workdir), stdout=fh, stderr=subprocess.STDOUT, env=shell_environment(context.session_id, env), start_new_session=True
        )
    finally:
        fh.close()
    jobs[job_id] = Job(id=job_id, command=command, cwd=workdir, log=log, process=process, started=time.monotonic(), sandboxed=sandboxed)
    await asyncio.sleep(0.3)  # long enough for an immediate failure (a typo, a missing binary) to show up in the answer
    status = f"running (pid {process.pid})" if process.returncode is None else f"already exited with code {process.returncode}"
    head = log.read_text(encoding="utf-8", errors="replace")[:1500]
    where = " in the sandbox" if sandboxed else ""
    return ok(context, f"{job_id}: {status}{where}; output in {log} (jobs do not survive a restart of the bot; the log does)\n{head}".rstrip(), job_id=job_id, pid=process.pid)


def _tail(path: Path, lines: int) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-max(1, lines):])


@tool(name="JobOutput", description="The latest output of a background job started with Exec(background=true): its status and the last lines of its log.")
async def job_output(context: ToolContext, job_id: str, tail_lines: int = 100) -> ToolResult:
    services = services_for(context)
    job = _jobs(services).get(job_id)
    if job is None:
        return error(context, f"no job {job_id!r}; JobList shows the jobs of this session")
    status = "running" if job.running else f"exited with code {job.process.returncode}"
    elapsed = time.monotonic() - job.started
    text = f"{job.id}: {status} after {elapsed:.0f}s — `{job.command[:200]}`\n{_tail(job.log, tail_lines)}"
    return ok(context, clip(text, services.max_tool_output_chars), running=job.running, exit_code=job.process.returncode)


@tool(name="JobKill", description="Stop a background job (its whole process group). Returns the job's final status.")
async def job_kill(context: ToolContext, job_id: str) -> ToolResult:
    services = services_for(context)
    job = _jobs(services).get(job_id)
    if job is None:
        return error(context, f"no job {job_id!r}")
    if job.running:
        try:
            os.killpg(job.process.pid, 15)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(job.process.wait(), timeout=5)
        except TimeoutError:
            try:
                os.killpg(job.process.pid, 9)
            except ProcessLookupError:
                pass
            await job.process.wait()
    return ok(context, f"{job.id}: exited with code {job.process.returncode}; log in {job.log}", exit_code=job.process.returncode)


@tool(name="JobList", description="The background jobs of this session: id, status, age, command.")
async def job_list(context: ToolContext) -> ToolResult:
    services = services_for(context)
    jobs = _jobs(services)
    if not jobs:
        return ok(context, "no background jobs in this session")
    lines = [f"- {j.id}: {'running' if j.running else f'exited {j.process.returncode}'}, {time.monotonic() - j.started:.0f}s, `{j.command[:120]}` → {j.log}" for j in jobs.values()]
    return ok(context, "\n".join(lines), count=len(jobs))


TOOLS = [exec_command, job_output, job_kill, job_list]

__all__ = ["TOOLS", "exec_command", "job_kill", "job_list", "job_output"]
