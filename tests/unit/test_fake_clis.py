"""The fake command-line agents, each started the way its adapter will start the real one — in a
terminal of the daemon's test double, with the real CLI's arguments — showing the screens, channels
and quirks the adapters will be tested against."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from daedalus.harness.capabilities import CAPABILITIES, parse_version, version_tested
from tests.support.fake_cli.tui import read_log
from tests.support.harness_ports import PtydTerminalPort, Rig

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")

CLAUDE_HOOKS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse", "Stop", "StopFailure", "Notification", "SessionEnd")
SESSION = "0b5c8a4e-3f7e-4d38-9a57-6f1f0b0e8a11"


def claude_settings(launch: dict[str, Any], *, hold_ms: int = 0, allow: list[str] | None = None) -> str:
    """The settings overlay the way the Claude adapter will write it: one http hook per event,
    posting to the launch's ingress under the event's name, with the token from the environment."""
    hooks = {}
    for event in CLAUDE_HOOKS:
        url = f"{launch['hook_url']}/{event}" + (f"?wait_ms={hold_ms}" if event == "PermissionRequest" and hold_ms else "")
        spec = {"type": "http", "url": url, "headers": {"Authorization": "Bearer $DAEDALUS_HOOK_TOKEN"}, "allowedEnvVars": ["DAEDALUS_HOOK_TOKEN"], "timeout": 30}
        hooks[event] = [{"hooks": [spec]}]
    return json.dumps({"hooks": hooks, "permissions": {"allow": allow or []}})


def trust(rig: Rig, folder: Path) -> None:
    """What accepting Claude's trust dialog once leaves behind."""
    (rig.home / ".claude.json").write_text(json.dumps({"projects": {str(folder): {"hasTrustDialogAccepted": True}}}))


