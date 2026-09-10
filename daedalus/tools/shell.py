"""``exec`` — run a shell command in the session workspace."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
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


async def sandbox_argv(command: str, workdir: Path, workspace: Path, exec_config: Any) -> tuple[list[str], bool]:
    """The argv to run ``command`` with: plain bash, or bash inside bubblewrap when the sandbox is on.

    The sandbox binds the whole filesystem read-only, makes the session workspace (and any
    configured extra path) writable, gives the command a private /tmp and PID namespace, and
    dies with the parent so a timeout kill cannot leave it behind. What is writable is the
    operator's choice alone: a working directory outside those paths is entered read-only.
    """
    global _warned_missing_bwrap
    plain = ["bash", "-lc", command]
    if getattr(exec_config, "sandbox", "off") != "workspace":
        return plain, False
    status = await asyncio.to_thread(bwrap_status)
    if status != "ok":
        if not _warned_missing_bwrap:
            logging.getLogger(__name__).warning("tools.exec.sandbox=workspace but the sandbox is unavailable (%s); running unsandboxed", status)
            _warned_missing_bwrap = True
        return plain, False
    bwrap = shutil.which("bwrap") or "bwrap"
    argv = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--unshare-pid", "--die-with-parent", "--new-session"]
    writable = [workspace, *[Path(p) for p in getattr(exec_config, "sandbox_extra_writable", [])]]
    for path in writable:
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
        "is clipped, so prefer writing large results to a file and reading it in parts. "
        "Long-running processes should be started in the background with nohup and "
        "redirected output (inside the sandbox, if one is on, they end with the command)."
    ),
)
async def exec_command(
    context: ToolContext,
    command: str,
    cwd: str | None = None,
    timeout_seconds: int | None = None,
    env: dict[str, str] | None = None,
) -> ToolResult:
    services = services_for(context)
    workdir = services.resolve(cwd)
    limit = float(timeout_seconds or services.tool_timeout_seconds)
    started = time.monotonic()
    if services.exec_backend is not None:
        outcome = await services.exec_backend.run(command, cwd=str(workdir), env=env, timeout=limit)
        elapsed = time.monotonic() - started
        body = clip(outcome.output, services.max_tool_output_chars, note="write to a file for the full output")
        header = f"exit_code={outcome.exit_code} elapsed={elapsed:.1f}s cwd={workdir}" + (f" TIMED OUT after {limit:.0f}s" if outcome.timed_out else "")
        text = f"{header}\n{body}" if body else header
        if outcome.timed_out or outcome.exit_code != 0:
            return error(context, text, exit_code=outcome.exit_code, timed_out=outcome.timed_out)
        return ok(context, text, exit_code=outcome.exit_code)
    if not workdir.exists():
        return error(context, f"working directory does not exist: {workdir}")
    environment = shell_environment(context.session_id, env)
    argv, sandboxed = await sandbox_argv(command, workdir, services.workspace_dir, tool_config(context).exec)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    chunks: list[bytes] = []
    total = 0
    # Only what the model can see is kept in memory; a process that prints for the whole timeout cannot pump it up.
    head_cap = services.max_tool_output_chars * 4

    async def _pump() -> None:
        nonlocal total
        assert proc.stdout is not None
        last_progress = time.monotonic()
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            if total < head_cap:
                chunks.append(chunk[: head_cap - total])
            total += len(chunk)
            if services.progress is not None and time.monotonic() - last_progress > 2.0:
                last_progress = time.monotonic()
                tail = chunk.decode("utf-8", "replace").strip().splitlines()
                if tail:
                    await services.progress(tail[-1][:160])

    timed_out = False
    try:
        await asyncio.wait_for(_pump(), timeout=limit)
        remaining = max(1.0, limit - (time.monotonic() - started))
        await asyncio.wait_for(proc.wait(), timeout=remaining)
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        await proc.wait()
    output = b"".join(chunks).decode("utf-8", "replace")
    elapsed = time.monotonic() - started
    body = clip(output, services.max_tool_output_chars, note="write to a file for the full output")
    header = f"exit_code={proc.returncode} elapsed={elapsed:.1f}s cwd={workdir}" + (" sandbox=workspace" if sandboxed else "")
    if timed_out:
        header += f" TIMED OUT after {limit:.0f}s (process group killed)"
    text = f"{header}\n{body}" if body else header
    if timed_out or (proc.returncode or 0) != 0:
        return error(context, text, exit_code=proc.returncode, timed_out=timed_out)
    return ok(context, text, exit_code=proc.returncode)


TOOLS = [exec_command]

__all__ = ["TOOLS", "exec_command"]
