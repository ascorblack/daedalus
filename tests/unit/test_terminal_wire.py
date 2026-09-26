"""The host's codec against the golden frames the daemon and the app are tested against as well."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daedalus import load
from daedalus.terminals.endpoint import EndpointMissing, read_endpoint, remember_hook_port, sealed_ports
from daedalus.terminals.wire import BrowserFrame, FrameError, decode_browser, decode_frame, encode_browser, encode_frame

ROOT = Path(__file__).resolve().parents[2]
WIRE = ROOT / "ptyd" / "internal" / "wire" / "testdata"
# The socket framing is shared with the browser daemon, so its golden form lives in ptyd's shared
# packages.
SOCKET_WIRE = ROOT / "ptyd" / "proto" / "wire" / "testdata"


def _value(frame: BrowserFrame) -> dict[str, object]:
    """A decoded frame in the fixtures' terms."""
    if frame.kind == "output":
        return {"kind": "output", "seq": frame.seq, "data": frame.data.decode()}
    if frame.kind == "snapshot":
        return {"kind": "snapshot", "cols": frame.cols, "rows": frame.rows, "seq": frame.seq, "data": frame.data.decode()}
    if frame.kind == "event":
        return {"kind": "event", "event": frame.json}
    if frame.kind == "attach":
        return {"kind": "attach", "request": frame.json}
    if frame.kind == "input":
        return {"kind": "input", "data": frame.data.decode()}
    if frame.kind == "resize":
        return {"kind": "resize", "cols": frame.cols, "rows": frame.rows, "px_w": frame.px_w, "px_h": frame.px_h}
    return {"kind": "ack", "seq": frame.seq}


def test_every_golden_browser_frame_decodes_and_encodes_back() -> None:
    golden = json.loads((WIRE / "frames.json").read_text(encoding="utf-8"))
    for case in golden["frames"]:
        raw = bytes.fromhex(case["hex"])
        frame = decode_browser(raw)
        assert _value(frame) == case["value"], case["name"]
        again = encode_browser(frame)
        if frame.kind in ("event", "attach"):
            # JSON is compared as values: key order and spacing are each encoder's own.
            assert again[0] == raw[0] and json.loads(again[1:]) == json.loads(raw[1:]), case["name"]
        else:
            assert again == raw, case["name"]
    for case in golden["malformed"]:
        with pytest.raises(FrameError):
            decode_browser(bytes.fromhex(case["hex"]))


def test_socket_frames_match_the_daemons_fixtures() -> None:
    for case in json.loads((SOCKET_WIRE / "socket_frames.json").read_text(encoding="utf-8")):
        raw = bytes.fromhex(case["hex"])
        assert decode_frame(raw) == (case["channel"], bytes.fromhex(case["payload_hex"])), case["name"]
        assert encode_frame(case["channel"], bytes.fromhex(case["payload_hex"])) == raw, case["name"]
    with pytest.raises(FrameError):
        decode_frame(bytes.fromhex("00100005" + "00000000"))  # a length far past the maximum
    with pytest.raises(FrameError):
        encode_frame(1, b"x" * ((1 << 20) + 1))


def test_the_run_directory_says_where_the_daemon_is(tmp_path: Path) -> None:
    run = tmp_path / "run"
    with pytest.raises(EndpointMissing) as missing:
        read_endpoint(run)
    assert missing.value.reason == "not_installed"
    run.mkdir()
    with pytest.raises(EndpointMissing) as empty:
        read_endpoint(run)
    assert empty.value.reason == "not_installed"
    (run / "token").write_text("ab" * 32 + "\n")
    with pytest.raises(EndpointMissing) as idle:
        read_endpoint(run)
    assert idle.value.reason == "not_running"
    (run / "endpoint").write_text("unix:ptyd.sock")
    endpoint = read_endpoint(run)
    assert endpoint.path == run / "ptyd.sock" and endpoint.token == b"ab" * 32
    (run / "endpoint").write_text("tcp:127.0.0.1:47001")
    assert (read_endpoint(run).kind, read_endpoint(run).port) == ("tcp", 47001)
    # A daemon that says it listens anywhere but this machine's loopback is not handed the token.
    (run / "endpoint").write_text("tcp:192.0.2.7:47001")
    with pytest.raises(EndpointMissing):
        read_endpoint(run)


def test_the_daemons_loopback_ports_are_sealed(tmp_path: Path, settings: object) -> None:
    run = tmp_path / "hostrun"
    run.mkdir()
    (run / "token").write_text("t")
    (run / "endpoint").write_text("tcp:127.0.0.1:47002")
    configured = settings.model_copy(update={"terminals_host_dir": run})  # type: ignore[attr-defined]
    remember_hook_port(run, 47003)
    try:
        assert set(sealed_ports(configured)) == {47002, 47003}
    finally:
        remember_hook_port(run, 0)
    assert sealed_ports(settings) == ()  # type: ignore[arg-type]


