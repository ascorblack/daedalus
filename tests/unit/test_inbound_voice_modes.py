"""Inbound events (payload flattening, signatures, dedupe, standing intents), voice transcription, session modes."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import AsrConfig, RuntimeConfig, Settings
from daedalus.extensions.inbound import Inbound, flatten_payload, verify_signature
from daedalus.host.engine_factory import runtime_constants
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.transport.telegram.voice import TranscriptionError, transcribe


def test_flatten_payload_is_bounded_and_readable() -> None:
    text = flatten_payload({"action": "opened", "pull_request": {"title": "Fix", "user": {"login": "x"}, "labels": [{"name": "bug"}]}})
    assert "action: opened" in text and "pull_request.user.login: x" in text and "pull_request.labels[0].name: bug" in text
    huge = flatten_payload({"k": ["v" * 100] * 500})
    assert len(huge) <= 2100 and huge.endswith("…")


def test_signature_schemes() -> None:
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature("github", "s3cret", body, {"x-hub-signature-256": sig})
    assert not verify_signature("github", "s3cret", body, {"x-hub-signature-256": "sha256=bad"})
    assert verify_signature("bearer", "tok", body, {"authorization": "Bearer tok"})
    assert not verify_signature("bearer", "", body, {"authorization": "Bearer "})


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    submitted: list[tuple[str, str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator") -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, origin))
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={}, submitted=submitted)
    yield app
    await manager.close()


async def test_webhook_deliveries_dedupe(app: Any) -> None:
    inbound = Inbound(app)
    assert await inbound.record_delivery("github", "d1") is True
    assert await inbound.record_delivery("github", "d1") is False
    assert await inbound.record_delivery("ci", "d1") is True


async def test_inbound_delivery_creates_a_standing_session_and_fires_intents(app: Any) -> None:
    inbound = Inbound(app)
    created = await inbound.create_intent(pattern=r"build\s+failed", action="investigate the failure", session_id=None, cooldown_minutes=60, max_fires=2, expires_in_hours=None, created_by=None)
    with pytest.raises(ValueError):
        await inbound.create_intent(pattern="(", action="x", session_id=None, cooldown_minutes=1, max_fires=1, expires_in_hours=None, created_by=None)
    result = await inbound.deliver(source="ci", text="Build FAILED on main", session_ref=None, default_title="[inbound]")
    assert result["run_id"] == "run-x"
    titles = [s["title"] for s in await app.manager.list_sessions()]
    assert "[inbound]" in titles
    # the event went to the standing session; the intent fired into its own (created_by is None → a fresh task session is needed)
    assert app.submitted[0][2] == "inbound:ci" and "[inbound event from ci]" in app.submitted[0][1]
    row = await app.db.fetchone("SELECT fired_count, last_fired_at FROM intents WHERE id = ?", (created["id"],))
    # without a scheduler extension the intent cannot start a task session; it is reported, not counted
    assert row["fired_count"] == 0
    # cooldown and budget: give the intent a session, fire twice, then it is exhausted
    app.extensions["scheduler"] = None
    state = await app.manager.create_session("watcher")
    await app.db.execute("UPDATE intents SET session_id = ? WHERE id = ?", (state.session.id, created["id"]))
    assert await inbound.match_intents("ci", "build failed again") == [created["id"]]
    assert await inbound.match_intents("ci", "build failed again") == []  # cooldown
    await app.db.execute("UPDATE intents SET last_fired_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (created["id"],))
    assert await inbound.match_intents("ci", "build failed once more") == [created["id"]]
    await app.db.execute("UPDATE intents SET last_fired_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (created["id"],))
    assert await inbound.match_intents("ci", "build failed yet again") == []  # budget of 2 used up
    row = await app.db.fetchone("SELECT enabled FROM intents WHERE id = ?", (created["id"],))
    assert row["enabled"] == 0
    assert [s for s in app.submitted if s[2] == "intent"]


async def test_transcribe_uses_an_openai_compatible_endpoint(tmp_path: Path) -> None:
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS fake")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"text": "  hello there  "})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    text = await transcribe(audio, AsrConfig(url="http://asr.local/v1", api_key="k1", model="whisper-1"), client=client)
    assert text == "hello there" and seen["url"] == "http://asr.local/v1/audio/transcriptions" and seen["auth"] == "Bearer k1"
    failing = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(TranscriptionError):
        await transcribe(audio, AsrConfig(url="http://asr.local/v1"), client=failing)
    with pytest.raises(TranscriptionError):
        await transcribe(audio, AsrConfig())


async def test_modes_override_limits_and_prompt(settings: Settings, db: Database) -> None:
    config = RuntimeConfig()
    rc = runtime_constants(config, context_window=100_000, max_output_tokens=8_000, thinking=False, mode=config.modes["quick"])
    assert rc.max_iterations == 25
    rc = runtime_constants(config, context_window=100_000, max_output_tokens=8_000, thinking=False, mode=None)
    assert rc.max_iterations == config.limits.max_iterations
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("m")
    assert await manager.set_mode(state.session.id, "deep") == "deep"
    assert manager.mode_for(state) is config.modes["deep"]
    with pytest.raises(ValueError):
        await manager.set_mode(state.session.id, "nonsense")
    assert await manager.set_mode(state.session.id, None) == "" and manager.mode_for(state) is None
    refreshed = await manager.sessions.get(state.session.id, "daedalus")
    assert "mode" not in refreshed.metadata
    await manager.close()
    assert json.dumps(config.modes["careful"].model_dump())
