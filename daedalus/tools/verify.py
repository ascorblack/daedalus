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
from daedalus.tools.shell import sandbox_argv, shell_environment

OUTPUT_HEAD_CHARS = 2000


@tool(
    name="Verify",
    description=(
        "Run a command that checks a claim and record the outcome as a verification receipt "
        "(criterion, command, exit code, output digest, time) tied to this run. Use it for the checks "
        "that back a statement such as 'tests pass' or 'the service answers': a receipt is what "
        "the operator sees on a change proposal, a sentence is not. Exit code 0 = verified. "
        "Pass dependencies to name the shared channels the observation rests on "
        "(e.g. 'container shell + provider API'), so a reviewer can see what the receipt does not cover. "
        "The receipt's time is the host clock; for time-sensitive claims, name the clock in dependencies."
    ),
)
async def verify(context: ToolContext, criterion: str, command: str, cwd: str | None = None, timeout_seconds: int | None = None, dependencies: str | None = None) -> ToolResult:
    # The criterion must be checkable by reading the command: a reviewer seeing only the command must be able to tell what the criterion asserts.
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
        env=shell_environment(context.session_id), start_new_session=True,
    )
    # The head feeds two consumers: the OUTPUT_HEAD_CHARS-char DB field and the
    # model-facing clip at services.max_tool_output_chars. Cap the in-memory
    # head at the worst-case UTF-8 size of the larger (4 bytes/char), so a
    # talkative grandchild that outlives the direct child cannot pump memory
    # for the whole timeout window; the digest is streamed over the whole
    # output regardless.
    head_cap = max(OUTPUT_HEAD_CHARS, services.max_tool_output_chars) * 4
    hasher = hashlib.sha256()
    head = bytearray()
    total_bytes = 0

    async def _pump() -> None:
        nonlocal total_bytes
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(4096):
            hasher.update(chunk)
            total_bytes += len(chunk)
            if len(head) < head_cap:
                head.extend(chunk[: head_cap - len(head)])

    timed_out = False
    try:
        await asyncio.wait_for(_pump(), timeout=limit)
        await asyncio.wait_for(proc.wait(), timeout=max(1.0, limit - (time.monotonic() - started)))
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        await proc.wait()  # a grandchild that escaped the group may still hold the pipe; the direct child is enough
    exit_code = -1 if timed_out else int(proc.returncode or 0)
    passed = exit_code == 0
    # Digest the raw bytes as they stream (the whole output), not the decoded
    # string: decode("replace") turns invalid UTF-8 into U+FFFD, and re-encoding
    # that would make the receipt hash a lossy copy instead of what the process
    # actually printed.
    full_digest = hasher.hexdigest()
    truncated = total_bytes > len(head)
    output = bytes(head).decode("utf-8", "replace")
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
    header = f"{'✅ verified' if passed else '❌ NOT verified'}: {criterion} — exit {exit_code}{' (timed out)' if timed_out else ''} · receipt {receipt_id or 'not recorded'} · digest {digest} · at {at}" + (f" · output {total_bytes} B, first {len(head)} B kept" if truncated else "") + (f" · deps: {deps}" if deps else "") + (" · sandbox=workspace" if sandboxed else "")
    body = clip(output, services.max_tool_output_chars, note="write the output to a file for the rest")
    text = f"{header}\n{body}" if body.strip() else header
    return ok(context, text, receipt=receipt_id, passed=passed, exit_code=exit_code) if passed else error(context, text, receipt=receipt_id, passed=passed, exit_code=exit_code)


TOOLS = [verify]

__all__ = ["TOOLS"]
