from __future__ import annotations

import os
from pathlib import Path

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.stores.database import Database
from tests.support.models import model_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def rebuilder_at(tmp_path: Path) -> Path:
    """A trigger directory with a fresh heartbeat in it: what a running rebuilder looks like.

    The capability probe asks whether something is on the other end of that directory, so a test that
    wants the full self-development surface has to put a rebuilder there rather than rely on the
    compose file naming one (it always does; the service is behind a profile that is off by default).
    """
    directory = tmp_path / "rebuild-trigger"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "alive").write_text("")
    return directory


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
        # The self-development prerequisites, stubbed: the checkouts are really there in the repository
        # under test, and these two stand in for the token and for a running rebuilder, so the capability
        # probe resolves to server mode and the suite exercises the full surface.
        github_token="stub-token-for-the-capability-probe",
        rebuild_trigger_dir=rebuilder_at(tmp_path),
    )


@pytest.fixture
def config() -> RuntimeConfig:
    """A configuration that can run: the shipped defaults carry no model at all."""
    return model_config()


@pytest.fixture
async def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    await database.open()
    yield database  # type: ignore[misc]
    await database.close()
