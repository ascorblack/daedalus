"""The Claude Code adapter against the real Claude Code and the real daemon: the harness manager's
self-check session — launch with the adapter's overlay, the folder-trust question answered by the
readiness gate, ``SessionStart``, the team tools loaded through ``ptyd team-mcp``, one prompt on the
cheapest model whose ``Report`` comes back through the team channel, and a clean exit.

It spends one tiny prompt of a real subscription, so it runs only when asked for by name:
``DAEDALUS_INTEGRATION=1 DAEDALUS_HARNESS_LIVE=claude DAEDALUS_PTYD_BIN=<built ptyd>
DAEDALUS_CLAUDE_CONFIG=<a signed-in Claude configuration directory>``. The configuration is copied
into a temporary home and used from there — the original is never written to, nor is any project
folder of a running installation touched.
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
from pathlib import Path

import pytest

from daedalus.config import HarnessConfig, TerminalsConfig
from daedalus.harness.claude import ClaudeCodeAdapter
from daedalus.harness.runtime import RuntimeEnvironment
from daedalus.harness.selfcheck import session_check
from daedalus.stores.database import Database
from daedalus.terminals.service import Terminals
from tests.integration.test_ptyd_real import FreeOnly

BINARY = os.environ.get("DAEDALUS_PTYD_BIN", "")
CONFIG = os.environ.get("DAEDALUS_CLAUDE_CONFIG", "")
CLAUDE = shutil.which("claude") or ""

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif("claude" not in os.environ.get("DAEDALUS_HARNESS_LIVE", "").split(","), reason="set DAEDALUS_HARNESS_LIVE=claude: it spends a prompt"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_PTYD_BIN to a built ptyd"),
    pytest.mark.skipif(not CONFIG or not Path(CONFIG, ".credentials.json").is_file(), reason="set DAEDALUS_CLAUDE_CONFIG to a signed-in Claude configuration"),
    pytest.mark.skipif(not CLAUDE, reason="claude is not installed"),
]


@pytest.fixture
def base() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-claude-")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def throwaway_config(base: Path) -> Path:
    """The signed-in configuration's credentials, with nothing else of it: a first run set as done,
    and no refresh token, so this copy can never rotate the original's sign-in."""
    config = base / "home" / ".claude"
    config.mkdir(parents=True)
    credentials = json.loads(Path(CONFIG, ".credentials.json").read_text())
    for value in credentials.values():
        if isinstance(value, dict):
            value.pop("refreshToken", None)
            value.pop("refreshTokenExpiresAt", None)
    target = config / ".credentials.json"
    target.write_text(json.dumps(credentials))
    target.chmod(0o600)
    # The first run counts as done only with the account it signed in as (measured: without it the
    # theme and the sign-in screens come back); that one key is taken from the original, read-only.
    state: dict[str, object] = {"hasCompletedOnboarding": True, "theme": "dark"}
    for place in (Path(CONFIG, ".claude.json"), Path(CONFIG).parent / ".claude.json"):
        if place.is_file():
            account = json.loads(place.read_text()).get("oauthAccount")
            if account:
                state["oauthAccount"] = account
                break
    (config / ".claude.json").write_text(json.dumps(state))
    return config


@pytest.fixture
async def service(db: Database, base: Path) -> AsyncIterator[Terminals]:
    config = throwaway_config(base)
    home = base / "home"
    env = {"PATH": f"{Path(CLAUDE).parent}:/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "CLAUDE_CONFIG_DIR": str(config), "LANG": "C.UTF-8"}
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
    try:
        yield made
    finally:
        await made.close()
        process.send_signal(signal.SIGTERM)
        await asyncio.to_thread(process.wait, 10)


async def test_the_self_check_session_against_the_real_claude_code(service: Terminals, base: Path) -> None:
    env = RuntimeEnvironment(service, "container", home=str(base / "home"))
    config = HarnessConfig(ready_timeout_s=60)
    # DAEDALUS_SELF_CHECK_TURN=0 runs everything but the prompt, which costs nothing.
    turn = os.environ.get("DAEDALUS_SELF_CHECK_TURN", "1") != "0"
    result = await session_check(ClaudeCodeAdapter(), service, lambda: config, env, "haiku", turn)
    assert result.ok, [(s.name, s.ok, s.detail) for s in result.steps]
    assert [s.name for s in result.steps] == ["launch", "ready", "team", "deliver", "reply", "exit"][: 6 if turn else 3] + ([] if turn else ["exit"])
    assert result.version.startswith("2.")
