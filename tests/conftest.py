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


class NetworkBlocked(OSError):
    """Raised in place of a connection the test suite is not allowed to make.

    An ``OSError`` on purpose, and not something exotic: every client in this code base already has a
    path for "the network did not answer", and that path is what a test on a machine with no route
    out would take. Refusing the connection this way exercises it rather than bypassing it.
    """


BLOCKED: list[str] = []
"""Every non-loopback address something tried to reach, in order, for when a test has to be found."""


def _loopback(address: object) -> bool:
    """Whether an address is this machine talking to itself, which the suite does constantly.

    The launcher fake, the model host fake, the supervisor's TCP port and every ASGI client are real
    sockets on 127.0.0.1 or a unix path, so the ban is on leaving the machine and not on sockets.
    """
    if not isinstance(address, tuple) or not address:
        return True  # a unix socket, or something with no host in it at all
    host = str(address[0] or "")
    return host in ("", "::", "0.0.0.0") or host.startswith("127.") or host in ("localhost", "::1", "::ffff:127.0.0.1")


@pytest.fixture(scope="session", autouse=True)
def no_network() -> object:
    """The suite may not leave the machine, and an attempt is refused rather than waited out.

    This is not tidiness. A real outbound call in a unit test is a test whose result depends on
    somebody else's server: it fails when that server is down, it leaks the machine's address to it,
    and — the reason this exists — it can hang. ``SessionManager.start`` refreshes model prices from
    models.dev on its first tick, so *every* test that starts a manager opened an HTTPS connection;
    when one of those was left half-closed the suite stopped dead, with the sockets in CLOSE-WAIT and
    nothing on the terminal, for as long as anybody was willing to wait.

    Patched at ``socket.socket.connect``, which is underneath httpx, aiohttp, asyncio's own
    ``create_connection`` and the standard library alike, so there is no client left to forget.
    """
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(self: socket.socket, address: object) -> object:
        if _loopback(address):
            return real_connect(self, address)  # type: ignore[arg-type]
        BLOCKED.append(str(address))
        raise NetworkBlocked(f"the test suite may not open a connection to {address!r}")

    def guard_ex(self: socket.socket, address: object) -> int:
        if _loopback(address):
            return real_connect_ex(self, address)  # type: ignore[arg-type]
        BLOCKED.append(str(address))
        raise NetworkBlocked(f"the test suite may not open a connection to {address!r}")

    socket.socket.connect = guard  # type: ignore[method-assign]
    socket.socket.connect_ex = guard_ex  # type: ignore[method-assign]
    try:
        yield None
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
