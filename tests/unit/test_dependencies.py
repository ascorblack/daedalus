"""Dependency planning cannot install; an approved recipe cannot smuggle commands into installers."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import signal
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from protocore.contracts.llm import ProviderDelta

from daedalus.host.dependencies import KEY, DependencyPlanner
from daedalus.tools.shell import shell_argv, shell_environment
from tests.unit.test_components import HEAD, FakeApp

SPEC = importlib.util.spec_from_file_location("dependency_runtime_test", Path(__file__).resolve().parents[2] / "launcher" / "dependencies.py")
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@pytest.mark.parametrize("package", ["--index-url", "https://example.com/a.whl", "../evil", "a;touch marker", "a\nb", "$(id)", "a[extra]", "a @ file:x", "-rfile"])
def test_recipe_rejects_commands_paths_and_sources(package: str) -> None:
    with pytest.raises(ValueError):
        runtime.recipe({"python": [package], "system": []})
    with pytest.raises(ValueError):
        runtime.recipe({"python": [], "system": [package]})


def test_recipe_canonicalizes_and_refuses_multiple_versions() -> None:
    assert runtime.recipe({"python": ["Pillow==11.0", "Pillow==11.0"], "system": ["gcc", "make"]}) == {"python": ["Pillow==11.0"], "system": ["gcc", "make"]}
    with pytest.raises(ValueError):
        runtime.recipe({"python": ["my-package==1", "my_package==2"], "system": []})
    with pytest.raises(ValueError):
        runtime.recipe({"python": [], "system": [], "command": "echo test"})


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    instance = runtime.Dependencies(tmp_path / "repo", tmp_path / "state", tmp_path / "trigger", native=True)
    monkeypatch.setattr(instance, "capability", lambda: {"mode": "native", "python": True, "system": True, "manager": "apt", "reason": ""})
    return instance


def test_preview_never_writes_and_accept_checks_staleness(service: Any) -> None:
    proposal = service.preview({"python": ["Pillow"], "system": []})
    assert "+" in proposal["patch"] and "Pillow" in proposal["patch"]
    assert not service.manifest.exists()
    runtime.write_json(service.manifest, {"python": ["numpy"], "system": []})
    with pytest.raises(ValueError, match="changed"):
        service.accept(proposal, "a" * 32)
    assert not (service.root / "job.json").exists()


def test_accept_revalidates_recipe_and_rejects_removals(service: Any) -> None:
    runtime.write_json(service.manifest, {"python": ["numpy"], "system": []})
    proposal = service.preview({"python": ["Pillow"], "system": []})
    proposal["recipe"]["python"] = ["Pillow"]
    with pytest.raises(ValueError, match="only add"):
        service.accept(proposal, "a" * 32)


def test_unprivileged_native_system_install_refuses_without_sudo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = runtime.Dependencies(tmp_path, tmp_path / "state", tmp_path / "trigger", native=True)
    monkeypatch.setattr(service, "system_manager", lambda: "apt")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/usr/bin/uv")
    assert service.capability()["python"] is True
    assert service.capability()["system"] is False
    with pytest.raises(ValueError, match="never elevates"):
        service.preview({"python": [], "system": ["gcc"]})


@pytest.mark.asyncio
async def test_native_installs_into_versioned_environment_and_never_host_python(service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def command(argv: list[str], **kwargs: Any) -> str:
        calls.append(argv)
        return ""

    monkeypatch.setattr(runtime, "command", command)
    proposal = service.preview({"python": ["Pillow"], "system": []})
    job = service.accept(proposal, "a" * 32)
    await service.install(job)
    assert calls[0][:2] == ["uv", "venv"]
    assert str(service.root / "python") in calls[1][4]
    assert calls[1][-2:] == ["--", "Pillow"]
    assert not any("sudo" in call for call in calls)
    assert service.active_bin().startswith(str(service.root / "python"))
    assert runtime.read_json(service.root / "job.json", {})["state"] == "restarting"


@pytest.mark.asyncio
async def test_docker_writes_recipe_and_trigger_but_never_installs_in_running_container(service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service.native = False
    service.trigger.mkdir()

    async def forbidden(*args: Any, **kwargs: Any) -> str:
        pytest.fail("Docker dependencies must be installed during image build only")

    monkeypatch.setattr(runtime, "command", forbidden)
    job = service.accept(service.preview({"python": ["Pillow"], "system": ["gcc"]}), "b" * 32)
    await service.install(job)
    assert service.current() == job["recipe"]
    assert (service.trigger / "dependencies-request").read_text().strip() == "b" * 32
    with pytest.raises(ValueError, match="already running"):
        service.accept(service.preview({"python": ["numpy"], "system": []}), "c" * 32)
    (service.trigger / f"dependencies-{job['id']}.result").write_text("build failed")
    assert service.status()["job"]["state"] == "failed"
    assert service.current() == runtime.EMPTY
    assert not (service.root / "maintenance").exists()
    service.preview({"python": ["Pillow"], "system": ["gcc"]})


def test_interrupted_native_install_reports_partial_failure_and_unlocks(service: Any) -> None:
    service.accept(service.preview({"python": ["Pillow"], "system": []}), "a" * 32)
    assert service.status()["job"]["state"] == "failed"
    assert not (service.root / "maintenance").exists()


def test_status_for_old_job_does_not_unlock_a_new_approval(service: Any) -> None:
    runtime.write_json(service.root / "job.json", {"id": "old", "state": "completed"})
    (service.root / "maintenance").write_text("new")
    service.status()
    assert (service.root / "maintenance").read_text() == "new"


def test_tool_shell_uses_agent_environment_without_changing_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent_bin = str(tmp_path / "agent bin")
    monkeypatch.setenv("DAEDALUS_AGENT_BIN", agent_bin)
    argv = shell_argv("python script.py", windows=False)
    assert argv[:2] == ["bash", "-lc"]
    assert argv[2].startswith("export PATH=") and argv[2].endswith("python script.py")
    assert shell_environment("session")["PATH"].startswith(agent_bin)


class MemoryDB:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def kv_get(self, key: str) -> Any:
        return self.values.get(key)

    async def kv_set(self, key: str, value: Any) -> None:
        self.values[key] = value


class Provider:
    def __init__(self, turns: list[list[tuple[str, dict[str, Any]]]]) -> None:
        self.turns = iter(turns)
        self.requests: list[Any] = []

    async def stream_with_tools(self, request: Any) -> Any:
        self.requests.append(request)
        for index, (name, arguments) in enumerate(next(self.turns)):
            yield ProviderDelta(kind="tool_use_start", tool_call_id=str(index), tool_name=name)
            yield ProviderDelta(kind="tool_use_stop", tool_call_id=str(index), tool_input_final=arguments)


def planner_for(tmp_path: Path, provider: Any) -> DependencyPlanner:
    preset = SimpleNamespace(provider="test", display=lambda key: key)
    manager = SimpleNamespace(resolve_model=lambda overrides: ([(provider, "test")], preset), budget_exceeded=lambda: None, provider_costs_nothing=lambda key: True, dependency_installation_busy=lambda: False)
    return DependencyPlanner(SimpleNamespace(db=MemoryDB(), settings=SimpleNamespace(state_dir=tmp_path), config=SimpleNamespace(presets={"test": preset}), manager=manager))


@pytest.mark.asyncio
async def test_model_only_gets_read_and_propose_tools_and_never_installs(tmp_path: Path, service: Any) -> None:
    provider = Provider([[('InspectEnvironment', {})], [('ProposeDependencies', {"python": ["Pillow"], "system": [], "explanation": "Image processing"})]])
    planner = planner_for(tmp_path, provider)
    calls = []

    async def rpc(op: str, **kwargs: Any) -> Any:
        calls.append(op)
        if op == "dependencies_preview":
            return service.preview(kwargs["additions"])
        return {"capability": service.capability(), "job": None}

    planner.rpc = rpc
    result = await planner.start("Process images", "test")
    await planner.task
    proposal = await planner.proposal()
    assert proposal["state"] == "ready" and proposal["id"] == result["id"]
    assert set(calls) == {"dependencies_status", "dependencies_inventory", "dependencies_preview"}
    assert {tool.name for tool in provider.requests[0].tools} == {"InspectEnvironment", "ProposeDependencies"}
    assert not service.manifest.exists()


@pytest.mark.asyncio
async def test_planner_rejects_unknown_tools_and_stops_after_five_turns(tmp_path: Path) -> None:
    provider = Provider([[('Exec', {"command": "echo forbidden"})]] * 5)
    planner = planner_for(tmp_path, provider)
    with pytest.raises(ValueError, match="five-turn"):
        await planner._run({"id": "a" * 32, "preset": "test", "request": "images"})
    assert len(provider.requests) == 5


@pytest.mark.asyncio
async def test_cancel_discards_proposal_and_cannot_then_approve(tmp_path: Path) -> None:
    planner = planner_for(tmp_path, None)
    await planner.app.db.kv_set(KEY, {"id": "a", "state": "ready"})
    await planner.cancel("a")
    assert (await planner.proposal())["state"] == "cancelled"
    with pytest.raises(ValueError, match="no longer"):
        await planner.approve("a")


@pytest.mark.asyncio
async def test_busy_agent_prevents_approval_and_clears_maintenance(tmp_path: Path) -> None:
    planner = planner_for(tmp_path, None)
    planner.app.manager.dependency_installation_busy = lambda: True
    await planner.app.db.kv_set(KEY, {"id": "a", "state": "ready"})

    async def rpc(op: str, **kwargs: Any) -> Any:
        assert op == "dependencies_status"
        return {"job": None}

    planner.rpc = rpc
    with pytest.raises(ValueError, match="stop running"):
        await planner.approve("a")
    assert not (tmp_path / "dependencies" / "maintenance").exists()
    assert (await planner.proposal())["state"] == "ready"


@pytest.mark.asyncio
async def test_approval_is_exactly_once_and_persists_across_page_reload(tmp_path: Path) -> None:
    planner = planner_for(tmp_path, None)
    await planner.app.db.kv_set(KEY, {"id": "a", "state": "ready", "proposal": {"recipe": {"python": ["Pillow"], "system": []}}})
    calls = []

    async def rpc(op: str, **kwargs: Any) -> Any:
        calls.append(op)
        assert (tmp_path / "dependencies" / "maintenance").exists()
        return {"id": "a", "state": "installing"}

    planner.rpc = rpc
    await planner.approve("a")
    other_page = DependencyPlanner(planner.app)
    assert (await other_page.proposal())["state"] == "accepted"
    with pytest.raises(ValueError):
        await planner.approve("a")
    assert calls == ["dependencies_apply"]


@pytest.mark.asyncio
async def test_supervisor_receipt_recovers_a_lost_approval_acknowledgement(tmp_path: Path) -> None:
    planner = planner_for(tmp_path, None)
    await planner.app.db.kv_set(KEY, {"id": "a", "state": "ready"})

    async def rpc(op: str, **kwargs: Any) -> Any:
        assert op in ("dependencies_status", "dependencies_inventory")
        return {"job": {"id": "a", "state": "installing"}}

    planner.rpc = rpc
    assert (await planner.view())["proposal"]["state"] == "accepted"
    assert (await planner.proposal())["state"] == "accepted"


@pytest.mark.asyncio
async def test_command_timeout_kills_process() -> None:
    with pytest.raises(TimeoutError):
        await runtime.command([runtime.sys.executable, "-c", "import time; time.sleep(30)"], limit=0.05)


@pytest.mark.asyncio
async def test_cancel_interrupts_planning(tmp_path: Path) -> None:
    planner = planner_for(tmp_path, None)
    await planner.app.db.kv_set(KEY, {"id": "a", "state": "planning"})
    planner.task = asyncio.create_task(asyncio.sleep(30))
    await planner.cancel("a")
    assert planner.task.cancelled()
    assert (await planner.proposal())["state"] == "cancelled"


@pytest.mark.asyncio
async def test_real_native_python_install_is_offline_and_isolated(service: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if not runtime.shutil.which("uv"):
        pytest.skip("uv is required for the native installer")
    wheel = tmp_path / "dependency_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dependency_fixture/__init__.py", "VALUE = 42\n")
        archive.writestr("dependency_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: dependency-fixture\nVersion: 1.0\n")
        archive.writestr("dependency_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("dependency_fixture-1.0.dist-info/RECORD", "dependency_fixture/__init__.py,,\ndependency_fixture-1.0.dist-info/METADATA,,\ndependency_fixture-1.0.dist-info/WHEEL,,\ndependency_fixture-1.0.dist-info/RECORD,,\n")
    real_command = runtime.command

    async def offline(argv: list[str], **kwargs: Any) -> str:
        if argv[:3] == ["uv", "pip", "install"]:
            argv = [*argv[:3], "--offline", "--no-index", "--find-links", str(tmp_path), *argv[3:]]
        return await real_command(argv, **kwargs)

    monkeypatch.setattr(runtime, "command", offline)
    proposal = service.preview({"python": ["dependency-fixture==1.0"], "system": []})
    await service.install(service.accept(proposal, "a" * 32))
    python = str(Path(service.active_bin()) / ("python.exe" if os.name == "nt" else "python"))
    assert await real_command([python, "-I", "-c", "import dependency_fixture; print(dependency_fixture.VALUE)"]) == "42"
    assert importlib.util.find_spec("dependency_fixture") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_rebuilder_only_replaces_container_after_successful_build(tmp_path: Path, failed: bool) -> None:
    if os.name == "nt":
        pytest.skip("Docker sidecar uses a POSIX shell")
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\n[ "$DAEDALUS_SECRETS_FILE" = /dev/null ] || exit 99\nprintf "%s\\n" "$*" >> "$CALLS"\ncase "$*" in\n *" build "*) exit "$BUILD_RESULT";;\nesac\nexit 0\n')
    fake.chmod(0o755)
    job_id = "b" * 32
    (trigger / "dependencies-request").write_text(job_id + "\n")
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""), "DAEDALUS_REBUILD_TRIGGER_DIR": str(trigger), "COMPOSE_FILE": "compose.yaml", "CALLS": str(tmp_path / "calls"), "BUILD_RESULT": "1" if failed else "0"}
    script = Path(__file__).resolve().parents[2] / "deploy" / "rebuild.sh"
    process = await asyncio.create_subprocess_exec("sh", str(script), env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        async with asyncio.timeout(5):
            while not (trigger / f"dependencies-{job_id}.result").exists():
                await asyncio.sleep(0.02)
        result = (trigger / f"dependencies-{job_id}.result").read_text()
        calls = (tmp_path / "calls").read_text()
        assert ("up -d --no-build --no-deps daedalus" in calls) is not failed
        assert (result.strip() == "completed") is not failed
        assert (trigger / "dependencies-alive").exists()
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        await process.wait()


def test_dependency_routes_require_auth_and_approve_only_stored_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.extensions.api import build_app

    app = FakeApp(tmp_path, native=True)
    app.db = MemoryDB()
    app.manager.dependency_installation_busy = lambda: False
    proposal_id = "c" * 32
    stored = {"base": "original", "recipe": {"python": ["Pillow"], "system": []}}
    app.db.values[KEY] = {"id": proposal_id, "state": "ready", "proposal": stored}
    calls = []

    async def rpc(self: Any, op: str, **kwargs: Any) -> Any:
        calls.append((op, kwargs))
        return {"id": proposal_id, "state": "installing"}

    monkeypatch.setattr(DependencyPlanner, "rpc", rpc)
    with TestClient(build_app(app, "tok")) as client:
        response = client.post(f"/api/dependencies/{proposal_id}/approve")
        assert response.status_code == 401
        assert not calls
        response = client.post(f"/api/dependencies/{proposal_id}/approve", headers=HEAD, json={"proposal": {"recipe": {"python": ["unapproved"], "system": []}}})
        assert response.status_code == 200
        assert calls == [("dependencies_apply", {"proposal": stored, "id": proposal_id})]
        assert client.post(f"/api/dependencies/{proposal_id}/approve", headers=HEAD).status_code == 409