def log_events(rig: Rig, what: str) -> list[dict[str, Any]]:
    return [entry for entry in read_log(rig.log) if entry["event"] == what]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def http(method: str, url: str, body: Any = None, password: str | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method, headers={"Content-Type": "application/json"})
    if password is not None:
        request.add_header("Authorization", "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode())
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        return exc.code, None


# -- every fake -------------------------------------------------------------------------------------------


async def test_every_fake_reports_a_version_inside_its_tested_range() -> None:
    expected = {"claude": r"(Claude Code)", "codex": "codex-cli ", "opencode": "", "pi": "", "grok": "grok "}
    async with Rig() as rig:
        for name, marker in expected.items():
            result = await rig.env_port.run([name, "--version"])
            assert result.exit_code == 0, (name, result.stderr)
            assert marker in result.stdout
            version = parse_version(result.stdout)
            assert version is not None and version_tested(CAPABILITIES[name], result.stdout), (name, result.stdout)


async def test_an_unknown_flag_fails_the_launch_as_the_real_cli_would() -> None:
    async with Rig() as rig:
        term = await rig.spawn(["claude", "--print", "hello"])
        exited = await rig.event("terminal.exited")
        assert exited["data"]["exit_code"] == 2
        assert "unknown option '--print'" in await term.screen()


async def test_the_update_path_moves_the_version() -> None:
    async with Rig(extra_env={"FAKE_GROK_LATEST": "1.0.41", "FAKE_CLAUDE_LATEST": "2.1.290"}) as rig:
        check = json.loads((await rig.env_port.run(["grok", "update", "--check", "--json"])).stdout)
        assert check == {**check, "currentVersion": "1.0.40", "latestVersion": "1.0.41", "updateAvailable": True}
        await rig.env_port.run(["grok", "update", "--version", "1.0.41"])
        assert "1.0.41" in (await rig.env_port.run(["grok", "--version"])).stdout
        updated = await rig.env_port.run(["claude", "update"])
        assert "Successfully updated from 2.1.281 to version 2.1.290" in updated.stdout
        assert (await rig.env_port.run(["claude", "--version"])).stdout.startswith("2.1.290")
        # OpenCode's bare upgrade goes to the next major version: the reason a target is always named.
        await rig.env_port.run(["opencode", "upgrade"])
        assert (await rig.env_port.run(["opencode", "--version"])).stdout.startswith("2.")


# -- Claude Code ------------------------------------------------------------------------------------------


async def test_a_claude_turn_with_hooks_runs_in_under_three_seconds() -> None:
    async with Rig() as rig:
        launch = await rig.register()
        started = time.monotonic()
        term = await rig.spawn(["claude", "--session-id", SESSION, "--settings", claude_settings(launch), "--permission-mode", "manual", "echo:hello"], launch_id=launch["launch_id"])
        screen = await rig.screen_until(term, "Do you trust the files in this folder")
        assert "❯ 1. Yes, proceed" in screen
        assert rig.hooks() == []  # nothing is posted before trust is accepted
        await term.write(keys=["Enter"])
        stop = await rig.event("hook", where={"name": "Stop"})
        elapsed = time.monotonic() - started
        assert elapsed < 3.0, f"a fake turn took {elapsed:.2f} s"
        assert [h["name"] for h in rig.hooks()] == ["SessionStart", "UserPromptSubmit", "Stop"]
        start = rig.hooks("SessionStart")[0]["body"]
        assert start["session_id"] == SESSION and start["source"] == "startup" and start["permission_mode"] == "default"
        assert rig.hooks("UserPromptSubmit")[0]["body"]["prompt"] == "echo:hello"
        assert stop["data"]["body"]["last_assistant_message"] == "hello"
        records = [json.loads(line) for line in Path(start["transcript_path"]).read_text().splitlines()]
        assert [r["type"] for r in records] == ["user", "assistant"]
        assert records[1]["message"]["content"][0]["text"] == "hello" and records[1]["message"]["usage"]["output_tokens"] > 0
        agents = json.loads((await rig.env_port.run(["claude", "agents", "--json"])).stdout)
        assert [(a["sessionId"], a["status"]) for a in agents] == [(SESSION, "idle")]
        await term.write(paste="/exit")
        await term.write(keys=["Enter"])
        await rig.event("hook", where={"name": "SessionEnd"})
        exited = await rig.event("terminal.exited")
        assert exited["data"]["exit_code"] == 0


async def test_claude_asks_trust_once_per_folder_and_refuses_a_used_session_id() -> None:
    async with Rig() as rig:
        launch = await rig.register()
        first = await rig.spawn(["claude", "--session-id", SESSION, "--settings", claude_settings(launch)], launch_id=launch["launch_id"])
        await rig.screen_until(first, "trust the files")
        await first.write(text="1")
        await rig.event("hook", where={"name": "SessionStart"})
        second_launch = await rig.register()
        again = await rig.spawn(["claude", "--session-id", "5f0b8b8e-1111-4d38-9a57-6f1f0b0e8a11", "--settings", claude_settings(second_launch)], launch_id=second_launch["launch_id"])
        await rig.event("hook", where={"name": "SessionStart", "launch_id": second_launch["launch_id"]})
        assert "trust" not in await again.screen()
        reused = await rig.spawn(["claude", "--session-id", SESSION])
        await rig.event("terminal.exited", where={"exit_code": 2})
        assert "already in use" in await reused.screen()


async def test_claude_permission_answered_by_a_held_hook_never_shows_a_dialog() -> None:
    async with Rig() as rig:
        trust(rig, rig.work)
        launch = await rig.register(hold_max_ms=10_000)
        await rig.spawn(["claude", "--session-id", SESSION, "--settings", claude_settings(launch, hold_ms=10_000), "perm:npm install grammy"], launch_id=launch["launch_id"])
        request = await rig.event("hook", where={"name": "PermissionRequest"})
        assert request["data"]["body"]["tool_name"] == "Bash" and request["data"]["body"]["tool_input"]["command"] == "npm install grammy"
        decision = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
        await rig.client.call("hooks.reply", {"reply_id": request["data"]["reply_id"], "status": 200, "body": decision})
        await rig.event("hook", where={"name": "Stop"})
        assert [h["name"] for h in rig.hooks()][-3:] == ["PermissionRequest", "PostToolUse", "Stop"]
        assert not [d for d in log_events(rig, "dialog_opened") if d["kind"] == "permission"]


async def test_claude_permission_dialog_answered_with_keys_and_its_late_notification() -> None:
    async with Rig(extra_env={"FAKE_CLI_TIME_SCALE": "0.02"}) as rig:
        trust(rig, rig.work)
        launch = await rig.register()
        term = await rig.spawn(["claude", "--session-id", SESSION, "--settings", claude_settings(launch), "perm:rm -rf build"], launch_id=launch["launch_id"])
        screen = await rig.screen_until(term, "Do you want to proceed")
        assert "rm -rf build" in screen and "❯ 1. Yes" in screen
        notice = await rig.event("hook", where={"name": "Notification"})
        assert notice["data"]["body"]["notification_type"] == "permission_prompt"
        agents = json.loads((await rig.env_port.run(["claude", "agents", "--json"])).stdout)
        assert agents[0]["status"] == "waiting" and agents[0]["waitingFor"] == "dialog open"
        await term.write(keys=["Down", "Down", "Enter"])  # "No, and tell Claude what to do differently"
        await rig.event("hook", where={"name": "Stop"})
        await rig.screen_until(term, "I did not run rm -rf build")
        # The idle notification comes a (scaled) minute after the turn and is only a notification.
        await wait(lambda: any(h["body"]["notification_type"] == "idle_prompt" for h in rig.hooks("Notification")))


async def test_claude_queues_a_message_typed_while_busy_and_esc_interrupts_without_stop() -> None:
    async with Rig() as rig:
        trust(rig, rig.work)
        launch = await rig.register()
        # Long enough that the turn cannot end on its own during the test: it ends by the Esc below.
        term = await rig.spawn(["claude", "--session-id", SESSION, "--settings", claude_settings(launch), "slow:1000"], launch_id=launch["launch_id"])
        await rig.screen_until(term, "Read\\(")
        await term.write(paste="echo:steered in")
        await term.write(keys=["Enter"])
        await rig.screen_until(term, "queued: echo:steered in")
        await wait(lambda: len(rig.hooks("UserPromptSubmit")) == 2)
        assert rig.hooks("UserPromptSubmit")[1]["body"]["prompt"] == "echo:steered in"
        await rig.screen_until(term, "● steered in")  # injected into the running turn
        await term.write(keys=["Esc"])
        await wait(lambda: bool(log_events(rig, "turn_ended")))
        assert log_events(rig, "turn_ended")[0]["outcome"] == "cancelled"
        await rig.screen_until(term, "Interrupted by user")
        assert rig.hooks("Stop") == []  # one turn, interrupted: no Stop at all
        transcript = Path(rig.hooks("SessionStart")[0]["body"]["transcript_path"]).read_text()
        assert "[Request interrupted by user]" in transcript
        # Esc twice on an idle composer opens the rewind dialog: an adapter sends it once.
        await term.write(keys=["Esc"])
        await term.write(keys=["Esc"])
        await rig.screen_until(term, "Rewind")


async def test_claude_paste_collapse_burst_guard_and_the_swallowed_enter() -> None:
    async with Rig(extra_env={"FAKE_CLAUDE_BURST_MS": "400", "FAKE_CLI_FAULTS": "swallow_enter_once"}) as rig:
        trust(rig, rig.work)
        launch = await rig.register()
        term = await rig.spawn(["claude", "--session-id", SESSION, "--settings", claude_settings(launch)], launch_id=launch["launch_id"])
        await rig.event("hook", where={"name": "SessionStart"})
        long = "echo:" + "x" * 900 + "\nsecond line\nthird line"
        # The paste and its Enter in one write, so they reach the TUI together however loaded the
        # machine is: the Enter is inside the burst window by construction.
        await term.write(text=f"\x1b[200~{long}\x1b[201~\r")
        await wait(lambda: len(log_events(rig, "enter_swallowed")) == 1)
        screen = await rig.screen_until(term, r"\[Pasted text #1 \+3 lines\]")
        await asyncio.sleep(0.5)  # a lower bound past the window, counted from after the TUI had it
        await term.write(keys=["Enter"])  # the fault: swallowed once more
        await wait(lambda: len(log_events(rig, "enter_swallowed")) == 2)
        assert "[Pasted text #1" in await term.screen()
        await term.write(keys=["Enter"])
        await rig.event("hook", where={"name": "UserPromptSubmit"})
        assert rig.hooks("UserPromptSubmit")[0]["body"]["prompt"] == long  # the marker is only on screen
        assert [e["reason"] for e in log_events(rig, "enter_swallowed")] == ["burst", "fault"]
        assert len(log_events(rig, "submitted")) == 1
        assert "Pasted text" in screen


async def test_a_dialog_during_a_paste_catches_an_enter() -> None:
    async with Rig(extra_env={"FAKE_CLI_FAULTS": "dialog_during_paste"}) as rig:
        trust(rig, rig.work)
        launch = await rig.register()
        term = await rig.spawn(["claude", "--session-id", SESSION, "--settings", claude_settings(launch)], launch_id=launch["launch_id"])
        await rig.event("hook", where={"name": "SessionStart"})
        await term.write(paste="echo:lost")
        await rig.screen_until(term, "a notice opened while you were pasting")
        await term.write(keys=["Enter"])
        await wait(lambda: bool(log_events(rig, "enter_into_dialog")))
        assert not log_events(rig, "submitted")


async def test_claude_team_tools_go_through_the_launchs_mcp_server() -> None:
    async with Rig() as rig:
        trust(rig, rig.work)
        launch = await rig.register(hold_max_ms=10_000)
        mcp = {"mcpServers": {"daedalus_team": {"command": str(rig.bin / "ptyd"), "args": ["team-mcp"], "env": {"DAEDALUS_ASK_HOLD_MS": "10000"}}}}
        allow = ["mcp__daedalus_team__Report", "mcp__daedalus_team__AskOrchestrator"]
        term = await rig.spawn(
            ["claude", "--session-id", SESSION, "--settings", claude_settings(launch, allow=allow), "--mcp-config", json.dumps(mcp), "report:done:the task is finished; askorch:Which branch?|main|dev"],
            launch_id=launch["launch_id"],
        )
        report = await rig.event("hook", where={"name": "team"})
        assert report["data"]["body"] == {"tool": "report", "kind": "done", "note": "the task is finished", "artifacts": []}
        await rig.client.call("hooks.reply", {"reply_id": report["data"]["reply_id"], "status": 200, "body": {"text": "recorded"}})
        ask = await rig.event("hook", where={"name": "team"}, after=rig.events.index(report) + 1)
        assert ask["data"]["body"]["tool"] == "ask" and ask["data"]["body"]["options"] == ["main", "dev"] and ask["data"]["reply_id"]
        await rig.client.call("hooks.reply", {"reply_id": ask["data"]["reply_id"], "status": 200, "body": {"text": "dev"}})
        await rig.screen_until(term, "AskOrchestrator: dev")
        assert not log_events(rig, "dialog_opened")  # allowed by rule: no permission dialog


async def test_claude_says_what_blocks_it_before_it_is_ready() -> None:
    async with Rig(extra_env={"FAKE_CLAUDE_LOGGED_IN": "0"}) as rig:
        term = await rig.spawn(["claude", "--session-id", SESSION])
        await rig.screen_until(term, "Select login method")
        status = await rig.env_port.run(["claude", "auth", "status", "--json"])
        assert status.exit_code == 1 and json.loads(status.stdout)["loggedIn"] is False
    async with Rig() as rig:
        trust(rig, rig.work)
        term = await rig.spawn(["claude", "--session-id", SESSION, "--permission-mode", "bypassPermissions"])
        screen = await rig.screen_until(term, "Bypass Permissions mode")
        assert "❯ 1. No, exit" in screen  # the default row refuses
        refused = await rig.spawn(["claude", "--permission-mode", "default"])
        await rig.event("terminal.exited", where={"exit_code": 2})
        assert "manual" in await refused.screen()


async def test_claude_faults_crash_and_silence() -> None:
    async with Rig(extra_env={"FAKE_CLI_FAULTS": "exit_after:1,no_stop_hook,slow_ready:300"}) as rig:
        trust(rig, rig.work)
        launch = await rig.register()
        started = time.monotonic()
        await rig.spawn(["claude", "--session-id", SESSION, "--settings", claude_settings(launch), "echo:bye"], launch_id=launch["launch_id"])
        await rig.event("hook", where={"name": "SessionStart"})
        assert time.monotonic() - started >= 0.3
        exited = await rig.event("terminal.exited")
        assert exited["data"]["exit_code"] == 3
        assert [h["name"] for h in rig.hooks()] == ["SessionStart", "UserPromptSubmit"]  # no Stop, no SessionEnd


async def test_a_silent_turn_ends_only_on_screen() -> None:
    async with Rig() as rig:
        trust(rig, rig.work)
        launch = await rig.register()
        term = await rig.spawn(["claude", "--session-id", SESSION, "--settings", claude_settings(launch), "silent"], launch_id=launch["launch_id"])
        await rig.screen_until(term, "esc to interrupt")
        await rig.screen_until(term, r"worked quietly[\s\S]*\? for shortcuts")
        assert [h["name"] for h in rig.hooks()] == ["SessionStart", "UserPromptSubmit"]


async def test_without_bracketed_paste_a_pasted_line_feed_submits() -> None:
    async with Rig(extra_env={"FAKE_CLI_FAULTS": "no_2004"}) as rig:
        trust(rig, rig.work)
        launch = await rig.register()
        term = await rig.spawn(["claude", "--session-id", SESSION, "--settings", claude_settings(launch)], launch_id=launch["launch_id"])
        await rig.event("hook", where={"name": "SessionStart"})
        assert (await term.modes())["bracketed_paste"] is False
        await term.write(paste="echo:one\necho:two")
        await rig.event("hook", where={"name": "UserPromptSubmit"})
        assert rig.hooks("UserPromptSubmit")[0]["body"]["prompt"] == "echo:one"


# -- Codex ------------------------------------------------------------------------------------------------


class CodexClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.next = 0
        self.notes: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.replies: dict[int, dict[str, Any]] = {}
        self.changed = asyncio.Event()
        self.task = asyncio.create_task(self.read())

    @classmethod
    async def connect(cls, path: str) -> CodexClient:
        reader, writer = await asyncio.open_unix_connection(path, limit=1 << 22)
        client = cls(reader, writer)
        await client.call("initialize", {"clientInfo": {"name": "daedalus", "version": "test"}})
        writer.write(b'{"method": "initialized"}\n')
        return client

    async def read(self) -> None:
        while line := await self.reader.readline():
            message = json.loads(line)
            if "method" in message and "id" in message:
                self.requests.append(message)
            elif "method" in message:
                self.notes.append(message)
            else:
                self.replies[message["id"]] = message
            self.changed.set()
            self.changed = asyncio.Event()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next += 1
        ident = self.next
        self.writer.write((json.dumps({"id": ident, "method": method, "params": params}) + "\n").encode())
        await self.until(lambda: ident in self.replies)
        return self.replies[ident]

    async def until(self, predicate: Any, timeout: float = 30) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await self.changed.wait()

    def note(self, method: str, **where: Any) -> dict[str, Any] | None:
        return next((n for n in self.notes if n["method"] == method and all(n["params"].get(k) == v for k, v in where.items())), None)

    def answer(self, request: dict[str, Any], result: dict[str, Any]) -> None:
        self.writer.write((json.dumps({"id": request["id"], "result": result}) + "\n").encode())

    async def close(self) -> None:
        self.writer.close()
        self.task.cancel()


async def codex_session(rig: Rig, *, approvals: str = "all", trusted: bool = True, update_check: bool = False) -> tuple[CodexClient, PtydTerminalPort, str]:
    launch = await rig.register()
    sock = f"{launch['dial_dir']}/codex.sock"
    overrides = []
    if trusted:
        overrides += ["-c", f'projects."{rig.work}".trust_level="trusted"']
    if not update_check:
        overrides += ["-c", "check_for_update_on_startup=false"]
    server = await rig.spawn(["codex", "app-server", "--listen", f"unix://{sock}", *overrides], launch_id=launch["launch_id"], env={"FAKE_CODEX_APPROVALS": approvals})
    await rig.screen_until(server, "listening on unix://")
    client = await CodexClient.connect(sock)
    started = await client.call("thread/start", {"cwd": str(rig.work), "approvalPolicy": "on-request", "sandbox": "workspace-write"})
    thread = started["result"]["thread"]["id"]
    tui = await rig.spawn(["codex", "--remote", f"unix://{sock}", "resume", thread], launch_id=launch["launch_id"])
    return client, tui, thread


async def test_codex_turn_steer_and_acknowledgement_by_client_id() -> None:
    async with Rig() as rig:
        client, tui, thread = await codex_session(rig)
        await rig.screen_until(tui, f"resumed thread {thread}")
        # A turn long enough never to end by itself here: it is interrupted once the steer is in.
        started = await client.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "slow:1000"}], "clientUserMessageId": "msg-1"})
        turn = started["result"]["turn"]["id"]
        await client.until(lambda: any(n["method"] == "item/started" and n["params"]["item"]["type"] == "commandExecution" for n in client.notes))
        ack = next(n for n in client.notes if n["method"] == "item/started" and n["params"]["item"]["type"] == "userMessage")
        assert ack["params"]["item"]["clientId"] == "msg-1"
        wrong = await client.call("turn/steer", {"threadId": thread, "input": [{"type": "text", "text": "x"}], "expectedTurnId": "not-it"})
        assert "precondition" in wrong["error"]["message"]
        steered = await client.call("turn/steer", {"threadId": thread, "input": [{"type": "text", "text": "echo:steered"}], "expectedTurnId": turn, "clientUserMessageId": "msg-2"})
        assert steered["result"]["turnId"] == turn
        await client.until(lambda: any(n["method"] == "item/started" and n["params"]["item"].get("clientId") == "msg-2" for n in client.notes))
        await client.until(lambda: any(n["method"] == "item/completed" and n["params"]["item"].get("text") == "steered" for n in client.notes))
        await rig.screen_until(tui, "• steered")
        await client.call("turn/interrupt", {"threadId": thread, "turnId": turn})
        await client.until(lambda: client.note("turn/completed") is not None)
        interrupted = client.note("turn/completed")
        assert interrupted is not None and interrupted["params"]["turn"]["status"] == "interrupted"
        items = await client.call("thread/items/list", {"threadId": thread})
        assert "steered" in [i.get("text") for i in items["result"]["data"] if i["type"] == "agentMessage"]
        done = await client.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "echo:done"}]})
        done_id = done["result"]["turn"]["id"]
        await client.until(lambda: any(n["method"] == "turn/completed" and n["params"]["turn"]["id"] == done_id for n in client.notes))
        completed = next(n for n in client.notes if n["method"] == "turn/completed" and n["params"]["turn"]["id"] == done_id)
        assert completed["params"]["turn"]["status"] == "completed"
        assert client.note("thread/tokenUsage/updated") is not None
        await client.close()


