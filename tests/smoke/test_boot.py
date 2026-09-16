"""Preflight gate: the tree must import, configure, register tools and open its database."""

from __future__ import annotations

import importlib
import pkgutil

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.capabilities import ALL_SELFDEV_TOOLS
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import DEFAULT_PRESET, model_config, presets


def test_every_module_imports() -> None:
    import daedalus

    for module in pkgutil.walk_packages(daedalus.__path__, "daedalus."):
        importlib.import_module(module.name)


def test_config_roundtrip(settings: Settings) -> None:
    config = RuntimeConfig.load(settings.config_path)
    assert config.presets == {}  # a fresh installation has no model until the operator adds one
    config.presets.update(presets())
    config.presets[DEFAULT_PRESET].label = "x"
    config.save(settings.config_path)
    assert RuntimeConfig.load(settings.config_path).presets[DEFAULT_PRESET].label == "x"


async def test_manager_starts_and_registers_tools(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    names = {t.name for t in manager.tools.list_all()}
    for required in ("Exec", "Read", "Write", "Edit", "Find", "Search", "AskUser", "SendFile", "ScheduleCreate", "Skill"):
        assert required in names
    # The self-development tools depend on the installation, and this is the supervisor's preflight: it
    # runs in whatever installation is being checked. Naming one of them here would fail a desktop
    # install — which has a checkout and no remote — on the gate rather than on the change.
    assert names & ALL_SELFDEV_TOOLS == set(manager.capabilities.selfdev.tools)
    skills = await manager.skills.list("daedalus")
    assert {s.name for s in skills} >= {"self-develop", "telegram-output", "scheduling"}
    await manager.close()
