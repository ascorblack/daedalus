"""The Codex and OpenCode adapters against the real CLIs and the real daemon: the harness manager's
self-check session — the launch with the adapter's configuration, the CLI's server reached through the
daemon, the team tools loaded through ``ptyd team-mcp``, with the model turn one prompt whose ``Report``
comes back through the team channel, and a clean exit.

With the model turn it spends one tiny prompt, so it runs only when asked for by name:
``DAEDALUS_INTEGRATION=1 DAEDALUS_HARNESS_LIVE=codex,opencode DAEDALUS_PTYD_BIN=<built ptyd>`` and, per
CLI, ``DAEDALUS_CODEX_HOME=<a signed-in ~/.codex>`` or ``DAEDALUS_OPENCODE_AUTH=<its auth.json>``
(``DAEDALUS_OPENCODE_MODEL`` names the model, ``provider/model``). ``DAEDALUS_SELF_CHECK_TURN=0``
runs everything but the prompt, which costs nothing. The credentials are copied into a temporary home
and used from there, without anything that could refresh them: the original sign-in is never written
to or rotated, and no project folder of a running installation is touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from daedalus.config import HarnessConfig, TerminalsConfig
from daedalus.harness.codex import CodexAdapter
from daedalus.harness.opencode import OpenCodeAdapter
from daedalus.harness.runtime import RuntimeEnvironment
from daedalus.harness.selfcheck import session_check
from daedalus.stores.database import Database
from daedalus.terminals.service import Terminals
from tests.integration.test_ptyd_real import FreeOnly

BINARY = os.environ.get("DAEDALUS_PTYD_BIN", "")
LIVE = os.environ.get("DAEDALUS_HARNESS_LIVE", "").split(",")
TURN = os.environ.get("DAEDALUS_SELF_CHECK_TURN", "1") != "0"

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_PTYD_BIN to a built ptyd"),
]


@pytest.fixture
def base() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-cli-")).resolve()
    (path / "home").mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def codex_home(base: Path) -> None:
    """The ChatGPT sign-in without its refresh token, marked as just refreshed, so Codex neither
    tries nor is able to rotate the original's tokens."""
    source = Path(os.environ["DAEDALUS_CODEX_HOME"], "auth.json")
    auth = json.loads(source.read_text())
    if isinstance(auth.get("tokens"), dict):
        auth["tokens"]["refresh_token"] = ""
    auth["last_refresh"] = datetime.now(UTC).isoformat()
    target = base / "home" / ".codex" / "auth.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(auth))
    target.chmod(0o600)


def opencode_home(base: Path) -> None:
    """Only the credentials that are plain keys: an OAuth entry carries a refresh token that a copy
    could rotate."""
    source = Path(os.environ["DAEDALUS_OPENCODE_AUTH"])
    keys = {name: entry for name, entry in json.loads(source.read_text()).items() if isinstance(entry, dict) and entry.get("type") == "api"}
    target = base / "home" / ".local" / "share" / "opencode" / "auth.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(keys))
    target.chmod(0o600)


@pytest.fixture
async def service(db: Database, base: Path) -> AsyncIterator[Terminals]:
    home = base / "home"
    paths = [str(Path(p).parent) for p in (shutil.which("codex"), shutil.which("opencode"), shutil.which("node")) if p]
    env = {"PATH": ":".join([*paths, "/usr/local/bin", "/usr/bin", "/bin"]), "HOME": str(home), "LANG": "C.UTF-8"}
    process = subprocess.Popen(  # noqa: S603 — the binary the caller named, with fixed arguments
        [BINARY, "serve", "--env", "container", "--run-dir", str(base / "run"), "--state-dir", str(base / "state"), "--shell", "/bin/sh", "--home", str(home)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    for _ in range(200):
        if (base / "run" / "endpoint").exists():
            break
        await asyncio.sleep(0.05)
    made = Terminals(db, run_dirs={"container": base / "run", "host": None}, config=lambda: TerminalsConfig(input_idle_ms=0), owners=FreeOnly())
    await made.start()
    assert await made.wait_available("container", timeout=15)
    made.set_extra_roots("container", "cli", [str(home)])
    try:
        yield made
    finally:
        await made.close()
        process.send_signal(signal.SIGTERM)
        await asyncio.to_thread(process.wait, 10)


def expected_steps() -> list[str]:
    return ["launch", "ready", "team", "deliver", "reply", "exit"] if TURN else ["launch", "ready", "team", "exit"]


@pytest.mark.skipif("codex" not in LIVE or not shutil.which("codex") or not os.environ.get("DAEDALUS_CODEX_HOME"), reason="set DAEDALUS_HARNESS_LIVE=codex and DAEDALUS_CODEX_HOME")
async def test_the_self_check_session_against_the_real_codex(service: Terminals, base: Path) -> None:
    codex_home(base)
    env = RuntimeEnvironment(service, "container", home=str(base / "home"))
    result = await session_check(CodexAdapter(), service, lambda: HarnessConfig(ready_timeout_s=90), env, "gpt-6-luna", TURN)
    assert result.ok, [(s.name, s.ok, s.detail) for s in result.steps]
    assert [s.name for s in result.steps] == expected_steps()


@pytest.mark.skipif("opencode" not in LIVE or not shutil.which("opencode") or not os.environ.get("DAEDALUS_OPENCODE_AUTH"), reason="set DAEDALUS_HARNESS_LIVE=opencode and DAEDALUS_OPENCODE_AUTH")
async def test_the_self_check_session_against_the_real_opencode(service: Terminals, base: Path) -> None:
    opencode_home(base)
    env = RuntimeEnvironment(service, "container", home=str(base / "home"))
    model = os.environ.get("DAEDALUS_OPENCODE_MODEL", "")
    result = await session_check(OpenCodeAdapter(), service, lambda: HarnessConfig(ready_timeout_s=90), env, model, TURN)
    assert result.ok, [(s.name, s.ok, s.detail) for s in result.steps]
    assert [s.name for s in result.steps] == expected_steps()
