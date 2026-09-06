"""Whole-history compaction between runs: the cut, the quoted operator messages, chunking, the trigger."""

from __future__ import annotations

from typing import Any

from protocore.contracts.llm import LLMResponse
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock, ToolResultBlock, ToolUseBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import (
    SessionManager,
    compaction_cut,
    identifier_index,
    operator_quotes,
    split_transcript,
)
from daedalus.providers.openai_compat import UsageRecord
from daedalus.stores.database import Database

SECTIONED = "## Goal\ng\n## Constraints\nc\n## State\ns\n## Discoveries\nd\n## Open\no\n## Next steps\nn\n## Unknowns\nu\n## Identifiers\ni"


def _op(text: str) -> Message:
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator"})


def test_cut_never_splits_a_tool_exchange() -> None:
    history = [
        _op("one"),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Exec", arguments_json="{}")]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="r")]),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="a1")]),
        _op("two"),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c2", name="Exec", arguments_json="{}")]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c2", content="r")]),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="a2")]),
    ]
    assert compaction_cut(history, 0) == len(history)
    assert compaction_cut(history, 2) == 4  # walks back from 'tool result' to the turn start at "two"
    assert compaction_cut(history, 4) == 4
    assert compaction_cut(history, 100) == 0


def test_operator_messages_are_quoted_by_code() -> None:
    history = [_op("Never push to main."), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="ok")]), _op("Use port 8765 for the API."), _op("recent one"), _op("recent two"), _op("recent three")]
    quotes = operator_quotes(history)
    assert "## Operator said (verbatim, oldest first)" in quotes and "- Never push to main." in quotes and "port 8765" in quotes
    assert "recent one" not in quotes  # the last three are printed whole by the tail
    assert operator_quotes([_op("only"), _op("two"), _op("three")]) == ""


def test_transcript_splits_on_lines_by_size() -> None:
    text = "\n".join(f"line {i} " + "x" * 100 for i in range(100))
    parts = split_transcript(text, chunk_tokens=1000)  # ~4000 chars per part
    assert len(parts) >= 3 and "\n".join(parts) == text and all(len(p) <= 4200 for p in parts)
    assert split_transcript("short", 1000) == ["short"]


async def test_auto_compaction_keeps_the_tail_and_quotes_the_operator(settings: Settings, db: Database) -> None:
    config = RuntimeConfig()
    config.compaction.auto_ratio = 0.5
    config.compaction.keep_recent_messages = 2
    config.compaction.min_messages = 4
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("long")
    history = [_op("Rule: answer in Russian only."), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="да")]), _op("Second ask about /srv/x."), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="сделано")]), _op("latest ask"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="ответ")])]
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    await manager.sessions.append_transcript(state.session.id, history)
    provider = manager.providers.get(manager.providers.available()[0])
    calls: list[str] = []

    async def fake_complete(request: Any) -> LLMResponse:
        calls.append(request.messages[0].content_blocks[0].text)
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=SECTIONED)]), stop_reason=StopReason.end_turn)

    provider.complete_text = fake_complete  # type: ignore[method-assign]
    # below the ratio: nothing happens
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 1000}, cost_usd=0.0, duration_ms=1, run_id="r0", session_id=state.session.id))
    await manager._maybe_auto_compact(state)
    assert not calls
    # above it: the older part becomes one summary, the last turn stays verbatim
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 120_000}, cost_usd=0.0, duration_ms=1, run_id="r1", session_id=state.session.id))
    seen: list[dict[str, Any]] = []

    async def hook(session_id: str, info: dict[str, Any]) -> None:
        seen.append(info)

    manager.compaction_hooks.append(hook)
    await manager._maybe_auto_compact(state)
    assert len(calls) == 1 and "Rule: answer in Russian only." in calls[0]
    messages = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert len(messages) == 3 and messages[0].metadata["daedalus.compaction"]["reason"] == "auto" and messages[0].metadata["daedalus.compaction"]["kept"] == 2
    body = messages[0].content_blocks[0].text  # type: ignore[union-attr]
    assert "Rule: answer in Russian only." in body and "## Recent operator messages (verbatim)" in body  # two operator turns: both fit the verbatim tail
    assert messages[1].content_blocks[0].text == "latest ask" and messages[2].content_blocks[0].text == "ответ"  # type: ignore[union-attr]
    assert seen and seen[0]["before_messages"] == 6 and seen[0]["after_messages"] == 3
    await manager.close()


def test_identifiers_are_indexed_by_code() -> None:
    history = [
        _op("Deploy to /srv/state/worktrees/bot and open PR #42 on port 8765; see https://example.org/x?y=1."),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Read", arguments_json='{"path": "/srv/workspaces/abc/notes.md"}')]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="/noise/from/results/only.txt 12345")]),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Session 62d62b5f668d done in 2026.")]),
    ]
    index = identifier_index(history)
    for token in ("/srv/state/worktrees/bot", "PR #42", "8765", "https://example.org/x?y=1", "/srv/workspaces/abc/notes.md", "62d62b5f668d"):
        assert f"- {token}" in index, token
    assert "/noise/from/results/only.txt" not in index and "- 2026" not in index
