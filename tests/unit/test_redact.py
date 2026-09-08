from __future__ import annotations

import logging
from pathlib import Path

from protocore.contracts.hooks import HookActionKind
from protocore.contracts.types import HookEvent

from daedalus.host.hooks import DaedalusHookManager
from daedalus.providers.wire import parse_json_arguments
from daedalus.security.redact import MASK, RedactingFilter, Redactor


def test_configured_values_are_masked_whatever_they_look_like() -> None:
    r = Redactor(["plain-looking-secret-value", "short"])
    assert r.redact("key=plain-looking-secret-value done") == f"key={MASK} done"
    assert r.redact("short stays") == "short stays"  # below the minimum length: too collision-prone


def test_secret_shapes_are_masked() -> None:
    r = Redactor()
    cases = {
        "token 1234567890:AAHfiqksKZ8WmR2zSjiQ7_v4TI7IqhIzLHo here": f"token {MASK} here",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345": f"Authorization: Bearer {MASK}",
        "export OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz": f"export OPENAI_API_KEY={MASK}",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789": MASK,
        "AKIAIOSFODNN7EXAMPLE": MASK,
        "postgres://user:s3cretpass@db.local/x": f"postgres://user:{MASK}@db.local/x",
        "DB_PASSWORD='hunter22'": f"DB_PASSWORD='{MASK}'",
        '{"api_key": "8f3c1d2e9a0b7c6d5e4f3a2b1c0d9e8f"}': f'{{"api_key": "{MASK}"}}',
        '{"password": "hunter2hunter2"}': f'{{"password": "{MASK}"}}',
        "X-Api-Key: 8f3c1d2e9a0b7c6d5e4f3a2b": f"X-Api-Key: {MASK}",
        'curl -H "Authorization: token 8f3c1d2e9a0b7c6d5e4f"': f'curl -H "Authorization: token {MASK}"',
        "glpat-ABCDEFGHIJKLMNOPQRST": MASK,
        "api_key: 8f3c1d2e9a0b7c6d5e4f": f"api_key: {MASK}",
    }
    for raw, expected in cases.items():
        assert r.redact(raw) == expected, raw


def test_pem_blocks_are_masked_whole() -> None:
    text = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\nafter"
    assert Redactor().redact(text) == f"before\n{MASK}\nafter"


def test_ordinary_text_and_source_code_are_untouched() -> None:
    r = Redactor(["real-secret-value-1"])
    for text in (
        "git status shows 3 files; the token bucket refills at 10/s; user=alice",
        'input_tokens=normalized["input_tokens"],',
        "tokens = response.json()",
        "access_token: Optional[str] = None",
        "self.password = derive(salt)",
        "TOKEN_BUDGET = 128000",
        'api_key = os.environ["OPENAI_API_KEY"]',
        "https://github.com/x/y/commit/9f86d081884c7d659a2feaa0c55ad015a3bf4f1b",
        "data:image/png;base64,eyJhbGciOiJIUzI1NiJ9AAAA",
    ):
        assert r.redact(text) == text, text


def test_own_source_files_survive_redaction_byte_identical() -> None:
    """The agent reads and rewrites its own code through the redactor; it must not mangle it."""
    root = Path(__file__).resolve().parents[2]
    r = Redactor()
    for rel in ("daedalus/providers/openai_compat.py", "daedalus/mcp/oauth.py", "daedalus/extensions/api.py", "daedalus/config.py", "launcher/supervisor.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert r.redact(text) == text, rel


def test_nested_arguments_are_redacted() -> None:
    r = Redactor(["real-secret-value-1"])
    out = r.redact_any({"command": "curl -H 'x: real-secret-value-1'", "env": {"A": ["real-secret-value-1"]}, "n": 3})
    assert out == {"command": f"curl -H 'x: {MASK}'", "env": {"A": [MASK]}, "n": 3}


def test_logging_filter_masks_records() -> None:
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "key %s", ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",), None)
    assert RedactingFilter(Redactor()).filter(record) is True
    assert record.getMessage() == f"key {MASK}"


async def test_post_tool_hook_rewrites_output() -> None:
    hooks = DaedalusHookManager(Redactor(["real-secret-value-1"]))
    result = await hooks.invoke(HookEvent.post_tool_use, {"tool_name": "Exec", "tool_output": "TOKEN=real-secret-value-1"}, "t")
    assert result.action == HookActionKind.MODIFY
    assert result.modifications["tool_output"] == f"TOKEN={MASK}"
    clean = await hooks.invoke(HookEvent.post_tool_use, {"tool_name": "Exec", "tool_output": "all good"}, "t")
    assert clean.action == HookActionKind.ALLOW
    assert hooks.redacted_calls == 1


def test_tool_argument_repairs() -> None:
    assert parse_json_arguments('{"a": 1,}') == {"a": 1}
    assert parse_json_arguments('```json\n{"a": [1, 2,]}\n```') == {"a": [1, 2]}
    assert parse_json_arguments("{'path': 'x.py'}") == {"path": "x.py"}
    assert parse_json_arguments('Here you go: {"q": "x"} thanks') == {"q": "x"}
    assert parse_json_arguments('{"command": "ls -la') == {"command": "ls -la"}
    assert parse_json_arguments("") == {}


async def test_failed_tool_result_is_masked_in_event_and_history() -> None:
    """A tool that raised skips the core hook; the session runner masks the event and the history."""
    from types import SimpleNamespace

    from protocore.contracts.types import Message, MessageRole, ToolResultBlock
    from protocore.runtime.events.envelope import TurnEvent
    from protocore.runtime.events.types import EventType

    from daedalus.host.session_runner import SessionManager

    raw = "tool 'WebFetch' execution failed: 401 for https://api.x.test/?key=sk-proj-abcdefghijklmnopqrstuvwxyz"
    engine = SimpleNamespace(history=[Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c9", content=raw, is_error=True)])])
    state = SimpleNamespace(engine=engine, last_error_kind="")
    manager = SimpleNamespace(redactor=Redactor(), _redact_history_result=SessionManager._redact_history_result)
    event = TurnEvent(type=EventType.ERROR, run_id="r", payload={"message": raw})
    SessionManager._redact_event(manager, state, event)  # type: ignore[arg-type]
    assert "sk-proj-" not in event.payload["message"]
    event = TurnEvent(type=EventType.TOOL_RESULT, run_id="r", payload={"tool_call_id": "c9", "content": raw})
    SessionManager._redact_event(manager, state, event)  # type: ignore[arg-type]
    assert "sk-proj-" not in event.payload["content"]
    block = engine.history[0].content_blocks[0]
    assert isinstance(block, ToolResultBlock) and "sk-proj-" not in block.content and block.is_error
