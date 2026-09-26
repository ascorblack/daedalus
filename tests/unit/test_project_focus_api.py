"""What the app's project focus mode reads from the host: the agents list's one entry for an
orchestrated project, what a session is to its project, and what was sent to a staff member."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

HEADERS = {"X-Daedalus-Token": "tok"}


def _client(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> httpx.AsyncClient:
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")  # type: ignore[arg-type]


async def test_an_orchestrated_project_says_who_works_and_what_waits_for_the_operator(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with _client(settings, config, db, manager) as client:
            bakery = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery"})).json()["id"]
            plain = (await client.post("/api/projects", headers=HEADERS, json={"name": "Plain"})).json()["id"]
            ada = (await client.post(f"/api/projects/{bakery}/staff", headers=HEADERS, json={"name": "Ada"})).json()
            await client.post(f"/api/projects/{bakery}/staff", headers=HEADERS, json={"name": "Bo"})
            gone = (await client.post(f"/api/projects/{bakery}/staff", headers=HEADERS, json={"name": "Cy"})).json()
            assert (await client.delete(f"/api/staff/{gone['id']}", headers=HEADERS)).status_code == 200
            await client.post(f"/api/projects/{plain}/staff", headers=HEADERS, json={"name": "Di"})

            orchestrator = await manager.create_session("Orchestrator · Bakery", metadata={"orchestrator_of": bakery}, project_id=bakery)
            await manager.projects.update_orchestrator(bakery, enabled=True)
            assert await manager.projects.set_orchestrator(bakery, expect="", value=orchestrator.session.id)

            live = await manager.staff.claim_session(ada["id"], kind="daedalus")
            await manager.staff.set_status(live.id, "working")
            # Only requests routed to the operator count, and only open ones.
            await manager.asks.open(bakery, origin="orchestrator", kind="question", text="Postgres or SQLite?", routed_to="operator")
            answered = await manager.asks.open(bakery, origin="orchestrator", kind="question", text="Which font?", routed_to="operator")
            await manager.asks.resolve(answered.id, "operator", {"text": "Inter"})
            await manager.asks.open(bakery, origin="staff", kind="question", text="Tabs or spaces?", routed_to="orchestrator", staff_id=ada["id"])

            listing = (await client.get("/api/sessions", headers=HEADERS)).json()
            folders = {p["id"]: p for p in listing["projects"]}
            assert folders[bakery]["orchestrator"] == {"enabled": True, "session_id": orchestrator.session.id, "staff": 2, "working": 1, "needs_you": 1}
            # A project without an orchestrator is listed as it always was, whatever team it has.
            assert folders[plain]["orchestrator"] is None

            # The orchestrator's chat says whose it is; an ordinary session says nothing of the kind.
            detail = (await client.get(f"/api/sessions/{orchestrator.session.id}", headers=HEADERS)).json()
            assert (detail["orchestrator_of"], detail["staff"]) == (bakery, None)
            other = await manager.create_session("Notes", project_id=bakery)
            plain_detail = (await client.get(f"/api/sessions/{other.session.id}", headers=HEADERS)).json()
            assert (plain_detail["orchestrator_of"], plain_detail["staff"]) == (None, None)
            staffed = await manager.create_session("Ada · Menu", metadata={"staff_id": ada["id"], "staff_session_id": live.id}, project_id=bakery)
            staff_detail = (await client.get(f"/api/sessions/{staffed.session.id}", headers=HEADERS)).json()
            assert staff_detail["staff"] == {"id": ada["id"], "session_id": live.id}
    finally:
        await manager.close()


async def test_what_was_sent_to_a_member_newest_first_with_its_delivery_state(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with _client(settings, config, db, manager) as client:
            pid = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery"})).json()["id"]
            ada = (await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Ada"})).json()
            first = await manager.staff.add_message(ada["id"], "Start with the menu", origin="orchestrator", mode="after_turn")
            await manager.staff.set_message_state(first.id, "submitted")
            await manager.staff.set_message_state(first.id, "acknowledged")
            second = await manager.staff.add_message(ada["id"], "Use the owner's sheet", origin="operator", mode="now")
            await manager.staff.set_message_state(second.id, "failed", "the session ended")

            answer = await client.get(f"/api/staff/{ada['id']}/messages?limit=5", headers=HEADERS)
            assert answer.status_code == 200
            rows: list[dict[str, Any]] = answer.json()
            assert [(m["text"], m["origin"], m["mode"], m["state"], m["error"]) for m in rows] == [
                ("Use the owner's sheet", "operator", "now", "failed", "the session ended"),
                ("Start with the menu", "orchestrator", "after_turn", "acknowledged", ""),
            ]
            assert (await client.get(f"/api/staff/{ada['id']}/messages", headers={})).status_code == 401
            assert (await client.get("/api/staff/st-nope/messages", headers=HEADERS)).status_code == 404
            assert (await client.get(f"/api/staff/{ada['id']}/messages?limit=0", headers=HEADERS)).status_code == 422
    finally:
        await manager.close()
