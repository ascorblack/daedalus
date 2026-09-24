"""``TerminalRead``: a session's agent reads its own terminals, and only those, redacted and bounded.

The terminals service runs against the in-process daemon; the tool reaches it the way it does in a
running host, through the manager's service hook.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import TerminalsConfig
from daedalus.host.services import SessionServices, locator
from daedalus.stores.database import Database
from daedalus.terminals.model import Owner, TerminalSpec
from daedalus.terminals.service import Terminals
from daedalus.tools.terminal import terminal_read
from tests.support.fake_ptyd import FakePtyd
from tests.unit.test_terminals_service import FakeOwners

SESSION = "s-reader"
OTHER = "s-other"
TOKEN = "ghp_" + "Zq9Yx8Wv7Ut6Sr5Qp4On3Ml2Kj1Ih0GfEdCb"


@pytest.fixture
def run_dirs() -> Iterable[dict[str, Path]]:
    # Unix socket paths are short; pytest's temporary directories can be too long for one.
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))
    yield {"container": path / "c", "host": path / "h"}
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemons(run_dirs: dict[str, Path]) -> AsyncIterator[dict[str, FakePtyd]]:
    made = {env: await FakePtyd(path, env=env).start() for env, path in run_dirs.items()}
    yield made
    for fake in made.values():
        await fake.stop()


@pytest.fixture
def cfg() -> TerminalsConfig:
    return TerminalsConfig()


@pytest.fixture
def owners() -> FakeOwners:
    fake = FakeOwners()
    fake.add(Owner("session", SESSION), cwd="/tmp")
    fake.add(Owner("session", SESSION), cwd="/tmp", env="host")
    fake.add(Owner("session", OTHER), cwd="/tmp")
    return fake


@pytest.fixture
async def service(db: Database, run_dirs: dict[str, Path], owners: FakeOwners, cfg: TerminalsConfig, daemons: dict[str, FakePtyd]) -> AsyncIterator[Terminals]:
    made = Terminals(db, run_dirs=dict(run_dirs), config=lambda: cfg, owners=owners)  # type: ignore[arg-type]
    await made.start()
    assert await made.wait_available("container") and await made.wait_available("host")
    yield made
    await made.close()


@pytest.fixture
def ctx(service: Terminals, tmp_path: Path) -> Iterator[ToolContext]:
    manager = SimpleNamespace(service_hooks={"terminals": service.agent_service})
    locator.register(SessionServices(session_id=SESSION, workspace_dir=tmp_path, max_tool_output_chars=4000, extra={"manager": manager}))
    yield ToolContext(tenant_id="t", run_id="r", session_id=SESSION, metadata={"tool_call_id": "c"})
    locator.unregister(SESSION)


async def _open(service: Terminals, session: str = SESSION, *, env: str = "container", title: str = "") -> str:
    return str((await service.create(TerminalSpec(env=env, owner=Owner("session", session), title=title)))["id"])


async def _read(ctx: ToolContext, **arguments: object) -> tuple[str, bool]:
    result = await terminal_read().invoke(ctx, arguments)
    return result.content, bool(result.is_error)


async def test_it_sees_only_its_own_session_and_never_the_host(service: Terminals, daemons: dict[str, FakePtyd], ctx: ToolContext) -> None:
    mine = await _open(service, title="tests")
    theirs = await _open(service, OTHER, title="theirs")
    host = await _open(service, env="host", title="laptop")
    daemons["container"].terminals[theirs].output += b"their secret plan\n"
    listed, failed = await _read(ctx, what="list")
    assert not failed and mine in listed and theirs not in listed and host not in listed
    # Another session's terminal and a host terminal are answered as if they did not exist.
    for other in (theirs, host):
        text, failed = await _read(ctx, what="output", terminal=other)
        assert failed and "no terminal" in text and "secret" not in text
    text, failed = await _read(ctx, what="output", terminal="theirs")
    assert failed and "no terminal 'theirs'" in text


async def test_a_setting_lets_it_read_the_host_terminals_of_its_own_session(service: Terminals, daemons: dict[str, FakePtyd], ctx: ToolContext, cfg: TerminalsConfig) -> None:
    host = await _open(service, env="host", title="laptop")
    daemons["host"].terminals[host].output += b"uptime 3 days\n"
    cfg.agent_reads_host = True
    listed, _ = await _read(ctx, what="list")
    assert host in listed and "host" in listed
    text, failed = await _read(ctx, what="output", terminal="laptop")
    assert not failed and "uptime 3 days" in text


async def test_output_is_read_by_title_or_alone_and_resumes_from_where_it_stopped(service: Terminals, daemons: dict[str, FakePtyd], ctx: ToolContext) -> None:
    tid = await _open(service, title="Tests")
    term = daemons["container"].terminals[tid]
    term.output += b"".join(f"line {i}\n".encode() for i in range(500))
    # The only terminal needs no name; by default the last 200 lines are shown.
    text, failed = await _read(ctx, what="output")
    assert not failed and "line 499" in text and "line 300" in text and "line 299\n" not in text
    assert "300 earlier ones left out" in text and f"next since_seq={len(term.output)}" in text
    seen = len(term.output)
    term.output += b"PASSED 12 tests\n"
    text, _ = await _read(ctx, what="output", terminal="tests", since_seq=seen)
    assert "PASSED 12 tests" in text and "line 499" not in text
    text, _ = await _read(ctx, what="output", terminal=tid, lines=3)
    assert text.count("\n") < 6 and "PASSED" in text


async def test_several_terminals_must_be_named(service: Terminals, ctx: ToolContext) -> None:
    first = await _open(service, title="server")
    second = await _open(service, title="tests")
    text, failed = await _read(ctx, what="output")
    assert failed and first in text and second in text
    text, failed = await _read(ctx, what="output", terminal="serv")
    assert not failed and "server" in text


async def test_what_it_reads_is_redacted_and_bounded(service: Terminals, daemons: dict[str, FakePtyd], ctx: ToolContext) -> None:
    tid = await _open(service)
    term = daemons["container"].terminals[tid]
    term.output += f"export GITHUB_TOKEN={TOKEN}\n".encode() + b"".join(f"{i:05d} ".encode() * 40 + b"\n" for i in range(150))
    text, failed = await _read(ctx, what="output")
    assert not failed and TOKEN not in text
    assert len(text) <= 4000 + 200 and "characters omitted" in text
    # The line that says where to read on survives the cut.
    assert f"next since_seq={len(term.output)}" in text
    term.screen = [f"$ echo {TOKEN}", TOKEN, "$ "]
    text, _ = await _read(ctx, what="screen")
    assert TOKEN not in text and "$ echo" in text


async def test_a_screen_is_read_when_the_service_keeps_one_and_output_stands_in_when_not(service: Terminals, daemons: dict[str, FakePtyd], ctx: ToolContext) -> None:
    tid = await _open(service)
    term = daemons["container"].terminals[tid]
    term.output += b"npm test\n3 failing\n"
    text, failed = await _read(ctx, what="screen")
    assert not failed and "could not be read" in text and "3 failing" in text
    term.screen = ["$ npm test", "  3 failing", "$ ", "", ""]
    text, failed = await _read(ctx, what="screen")
    assert not failed and "screen 80×24" in text and "  3 failing" in text and "could not be read" not in text


async def test_commands_come_from_the_shell_or_say_they_cannot(service: Terminals, daemons: dict[str, FakePtyd], ctx: ToolContext) -> None:
    tid = await _open(service)
    text, failed = await _read(ctx, what="commands")
    assert not failed and "does not record the commands" in text
    daemons["container"].terminals[tid].commands = [
        {"n": 1, "command": "npm ci", "cwd": "/tmp/app", "exit_code": 0, "finished_at": "x"},
        {"n": 2, "command": "npm test", "cwd": "/tmp/app", "exit_code": 1, "finished_at": "x"},
    ]
    text, failed = await _read(ctx, what="commands", lines=1)
    assert not failed and "`npm test` → exit 1" in text and "npm ci" not in text


async def test_nothing_to_read_and_bad_arguments_are_plain_answers(service: Terminals, ctx: ToolContext) -> None:
    text, failed = await _read(ctx, what="list")
    assert not failed and "no terminals" in text
    text, failed = await _read(ctx, what="output")
    assert failed and "no terminals" in text
    text, failed = await _read(ctx, what="type")
    assert failed and "what is one of" in text
