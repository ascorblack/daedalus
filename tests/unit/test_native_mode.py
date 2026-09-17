"""Native mode: the agent as a process on the operator's machine rather than in a container.

Nothing in the agent changes shape — the same code, the same supervisor, the same policy engine.
What changes is where the paths point, what the defaults are, and what the doctor has to say about
checks that only mean something on one side of a container boundary. Each of those is decided in one
place, and this is what pins them down.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from daedalus import supervisor_client
from daedalus.config import RuntimeConfig, Settings, keyproxy_base, native_mode, sandbox_never_writable
from daedalus.host.policy import CONTAINER_CHECKOUTS, Policy
from daedalus.tools import shell as shell_tool

LAUNCHER = Path(__file__).resolve().parents[2] / "launcher" / "supervisor.py"


def reloaded_config(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    """The configuration module read again with this environment, since what it decides at import
    time is exactly what the mode changes."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import daedalus.config as config

    return importlib.reload(config)


def test_the_mode_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("DAEDALUS_NATIVE", value)
        assert native_mode() is True, value
    for value in ("", "0", "no", "false"):
        monkeypatch.setenv("DAEDALUS_NATIVE", value)
        assert native_mode() is False, value
    monkeypatch.delenv("DAEDALUS_NATIVE", raising=False)
    assert native_mode() is False


def test_the_never_writable_list_is_this_installations_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed /srv list on a machine with no /srv protects nothing while reading as if it did."""
    monkeypatch.setenv("STATE_DIR", "/home/someone/daedalus/data/state")
    monkeypatch.setenv("BOT_REPO_DIR", "/home/someone/daedalus/data/daedalus")
    monkeypatch.setenv("CORE_REPO_DIR", "/home/someone/daedalus/data/protocore-exp")
    monkeypatch.setenv("DAEDALUS_RUNTIME", "/home/someone/daedalus/data/runtime")
    never = sandbox_never_writable()
    for path in ("/home/someone/daedalus/data/state", "/home/someone/daedalus/data/daedalus", "/home/someone/daedalus/data/protocore-exp", "/home/someone/daedalus/data/runtime"):
        assert path in never, path
    # The machine's own directories are never writable in either mode.
    assert {"/etc/ssl", "/usr/local", "/var/lib"} <= never


def test_the_never_writable_list_still_guards_the_container(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("STATE_DIR", "BOT_REPO_DIR", "CORE_REPO_DIR", "DAEDALUS_RUNTIME"):
        monkeypatch.delenv(name, raising=False)
    assert {"/srv/state", "/srv/daedalus", "/srv/protocore-exp"} <= sandbox_never_writable()


def test_the_sandbox_cannot_be_opened_onto_this_installations_own_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATE_DIR", "/home/someone/data/state")
    with pytest.raises(ValueError):
        RuntimeConfig.model_validate({"tools": {"exec": {"sandbox_extra_writable": ["/home/someone/data/state"]}}})
    # A directory that is nothing to do with the installation is still the operator's to open.
    RuntimeConfig.model_validate({"tools": {"exec": {"sandbox_extra_writable": ["/home/someone/scratch/build"]}}})


def test_services_are_reached_at_the_loopback_address_natively(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no docker host to publish from: a service the agent starts listens here."""
    config = reloaded_config(monkeypatch, DAEDALUS_NATIVE="1")
    try:
        assert config.Settings().services_public_host == "127.0.0.1"
        assert config.Settings().native is True
    finally:
        monkeypatch.delenv("DAEDALUS_NATIVE", raising=False)
        importlib.reload(config)
    assert Settings().services_public_host == ""


