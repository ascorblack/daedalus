"""An orchestrator's request for a folder, from the question to what the operator and the orchestrator
are told afterwards: a folder another project already has, an approval that fails, and approval by
the request's first option from every window it is answered in."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec
from tests.unit.test_orchestrator import events, events_messages, rig


async def test_a_folder_of_another_project_is_asked_for_with_that_said_and_added_on_approval(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        labs = tmp_path / "labs"
        labs.mkdir()
        other = await r.manager.projects.create("Labs", [FolderSpec(str(labs), env="host")])
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id

        await r.call(sid, "folders", op="add", path=str(labs), env="host", label="protocore")
        [ask] = await r.manager.asks.open_for(r.project.id, routed_to="operator")
        assert ask.kind == "folder" and ask.text.endswith("It is also in the project Labs.")
        assert ask.detail["also_in"] == ["Labs"]

        result = await r.team.answer(ask.short_id, selected=["Add"], by="operator", via="notification")
        assert (result["delivered"], result["error"]) == (True, "")
        assert str(labs) in [str(f.path) for f in (await r.refreshed()).folders]
        still = await r.manager.projects.get(other.id)
        assert still is not None and [str(f.path) for f in still.folders] == [str(labs)], "the other project keeps its folder"
        assert (await r.manager.asks.get(ask.id)).resolution["outcome"] == "added"  # type: ignore[union-attr]

        # Once in the project, asking for it again is refused at once, before the operator is troubled.
        with pytest.raises(Refused, match="already has the folder"):
            await r.call(sid, "folders", op="add", path=str(labs), env="host")
        assert await r.manager.asks.open_for(r.project.id, routed_to="operator") == []
        # A folder nesting in another project's is still refused, and still before anyone is asked.
        (labs / "inner").mkdir()
        with pytest.raises(Refused, match="inside the project"):
            await r.call(sid, "folders", op="add", path=str(labs / "inner"), env="host")
    finally:
        await r.manager.close()


async def test_an_approval_that_fails_is_told_to_the_operator_and_to_the_orchestrator_with_the_reason(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        shop = tmp_path / "shop"
        shop.mkdir()
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        await r.call(sid, "folders", op="add", path=str(shop), env="host")
        [ask] = await r.manager.asks.open_for(r.project.id, routed_to="operator")
        # Between the question and the answer the folder reached the project another way.
        await r.manager.projects.add_folder(r.project.id, str(shop), env="host")
        notes = r.team.app.notifications
        before = len(notes.posted)

        result = await r.team.answer(ask.short_id, selected=["Add"], by="operator", via="app")
        assert result["delivered"] is False and "already has the folder" in result["error"]

        posted = notes.posted[before:]
        [failed] = [d for d in posted if d.kind == "orchestrator_request_failed"]
        assert failed.tone == "error" and failed.project_id == r.project.id and "already has the folder" in failed.body
        chat = await events_messages(r.manager, sid)
        assert any(f"you approved request [{ask.short_id}]" in text and "already has the folder" in text for text in chat), chat

        [answered] = await events(r.manager, "ask.answered")
        line = await r.orch.line(await r.refreshed(), answered)
        assert f"[{ask.short_id}]" in line and "could not be added" in line and "already has the folder" in line
        assert "approved —" not in line and not line.endswith(": approved")
    finally:
        await r.manager.close()


@pytest.mark.parametrize("via", ["app", "project", "main", "telegram", "notification", "push"])
async def test_the_first_option_approves_a_folder_from_every_window(settings: Settings, db: Database, tmp_path: Path, via: str) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        place = tmp_path / f"place-{via}"
        place.mkdir()
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        await r.call(sid, "folders", op="add", path=str(place), env="host")
        [ask] = await r.manager.asks.open_for(r.project.id, routed_to="operator")
        assert ask.detail["options"] == ["Add", "Don't add"]
        if via in ("notification", "push"):
            # A notification's button carries the option's index, not its word.
            await r.team.resolve_action(SimpleNamespace(target=ask.id, action="answer:0", value=None, via=via))
        else:
            await r.team.answer(ask.short_id, selected=["Add"], by="operator", via=via)
        assert str(place) in [str(f.path) for f in (await r.refreshed()).folders]
        [answered] = await events(r.manager, "ask.answered")
        assert (await r.orch.line(await r.refreshed(), answered)).endswith(": approved")
    finally:
        await r.manager.close()


async def test_the_second_option_and_a_plain_no_decline_and_add_nothing(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        for index, answer in enumerate(({"selected": ["Don't add"]}, {"allow": False})):
            place = tmp_path / f"no-{index}"
            place.mkdir()
            await r.call(sid, "folders", op="add", path=str(place), env="host")
            [ask] = await r.manager.asks.open_for(r.project.id, routed_to="operator")
            await r.team.answer(ask.short_id, by="operator", **answer)  # type: ignore[arg-type]
            assert str(place) not in [str(f.path) for f in (await r.refreshed()).folders]
        assert len((await r.refreshed()).folders) == 1
    finally:
        await r.manager.close()