@pytest.mark.parametrize("approvals", ["all", "owner"])
async def test_codex_approval_requests_reach_the_clients_the_mode_names(approvals: str) -> None:
    async with Rig() as rig:
        client, tui, thread = await codex_session(rig, approvals=approvals)
        await rig.screen_until(tui, "resumed thread")
        await client.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "perm:make deploy"}]})
        await client.until(lambda: bool(client.requests))
        request = client.requests[0]
        assert request["method"] == "item/commandExecution/requestApproval" and request["params"]["command"] == "make deploy"
        await client.until(lambda: any(n["params"]["status"].get("activeFlags") == ["waitingOnApproval"] for n in client.notes if n["method"] == "thread/status/changed"))
        if approvals == "all":
            await rig.screen_until(tui, "Would you like to run the following command")
        client.answer(request, {"decision": "accept"})
        await client.until(lambda: client.note("serverRequest/resolved") is not None)
        await client.until(lambda: client.note("turn/completed") is not None)
        screen = await rig.screen_until(tui, "• Ran make deploy")
        assert "Would you like to run" not in screen  # the TUI's dialog closed when the host answered
        await client.close()


async def test_codex_answered_in_the_tui_resolves_for_the_host() -> None:
    async with Rig() as rig:
        client, tui, thread = await codex_session(rig)
        await rig.screen_until(tui, "resumed thread")
        await client.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "perm:ls"}]})
        await rig.screen_until(tui, "Would you like to run the following command")
        await tui.write(text="3")  # declined in the terminal
        await client.until(lambda: client.note("serverRequest/resolved") is not None)
        await client.until(lambda: client.note("turn/completed") is not None)
        declined = next(n for n in client.notes if n["method"] == "item/completed" and n["params"]["item"]["type"] == "commandExecution")
        assert declined["params"]["item"]["status"] == "declined"
        await client.close()