def test_the_key_proxy_is_wherever_the_launcher_put_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a container it is a service name on a private network; natively it is a loopback port."""
    monkeypatch.delenv("KEYPROXY_BASE_URL", raising=False)
    assert keyproxy_base() == "http://keyproxy:3200"
    assert RuntimeConfig().providers["deepseek"].base_url == "http://keyproxy:3200/deepseek"
    monkeypatch.setenv("KEYPROXY_BASE_URL", "http://127.0.0.1:19160/")
    assert keyproxy_base() == "http://127.0.0.1:19160"
    fresh = RuntimeConfig()
    assert fresh.providers["deepseek"].base_url == "http://127.0.0.1:19160/deepseek"
    assert fresh.providers["claude"].base_url == "http://127.0.0.1:19160/claude/v1"
    assert fresh.tools.web.search.serper.base_url == "http://127.0.0.1:19160/serper"


def test_the_supervisor_address_is_a_socket_or_a_port() -> None:
    settings = Settings()
    settings.supervisor_socket = Path("/run/daedalus/supervisor.sock")
    settings.supervisor_tcp = ""
    assert settings.supervisor_address == "/run/daedalus/supervisor.sock"
    assert supervisor_client.tcp_endpoint(settings.supervisor_address) is None

    settings.supervisor_tcp = "127.0.0.1:8769"
    assert settings.supervisor_address == "tcp://127.0.0.1:8769"
    assert supervisor_client.tcp_endpoint(settings.supervisor_address) == ("127.0.0.1", 8769)
    assert supervisor_client.present(settings.supervisor_address) is True

    with pytest.raises(supervisor_client.SupervisorUnavailable):
        supervisor_client.tcp_endpoint("tcp://nonsense")


def test_a_socket_that_is_not_there_is_not_present(tmp_path: Path) -> None:
    assert supervisor_client.present(tmp_path / "supervisor.sock") is False
    (tmp_path / "supervisor.sock").write_text("")
    assert supervisor_client.present(tmp_path / "supervisor.sock") is True


def test_the_policy_guards_this_installations_checkouts(tmp_path: Path) -> None:
    """The checkouts come from Settings. A native installation has none under /srv, and naming /srv
    there would read exactly like a rule while guarding a directory that is not there."""
    bot, core = tmp_path / "daedalus", tmp_path / "protocore-exp"
    policy = Policy(operator_checkouts=(bot, core), selfdev_mode="local")
    assert policy.evaluate("Exec", {"command": "git push origin main", "cwd": str(bot)}).action == "deny"
    assert policy.evaluate("Exec", {"command": f"git -C {core} push"}).action == "deny"
    for path in CONTAINER_CHECKOUTS:
        assert path not in policy.operator_checkouts
    # Given nothing, the container's paths are the fallback, which is what a container install is.
    assert Policy().operator_checkouts == list(CONTAINER_CHECKOUTS)


def test_the_shell_is_bash_here_and_a_posix_shell_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert shell_tool.shell_argv("ls -la")[:2] == ["bash", "-lc"]
    runtime = tmp_path / "runtime"
    (runtime / "git" / "usr" / "bin").mkdir(parents=True)
    (runtime / "git" / "usr" / "bin" / "sh.exe").write_text("")
    monkeypatch.setenv("DAEDALUS_RUNTIME", str(runtime))
    argv = shell_tool.shell_argv("ls -la", windows=True)
    assert argv[0].endswith("sh.exe") and argv[1] == "-c" and argv[2] == "ls -la"
    assert str(runtime) in argv[0]


def test_bubblewrap_is_not_reported_as_missing_software_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_tool, "_bwrap_state", None)
    monkeypatch.setattr(shell_tool, "_bwrap_probed_at", 0.0)
    monkeypatch.setattr(shell_tool.sys, "platform", "darwin")
    status = shell_tool.bwrap_status()
    assert "Linux facility" in status and "darwin" in status
    assert "not installed" not in status


def test_the_isolation_note_says_what_is_gone_and_what_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_tool.sys, "platform", "linux")
    note = shell_tool.native_sandbox_note()
    assert "no container boundary" in note and "bubblewrap" in note
    monkeypatch.setattr(shell_tool.sys, "platform", "win32")
    assert "no container boundary" in shell_tool.native_sandbox_note()


@pytest.mark.asyncio
async def test_the_doctor_says_which_checks_a_container_would_have_answered() -> None:
    from daedalus import doctor

    settings = Settings()
    settings.native = True
    ctx = doctor.DoctorContext(settings=settings, config=RuntimeConfig())
    checks = await doctor._native(ctx)
    by_name = {c.name: c for c in checks}
    for name, _why in doctor.CONTAINER_ONLY:
        assert name in by_name, name
        assert by_name[name].message.startswith("not applicable (native)")
        assert by_name[name].ok is True  # a check that does not apply is not a failure
    assert "no container boundary" in by_name["isolation"].message
    assert "policy rules" in by_name["isolation"].fix_hint

    settings.native = False
    assert await doctor._native(doctor.DoctorContext(settings=settings, config=RuntimeConfig())) == []


def load_supervisor() -> Any:
    spec = importlib.util.spec_from_file_location("supervisor_native_under_test", LAUNCHER)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["supervisor_native_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.asyncio
async def test_the_supervisor_listens_on_a_port_where_there_are_no_unix_sockets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Windows has no unix sockets. The same protocol, the same commands, a loopback port instead."""
    port = free_port()
    monkeypatch.setenv("DAEDALUS_SUPERVISOR_TCP", f"127.0.0.1:{port}")
    monkeypatch.setenv("DAEDALUS_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DAEDALUS_BOT_REPO", str(tmp_path / "daedalus"))
    monkeypatch.setenv("DAEDALUS_CORE_REPO", str(tmp_path / "protocore-exp"))
    supervisor = load_supervisor()
    assert supervisor.SUPERVISOR_TCP == f"127.0.0.1:{port}"
    assert supervisor.bot_env()["SUPERVISOR_TCP"] == f"127.0.0.1:{port}"

    instance = supervisor.Supervisor()
    server = asyncio.create_task(instance.serve_socket())
    try:
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.02)
        else:
            pytest.fail("the supervisor never opened its port")
        writer.write(json.dumps({"op": "status"}).encode() + b"\n")
        await writer.drain()
        answer = json.loads(await asyncio.wait_for(reader.readline(), timeout=10))
        writer.close()
        assert answer["ok"] is True
        assert "selfdev_mode" in answer["result"]
        # And the client above reaches it through exactly the address Settings hands out.
        result = await supervisor_client.call(f"tcp://127.0.0.1:{port}", "status", timeout=10)
        assert "child_running" in result
    finally:
        server.cancel()


def test_zombies_are_a_linux_idea(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reaping is reading /proc. macOS and Windows have neither, and asyncio waits on the child."""
    supervisor = load_supervisor()
    monkeypatch.setattr(supervisor, "Path", lambda *args: tmp_path / "nowhere")
    assert supervisor.reap_zombies(set()) == 0


def test_the_owner_is_only_restored_where_there_are_owners(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The supervisor runs as root over a host mount in a container. Natively it is the operator's
    own process writing the operator's own files, and on Windows there are no uids at all."""
    supervisor = load_supervisor()
    calls: list[list[str]] = []
    monkeypatch.setattr(supervisor, "run", lambda cmd, **_: calls.append(cmd) or (0, ""))
    monkeypatch.setattr(supervisor, "POSIX", False)
    supervisor.restore_owner(tmp_path)
    assert calls == []
