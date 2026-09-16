"""The voice concierge: what it may call, what it does with a task, and when it is told things."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import VOICE_ONLY_TOOLS, VOICE_TOOLS, RuntimeConfig, Settings, TtsConfig, VoiceConfig
from daedalus.extensions.voice import Voice, speakable, split_sentences, tts_configured
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    application = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})

    async def create_session(title: str, *, metadata: dict[str, Any] | None = None, workspace: Any = None) -> Any:
        return await manager.create_session(title, metadata=metadata, workspace=workspace)

    application.create_session = create_session
    yield application
    await manager.close()


def _capture(manager: SessionManager) -> list[tuple[str, str, str]]:
    submitted: list[tuple[str, str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator") -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, origin))
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    return submitted


# -- the chunker ---------------------------------------------------------------------------


def test_sentences_are_cut_where_speech_can_start() -> None:
    assert split_sentences("Hello there. I am setting that up") == (["Hello there."], "I am setting that up")
    assert split_sentences("no punctuation yet") == ([], "no punctuation yet")
    # A sentence too short to speak on its own waits for the next one instead of being read alone.
    assert split_sentences("Yes. ") == ([], "Yes. ")
    said, rest = split_sentences("Да. Одну минуту, я это запускаю. Дальше")
    assert said == ["Да. Одну минуту, я это запускаю."] and rest == "Дальше"
    # Nothing is dropped: every word is either spoken or still in the tail.
    for text in ("One. Two three four five.", "A? B! C is long enough…", "para one\n\npara two is long"):
        spoken, tail = split_sentences(text)
        assert "".join((" ".join(spoken) + " " + tail).split()) == "".join(text.split())


def test_spoken_text_drops_what_cannot_be_read_aloud() -> None:
    assert speakable("**Done.** ```py\nx=1\n``` The rest.\n\n⟦ a | b | c ⟧") == "Done. The rest."


# -- the tool surface ----------------------------------------------------------------------


async def test_the_concierge_gets_its_four_tools_and_nothing_else(app: Any) -> None:
    manager: SessionManager = app.manager
    known = {t.name for t in manager.tools.list_all()}
    voice = await manager.create_session("Voice", metadata={"voice": True})
    blocked = manager.blocked_tools_for(voice)
    assert blocked == known - set(VOICE_TOOLS)
    for name in ("Exec", "Read", "Write", "SpawnAgent", "SubAgent", "WebFetch"):
        assert name in blocked
    for name in VOICE_TOOLS:
        assert name not in blocked
    # And the delegation tools are the concierge's alone: a working session cannot reach them.
    worker = await manager.create_session("work")
    assert manager.blocked_tools_for(worker) == set(VOICE_ONLY_TOOLS)


async def test_a_mode_cannot_give_a_voice_session_a_shell(app: Any) -> None:
    manager: SessionManager = app.manager
    voice = await manager.create_session("Voice", metadata={"voice": True})
    manager.config.modes["loose"] = manager.config.modes["quick"].model_copy(update={"tools_only": ["Exec", "Read"]})
    await manager.set_mode(voice.session.id, "loose")
    assert "Exec" in manager.blocked_tools_for(voice)


# -- delegation ----------------------------------------------------------------------------


async def test_delegate_starts_an_agent_and_steers_the_one_it_started(app: Any) -> None:
    manager: SessionManager = app.manager
    voice = Voice(app)
    submitted = _capture(manager)
    result = await voice.delegate(title="Bakery site", task="rebuild the menu page")
    child = await manager.get_state(result["session_id"])
    assert child is not None and child.metadata["voice_parent"] == await voice.session_id()
    assert result["steered"] is False and submitted[-1][0] == child.session.id and submitted[-1][2] == "voice"

    steered = await voice.delegate(title="", task="use the seasonal photos", session_id=child.session.id)
    assert steered["steered"] is True and submitted[-1] == (child.session.id, "use the seasonal photos", "voice")

    # Only the agents it started: a session of the operator's own is not the concierge's to steer.
    other = await manager.create_session("not mine")
    with pytest.raises(KeyError):
        await voice.delegate(title="", task="stop", session_id=other.session.id)
    with pytest.raises(ValueError):
        await voice.delegate(title="x", task="   ")


async def test_the_agent_list_shows_only_the_concierges_agents(app: Any) -> None:
    manager: SessionManager = app.manager
    voice = Voice(app)
    _capture(manager)
    result = await voice.delegate(title="Digest", task="write the weekly digest")
    await manager.create_session("somebody else's")
    await manager.sessions.append_transcript(result["session_id"], [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="the digest is out")])])
    rows = await voice.agents()
    assert [r["session_id"] for r in rows] == [result["session_id"]]
    assert rows[0]["title"] == "Digest" and rows[0]["answer"] == "the digest is out"
    assert (await voice.result(result["session_id"]))["answer"] == "the digest is out"


# -- reports -------------------------------------------------------------------------------


async def test_a_finished_agent_is_reported_at_once_only_while_a_page_is_listening(app: Any) -> None:
    manager: SessionManager = app.manager
    voice = Voice(app)
    submitted = _capture(manager)
    result = await voice.delegate(title="Digest", task="write the weekly digest")
    await manager.sessions.append_transcript(result["session_id"], [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="the digest is out\n\n⟦ h | status: completed ⟧")])])
    before = len(submitted)

    # Nobody is listening: the report is held rather than spoken to an empty room.
    await voice.on_run_finished(result["session_id"], "run-x", "completed")
    assert len(submitted) == before and voice.held

    async with voice.listen():
        pass
    assert len(submitted) == before + 1
    session_id, text, origin = submitted[-1]
    assert session_id == await voice.session_id() and origin == "agent"
    assert 'agent "Digest" finished' in text and "the digest is out" in text and "⟦" not in text
    assert not voice.held

    # With a page connected the next one goes straight through, as one turn each.
    async with voice.listen():
        await voice.on_run_finished(result["session_id"], "run-y", "completed")
        assert len(submitted) == before + 2


async def test_held_reports_are_delivered_as_one_turn(app: Any) -> None:
    manager: SessionManager = app.manager
    voice = Voice(app)
    submitted = _capture(manager)
    await voice.session_id()
    await voice.report("[agent \"a\" finished: one]")
    await voice.report("[agent \"b\" finished: two]")
    async with voice.listen():
        pass
    assert len(submitted) == 1 and "one" in submitted[0][1] and "two" in submitted[0][1]


# -- configuration -------------------------------------------------------------------------


def test_voice_defaults_name_a_fast_model_and_no_speech_endpoint() -> None:
    config = RuntimeConfig()
    assert config.voice.enabled is True
    preset = config.presets[config.voice.preset]
    # The concierge must answer while the operator is still listening: no thinking, small output.
    assert preset.thinking is False and preset.max_output_tokens <= 8_000
    assert tts_configured(config.voice.tts) is False  # the browser speaks until an endpoint is configured
    assert VoiceConfig().tts.format == "mp3"
    assert tts_configured(TtsConfig(url="http://speech.invalid/v1")) is True
    assert tts_configured(TtsConfig(provider="openrouter")) is True
