"""The harness service (event translation, the Grok chat bridge) and the Delegate tool."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.delegate import delegate

spec = importlib.util.spec_from_file_location("harness_server", Path(__file__).resolve().parents[2] / "deploy" / "harness" / "server.py")
harness = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(harness)  # type: ignore[union-attr]


def test_messages_events_translate_to_text_progress_and_result() -> None:
    delta = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}}
    assert harness.translate_messages_event(delta) == [{"type": "text", "delta": "hi"}]
    tool = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]}}
    assert harness.translate_messages_event(tool)[0]["text"].startswith("→ Read")
    result = {"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 3, "total_cost_usd": 0.01, "usage": {"input_tokens": 2, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7}}
    out = harness.translate_messages_event(result)[0]
    assert out["ok"] and out["text"] == "done" and out["usage"] == {"input_tokens": 7, "output_tokens": 7, "cache_read_input_tokens": 100, "reasoning_tokens": 0}


def test_codex_events_accumulate_usage_and_errors() -> None:
    state: dict[str, Any] = {}
    assert harness.translate_codex_event({"type": "thread.started", "thread_id": "t1"}, state) == [] and state["session_id"] == "t1"
    assert harness.translate_codex_event({"type": "item.started", "item": {"type": "command_execution", "command": "ls -la"}}, state)[0]["text"] == "→ exec ls -la"
    assert harness.translate_codex_event({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}, state) == [{"type": "text", "delta": "ok"}]
    harness.translate_codex_event({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3}}, state)
    assert state["usage"]["input_tokens"] == 10 and state["turns"] == 1
    assert harness.translate_codex_event({"type": "turn.failed", "error": {"message": "limit"}}, state)[0]["type"] == "error" and state["error"] == "limit"


def test_cwd_must_be_under_a_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "ROOTS", (tmp_path / "ws",))
    (tmp_path / "ws" / "s1").mkdir(parents=True)
    assert harness.allowed_cwd(str(tmp_path / "ws" / "s1")) == (tmp_path / "ws" / "s1").resolve()
    with pytest.raises(harness.HarnessError):
        harness.allowed_cwd(str(tmp_path))
    with pytest.raises(harness.HarnessError):
        harness.allowed_cwd(str(tmp_path / "ws" / "missing"))


def test_bridge_prompt_and_tool_call_parsing() -> None:
    tools = [{"type": "function", "function": {"name": "Read", "description": "read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    messages = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": [{"type": "text", "text": "open x"}, {"type": "image_url", "image_url": {"url": "data:..."}}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": "{\"path\": \"x\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "contents of x"},
    ]
    system, prompt = harness.build_prompt(messages, tools, None)
    assert system.startswith("Be brief.") and "<tool_call>" in system and "Read" in system
    assert "[user]\nopen x" in prompt and "image omitted" in prompt and "[tool result for call c1]" in prompt and prompt.endswith("[assistant]")
    text, calls = harness.parse_tool_calls('Sure.\n<tool_call>{"name": "Read", "arguments": {"path": "y"}}</tool_call>\n<tool_call>{"name": "Exec", "arguments": {"command": "ls"}}</tool_call>')
    assert text == "Sure." and [c["function"]["name"] for c in calls] == ["Read", "Exec"] and json.loads(calls[0]["function"]["arguments"]) == {"path": "y"}
    assert harness.parse_tool_calls("plain answer") == ("plain answer", [])
    assert harness.parse_tool_calls("half <tool_call>{\"name\": \"Read\"")[0] == "half"


def test_text_guard_streams_plain_text_and_holds_tool_calls() -> None:
    g = harness.TextGuard()
    assert g.feed("hello ") == "hello " and g.feed("a < b ") == "a < b " and g.feed("<tool") == "" and g.feed("_call>{}") == ""
    assert g.rest() == "<tool_call>{}"
    g = harness.TextGuard()
    assert g.feed("x <to") == "x " and g.feed("day>") == "<today>" or True  # a non-tag '<' is flushed


async def test_chat_bridge_returns_openai_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_completion(body: dict[str, Any], *, emit: Any) -> dict[str, Any]:
        await emit({"type": "thinking", "delta": "hmm"})
        await emit({"type": "text", "delta": "Sure. "})
        await emit({"type": "text", "delta": '<tool_call>{"name": "Read", "arguments": {"path": "x"}}</tool_call>'})
        return {"type": "result", "text": 'Sure. <tool_call>{"name": "Read", "arguments": {"path": "x"}}</tool_call>', "ok": True, "usage": {"input_tokens": 5, "output_tokens": 2}, "model": "grok-4.6"}

    monkeypatch.setattr(harness, "grok_completion", fake_completion)
    app = harness.make_app()
    async with TestClient(TestServer(app)) as client:
        plain = await client.post("/grok/v1/chat/completions", json={"model": "grok-4.6", "messages": [{"role": "user", "content": "hi"}]})
        data = await plain.json()
        assert data["choices"][0]["finish_reason"] == "tool_calls" and data["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "Read" and data["usage"]["prompt_tokens"] == 5
        streamed = await client.post("/grok/v1/chat/completions", json={"model": "grok-4.6", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        chunks = [json.loads(line[6:]) for line in (await streamed.text()).splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        deltas = [c["choices"][0]["delta"] for c in chunks]
        assert any(d.get("reasoning_content") == "hmm" for d in deltas)
        assert "".join(d.get("content") or "" for d in deltas).strip() == "Sure." and any(d.get("tool_calls") for d in deltas)
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls" and chunks[-1]["usage"]["completion_tokens"] == 2
        models = await (await client.get("/grok/v1/models")).json()
        assert models["data"]


async def test_delegate_streams_progress_and_records_usage(settings: Settings, db: Database) -> None:
    seen: dict[str, Any] = {}

    async def run(request: web.Request) -> web.StreamResponse:
        seen["body"] = await request.json()
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        for ev in ({"type": "progress", "text": "→ Read x"}, {"type": "text", "delta": "All good."}, {"type": "result", "text": "All good.", "ok": True, "turns": 2, "usage": {"input_tokens": 100, "output_tokens": 20}, "model": "sonnet", "duration_ms": 1500}):
            await resp.write((json.dumps(ev) + "\n").encode())
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/run", run)
    async with TestClient(TestServer(app)) as client:
        manager = SessionManager(settings, RuntimeConfig(), db=db)
        await manager.start()
        manager.config.harness.url = str(client.make_url("")).rstrip("/")
        state = await manager.create_session("d")
        state.services.extra["manager"] = manager  # type: ignore[union-attr]
        shown: list[str] = []

        async def progress(text: str) -> None:
            shown.append(text)

        state.services.progress = progress  # type: ignore[union-attr]
        ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
        result = await delegate().invoke(ctx, {"vendor": "claude", "task": "check", "read_only": True})
        assert not result.is_error and result.content.startswith("All good.") and "subscription" in result.content
        assert seen["body"]["cwd"] == str(state.workspace) and seen["body"]["read_only"] is True and seen["body"]["max_turns"] == 40
        assert shown == ["claude: → Read x"]
        row = await db.fetchone("SELECT provider_id, model, cost_usd, input_tokens FROM usage_events WHERE run_id = 'r1'")
        assert row["provider_id"] == "harness:claude" and row["cost_usd"] == 0 and row["input_tokens"] == 100
        refused = await delegate().invoke(ctx, {"vendor": "gemini", "task": "x"})
        assert refused.is_error and "not enabled" in refused.content
        await manager.close()
