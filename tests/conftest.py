from __future__ import annotations

import os
from pathlib import Path

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.stores.database import Database

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    os.environ.setdefault("OWNER_USER_ID", "1")
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        state_dir=tmp_path / "state",
        workspaces_dir=tmp_path / "workspaces",
        bot_repo_dir=REPO_ROOT,
        core_repo_dir=REPO_ROOT.parent / "protocore-exp",
        owner_user_id=1,
        telegram_bot_token="123:abc",
        # The self-development prerequisites, stubbed: the checkouts and the compose rebuilder are really
        # there in the repository under test, and this stands in for the token, so the capability probe
        # resolves to server mode and the suite exercises the full surface.
        github_token="stub-token-for-the-capability-probe",
    )


@pytest.fixture
def config() -> RuntimeConfig:
    return RuntimeConfig()


@pytest.fixture
async def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    await database.open()
    yield database  # type: ignore[misc]
    await database.close()
