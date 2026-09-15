"""An installation with no bot token: it starts, it talks to the operator, it makes sessions.

Telegram used to be a precondition — ``start()`` refused without a token, and a notice with no chat
to go to was simply lost. These tests pin the other shape: the front is absent, the API and the
extensions are there, and what would have been a chat line is an inbox entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.app import Application
from daedalus.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        state_dir=tmp_path / "state",
        workspaces_dir=tmp_path / "workspaces",
        bot_repo_dir=Path(__file__).resolve().parents[2],
        telegram_bot_token="",
        owner_user_id=0,
        api_port=0,  # an ephemeral port: the test never talks to the server it starts
    )


@pytest.fixture
async def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    application = Application(_settings(tmp_path))
    await application.start()
    yield application
    await application.shutdown()


async def test_the_application_starts_and_installs_its_extensions_without_a_front(app: Application) -> None:
    assert app.front is None
    assert app.manager is not None
    for name in ("inbox", "scheduler", "loops", "board", "api_token"):
        assert name in app.extensions, name


async def test_a_token_without_an_owner_id_is_still_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.telegram_bot_token = "123:abc"
    application = Application(settings)
    with pytest.raises(RuntimeError, match="OWNER_USER_ID"):
        await application.start()
    await application.shutdown()


async def test_notify_lands_in_the_inbox_when_there_is_no_chat(app: Application) -> None:
    await app.notify("💸 Daily budget exceeded (openrouter).\nNew runs are refused until tomorrow.", kind="budget", severity="warning")
    entries = await app.extensions["inbox"].list(limit=10)  # type: ignore[attr-defined]
    entry = next(e for e in entries if e["kind"] == "budget")
    assert entry["title"].startswith("💸 Daily budget exceeded")
    assert entry["body"] == "New runs are refused until tomorrow."
    assert entry["severity"] == "warning"


async def test_create_session_makes_a_plain_session(app: Application) -> None:
    state = await app.create_session("a title", metadata={"unattended": True})
    assert state.session.title == "a title"
    assert state.session.metadata["unattended"] is True
    assert (await app.db.fetchall("SELECT * FROM topics")) == []  # no chat, so no topic to bind
    assert state.workspace.is_dir()


async def test_create_session_honours_a_workspace_the_caller_names(app: Application, tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    state = await app.create_session("in a named directory", metadata={"workspace": str(shared)}, workspace=shared)
    assert state.workspace == shared
