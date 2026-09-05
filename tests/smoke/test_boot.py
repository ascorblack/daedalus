"""Preflight gate: the tree must import, configure, register tools and open its database."""

from __future__ import annotations

import importlib
import pkgutil

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


def test_every_module_imports() -> None:
    import daedalus

    for module in pkgutil.walk_packages(daedalus.__path__, "daedalus."):
        importlib.import_module(module.name)


def test_config_roundtrip(settings: Settings) -> None:
    config = RuntimeConfig.load(settings.config_path)
    config.model.name = "x"
    config.save(settings.config_path)
    assert RuntimeConfig.load(settings.config_path).model.name == "x"


async def test_manager_starts_and_registers_tools(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    names = {t.name for t in manager.tools.list_all()}
    for required in ("Exec", "Read", "Write", "Edit", "Find", "Search", "AskUser", "SendFile", "SelfPropose", "ScheduleCreate", "Skill"):
        assert required in names
    skills = await manager.skills.list("daedalus")
    assert {s.name for s in skills} >= {"self-develop", "telegram-output", "scheduling"}
    await manager.close()
