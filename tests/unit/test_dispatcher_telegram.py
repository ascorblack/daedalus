"""The main orchestrator in Telegram: General in a forum, its header in the private chat, reply-to first,
and the questions of its chat posted once with buttons."""

from __future__ import annotations

import asyncio
from typing import Any

from aiogram.types import Message

from daedalus.config import Settings
from daedalus.extensions.dispatcher import Dispatcher
from daedalus.extensions.dispatcher_telegram import DispatcherTelegram
from daedalus.extensions.dispatches import Dispatches
from daedalus.transport.telegram.front import MAIN_HEADER, TelegramFront
from tests.unit.test_front import OWNER, RecordingBot, front  # noqa: F401 — the fixture is reused here
from tests.unit.test_staff_runtime import project_with, team_for

FORUM = -100


def _message(text: str, *, chat_id: int = OWNER, chat_type: str = "private", thread: int | None = None, reply_to: int | None = None, reply_from_bot: bool = True, reply_text: str = "") -> Message:
    data: dict[str, Any] = {
        "message_id": 500,
        "date": 2_000_000_000,
        "chat": {"id": chat_id, "type": chat_type, "is_forum": chat_type == "supergroup"},
        "from": {"id": OWNER, "is_bot": False, "first_name": "A"},
        "text": text,
    }
    if thread is not None:
        data["message_thread_id"] = thread
        data["is_topic_message"] = True
    if reply_to is not None:
        data["reply_to_message"] = {
            "message_id": reply_to,
            "date": 2_000_000_000,
            "chat": data["chat"],
            "from": {"id": 1, "is_bot": reply_from_bot, "first_name": "Bot"},
            "text": reply_text or "earlier",
        }
    return Message.model_validate(data)


def _said(message: Message, replies: list[str]) -> Message:
    async def answer(text: str, **_: Any) -> None:
        replies.append(str(text))

    object.__setattr__(message, "answer", answer)
    object.__setattr__(message, "reply", answer)
    return message


async def _main(front: TelegramFront, settings: Settings) -> tuple[Dispatcher, Dispatches, Any]:
    team = await team_for(settings, front.manager)
    app = team.app
    app.front = front
    dispatches = Dispatches(app)
    app.extensions["dispatches"] = dispatches
    front.manager.asks.default_dispatch = dispatches.default_dispatch
    main = Dispatcher(app)
    app.extensions["dispatcher"] = main
    return main, dispatches, team


