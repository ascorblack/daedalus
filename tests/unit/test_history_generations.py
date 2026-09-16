"""The working history is appended to, and only a rewrite of the sequence starts a generation."""

from __future__ import annotations

from protocore.contracts.types import Message, MessageRole, Session, TextBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteSessionStore

TENANT = "daedalus"


def _msg(text: str) -> Message:
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)])


async def _rows(db: Database, sid: str) -> list[tuple[int, str]]:
    rows = await db.fetchall("SELECT seq, gen FROM session_messages WHERE session_id = ? ORDER BY seq", (sid,))
    return [(int(r["seq"]), int(r["gen"])) for r in rows]


async def test_a_round_writes_only_its_new_rows(db: Database) -> None:
    store = SqliteSessionStore(db)
    await store.create(Session(id="g1", tenant_id=TENANT, title="t"))
    history = [_msg(f"m{i}") for i in range(5)]
    assert await store.sync_messages("g1", TENANT, history) == 5
    first = await _rows(db, "g1")
    history.append(_msg("m5"))
    assert await store.sync_messages("g1", TENANT, history) == 1
    after = await _rows(db, "g1")
    assert after[: len(first)] == first  # the rows that were there were not rewritten
    assert len(after) == 6
    assert [b.text for m in await store.list_messages("g1", TENANT, limit=100) for b in m.content_blocks] == [f"m{i}" for i in range(6)]
    assert await store.sync_messages("g1", TENANT, history) == 0  # nothing new, nothing written


async def test_a_rewritten_history_becomes_a_new_generation_and_the_old_one_goes(db: Database) -> None:
    store = SqliteSessionStore(db)
    await store.create(Session(id="g2", tenant_id=TENANT, title="t"))
    await store.sync_messages("g2", TENANT, [_msg(f"m{i}") for i in range(6)])
    assert {gen for _, gen in await _rows(db, "g2")} == {0}
    rebuilt = [_msg("summary"), _msg("m5")]
    await store.sync_messages("g2", TENANT, rebuilt)
    assert {gen for _, gen in await _rows(db, "g2")} == {1}
    assert len(await _rows(db, "g2")) == 2
    read = await store.list_messages("g2", TENANT, limit=100)
    assert [b.text for m in read for b in m.content_blocks] == ["summary", "m5"]
    # And the next round appends to the new generation rather than starting another.
    assert await store.sync_messages("g2", TENANT, [*rebuilt, _msg("m6")]) == 1
    assert {gen for _, gen in await _rows(db, "g2")} == {1}


async def test_the_history_survives_a_compaction_and_keeps_appending(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    state = await manager.create_session("gen")
    sid = state.session.id
    history = [_msg(f"m{i}") for i in range(8)]
    await manager.sessions.sync_messages(sid, TENANT, history)
    rebuilt = [_msg("compacted"), *history[-2:]]
    await manager.sessions.replace_messages(sid, TENANT, rebuilt)
    kept = await manager.sessions.list_messages(sid, TENANT, limit=100)
    assert [b.text for m in kept for b in m.content_blocks] == ["compacted", "m6", "m7"]
    before = await _rows(db, sid)
    await manager.sessions.sync_messages(sid, TENANT, [*rebuilt, _msg("m8")])
    after = await _rows(db, sid)
    assert after[: len(before)] == before and len(after) == len(before) + 1
    assert [b.text for m in await manager.sessions.list_messages(sid, TENANT, limit=100) for b in m.content_blocks] == ["compacted", "m6", "m7", "m8"]
    await manager.close()


async def test_rows_written_before_the_generations_existed_are_rewritten_once(db: Database) -> None:
    store = SqliteSessionStore(db)
    await store.create(Session(id="g4", tenant_id=TENANT, title="t"))
    old = [_msg("old one"), _msg("old two")]
    for message in old:  # as the pre-migration writer left them: generation 0, no key
        await db.execute("INSERT INTO session_messages(session_id, tenant_id, message) VALUES (?, ?, ?)", ("g4", TENANT, message.model_dump_json()))
    await store.sync_messages("g4", TENANT, [*old, _msg("new")])
    assert {gen for _, gen in await _rows(db, "g4")} == {1}
    assert [b.text for m in await store.list_messages("g4", TENANT, limit=100) for b in m.content_blocks] == ["old one", "old two", "new"]
