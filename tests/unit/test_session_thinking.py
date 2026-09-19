"""Per-session thinking effort: the composer stores it on the session, not on the preset."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config

H = {"X-Daedalus-Token": "tok"}


def _client(settings: Settings, db: Database, manager: SessionManager) -> httpx.AsyncClient:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test")  # type: ignore[arg-type]


async def test_session_thinking_starts_as_the_preset_and_an_override_stays_on_the_session(
    settings: Settings, db: Database
) -> None:
    config = model_config()
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("think")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        detail = (await client.get(f"/api/sessions/{sid}", headers=H)).json()
        assert detail["thinking"] is True
        assert detail["reasoning_effort"] == "medium"

        r = await client.post(f"/api/sessions/{sid}/model", json={"thinking": True, "reasoning_effort": "xhigh"}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["reasoning_effort"] == "xhigh"
        assert r.json()["thinking"] is True

        detail = (await client.get(f"/api/sessions/{sid}", headers=H)).json()
        assert detail["thinking"] is True and detail["reasoning_effort"] == "xhigh"

        bad = await client.post(f"/api/sessions/{sid}/model", json={"reasoning_effort": "nope"}, headers=H)
        assert bad.status_code == 400

        preset_id = next(iter(config.presets))
        picked = await client.post(f"/api/sessions/{sid}/model", json={"preset": preset_id}, headers=H)
        assert picked.status_code == 200, picked.text
        detail = (await client.get(f"/api/sessions/{sid}", headers=H)).json()
        assert detail["reasoning_effort"] == "xhigh", "a model pick must not clear the session's effort"

        cleared = await client.post(f"/api/sessions/{sid}/model", json={"clear": True}, headers=H)
        assert cleared.status_code == 200, cleared.text
        detail = (await client.get(f"/api/sessions/{sid}", headers=H)).json()
        assert detail["reasoning_effort"] == "medium" and detail["thinking"] is True
    await manager.close()