async def test_codex_trust_and_update_prompts_unless_the_launch_answers_them() -> None:
    async with Rig(extra_env={"FAKE_CODEX_LATEST": "0.156.1"}) as rig:
        _, tui, _ = await codex_session(rig, trusted=False, update_check=True)
        await rig.screen_until(tui, f"You are running Codex in {rig.work}")
        await tui.write(text="1")
        await rig.screen_until(tui, r"Update available! 0\.155\.1 -> 0\.156\.1")
        await tui.write(text="2")
        await rig.screen_until(tui, "resumed thread")


# -- OpenCode ---------------------------------------------------------------------------------------------


async def read_sse(port: int, password: str, events: list[dict[str, Any]]) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    auth = base64.b64encode(f"opencode:{password}".encode()).decode()
    writer.write(f"GET /event HTTP/1.1\r\nHost: x\r\nAuthorization: Basic {auth}\r\n\r\n".encode())
    try:
        while line := await reader.readline():
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:]))
    finally:
        writer.close()


async def test_opencode_server_events_prompt_and_permission_reply() -> None:
    async with Rig() as rig:
        port = free_port()
        term = await rig.spawn(["opencode", str(rig.work), "--port", str(port), "--hostname", "127.0.0.1"], env={"OPENCODE_SERVER_PASSWORD": "pw"})
        await rig.screen_until(term, "enter send")
        base = f"http://127.0.0.1:{port}"
        assert (await asyncio.to_thread(http, "GET", f"{base}/global/health"))[0] == 401
        status, health = await asyncio.to_thread(http, "GET", f"{base}/global/health", None, "pw")
        assert status == 200 and health["healthy"] is True
        events: list[dict[str, Any]] = []
        sse = asyncio.create_task(read_sse(port, "pw", events))
        await wait(lambda: bool(events))
        assert events[0]["type"] == "server.connected"
        _, session = await asyncio.to_thread(http, "POST", f"{base}/session", {"title": "staff"}, "pw")
        await asyncio.to_thread(http, "POST", f"{base}/tui/select-session", {"sessionID": session["id"]}, "pw")
        await asyncio.to_thread(http, "POST", f"{base}/tui/append-prompt", {"text": "perm:npm test"}, "pw")
        await rig.screen_until(term, "perm:npm test")  # the operator sees the text arrive
        await asyncio.to_thread(http, "POST", f"{base}/tui/submit-prompt", {}, "pw")
        asked = await wait_event(events, "permission.asked")
        user = next(e for e in events if e["type"] == "message.updated" and e["properties"]["info"]["role"] == "user")
        assert user["properties"]["info"]["sessionID"] == session["id"]
        assert any(e["type"] == "session.status" and e["properties"]["status"]["type"] == "busy" for e in events)
        status, _ = await asyncio.to_thread(http, "POST", f"{base}/permission/{asked['properties']['id']}/reply", {"reply": "once"}, "pw")
        assert status == 200
        await wait_event(events, "session.idle")
        assert [e["properties"]["reply"] for e in events if e["type"] == "permission.replied"] == ["once"]
        _, messages = await asyncio.to_thread(http, "GET", f"{base}/session/{session['id']}/message", None, "pw")
        assert [m["info"]["role"] for m in messages] == ["user", "assistant"]
        sse.cancel()
        # After the process is gone the conversation is still there through export.
        await rig.client.call("terminal.kill", {"id": term.id})
        exported = json.loads((await rig.env_port.run(["opencode", "export", session["id"]])).stdout)
        assert [m["info"]["role"] for m in exported["messages"]] == ["user", "assistant"]