# -- the projection ---------------------------------------------------------------------------


def test_the_likely_cost_follows_what_runs_then_what_was_measured_then_a_guess() -> None:
    costs = load.ProfileCosts(window=3)
    for rss in (100, 200, 300, 400):
        costs.add("harness:claude", rss, 10)
    costs.add("shell", 10, 1)
    measured = costs.profiles()
    assert measured["harness:claude"].rss_bytes == 300  # the window keeps the last three
    cost, basis = load.likely_cost(measured, ["harness:claude", "harness:claude", "shell"])
    assert basis == "running" and cost.rss_bytes == pytest.approx((300 + 300 + 10) / 3)
    cost, basis = load.likely_cost(measured, [])
    assert basis == "measured" and cost.rss_bytes == pytest.approx(155)
    cost, basis = load.likely_cost({}, ["shell"])
    assert basis == "default" and cost.rss_bytes == load.DEFAULT_COST["rss_bytes"]
    restored = load.ProfileCosts(window=3)
    restored.load(json.loads(json.dumps(costs.dump())))
    assert restored.profiles() == measured


def test_the_projection_is_the_machine_with_the_cap_filled() -> None:
    gib = 1 << 30
    machine = {"mem_total_bytes": 64 * gib, "mem_available_bytes": 40 * gib, "cpus": 16, "cpu_percent": 20.0}
    cost = load.Cost(rss_bytes=1 * gib, cpu_percent=32.0, samples=5)
    out = load.project(cap=20, running=4, used_rss=6 * gib, used_cpu=50.0, machine=machine, cost=cost)
    # Sixteen more terminals of a gigabyte each on top of the 24 GB in use now: 40 of 64.
    assert out["terminals_rss_bytes"] == 22 * gib and out["machine_used_bytes"] == 40 * gib
    assert out["mem_percent"] == pytest.approx(62.5) and out["level"] == "ok"
    # Sixteen more at 32 % of one CPU each on sixteen CPUs is 32 % of the machine, on top of 20 %.
    assert out["cpu_percent"] == pytest.approx(52.0) and out["cpu_level"] == "ok"
    assert load.project(cap=30, running=4, used_rss=6 * gib, used_cpu=0, machine=machine, cost=cost)["level"] == "warn"
    assert load.project(cap=40, running=4, used_rss=6 * gib, used_cpu=0, machine=machine, cost=cost)["level"] == "bad"
    # Past the cap already: nothing more is projected, and the count says how many there are.
    past = load.project(cap=2, running=4, used_rss=6 * gib, used_cpu=0, machine=machine, cost=cost)
    assert past["terminals_rss_bytes"] == 6 * gib and past["sessions"] == 4


def test_a_containers_limit_is_its_memory() -> None:
    gib = 1 << 30
    machine = {"mem_total_bytes": 64 * gib, "mem_available_bytes": 40 * gib, "cgroup_limit_bytes": 8 * gib, "cgroup_used_bytes": 6 * gib, "cpus": 16, "cgroup_cpus": 2.0}
    assert load.effective_memory(machine) == (8 * gib, 2 * gib)
    assert load.effective_cpus(machine) == 2.0


def test_a_windows_daemons_state_directory_is_remembered(tmp_path: Path, settings: object) -> None:
    # A native daemon on Windows reports its state directory with a drive letter; it must be sealed
    # like a Unix one, since the agent runs as the operator there.
    from daedalus.terminals.endpoint import remember_state_dir, sealed_state_dirs

    run = tmp_path / "hostrun"
    configured = settings.model_copy(update={"terminals_host_dir": run})  # type: ignore[attr-defined]
    remember_state_dir(run, "C:\\Users\\someone\\Daedalus\\data\\runtime\\ptyd\\state")
    try:
        assert [str(p) for p in sealed_state_dirs(configured)] == ["C:\\Users\\someone\\Daedalus\\data\\runtime\\ptyd\\state"]
    finally:
        remember_state_dir(run, "")
    remember_state_dir(run, "relative\\state")
    assert sealed_state_dirs(configured) == ()


def test_the_notes_about_owners_hold_without_unix_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.terminals import endpoint as ep

    monkeypatch.setattr(ep.os, "name", "nt")
    monkeypatch.delattr(ep.os, "getuid", raising=False)
    # Windows reports uid 0 for every file; the root-owned note would always fire there.
    assert ep.owned_by_root(tmp_path) == ""
    detail = ep.permission_detail(tmp_path / "token", PermissionError(13, "Access is denied"))
    assert "this user" in detail and "uid" not in detail


def test_a_launcher_without_a_daemon_says_why(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "unavailable").write_text("this build carries no ptyd; host terminals are unavailable\n")
    with pytest.raises(EndpointMissing) as missing:
        read_endpoint(run)
    assert missing.value.reason == "not_installed"
    assert missing.value.detail == "this build carries no ptyd; host terminals are unavailable"
