"""``Verify`` — a claim becomes a receipt the host recorded, not a sentence the model wrote.

The tool runs the check itself and stores what happened (command, exit code, output digest)
against the run. Self-development proposals attach the receipts of their session, so the
operator approves evidence, not confidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import UTC, datetime

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.security import redact
from daedalus.tools._common import clip, error, ok, services_for, tool_config
from daedalus.tools.shell import sandbox_argv

OUTPUT_HEAD_CHARS = 2000


@tool(
    name="Verify",
    description=(
        "Run a command that checks a claim and record the outcome as a verification receipt "
        "(criterion, command, exit code, output digest, time) tied to this run. Use it for the checks "
        "that back a statement such as 'tests pass' or 'the service answers': a receipt is what "
        "the operator sees on a change proposal, a sentence is not. Exit code 0 = verified. "
        "Pass dependencies to name the shared channels the observation rests on "
        "(e.g. 'container shell + provider API'), so a reviewer can see what the receipt does not cover."
    ),
)
async def verify(context: ToolContext, criterion: str, command: str, cwd: str | None = None, timeout_seconds: int | None = None, dependencies: str | None = None) -> ToolResult:
    services = services_for(context)
    manager = services.extra.get("manager")
    workdir = services.resolve(cwd)
    if not workdir.exists():
        return error(context, f"working directory does not exist: {workdir}")
    limit = float(timeout_seconds or services.tool_timeout_seconds)
    started = time.monotonic()
    # A failure anywhere in a pipeline fails the check; `pytest | tail` must not pass on tail's exit code.
    argv, sandboxed = await sandbox_argv("set -o pipefail\n" + command, workdir, services.workspace_dir, tool_config(context).exec)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(workdir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "DAEDALUS_SESSION_ID": context.session_id}, start_new_session=True,
    )
    chunks: list[bytes] = []

    async def _pump() -> None:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(4096):
            chunks.append(chunk)

    timed_out = False
    try:
        await asyncio.wait_for(_pump(), timeout=limit)
        await asyncio.wait_for(proc.wait(), timeout=max(1.0, limit - (time.monotonic() - started)))
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        await proc.wait()  # a grandchild that escaped the group may still hold the pipe; the direct child is enough
    raw_output = b"".join(chunks)
    exit_code = -1 if timed_out else int(proc.returncode or 0)
    passed = exit_code == 0
    # Digest the raw bytes, not the decoded string: decode("replace") turns
    # invalid UTF-8 into U+FFFD, and re-encoding that would make the receipt
    # hash a lossy copy instead of what the process actually printed.
    full_digest = hashlib.sha256(raw_output).hexdigest()
    output = raw_output.decode("utf-8", "replace")
    digest = full_digest[:16]
    at = datetime.now(UTC).isoformat()
    receipt_id = ""
    if manager is not None:
        # Receipts travel to proposal cards and pull-request bodies: nothing secret may be recorded.
        r = redact.shared()
        deps = (dependencies or "").strip()
        async with manager.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO verifications(session_id, run_id, criterion, command, cwd, exit_code, passed, output_digest, output_head, duration_ms, at, sandboxed, dependencies)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    context.session_id, context.run_id, r.redact(criterion)[:300], r.redact(command)[:2000], str(workdir), exit_code, int(passed), full_digest,
                    r.redact(output[:OUTPUT_HEAD_CHARS]), int((time.monotonic() - started) * 1000), at, int(sandboxed), r.redact(deps)[:300],
                ),
            )
            receipt_id = f"v{cursor.lastrowid}"
    deps = (dependencies or "").strip()
    header = f"{'✅ verified' if passed else '❌ NOT verified'}: {criterion} — exit {exit_code}{' (timed out)' if timed_out else ''} · receipt {receipt_id or 'not recorded'} · digest {digest} · at {at}" + (f" · deps: {deps}" if deps else "") + (" · sandbox=workspace" if sandboxed else "")
    body = clip(output, services.max_tool_output_chars, note="write the output to a file for the rest")
    text = f"{header}\n{body}" if body.strip() else header
    return ok(context, text, receipt=receipt_id, passed=passed, exit_code=exit_code) if passed else error(context, text, receipt=receipt_id, passed=passed, exit_code=exit_code)


TOOLS = [verify]

__all__ = ["TOOLS"]