async def test_opencode_has_no_steering_and_a_taken_port_ends_it() -> None:
    async with Rig() as rig:
        port = free_port()
        term = await rig.spawn(["opencode", str(rig.work), "--port", str(port)])
        await rig.screen_until(term, "enter send")
        base = f"http://127.0.0.1:{port}"
        _, session = await asyncio.to_thread(http, "POST", f"{base}/session", {})
        await asyncio.to_thread(http, "POST", f"{base}/session/{session['id']}/prompt_async", {"parts": [{"type": "text", "text": "slow:1000"}]})
        await rig.screen_until(term, "Read\\(")
        await asyncio.to_thread(http, "POST", f"{base}/session/{session['id']}/prompt_async", {"parts": [{"type": "text", "text": "echo:after the turn"}]})
        await wait(lambda: len(log_events(rig, "submitted")) == 2)
        await rig.screen_until(term, r"file2\.txt")  # the turn went on past a tool boundary without it
        assert "● after the turn" not in await term.screen()  # only shown as queued
        await asyncio.to_thread(http, "POST", f"{base}/session/{session['id']}/abort", {})
        await rig.screen_until(term, "● after the turn")
        screen = (await term.screen()).splitlines()
        interrupted = next(i for i, line in enumerate(screen) if "Interrupted by user" in line)
        assert interrupted < next(i for i, line in enumerate(screen) if "● after the turn" in line)  # queued, not steered
        clash = await rig.spawn(["opencode", str(rig.work), "--port", str(port)])
        await rig.event("terminal.exited", where={"exit_code": 1})
        assert f"Failed to start server on port {port}" in await clash.screen()


