"""``exec`` — run a shell command in the session workspace."""

from __future__ import annotations

import asyncio
import os
import time

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import clip, error, ok, services_for


@tool(
    name="Exec",
    description=(
        "Run a shell command with bash. The working directory defaults to the session "
        "workspace. Output (stdout and stderr, interleaved) is returned; very long output "
        "is clipped, so prefer writing large results to a file and reading it in parts. "
        "Long-running processes should be started in the background with nohup and "
        "redirected output."
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
    if not workdir.exists():
        return error(context, f"working directory does not exist: {workdir}")
    limit = float(timeout_seconds or services.tool_timeout_seconds)
    environment = {**os.environ, **(env or {}), "DAEDALUS_SESSION_ID": context.session_id}
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "bash",
        "-lc",
        command,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    chunks: list[bytes] = []
    total = 0

    async def _pump() -> None:
        nonlocal total
        assert proc.stdout is not None
        last_progress = time.monotonic()
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            chunks.append(chunk)
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
    header = f"exit_code={proc.returncode} elapsed={elapsed:.1f}s cwd={workdir}"
    if timed_out:
        header += f" TIMED OUT after {limit:.0f}s (process group killed)"
    text = f"{header}\n{body}" if body else header
    if timed_out or (proc.returncode or 0) != 0:
        return error(context, text, exit_code=proc.returncode, timed_out=timed_out)
    return ok(context, text, exit_code=proc.returncode)


TOOLS = [exec_command]

__all__ = ["TOOLS", "exec_command"]
