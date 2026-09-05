"""Drive one session from the terminal — the Telegram-free way to exercise the host."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

_DIM = "\x1b[2m"
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"


class TerminalRenderer:
    def __init__(self) -> None:
        self.tool_names: dict[str, str] = {}

    async def __call__(self, session_id: str, event: TurnEvent) -> None:
        p = event.payload
        t = event.type
        if t is EventType.CONTENT_BLOCK_DELTA:
            delta = p.get("delta") or {}
            text = delta.get("text") or ""
            if delta.get("type") == "thinking_delta":
                sys.stdout.write(f"{_DIM}{text}{_RESET}")
            else:
                sys.stdout.write(text)
            sys.stdout.flush()
        elif t is EventType.TOOL_USE_START:
            self.tool_names[str(p.get("tool_call_id"))] = str(p.get("tool_name") or "")
        elif t is EventType.TOOL_USE_STOP:
            args = p.get("final_input") or {}
            name = self.tool_names.get(str(p.get("tool_call_id")), "")
            sys.stdout.write(f"\n{_BOLD}→ {name}{_RESET}({json.dumps(args, ensure_ascii=False)[:300]})\n")
        elif t is EventType.TOOL_RESULT:
            content = str(p.get("content") or p.get("output") or "")
            sys.stdout.write(f"{_DIM}{content[:600]}{_RESET}\n")
        elif t is EventType.TOOL_CALL_PENDING:
            sys.stdout.write(f"\n{_BOLD}? AskUser{_RESET} {json.dumps(p.get('ask_user_payload'), ensure_ascii=False)}\n")
        elif t is EventType.ERROR:
            sys.stdout.write(f"\n[error] {json.dumps(p, ensure_ascii=False)}\n")
        elif t is EventType.MESSAGE_STOP:
            sys.stdout.write(f"\n{_DIM}[message_stop {p.get('stop_reason')} tokens={p.get('tokens_used')}]{_RESET}\n")
        elif t is EventType.STATE_CHANGED:
            sys.stdout.write(f"{_DIM}[state {p.get('from')} → {p.get('to')}]{_RESET}\n")
        sys.stdout.flush()


async def run_terminal_session(
    settings: Settings, config: RuntimeConfig, *, prompt: str | None, title: str
) -> int:
    db = Database(settings.db_path)
    await db.open()
    try:
        return await _run(db, settings, config, prompt=prompt, title=title)
    finally:
        await db.close()


async def _run(
    db: Database, settings: Settings, config: RuntimeConfig, *, prompt: str | None, title: str
) -> int:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    manager.add_sink(TerminalRenderer())
    done = asyncio.Event()
    outcome: dict[str, Any] = {}

    async def finished(session_id: str, run_id: str, status: str) -> None:
        outcome["status"] = status
        done.set()

    manager.on_finished(finished)
    state = await manager.create_session(title)
    print(f"session {state.session.id} workspace {state.workspace}")
    text = prompt or await asyncio.to_thread(input, "> ")
    while True:
        done.clear()
        await manager.submit(state.session.id, text)
        await done.wait()
        status = outcome.get("status")
        if status == "awaiting" and state.pending is not None:
            questions = state.pending.payload.get("questions", [])
            answers: list[dict[str, Any]] = []
            for q in questions:
                reply = await asyncio.to_thread(input, f"{q.get('question')} {[o.get('label') for o in q.get('options', [])]}: ")
                labels = [o.get("label") for o in q.get("options", [])]
                answers.append({"question": q.get("question"), "selected": [reply] if reply in labels else [], "custom": None if reply in labels else reply})
            done.clear()
            await manager.answer(state.session.id, answers)
            await done.wait()
            status = outcome.get("status")
        print(f"\n[run {status}]")
        if prompt is not None:
            break
        try:
            text = await asyncio.to_thread(input, "> ")
        except EOFError:
            break
        if text.strip() in ("/quit", "/exit"):
            break
    await manager.close()
    return 0 if outcome.get("status") in ("completed", "awaiting") else 1


__all__ = ["TerminalRenderer", "run_terminal_session"]