# -- pi ---------------------------------------------------------------------------------------------------


async def test_pi_bridge_sends_steers_and_acknowledges_by_id() -> None:
    async with Rig() as rig:
        launch = await rig.register(files={"pi_bridge.ts": b"// the bridge extension"})
        term = await rig.spawn(["pi", "--session-id", "s-1", "-e", f"{launch['dir']}/pi_bridge.ts", "--name", "Ada", "--thinking", "low"], launch_id=launch["launch_id"])
        ready = await rig.event("hook", where={"name": "pi"})
        assert ready["data"]["body"]["event"] == "ready" and ready["data"]["body"]["sessionId"] == "s-1"
        session_file = Path(ready["data"]["body"]["sessionFile"])
        reader, writer = await asyncio.open_unix_connection(f"{launch['dial_dir']}/pi.sock")

        async def op(message: dict[str, Any]) -> dict[str, Any]:
            writer.write((json.dumps(message) + "\n").encode())
            return dict(json.loads(await reader.readline()))

        assert (await op({"op": "send", "id": "m1", "text": "slow:1000"}))["ok"]
        await rig.screen_until(term, "Read\\(")
        assert (await op({"op": "state"}))["idle"] is False
        await op({"op": "send", "id": "m2", "text": "echo:steered", "deliverAs": "steer"})
        await rig.screen_until(term, "● steered")  # taken in after a tool call, inside the turn
        assert (await op({"op": "abort"}))["ok"]
        await wait_hook(rig, "agent_settled")
        inputs = [(h["body"]["id"], h["body"]["source"]) for h in rig.hooks("pi") if h["body"]["event"] == "input"]
        assert inputs == [("m1", "extension"), ("m2", "extension")]
        entries = [json.loads(line) for line in session_file.read_text().splitlines()]
        assert entries[0]["type"] == "session" and entries[0]["version"] == 3
        assert any(e.get("message", {}).get("role") == "toolResult" for e in entries)
        writer.close()


