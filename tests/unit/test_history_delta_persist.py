"""What a round tells the store, and what the store does with a surface it is only shown once.

Two contracts the core can offer. The older one hands the store the whole working history on
every round and the store works out what changed; the newer one says what changed, and the
round writes its own two rows instead of the conversation's eight hundred. The host speaks
both, so these tests run under either core and skip only what the core cannot do.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from protocore.contracts.types import Event, Message, MessageRole, Session, TextBlock

from daedalus.config import Settings
from daedalus.host.session_runner import SessionManager, _forget_persisted
from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteEventStream, SqliteSessionStore
from tests.support.models import model_config

TENANT = "daedalus"

try:  # the incremental contract; an older core carries only the full write
    from protocore.runtime.history_persist import persist_history
except ImportError:  # pragma: no cover - taken by the run against the older core
    persist_history = None

needs_delta = pytest.mark.skipif(persist_history is None, reason="this core hands the store no delta")


def _marker(engine) -> object | None:  # type: ignore[no-untyped-def]
    """What the core remembers handing over, whatever it calls it."""
    for name in ("persisted_history_marker", "persisted_history_prefix"):
        if hasattr(engine, name):
            return getattr(engine, name)
    raise AssertionError("this core remembers nothing about what it persisted")


def _msg(text: str) -> Message:
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)])


async def _rows(db: Database, sid: str) -> list[tuple[int, int, str]]:
    rows = await db.fetchall("SELECT seq, gen, message FROM session_messages WHERE session_id = ? ORDER BY seq", (sid,))
    return [(int(r["seq"]), int(r["gen"]), str(r["message"])) for r in rows]


async def _settled(state) -> None:  # type: ignore[no-untyped-def]
    """Wait out the writes a hand-over started; they are tasks, not part of the call."""
    for _ in range(50):
        pending = [t for t in state.persist_tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


# -- the store ---------------------------------------------------------------------------


async def test_append_messages_adds_to_the_generation_and_leaves_the_rest_alone(db: Database) -> None:
    store = SqliteSessionStore(db)
    await store.create(Session(id="d1", tenant_id=TENANT, title="t"))
    history = [_msg(f"m{i}") for i in range(4)]
    await store.replace_messages("d1", TENANT, history)
    before = await _rows(db, "d1")
    assert await store.append_messages("d1", TENANT, [_msg("m4"), _msg("m5")]) == 2
    after = await _rows(db, "d1")
    assert after[: len(before)] == before, "the rows that were there were rewritten"
    assert len(after) == 6
    assert {gen for _, gen, _ in after} == {gen for _, gen, _ in before}, "an append does not start a generation"
    read = await store.list_messages("d1", TENANT, limit=100)
    assert [b.text for m in read for b in m.content_blocks] == [f"m{i}" for i in range(6)]


async def test_append_messages_with_nothing_to_add_touches_nothing(db: Database) -> None:
    store = SqliteSessionStore(db)
    await store.create(Session(id="d2", tenant_id=TENANT, title="t"))
    await store.replace_messages("d2", TENANT, [_msg("only")])
    before = await _rows(db, "d2")
    assert await store.append_messages("d2", TENANT, []) == 0
    assert await _rows(db, "d2") == before


# -- the host, under the newer core -------------------------------------------------------


@needs_delta
async def test_a_round_writes_only_what_it_added(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("delta")
        engine = await manager._build_engine(state, "run-1")
        state.engine = engine
        engine.history = [_msg(f"m{i}") for i in range(4)]
        persist_history(engine)
        await _settled(state)
        opening = await _rows(db, state.session.id)
        assert len(opening) == 4

        engine.history = [*engine.history, _msg("m4")]
        persist_history(engine)
        await _settled(state)
        after = await _rows(db, state.session.id)
        assert after[:4] == opening, "the round rewrote rows it had not touched"
        assert len(after) == 5 and after[-1][1] == opening[-1][1], "the round started a generation"
        assert [b.text for m in await manager.sessions.list_messages(state.session.id, TENANT, limit=100) for b in m.content_blocks] == [
            f"m{i}" for i in range(5)
        ]
    finally:
        await manager.close()


@needs_delta
async def test_a_rewritten_history_becomes_a_new_generation(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("delta")
        engine = await manager._build_engine(state, "run-1")
        state.engine = engine
        engine.history = [_msg(f"m{i}") for i in range(6)]
        persist_history(engine)
        await _settled(state)
        first_gen = {gen for _, gen, _ in await _rows(db, state.session.id)}

        engine.history = [_msg("compacted"), *engine.history[-2:]]  # a summary replaced the span
        persist_history(engine)
        await _settled(state)
        rows = await _rows(db, state.session.id)
        assert len(rows) == 3
        second_gen = {gen for _, gen, _ in rows}
        assert second_gen != first_gen and second_gen.pop() > first_gen.pop()
        assert [b.text for m in await manager.sessions.list_messages(state.session.id, TENANT, limit=100) for b in m.content_blocks] == [
            "compacted",
            "m4",
            "m5",
        ]
    finally:
        await manager.close()


@needs_delta
async def test_dropping_the_rows_makes_the_next_hand_over_a_rewrite(settings: Settings, db: Database) -> None:
    """Rows the host dropped are not there to be appended onto. Nothing about the working history
    says so — it only grew — so the host says it, and the round after writes the history whole."""
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("delta")
        engine = await manager._build_engine(state, "run-1")
        state.engine = engine
        engine.history = [_msg("a"), _msg("b")]
        persist_history(engine)
        await _settled(state)
        assert _marker(engine) is not None

        await db.execute("DELETE FROM session_messages WHERE session_id = ?", (state.session.id,))
        _forget_persisted(state)
        assert _marker(engine) is None

        engine.history = [*engine.history, _msg("c")]
        persist_history(engine)
        await _settled(state)
        assert [b.text for m in await manager.sessions.list_messages(state.session.id, TENANT, limit=100) for b in m.content_blocks] == [
            "a",
            "b",
            "c",
        ], "the round appended onto rows that were gone"
    finally:
        await manager.close()


@needs_delta
async def test_clearing_the_history_tells_the_core_to_forget_what_it_persisted(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("delta")
        engine = await manager._build_engine(state, "run-1")
        state.engine = engine
        engine.history = [_msg("a"), _msg("b")]
        persist_history(engine)
        await _settled(state)
        assert _marker(engine) is not None

        assert (await manager.clear_history(state.session.id))["dropped"] == 2
        assert _marker(engine) is None
    finally:
        await manager.close()


# -- the host, under either core ------------------------------------------------------------


async def test_the_full_write_still_persists_a_history(settings: Settings, db: Database) -> None:
    """The fallback the older core calls, and the one a host keeps for it: handed nothing but the
    engine, the store brings the stored history up to the one it holds."""
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("fallback")
        engine = await manager._build_engine(state, "run-1")
        state.engine = engine
        engine.history = [_msg("a"), _msg("b")]
        engine.persist_session_history(engine)
        await _settled(state)
        assert len(await _rows(db, state.session.id)) == 2

        engine.history = [*engine.history, _msg("c")]
        engine.persist_session_history(engine)
        await _settled(state)
        assert [b.text for m in await manager.sessions.list_messages(state.session.id, TENANT, limit=100) for b in m.content_blocks] == ["a", "b", "c"]
    finally:
        await manager.close()


# -- the advertised tool surface --------------------------------------------------------------


def _advert(run_id: str, tools: list[dict[str, object]], **payload: object) -> Event:
    return Event(id=f"ad-{run_id}", run_id=run_id, name="tool_surface_advertised", payload={"tools": tools, **payload})


async def test_the_surface_is_kept_by_its_digest_and_its_descriptions_are_not_lost(db: Database) -> None:
    """The runtime sends the descriptions the first time it names a digest and not again. A copy
    kept under the hash of what arrived would keep two surfaces for one digest and lose the
    descriptions with whichever row was swept first."""
    events = SqliteEventStream(db)
    digest = "b" * 64
    described = [{"name": f"Tool{i}", "description": "what this one does, at length. " * 10} for i in range(20)]
    named_only = [{"name": t["name"]} for t in described]

    await events.emit(_advert("r1", described, tool_surface_digest=digest, tool_surface_described=True))
    for run in ("r2", "r3"):
        await events.emit(_advert(run, named_only, tool_surface_digest=digest, tool_surface_described=False))

    blobs = await db.fetchall("SELECT key, value FROM kv WHERE key LIKE 'event_blob:%'")
    assert len(blobs) == 1, "one surface, one copy — the undescribed advertisements named the same one"
    kept = await events.tool_surface(digest)
    assert kept is not None and kept[0]["description"].startswith("what this one does")

    rows = await db.fetchall("SELECT payload FROM events ORDER BY seq")
    assert len(rows) == 3 and all("what this one does" not in r["payload"] for r in rows)
    assert all(str(blobs[0]["key"])[len("event_blob:") :] in str(r["payload"]) for r in rows), "a row that does not name the blob loses it to the sweep"


async def test_an_undescribed_surface_never_displaces_the_descriptions(db: Database) -> None:
    events = SqliteEventStream(db)
    digest = "c" * 64
    described = [{"name": "One", "description": "the whole schema"}]
    await events.emit(_advert("r1", described, tool_surface_digest=digest, tool_surface_described=True))
    await events.emit(_advert("r2", [{"name": "One"}], tool_surface_digest=digest, tool_surface_described=False))
    kept = await events.tool_surface(digest)
    assert kept == described


async def test_a_surface_first_seen_without_its_descriptions_is_still_kept(db: Database) -> None:
    """A host that came up mid-conversation with the runtime has no described copy to hold onto;
    what it was given is what it keeps, until a described advertisement replaces it."""
    events = SqliteEventStream(db)
    digest = "d" * 64
    await events.emit(_advert("r1", [{"name": "One"}], tool_surface_digest=digest, tool_surface_described=False))
    assert await events.tool_surface(digest) == [{"name": "One"}]
    await events.emit(_advert("r2", [{"name": "One", "description": "at last"}], tool_surface_digest=digest, tool_surface_described=True))
    assert await events.tool_surface(digest) == [{"name": "One", "description": "at last"}]


async def test_a_runtime_that_names_no_digest_is_stored_as_it_always_was(db: Database) -> None:
    events = SqliteEventStream(db)
    tools = [{"name": "One", "description": "unchanged"}]
    await events.emit(_advert("r1", tools))
    await events.emit(_advert("r2", tools))
    blobs = await db.fetchall("SELECT key, value FROM kv WHERE key LIKE 'event_blob:%'")
    assert len(blobs) == 1 and json.loads(blobs[0]["value"]) == tools
    assert await events.tool_surface(str(blobs[0]["key"])[len("event_blob:") :]) == tools


@needs_delta
async def test_a_write_that_never_landed_is_not_left_behind(settings: Settings, db: Database) -> None:
    """The hook returns as soon as the write is a task, which says the round is recorded. When
    that task fails it is not, and the rounds after it would append after messages the store
    never got."""
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("delta")
        engine = await manager._build_engine(state, "run-1")
        state.engine = engine
        engine.history = [_msg("a")]
        persist_history(engine)
        await _settled(state)
        assert _marker(engine) is not None

        async def fail(*_: object, **__: object) -> None:
            raise RuntimeError("the disk said no")

        engine.history = [*engine.history, _msg("b")]
        manager._persist_history = fail  # type: ignore[assignment]
        persist_history(engine)
        await _settled(state)
        assert _marker(engine) is None, "the core still believes a write that failed"
    finally:
        await manager.close()


@needs_delta
async def test_a_round_the_store_dropped_is_written_by_the_next_one(settings: Settings, db: Database) -> None:
    """Hand-overs outrunning the writes: the repair may not depend on one more round arriving late.

    The forget used to be a done-callback, which fires a loop iteration after the failure — by
    which time every hand-over made in between was queued as an append onto rows that were never
    written, and the round's messages were simply absent from the history the next load reads.
    """
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("delta")
        engine = await manager._build_engine(state, "run-1")
        state.engine = engine
        store = manager.sessions
        real_append = store.append_messages
        calls = {"n": 0}

        async def flaky(session_id: str, tenant_id: str, messages: list[Message]) -> int:
            calls["n"] += 1
            if calls["n"] == 20:
                raise RuntimeError("the store is down")
            return await real_append(session_id, tenant_id, messages)

        store.append_messages = flaky  # type: ignore[assignment]
        history: list[Message] = []
        for i in range(30):
            history = [*history, _msg(f"m{i}")]
            engine.history = history
            persist_history(engine)
            await asyncio.sleep(0)  # the next round hands over before the last one's write has run
        await _settled(state)
        assert calls["n"] >= 20, "the failing write never fired"

        stored = [b.text for m in await manager.sessions.list_messages(state.session.id, TENANT, limit=500) for b in m.content_blocks]
        assert stored == [f"m{i}" for i in range(30)], f"the history the next load reads has a hole: {stored}"
    finally:
        await manager.close()


@needs_delta
async def test_the_last_round_of_a_run_is_repaired_with_nothing_following_it(settings: Settings, db: Database) -> None:
    """The failing write is the last hand-over of the run: no round follows it to carry the repair.

    Recovery used to be entirely in the hands of the next hand-over. When the run goes idle after
    the failure there is no next hand-over, and the round stayed missing until something else
    happened to write the history — which, for a session nobody comes back to, is never.
    """
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("delta")
        engine = await manager._build_engine(state, "run-1")
        state.engine = engine
        engine.history = [_msg("a")]
        persist_history(engine)
        await _settled(state)

        store = manager.sessions
        real_append = store.append_messages
        failed = {"n": 0}

        async def once(session_id: str, tenant_id: str, messages: list[Message]) -> int:
            failed["n"] += 1
            if failed["n"] == 1:
                raise RuntimeError("the store is down")
            return await real_append(session_id, tenant_id, messages)

        store.append_messages = once  # type: ignore[assignment]
        engine.history = [*engine.history, _msg("b")]
        persist_history(engine)
        await _settled(state)
        assert failed["n"] == 1, "the failing write never fired"

        stored = [b.text for m in await manager.sessions.list_messages(state.session.id, TENANT, limit=500) for b in m.content_blocks]
        assert stored == ["a", "b"], f"nothing followed the failure, so nothing repaired it: {stored}"
        assert _marker(engine) is None, "the core still believes a write that failed"
    finally:
        await manager.close()
