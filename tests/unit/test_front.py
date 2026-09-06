"""Telegram front handlers driven with constructed updates and a recording bot."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, Message

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.transport.telegram.front import TelegramFront

OWNER = 1


class RecordingBot:
    """The subset of the aiogram Bot API the front uses."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.topics: list[str] = []
        self._id = 100

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        self._id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "id": self._id, **kwargs})
        return SimpleNamespace(message_id=self._id)

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int, **kwargs: Any) -> None:
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def create_forum_topic(self, chat_id: int, name: str) -> SimpleNamespace:
        self.topics.append(name)
        return SimpleNamespace(message_thread_id=len(self.topics) * 10)

    async def get_file(self, file_id: str) -> SimpleNamespace:
        return SimpleNamespace(file_path=f"documents/{file_id}")

    async def download(self, file: Any, destination: Path) -> None:
        Path(destination).write_bytes(b"payload")


def _message(text: str | None = None, *, chat_type: str = "private", chat_id: int = OWNER, thread: int | None = None, document: bool = False, user: int = OWNER) -> Message:
    data: dict[str, Any] = {
        "message_id": 1,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": chat_type, "is_forum": chat_type == "supergroup"},
        "from": {"id": user, "is_bot": False, "first_name": "A"},
    }
    if text is not None:
        data["text"] = text
    if thread is not None:
        data["message_thread_id"] = thread
        data["is_topic_message"] = True
    if document:
        data["document"] = {"file_id": "f1", "file_unique_id": "u1", "file_name": "data.csv", "mime_type": "text/csv"}
        data["caption"] = text
        data.pop("text", None)
    return Message.model_validate(data)


@pytest.fixture
async def front(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> TelegramFront:
    config = RuntimeConfig()
    config.telegram.inbound_merge_window_seconds = 0.01
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    submitted: list[tuple[str, str, list[str]]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer: bool = False) -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, [a.path.name for a in attachments]))
        return "run"

    answered: list[tuple[str, list[dict[str, Any]]]] = []

    async def fake_answer(session_id: str, answers: list[dict[str, Any]]) -> str:
        answered.append((session_id, answers))
        return "run"

    monkeypatch.setattr(manager, "submit", fake_submit)
    monkeypatch.setattr(manager, "answer", fake_answer)

    async def save(cfg: RuntimeConfig) -> None:
        return None

    f = TelegramFront(settings, config, manager, save_config=save)
    f.bot = RecordingBot()  # type: ignore[assignment]
    f.submitted = submitted  # type: ignore[attr-defined]
    f.answered = answered  # type: ignore[attr-defined]
    yield f  # type: ignore[misc]
    await manager.close()


async def test_private_message_creates_the_direct_session_and_submits(front: TelegramFront) -> None:
    await front.on_message(_message("hello agent"))
    await asyncio.sleep(0.05)
    assert front.submitted == [(front.submitted[0][0], "hello agent", [])]  # type: ignore[attr-defined]
    binding = await front.binding_for_topic(OWNER, 0)
    assert binding is not None and binding.title == "direct"


async def test_non_owner_is_ignored(front: TelegramFront) -> None:
    await front.on_message(_message("hi", user=999))
    await asyncio.sleep(0.05)
    assert front.submitted == []  # type: ignore[attr-defined]


async def test_fragments_and_files_merge_into_one_submission(front: TelegramFront) -> None:
    await front.on_message(_message("part one"))
    await front.on_message(_message("part two", document=True))
    await asyncio.sleep(0.05)
    assert len(front.submitted) == 1  # type: ignore[attr-defined]
    _, text, files = front.submitted[0]  # type: ignore[attr-defined]
    assert text == "part one\npart two" and files == ["data.csv"]


async def test_new_in_forum_creates_topic_and_binding(front: TelegramFront) -> None:
    front.config.telegram.forum_chat_id = -100
    await front.cmd_new(_message("/new research", chat_type="supergroup", chat_id=-100), CommandObject(prefix="/", command="new", args="research"))
    assert front.bot.topics == ["research"]  # type: ignore[attr-defined]
    binding = await front.binding_for_topic(-100, 10)
    assert binding is not None and binding.title == "research"
    await front.on_message(_message("go", chat_type="supergroup", chat_id=-100, thread=10))
    await asyncio.sleep(0.05)
    assert front.submitted[0][0] == binding.session_id  # type: ignore[attr-defined]


async def test_ask_user_keyboard_single_choice_resumes_run(front: TelegramFront) -> None:
    state = await front.manager.create_session("q")
    await front.bind_topic(OWNER, 0, state.session.id, "q")
    await front._ask(state.session.id, {"questions": [{"question": "Color?", "options": [{"label": "Red"}, {"label": "Blue"}], "multiSelect": False, "allow_custom": False}]})
    sent = front.bot.sent[-1]  # type: ignore[attr-defined]
    buttons = [b for row in sent["reply_markup"].inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["Red", "Blue"]
    query = CallbackQuery.model_validate(
        {
            "id": "cq",
            "from": {"id": OWNER, "is_bot": False, "first_name": "A"},
            "chat_instance": "x",
            "data": buttons[1].callback_data,
            "message": {"message_id": sent["id"], "date": 1, "chat": {"id": OWNER, "type": "private"}, "text": sent["text"]},
        }
    )

    async def answer(*args: Any, **kwargs: Any) -> None:
        return None

    query = query.model_copy(update={})
    object.__setattr__(query, "answer", answer)  # bound API call needs a bot; stub it
    object.__setattr__(query.message, "edit_text", answer)
    await front.on_callback(query)
    assert front.answered == [(state.session.id, [{"question": "Color?", "selected": ["Blue"], "custom": None}])]  # type: ignore[attr-defined]


def test_builtin_callback_prefixes_do_not_shadow_extension_hooks() -> None:
    """Extensions register callback prefixes (selfdev uses ``cp``); the front's own must stay distinct."""
    import re
    from pathlib import Path

    front_src = Path("daedalus/transport/telegram/front.py").read_text()
    builtin = set(re.findall(r'if data\[0\] == "(\w+)"', front_src))
    selfdev_src = Path("daedalus/extensions/selfdev.py").read_text()
    extension = set(re.findall(r'callback_hooks\["(\w+)"\]', selfdev_src))
    assert builtin.isdisjoint(extension), builtin & extension