async def test_pi_under_tmux_loses_every_enter_and_credentials_are_a_trap() -> None:
    async with Rig() as rig:
        term = await rig.spawn(["pi", "--approve"], env={"TMUX": "/tmp/tmux-0/default"})
        await rig.screen_until(term, "enter send")
        await term.write(paste="echo:never")
        await term.write(keys=["Enter"])
        await wait(lambda: bool(log_events(rig, "enter_swallowed")))
        assert [e["reason"] for e in log_events(rig, "enter_swallowed")] == ["environment"]
        assert not log_events(rig, "submitted")
        check = await rig.env_port.run(["pi", "auth", "check", "--provider", "anthropic", "--json", "--no-refresh"])
        assert json.loads(check.stdout) == {"provider": "anthropic", "authenticated": True}
        await rig.env_port.run(["pi", "auth", "check", "--provider", "anthropic", "--credentials"])
        assert log_events(rig, "credentials_printed")


# -- Grok Build -------------------------------------------------------------------------------------------


GROK_SESSION = "8c1d7a60-2f7e-4d38-9a57-6f1f0b0e8a11"


async def test_grok_session_files_and_the_preselected_permission_row() -> None:
    async with Rig() as rig:
        term = await rig.spawn(["grok", "--cwd", str(rig.work), "-s", GROK_SESSION, "--trust", "perm:rm -rf dist"])
        screen = await rig.screen_until(term, r"Allow Bash\?")
        assert "❯ 3. Always allow on all sessions" in screen  # the default nobody should get
        await term.write(keys=["Esc"])
        await rig.screen_until(term, "I did not run rm -rf dist")
        directory = next((rig.home / ".grok" / "sessions").glob(f"*/{GROK_SESSION}"))
        assert json.loads((directory / "summary.json").read_text())["sessionId"] == GROK_SESSION
        updates = [json.loads(line)["update"]["sessionUpdate"] for line in (directory / "updates.jsonl").read_text().splitlines()]
        assert updates[0] == "user_message_chunk" and "agent_message_chunk" in updates
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        requested = next(e for e in events if e["type"] == "permission_requested")
        assert requested["selected"] == "allow_always" and requested["command"] == "rm -rf dist"
    async with Rig() as rig:
        term = await rig.spawn(["grok", "--cwd", str(rig.work), "-s", GROK_SESSION, "--trust", "perm:ls"], env={"GROK_DEFAULT_SELECTED_PERMISSION": "allow_once"})
        assert "❯ 1. Allow once" in await rig.screen_until(term, r"Allow Bash\?")
        await term.write(text="1")  # no digit shortcuts in this dialog
        await wait(lambda: bool(log_events(rig, "typed_into_dialog")))
        assert "Allow Bash?" in await term.screen()
        await term.write(keys=["Enter"])
        await rig.screen_until(term, "Ran ls")


