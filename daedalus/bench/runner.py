"""Headless runs of one task at a time, N at a time, with a record per task."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from protocore.contracts.types import Message, MessageRole, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

from daedalus.bench.manifest import DEFAULT_TOOLS_OFF, Manifest, Task
from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TaskRecord:
    task: str
    status: str
    """How the run ended: completed, failed, cancelled, awaiting, timeout, error."""
    passed: bool | None
    """The check command's verdict; ``None`` when the task has no check."""
    turns: int
    tool_calls: int
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cost_usd: float | None
    wall_seconds: float
    model: str
    started_at: str
    session_id: str
    check_output: str = ""
    error: str = ""
    tags: list[str] = field(default_factory=list)


def trajectory(messages: list[Message]) -> list[dict[str, Any]]:
    """The run as a list of steps in a plain shape (role, text, tool calls, tool results)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        step: dict[str, Any] = {"role": m.role.value, "text": "", "thinking": "", "tool_calls": [], "tool_results": []}
        for b in m.content_blocks:
            if isinstance(b, TextBlock):
                step["text"] += b.text
            elif isinstance(b, ThinkingBlock):
                step["thinking"] += b.text
            elif isinstance(b, ToolUseBlock):
                try:
                    args = json.loads(b.arguments_json or "{}")
                except json.JSONDecodeError:
                    args = {"raw": b.arguments_json}
                step["tool_calls"].append({"id": b.tool_call_id, "name": b.name, "arguments": args})
            elif isinstance(b, ToolResultBlock):
                step["tool_results"].append({"id": b.tool_call_id, "content": b.content, "is_error": b.is_error})
        out.append(step)
    return out


def count_turns(messages: list[Message]) -> tuple[int, int]:
    turns = sum(1 for m in messages if m.role is MessageRole.assistant)
    calls = sum(1 for m in messages for b in m.content_blocks if isinstance(b, ToolUseBlock))
    return turns, calls


async def _shell(command: str, cwd: Path, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec("bash", "-lc", command, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace")


class BenchRunner:
    """Owns one :class:`SessionManager` and runs tasks through it; the state directory is the bench's own."""

    def __init__(self, settings: Settings, config: RuntimeConfig, *, preset: str | None = None, out_dir: Path, tools_off: list[str] | None = None) -> None:
        self.settings = settings
        self.config = config
        self.preset = preset
        self.out_dir = out_dir
        self.tools_off = tools_off if tools_off is not None else list(DEFAULT_TOOLS_OFF)
        self.db = Database(settings.db_path)
        self.manager: SessionManager | None = None
        self._done: dict[str, asyncio.Event] = {}
        self._status: dict[str, str] = {}

    async def __aenter__(self) -> BenchRunner:
        await self.db.open()
        self.manager = SessionManager(self.settings, self.config, db=self.db)
        await self.manager.start(recovering=False)
        self.manager.on_finished(self._finished)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.manager is not None:
            await self.manager.close()
        await self.db.close()

    async def _finished(self, session_id: str, run_id: str, status: str) -> None:
        self._status[session_id] = status
        event = self._done.get(session_id)
        if event is not None:
            event.set()

    async def run_task(self, task: Task) -> TaskRecord:
        manager = self.manager
        assert manager is not None
        started = datetime.now(UTC)
        clock = time.monotonic()
        state = await manager.create_session(f"[bench] {task.id}", metadata={"unattended": True, "bench": True})
        sid = state.session.id
        workspace = state.workspace
        record = TaskRecord(task=task.id, status="error", passed=None, turns=0, tool_calls=0, input_tokens=0, cache_read_tokens=0, output_tokens=0, cost_usd=None, wall_seconds=0.0, model="", started_at=started.isoformat(), session_id=sid, tags=list(task.tags))
        try:
            if self.preset:
                await manager.set_model(sid, preset=self.preset)
            if self.tools_off:
                await manager.set_tools_off(sid, list(self.tools_off))
            for rel, content in task.files.items():
                target = workspace / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            if task.setup:
                code, out = await _shell(task.setup, workspace, timeout=600)
                if code != 0:
                    record.error = f"setup failed ({code}): {out[-2000:]}"
                    return record
            self._done[sid] = asyncio.Event()
            await manager.submit(sid, task.prompt, origin="bench")
            try:
                await asyncio.wait_for(self._done[sid].wait(), timeout=task.timeout_minutes * 60)
                record.status = self._status.get(sid, "error")
            except TimeoutError:
                record.status = "timeout"
                await manager.stop(sid)
            if task.check:
                code, out = await _shell(task.check, workspace, timeout=600)
                record.passed = code == 0
                record.check_output = out[-4000:]
            messages = await manager.transcript(sid)
            record.turns, record.tool_calls = count_turns(messages)
            usage = await self.db.fetchone("SELECT sum(input_tokens) i, sum(cache_read_tokens) ch, sum(output_tokens) o, sum(cost_usd) usd, sum(cost_usd IS NULL) unmetered, max(model) model FROM usage_events WHERE session_id = ?", (sid,))
            if usage is not None:
                record.input_tokens = int(usage["i"] or 0)
                record.cache_read_tokens = int(usage["ch"] or 0)
                record.output_tokens = int(usage["o"] or 0)
                record.cost_usd = None if (usage["unmetered"] or 0) else (float(usage["usd"]) if usage["usd"] is not None else None)
                record.model = str(usage["model"] or "")
            (self.out_dir / "trajectories").mkdir(exist_ok=True)
            (self.out_dir / "trajectories" / f"{task.id}.json").write_text(json.dumps({"task": task.id, "session": sid, "steps": trajectory(messages)}, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — one task's crash is a record, not the end of the run
            logger.exception("task %s crashed", task.id)
            record.status = "error"
            record.error = f"{type(exc).__name__}: {exc}"
        finally:
            record.wall_seconds = round(time.monotonic() - clock, 1)
            self._done.pop(sid, None)
            try:
                await manager.delete_session(sid, delete_workspace=False)
            except Exception:  # noqa: BLE001
                logger.exception("could not delete bench session %s", sid)
            if not task.check or record.passed:
                shutil.rmtree(workspace, ignore_errors=True)  # a failed task keeps its workspace for inspection
        with (self.out_dir / "records.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        return record

    async def run(self, manifest: Manifest, *, concurrency: int = 1, only: list[str] | None = None) -> list[TaskRecord]:
        tasks = [t for t in manifest.tasks if not only or t.id in only]
        if manifest.tools_off:
            self.tools_off = list(manifest.tools_off)
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def one(task: Task) -> TaskRecord:
            async with semaphore:
                record = await self.run_task(task)
                logger.warning("bench %s: %s pass=%s turns=%d cost=%s wall=%.0fs", task.id, record.status, record.passed, record.turns, record.cost_usd, record.wall_seconds)
                return record

        records = list(await asyncio.gather(*(one(t) for t in tasks)))
        summary = summarize(manifest.name, records, preset=self.preset or self.config.model.preset)
        (self.out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        return records


def summarize(name: str, records: list[TaskRecord], *, preset: str) -> dict[str, Any]:
    judged = [r for r in records if r.passed is not None]
    costs = [r.cost_usd for r in records if r.cost_usd is not None]
    return {
        "name": name,
        "preset": preset,
        "at": datetime.now(UTC).isoformat(),
        "tasks": len(records),
        "judged": len(judged),
        "passed": sum(1 for r in judged if r.passed),
        "pass_rate": round(sum(1 for r in judged if r.passed) / len(judged), 4) if judged else None,
        "statuses": {s: sum(1 for r in records if r.status == s) for s in sorted({r.status for r in records})},
        "turns_mean": round(sum(r.turns for r in records) / len(records), 1) if records else 0,
        "tool_calls_mean": round(sum(r.tool_calls for r in records) / len(records), 1) if records else 0,
        "input_tokens": sum(r.input_tokens for r in records),
        "cache_read_tokens": sum(r.cache_read_tokens for r in records),
        "output_tokens": sum(r.output_tokens for r in records),
        "cost_usd": round(sum(costs), 4) if costs else None,
        "unmetered": sum(1 for r in records if r.cost_usd is None),
        "wall_seconds": round(sum(r.wall_seconds for r in records), 1),
    }


__all__ = ["BenchRunner", "TaskRecord", "count_turns", "summarize", "trajectory"]