def _forum(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = FORUM
    front.config.telegram.mode = "topics"


async def test_in_a_forum_general_is_the_main_orchestrators_home(front: TelegramFront, settings: Settings) -> None:  # noqa: F811
    _forum(front)
    main, _dispatches, _team = await _main(front, settings)
    sid = await main.ensure()
    outbox = await front.outbox_for_session(sid)
    assert outbox is not None and outbox.chat_id == FORUM and outbox.thread_id is None and outbox.attributed("hi") == "hi"
    assert await front.ensure_topic(sid, "Main") is None and front.bot.topics == []  # type: ignore[attr-defined]
    assert await front.adopt_sessions_into_topics() == 0 or "Main" not in front.bot.topics  # type: ignore[attr-defined]
    await front.on_message(_message("in Bakery, add a gluten-free menu", chat_id=FORUM, chat_type="supergroup"))
    await _flushed(front)
    assert [s for s, _t, _a in front.submitted] == [sid]  # type: ignore[attr-defined]


async def test_a_reply_in_general_goes_to_the_session_that_posted_there(front: TelegramFront, settings: Settings) -> None:  # noqa: F811
    _forum(front)
    main, _dispatches, _team = await _main(front, settings)
    await main.ensure()
    # A session that fell back to General because Telegram would not open its topic speaks there under its name.
    other = await front.manager.create_session("Reports")
    general = front._general_outbox(header=lambda: "▸ Reports")
    assert general is not None
    general.on_sent = front.remember_post(other.session.id)
    posted = await general.send_text("the weekly report", markdown=False)
    await front.on_message(_message("thanks, and next week too", chat_id=FORUM, chat_type="supergroup", reply_to=posted))
    await _flushed(front)
    assert [s for s, _t, _a in front.submitted] == [other.session.id], "reply-to wins over General's owner"


async def test_in_the_private_chat_it_speaks_under_its_header_and_a_reply_reaches_it(front: TelegramFront, settings: Settings) -> None:  # noqa: F811
    main, _dispatches, _team = await _main(front, settings)
    await front.on_message(_message("hello"))
    await _flushed(front)
    current = await front.current_session_id()
    sid = await main.ensure()
    assert sid != current
    outbox = await front.outbox_for_session(sid)
    assert outbox is not None and outbox.chat_id == OWNER and outbox.attributed("Bakery is done").startswith(MAIN_HEADER)
    posted = await outbox.send_text("Bakery is done", markdown=False)
    front.submitted.clear()  # type: ignore[attr-defined]
    await front.on_message(_message("great, now the vegan menu", reply_to=posted))
    await _flushed(front)
    assert [s for s, _t, _a in front.submitted] == [sid]  # type: ignore[attr-defined]
    front.submitted.clear()  # type: ignore[attr-defined]
    await front.on_message(_message("and back to you"))
    await _flushed(front)
    assert [s for s, _t, _a in front.submitted] == [current], "plain text stays with the current session"
    # After a restart the remembered posts are gone; the header still says whose a post is.
    front._posts.clear()
    front.submitted.clear()  # type: ignore[attr-defined]
    await front.on_message(_message("one more", reply_to=posted, reply_text=f"{MAIN_HEADER}\n\nBakery is done"))
    await _flushed(front)
    assert [s for s, _t, _a in front.submitted] == [sid]  # type: ignore[attr-defined]
    replies: list[str] = []
    await front.cmd_main(_said(_message("/main"), replies))
    assert await front.current_session_id() == sid and MAIN_HEADER in replies[-1]


async def test_a_question_under_a_dispatch_is_posted_once_with_buttons_and_closed_when_answered(front: TelegramFront, settings: Settings, tmp_path: Any) -> None:  # noqa: F811
    _forum(front)
    main, dispatches, team = await _main(front, settings)
    await main.ensure()
    (tmp_path / "bakery").mkdir()
    project = await project_with(front.manager, tmp_path / "bakery", orchestrator=False)
    dispatch = await dispatches.create(project, text="Choose a database")
    ask = await front.manager.asks.open(project.id, origin="orchestrator", kind="question", text="Postgres or SQLite?", routed_to="operator", detail={"options": ["Postgres", "SQLite"], "event_ref": "orchestrator:x:y"}, dispatch_id=dispatch.id)
    unlinked = await front.manager.asks.open(project.id, origin="orchestrator", kind="question", text="Logo?", routed_to="operator", detail={"options": []})
    window = DispatcherTelegram(team.app, front, main)
    assert await window.sync() == 1
    assert await window.sync() == 0, "posted once"
    bot: RecordingBot = front.bot  # type: ignore[assignment]
    [post] = [m for m in bot.sent if "Postgres or SQLite?" in m["text"]]
    assert post["chat_id"] == FORUM and f"[{ask.short_id}]" in post["text"] and "Bakery" in post["text"]
    labels = [b.text for row in post["reply_markup"].inline_keyboard for b in row]
    assert labels == ["Postgres", "SQLite", "✍️ Answer"]
    assert not any("Logo?" in m["text"] for m in bot.sent), f"{unlinked.short_id} is not shown under a dispatch"

    await team.answer(ask.id, selected=["SQLite"], by="operator", via="main")
    await window.sync()
    [edit] = bot.edited
    assert edit["message_id"] == post["id"] and "answered in the main chat: SQLite" in edit["text"]


async def test_a_request_that_acts_on_the_host_is_posted_without_buttons(front: TelegramFront, settings: Settings, tmp_path: Any) -> None:  # noqa: F811
    _forum(front)
    main, dispatches, team = await _main(front, settings)
    await main.ensure()
    (tmp_path / "bakery").mkdir()
    project = await project_with(front.manager, tmp_path / "bakery", orchestrator=False)
    dispatch = await dispatches.create(project, text="Set up")
    ask = await front.manager.asks.open(project.id, origin="orchestrator", kind="folder", text="Add /home/someone/shop?", routed_to="operator", detail={"env": "host", "path": "/home/someone/shop", "options": ["Add", "Don't add"]}, dispatch_id=dispatch.id)
    window = DispatcherTelegram(team.app, front, main)
    await window.sync()
    bot: RecordingBot = front.bot  # type: ignore[assignment]
    [post] = [m for m in bot.sent if ask.short_id in m["text"]]
    assert "answer it in the app" in post["text"] and "reply_markup" not in post


async def _flushed(front: TelegramFront) -> None:
    """The inbound buffer waits a moment before it submits; let it."""
    await asyncio.sleep(0.05)