async def test_grok_cancels_and_sends_and_runs_hooks_from_its_agent_file() -> None:
    async with Rig() as rig:
        launch = await rig.register()
        hook = {"type": "command", "command": f"{rig.bin / 'hook-post'} grok"}
        agent = rig.work / "daedalus-staff.md"
        hooks = {name: [{"hooks": [hook]}] for name in ("SessionStart", "UserPromptSubmit", "Stop", "StopCancelled")}
        agent.write_text(f"---\nname: staff\nhooks: {json.dumps(hooks)}\n---\nYou are staff.\n")
        term = await rig.spawn(["grok", "--cwd", str(rig.work), "-s", GROK_SESSION, "--trust", "--agent", str(agent), "slow:1000"], launch_id=launch["launch_id"])
        await rig.screen_until(term, "Read\\(")
        await term.write(paste="echo:instead")
        await term.write(keys=["Enter"])
        await rig.screen_until(term, "● instead")
        await wait(lambda: any(h["body"].get("hook_event_name") == "Stop" for h in rig.hooks("grok")))
        names = [h["body"]["hook_event_name"] for h in rig.hooks("grok")]
        assert names == ["SessionStart", "UserPromptSubmit", "StopCancelled", "UserPromptSubmit", "Stop"]
        assert rig.hooks("grok")[-1]["body"]["hookEventName"] == "stop"
        assert log_events(rig, "cancel_and_send")


async def test_grok_runs_the_operators_claude_hooks_unless_told_not_to() -> None:
    async with Rig() as rig:
        (rig.home / ".claude").mkdir()
        (rig.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}))
        term = await rig.spawn(["grok", "--cwd", str(rig.work), "-s", GROK_SESSION, "--trust", "echo:a"])
        await rig.screen_until(term, "● a")
        await wait(lambda: bool(log_events(rig, "claude_hooks_ran")))
        await rig.spawn(["grok", "--cwd", str(rig.work), "-s", "9c1d7a60-2f7e-4d38-9a57-6f1f0b0e8a11", "--trust", "echo:b"], env={"GROK_COMPAT_CLAUDE_HOOKS": "0"})
        await wait(lambda: len(log_events(rig, "turn_ended")) == 2)  # its Stop hooks, if any, have run
        assert len(log_events(rig, "claude_hooks_ran")) == 1


# -- helpers ----------------------------------------------------------------------------------------------


async def wait(predicate: Any, timeout: float = 30) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def wait_event(events: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    await wait(lambda: any(e["type"] == kind for e in events))
    return next(e for e in events if e["type"] == kind)


async def wait_hook(rig: Rig, bridge_event: str) -> dict[str, Any]:
    await wait(lambda: any(h["body"].get("event") == bridge_event for h in rig.hooks("pi")))
    return next(h for h in rig.hooks("pi") if h["body"].get("event") == bridge_event)
