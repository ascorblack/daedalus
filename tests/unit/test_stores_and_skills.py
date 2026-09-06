from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from protocore.contracts.memory import MemoryScope
from protocore.contracts.skills import SkillUpsertInput
from protocore.contracts.types import Event, Message, MessageRole, Run, RunStatus, Session, TextBlock

from daedalus.extensions.api import validate_init_data
from daedalus.host.skills import DirectorySkillStore
from daedalus.stores.database import Database
from daedalus.stores.persistent import PersistentMemory
from daedalus.stores.sqlite import LiveControlStore, SqliteEventStream, SqliteRunStore, SqliteSessionStore


async def test_session_and_run_stores_roundtrip(db: Database) -> None:
    sessions = SqliteSessionStore(db)
    runs = SqliteRunStore(db)
    await sessions.create(Session(id="s1", tenant_id="t", title="one"))
    await sessions.append_message("s1", "t", Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")]))
    assert (await sessions.get("s1", "t")).title == "one"
    assert (await sessions.list_messages("s1", "t"))[0].content_blocks[0].text == "hi"
    await runs.create(Run(id="r1", tenant_id="t", session_id="s1", status=RunStatus.running))
    await runs.update_status("r1", "t", RunStatus.completed)
    assert (await runs.get("r1", "t")).status is RunStatus.completed


async def test_event_stream_keeps_latest_snapshot_apart_from_log(db: Database) -> None:
    stream = SqliteEventStream(db)
    stream.bind_run("r1", "s1")
    await stream.emit(Event(run_id="r1", name="state_snapshot", payload={"tenant_id": "t", "snapshot": {"state": "running", "x": 1}}))
    await stream.emit(Event(run_id="r1", name="state_snapshot", payload={"tenant_id": "t", "snapshot": {"state": "running", "x": 2}}))
    await stream.emit(Event(run_id="r1", name="tool_result", payload={"tenant_id": "t"}))
    assert (await stream.load_snapshot("r1"))["x"] == 2
    assert [name for _, e in await stream.list_events("r1") for name in [e.name]] == ["tool_result"]
    unfinished = await stream.unfinished_snapshots()
    assert unfinished[0]["session_id"] == "s1"


async def test_live_control_queues_and_overrides(db: Database) -> None:
    live = LiveControlStore(db)
    await live.enqueue("s", "follow_up", {"id": "q1", "kind": "follow_up", "text": "more", "attachments": []})
    await live.set_model("s", model_name="m2", thinking_enabled=False)
    state = await live.load("s")
    assert state["follow_up"][0]["text"] == "more" and state["model_name"] == "m2" and state["thinking_enabled"] is False


async def test_persistent_memory_survives_reload(db: Database) -> None:
    memory = PersistentMemory(db)
    await memory.load()
    result = await memory.write("t", MemoryScope.user, "owner", "the operator likes tea", kind="fact")
    fresh = PersistentMemory(db)
    await fresh.load()
    hits = await fresh.recall("t", "tea", scopes=[MemoryScope.user], scope_keys={MemoryScope.user: "owner"})
    assert hits and hits[0].record.id == result.record.id


async def test_directory_skill_store(tmp_path: Path) -> None:
    store = DirectorySkillStore(tmp_path / "skills")
    entry = await store.create("t", SkillUpsertInput(name="demo", description="a demo", body_md="# Body\ntext"))
    assert [e.name for e in await store.list("t")] == ["demo"]
    bundle = await store.load("t", entry.id)
    assert bundle.body.strip().startswith("# Body")
    (tmp_path / "skills" / entry.id / "extra.txt").write_text("x")
    files = await store.list_files("t", entry.id)
    assert {f.path for f in files} == {"SKILL.md", "extra.txt"}
    assert await store.load_file("t", entry.id, "extra.txt") == b"x"
    assert await store.load_file("t", entry.id, "../../etc/passwd") is None
    await store.set_enabled("t", entry.id, enabled=False)
    assert await store.list("t") == []
    assert [e.id for e in await store.list_subset("t", ["demo"])] == [entry.id]


def _init_data(token: str, user_id: int, age: int = 0) -> str:
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "q", "user": json.dumps({"id": user_id, "first_name": "A"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_init_data_validation() -> None:
    data = validate_init_data(_init_data("123:abc", 42), "123:abc")
    assert data["user"]["id"] == 42
    with pytest.raises(ValueError):
        validate_init_data(_init_data("123:abc", 42), "other")
    with pytest.raises(ValueError):
        validate_init_data(_init_data("123:abc", 42, age=100_000), "123:abc")


def test_config_with_optional_sections_round_trips_through_toml(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from daedalus.config import McpServerConfig, RuntimeConfig

    config = RuntimeConfig()
    config.mcp.servers["board"] = McpServerConfig(transport="http", url="https://example.test/mcp")
    path = tmp_path / "config.toml"
    config.save(path)  # an unset optional block must not break serialisation
    loaded = RuntimeConfig.load(path)
    assert loaded.mcp.servers["board"].url == "https://example.test/mcp"
    assert loaded.mcp.servers["board"].oauth is None


async def test_transcript_survives_history_rewrites(db) -> None:  # type: ignore[no-untyped-def]
    from protocore.contracts.types import Message, MessageRole, TextBlock

    from daedalus.stores.sqlite import SqliteSessionStore

    store = SqliteSessionStore(db)
    ask = Message(role=MessageRole.user, content_blocks=[TextBlock(text="the task")])
    reply = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="done")])
    assert await store.append_transcript("s1", [ask, reply]) == 2
    assert await store.append_transcript("s1", [ask, reply]) == 0  # idempotent
    summary = Message(role=MessageRole.user, content_blocks=[TextBlock(text="<compacted-turn>x</compacted-turn>")], metadata={"protocore.compaction_summary": True})
    await store.replace_messages("s1", "t", [summary])  # the model's history shrank
    await store.append_transcript("s1", [summary])
    texts = [m.content_blocks[0].text for m in await store.list_transcript("s1")]  # type: ignore[union-attr]
    assert texts == ["the task", "done", "<compacted-turn>x</compacted-turn>"]
    assert len(await store.list_transcript("s1", limit=2)) == 2
