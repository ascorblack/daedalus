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

from daedalus.tools._common import clip, error, ok, services_for

OUTPUT_HEAD_CHARS = 2000


@tool(
    name="Verify",
    description=(
        "Run a command that checks a claim and record the outcome as a verification receipt "
        "(criterion, command, exit code, output digest) tied to this run. Use it for the checks "
        "that back a statement such as 'tests pass' or 'the service answers': a receipt is what "
        "the operator sees on a change proposal, a sentence is not. Exit code 0 = verified."
    ),
)
async def verify(context: ToolContext, criterion: str, command: str, cwd: str | None = None, timeout_seconds: int | None = None) -> ToolResult:
    services = services_for(context)
    manager = services.extra.get("manager")
    workdir = services.resolve(cwd)
    if not workdir.exists():
        return error(context, f"working directory does not exist: {workdir}")
    limit = float(timeout_seconds or services.tool_timeout_seconds)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "bash", "-lc", command, cwd=str(workdir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "DAEDALUS_SESSION_ID": context.session_id}, start_new_session=True,
    )
    timed_out = False
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        raw, _ = await proc.communicate()
    output = raw.decode("utf-8", "replace")
    exit_code = -1 if timed_out else int(proc.returncode or 0)
    passed = exit_code == 0
    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()[:16]
    receipt_id = ""
    if manager is not None:
        async with manager.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO verifications(session_id, run_id, criterion, command, cwd, exit_code, passed, output_digest, output_head, duration_ms, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    context.session_id, context.run_id, criterion[:300], command[:2000], str(workdir), exit_code, int(passed), digest,
                    output[:OUTPUT_HEAD_CHARS], int((time.monotonic() - started) * 1000), datetime.now(UTC).isoformat(),
                ),
            )
            receipt_id = f"v{cursor.lastrowid}"
    header = f"{'✅ verified' if passed else '❌ NOT verified'}: {criterion} — exit {exit_code}{' (timed out)' if timed_out else ''} · receipt {receipt_id or 'not recorded'} · digest {digest}"
    body = clip(output, services.max_tool_output_chars, note="write the output to a file for the rest")
    text = f"{header}\n{body}" if body.strip() else header
    return ok(context, text, receipt=receipt_id, passed=passed, exit_code=exit_code) if passed else error(context, text, receipt=receipt_id, passed=passed, exit_code=exit_code)


TOOLS = [verify]

__all__ = ["TOOLS"]
