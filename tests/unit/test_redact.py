from __future__ import annotations

import logging

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
    }
    for raw, expected in cases.items():
        assert r.redact(raw) == expected, raw


def test_pem_blocks_are_masked_whole() -> None:
    text = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\nafter"
    assert Redactor().redact(text) == f"before\n{MASK}\nafter"


def test_ordinary_text_is_untouched() -> None:
    r = Redactor(["real-secret-value-1"])
    text = "git status shows 3 files; the token bucket refills at 10/s; user=alice"
    assert r.redact(text) == text


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
