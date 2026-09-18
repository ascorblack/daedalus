"""The steer queue as the composer sees it: what is waiting, taking one back, and when it is too late."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.types import TextBlock

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import STEER_CARD_CHARS, STEER_CARD_LIMIT, SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config
from tests.unit.test_session_runner import ScriptedProvider

H = {"X-Daedalus-Token": "tok"}


async def _manager(settings: Settings, db: Database, provider: ScriptedProvider) -> SessionManager:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config: [(provider, "scripted-model")]  # type: ignore[method-assign]
    return manager


def _client(settings: Settings, db: Database, manager: SessionManager) -> httpx.AsyncClient:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test")  # type: ignore[arg-type]


def _watch(manager: SessionManager) -> list[dict[str, Any]]:
    """Every ``steer_changed`` the manager raises, in order."""
    seen: list[dict[str, Any]] = []

    async def sink(session_id: str, event: Any) -> None:
        if getattr(event.type, "value", "") == "steer_changed":
            seen.append(event.payload)

    manager.add_sink(sink)
    return seen


async def _await_run(manager: SessionManager) -> None:
    done = asyncio.Event()

    async def on_finished(session_id: str, run_id: str, status: str) -> None:
        done.set()

    manager.on_finished(on_finished)
    await asyncio.wait_for(done.wait(), timeout=30)


async def test_a_steer_is_listed_while_it_waits_and_can_be_taken_back(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("steered")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        assert (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json() == []
        await manager.submit(sid, "start")
        await asyncio.sleep(0.3)
        assert state.running

        posted = await client.post(f"/api/sessions/{sid}/messages", json={"text": "also look at the log", "steer": True}, headers=H)
        assert posted.status_code == 200
        queued = (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()
        assert len(queued) == 1 and queued[0]["text"] == "also look at the log"
        assert queued[0]["id"] and queued[0]["queued_at"]
        assert changes[-1]["reason"] == "queued" and changes[-1]["count"] == 1
        assert changes[-1]["queued"][0]["id"] == queued[0]["id"]

        dropped = await client.delete(f"/api/sessions/{sid}/steer/{queued[0]['id']}", headers=H)
        assert dropped.status_code == 200 and dropped.json() == {"deleted": True}
        assert (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json() == []
        assert changes[-1]["reason"] == "withdrawn" and changes[-1]["count"] == 0

        # Gone before the run read it: the model is never shown the withdrawn text.
        await _await_run(manager)
        texts = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
        assert not any("also look at the log" in t for t in texts)
    await manager.close()


async def test_a_steer_the_run_has_read_is_gone_from_the_queue_and_cannot_be_withdrawn(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 1"}}, {"text": "first"}, {"text": "second"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("steered")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await manager.submit(sid, "start")
        await asyncio.sleep(0.3)
        assert state.running
        await manager.submit(sid, "and also this", steer=True)
        item_id = (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()[0]["id"]

        await _await_run(manager)
        texts = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
        assert any("and also this" in t for t in texts)
        assert (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json() == []
        assert [c["reason"] for c in changes] == ["queued", "consumed"]

        late = await client.delete(f"/api/sessions/{sid}/steer/{item_id}", headers=H)
        assert late.status_code == 409 and "already reached" in late.json()["detail"]
    await manager.close()


async def test_the_queue_endpoints_answer_for_a_session_and_only_to_a_caller_with_the_token(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "idle"}])
    manager = await _manager(settings, db, provider)
    async with _client(settings, db, manager) as client:
        assert (await client.get("/api/sessions/nope/steer", headers=H)).status_code == 404
        assert (await client.delete("/api/sessions/nope/steer/q_1", headers=H)).status_code == 404
        state = await manager.create_session("quiet")
        assert (await client.delete(f"/api/sessions/{state.session.id}/steer/q_missing", headers=H)).status_code == 409
        assert (await client.get(f"/api/sessions/{state.session.id}/steer")).status_code in {401, 403}
    await manager.close()


@pytest.mark.parametrize("kind", ["steer"])
async def test_the_id_the_app_holds_is_the_id_the_store_wrote(settings: Settings, db: Database, kind: str) -> None:
    """The card's ``×`` quotes an id back, so it has to be the one the persist path keeps."""
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("ids")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.3)
    await manager.submit(sid, "one", steer=True)
    await manager.submit(sid, "two", steer=True)
    listed = await manager.queued_steers(sid)
    stored = [item["id"] for item in (await manager.live.load(sid))[kind]]
    assert [item["id"] for item in listed] == stored and len(stored) == 2
    assert await manager.drop_queued_steer(sid, stored[0])
    assert [item["id"] for item in await manager.queued_steers(sid)] == stored[1:]
    await manager.close()


async def test_a_steer_queued_after_the_round_read_the_queue_survives_the_round(settings: Settings, db: Database) -> None:
    """The round writes back what it is holding, and what arrived behind its back is still waiting."""
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("racing")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.3)
    engine = state.engine
    assert engine is not None

    # The round reads the queue — empty — and the operator's message lands after that read.
    await engine.reload_live_control(engine)
    await manager.submit(sid, "and while you are there", steer=True)
    assert list(getattr(engine, "_steer_queue", [])) == []

    await engine.persist_live_control(engine)
    waiting = await manager.queued_steers(sid)
    assert [item["text"] for item in waiting] == ["and while you are there"]
    assert [c["reason"] for c in changes] == ["queued"]

    # Now the round is handed it and places it: that, and only that, is consumed.
    await engine.reload_live_control(engine)
    assert [item["id"] for item in engine._steer_queue] == [waiting[0]["id"]]
    engine._steer_queue = []
    await engine.persist_live_control(engine)
    assert await manager.queued_steers(sid) == []
    assert [c["reason"] for c in changes] == ["queued", "consumed"]
    await manager.close()


async def test_a_withdrawn_steer_is_not_brought_back_by_a_reload_that_raced_it(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("withdrawn")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.3)
    engine = state.engine
    assert engine is not None
    await engine.reload_live_control(engine)
    await manager.submit(sid, "forget this", steer=True)
    item_id = (await manager.queued_steers(sid))[0]["id"]

    # The row as a reload that started before the withdrawal would be handed it.
    stale = await manager.live.load(sid)
    assert await manager.drop_queued_steer(sid, item_id)

    original = manager.live.load

    async def stale_once(session_id: str) -> Any:
        manager.live.load = original  # type: ignore[method-assign]
        return stale

    manager.live.load = stale_once  # type: ignore[method-assign]
    await engine.reload_live_control(engine)
    assert list(getattr(engine, "_steer_queue", [])) == []

    await engine.persist_live_control(engine)
    assert await manager.queued_steers(sid) == []
    await manager.close()


async def test_a_change_event_carries_cards_and_the_true_count(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "idle"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("chatty")
    sid = state.session.id
    for i in range(STEER_CARD_LIMIT + 5):
        await manager.live.enqueue(sid, "steer", {"id": f"q_{i}", "text": "x" * (STEER_CARD_CHARS + 50), "queued_at": None})

    cards = await manager.queued_steers(sid)
    assert len(cards) == STEER_CARD_LIMIT
    assert all(len(card["text"]) == STEER_CARD_CHARS and card["truncated"] for card in cards)

    await manager.steer_changed(sid, reason="queued")
    assert changes[-1]["count"] == STEER_CARD_LIMIT + 5 and len(changes[-1]["queued"]) == STEER_CARD_LIMIT
    assert state.session.id == sid
    await manager.close()
