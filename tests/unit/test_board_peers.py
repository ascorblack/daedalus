"""The task board (dependencies, WIP limit, checklist gate, stale hand-back) and named peers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.board import Board
from daedalus.extensions.inbox import Inbox
from daedalus.extensions.peers import Peers
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    app.extensions["inbox"] = Inbox(app)  # type: ignore[arg-type]
    yield app
    await manager.close()


async def test_board_dependencies_wip_and_checklist(app: Any) -> None:
    board = Board(app)
    app.config.board.wip_limit = 1
    a = await board.add(title="design", checklist=["sketch", "review"])
    b = await board.add(title="build", depends_on=[a["id"]])
    assert a["status"] == "todo" and b["status"] == "blocked"
    with pytest.raises(ValueError):
        await board.add(title="x", depends_on=["nope"])
    await board.update(a["id"], status="doing", session_id="s1")
    with pytest.raises(ValueError):  # WIP limit
        await board.update(b["id"], status="doing")
    with pytest.raises(ValueError):  # dependency still open
        await board.update(b["id"], status="doing")
    with pytest.raises(ValueError):  # checklist incomplete
        await board.update(a["id"], status="done")
    await board.update(a["id"], check=[0, 1], note="all sketched")
    done = await board.update(a["id"], status="done")
    assert done["status"] == "done" and "all sketched" in done["notes"]
    assert (await board.get(b["id"]))["status"] == "todo"  # promoted
    text = board.render(await board.list(None, include_done=True))
    assert "✅" in text and "build" in text


async def test_board_done_applies_checklist_edits_before_the_gate(app: Any) -> None:
    """Finishing the last item and closing the task is one call, not two.

    The completeness gate used to test the checklist the call *arrived* to, so the one call a
    finishing agent naturally makes — check=[...] together with status='done' — was refused, with a
    message that named an operation the board does not have ("drop them").
    """
    board = Board(app)
    t = await board.add(title="ship it", checklist=["write", "test", "announce"])
    with pytest.raises(ValueError) as exc:
        await board.update(t["id"], status="done", check=[0, 1])
    message = str(exc.value)
    assert "2 (announce)" in message, message  # names what is still open
    assert "Nothing was stored" in message, message  # and says the call changed nothing at all
    refused = await board.get(t["id"])
    assert refused["status"] == "todo"  # a refused call changes nothing …
    assert [c["done"] for c in refused["checklist"]] == [False, False, False]  # … not even the checks it sent
    done = await board.update(t["id"], status="done", check=[0, 1, 2], note="published")
    assert done["status"] == "done"
    assert all(c["done"] for c in done["checklist"])
    assert "published" in done["notes"]
    # Uncheck still wins over check for the same index, as it did before the ordering changed.
    t2 = await board.add(title="reopen", checklist=["a", "b"])
    await board.update(t2["id"], check=[0, 1])
    again = await board.update(t2["id"], check=[1], uncheck=[1])
    assert [c["done"] for c in again["checklist"]] == [True, False]


async def test_board_done_gate_reads_the_resulting_checklist(app: Any) -> None:
    """Edge cases of the reordered gate, each one a way the ordering could have gone wrong."""
    board = Board(app)

    # A task with no checklist, and one with an empty checklist, can still be closed.
    assert (await board.update((await board.add(title="bare"))["id"], status="done"))["status"] == "done"
    empty = await board.add(title="empty", checklist=[])
    assert (await board.update(empty["id"], status="done"))["status"] == "done"

    # Indexes outside the list stay ignored, as they were before.
    ranges = await board.add(title="ranges", checklist=["a"])
    done = await board.update(ranges["id"], status="done", check=[0, 5, -3])
    assert [c["done"] for c in done["checklist"]] == [True]
    assert [c["done"] for c in (await board.update(ranges["id"], check=[7], uncheck=[7]))["checklist"]] == [True]

    # An index in both lists ends up unchecked, so such a call leaves an item open and cannot close
    # the task. The old gate tested the checklist the call arrived to, so it let this through and
    # stored a finished task with an unchecked item.
    both = await board.add(title="both", checklist=["a", "b", "c"])
    await board.update(both["id"], check=[0, 1, 2])
    with pytest.raises(ValueError):
        await board.update(both["id"], status="done", check=[1], uncheck=[1])
    assert (await board.get(both["id"]))["status"] != "done"  # the refused call changed nothing
    assert [c["done"] for c in (await board.get(both["id"]))["checklist"]] == [True, True, True]

    # A status other than 'done' is not gated at all.
    other = await board.add(title="review", checklist=["a", "b"])
    assert (await board.update(other["id"], status="review", note="half"))["status"] == "review"

    # A finished task cannot be reopened from under its own status: an uncheck that left an item open
    # while the task stayed done is the same inconsistent state the gate exists to prevent, so it is
    # refused until the task itself is reopened.
    finished = await board.add(title="finished", checklist=["a", "b"])
    await board.update(finished["id"], status="done", check=[0, 1])
    with pytest.raises(ValueError) as exc:
        await board.update(finished["id"], uncheck=[0])
    assert "Reopen the task first" in str(exc.value), str(exc.value)
    after = await board.get(finished["id"])
    assert after["status"] == "done" and [c["done"] for c in after["checklist"]] == [True, True]
    reopened = await board.update(finished["id"], status="doing", uncheck=[0])
    assert reopened["status"] == "doing" and [c["done"] for c in reopened["checklist"]] == [False, True]

    # A long checklist names the first five open items and counts the rest.
    long_task = await board.add(title="long", checklist=[f"item {i}" for i in range(400)])
    with pytest.raises(ValueError) as exc:
        await board.update(long_task["id"], status="done", check=list(range(393)))
    message = str(exc.value)
    assert "393 (item 393)" in message and "and 2 more" in message, message
    final = await board.update(long_task["id"], status="done", check=list(range(400)))
    assert final["status"] == "done" and all(c["done"] for c in final["checklist"])


async def test_board_hands_back_quiet_tasks(app: Any) -> None:
    board = Board(app)
    app.config.board.stale_hours = 1
    t = await board.add(title="lingering")
    await board.update(t["id"], status="doing", session_id="ghost")
    await app.db.execute("UPDATE board_tasks SET heartbeat_at = '2020-01-01T00:00:00+00:00', updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (t["id"],))
    assert await board.recover_stale() == [t["id"]]
    assert (await board.get(t["id"]))["status"] == "todo"
    entries = await app.extensions["inbox"].list()
    assert entries and entries[0]["kind"] == "board_stale"


async def test_peers_register_ask_and_depth(app: Any) -> None:
    peers = Peers(app)
    manager: SessionManager = app.manager
    asker = await manager.create_session("asker")
    target = await manager.create_session("reviewer")
    with pytest.raises(ValueError):
        await peers.register("Bad Name!", target.session.id)
    await peers.register("reviewer", target.session.id)
    assert await peers.registry() == {"reviewer": target.session.id}
    submitted: list[tuple[str, str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator") -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, origin))
        return "run-p"

    manager.submit = fake_submit  # type: ignore[method-assign]
    result = await peers.ask(from_session=asker.session.id, name="reviewer", prompt="is this ok?", wait=False, timeout_minutes=None)
    assert result["run_id"] == "run-p" and submitted[0][0] == target.session.id and submitted[0][2].startswith("peer:")
    assert target.metadata["peer_depth"] == 1
    with pytest.raises(ValueError):
        await peers.ask(from_session=target.session.id, name="reviewer", prompt="self", wait=False, timeout_minutes=None)
    app.config.peers.max_depth = 1
    with pytest.raises(ValueError):  # target is at depth 1 already; asking onward would be depth 2
        await peers.ask(from_session=target.session.id, name="reviewer2", prompt="x", wait=False, timeout_minutes=None)
    assert await peers.forget("reviewer") and await peers.registry() == {}


async def test_board_releases_the_claim_and_reblocks_on_reopen(app: Any) -> None:
    board = Board(app)
    a = await board.add(title="first")
    b = await board.add(title="second", depends_on=[a["id"]])
    await board.update(a["id"], status="doing", session_id="s1", run_id="r1")
    moved = await board.update(a["id"], status="review")
    assert moved["session_id"] is None and moved["run_id"] is None
    await board.update(a["id"], status="done")
    assert (await board.get(b["id"]))["status"] == "todo"
    await board.update(a["id"], status="todo")
    assert (await board.get(b["id"]))["status"] == "blocked"
    await board.delete(a["id"])
    assert (await board.get(b["id"]))["depends_on"] == [] and (await board.get(b["id"]))["status"] == "todo"


async def test_peer_answer_only_counts_what_came_after_the_question(app: Any) -> None:
    from protocore.contracts.types import Message, MessageRole, TextBlock

    peers = Peers(app)
    manager: SessionManager = app.manager
    target = await manager.create_session("reviewer")
    old = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="old reply")])
    await manager.sessions.append_transcript(target.session.id, [old])
    watermark = max(int(m.metadata["daedalus.seq"]) for m in await manager.sessions.list_transcript(target.session.id))
    assert await peers.answer_after(target.session.id, watermark) is None
    await manager.sessions.append_transcript(target.session.id, [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="fresh reply\n\n⟦ h | status: completed; next: none | anchors: x ⟧")])])
    assert await peers.answer_after(target.session.id, watermark) == "fresh reply"
    target.metadata["peer_depth"] = 2
    await peers.on_run_finished(target.session.id, "r", "completed")
    assert "peer_depth" not in target.metadata
