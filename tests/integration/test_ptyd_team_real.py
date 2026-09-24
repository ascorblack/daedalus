"""The daemon's bridge commands, the real ones, under the fake Claude Code: its team tools served by
``ptyd team-mcp`` from a per-launch MCP entry, and its command hooks run as ``ptyd hook <event>``,
including a permission answered through a held hook and a hook that outlives its launch.

The daemon around them is the test double (``LivePtyd``), which keeps the listener's contract; the
commands are the built binary. Needs ``DAEDALUS_INTEGRATION=1 DAEDALUS_PTYD_BIN=<path to ptyd>``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support.fake_cli.tui import read_log
from tests.support.harness_ports import Rig

BINARY = os.environ.get("DAEDALUS_PTYD_BIN", "")

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_PTYD_BIN to a built ptyd"),
    pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux"),
]

SESSION = "0b5c8a4e-3f7e-4d38-9a57-6f1f0b0e8a11"
HOOKS = ("SessionStart", "UserPromptSubmit", "PermissionRequest", "PostToolUse", "Stop")


def trust(rig: Rig) -> None:
    (rig.home / ".claude.json").write_text(json.dumps({"projects": {str(rig.work): {"hasTrustDialogAccepted": True}}}))


def command_hooks(*, hold_ms: int = 0, allow: list[str] | None = None) -> str:
    """A settings overlay whose hooks are all ``ptyd hook <event>``, as a CLI with command-only hooks
    is configured."""
    hooks = {}
    for event in HOOKS:
        wait = f" --wait-ms {hold_ms}" if event == "PermissionRequest" and hold_ms else ""
        hooks[event] = [{"hooks": [{"type": "command", "command": f'"$DAEDALUS_PTYD_BIN" hook {event}{wait}', "timeout": 30}]}]
    return json.dumps({"hooks": hooks, "permissions": {"allow": allow or []}})


def logged(rig: Rig, what: str) -> list[dict[str, Any]]:
    return [entry for entry in read_log(rig.log) if entry["event"] == what]


async def team_post(rig: Rig, tool: str) -> dict[str, Any]:
    """The first team post of a tool; the server's announcements that the tools loaded come first."""
    async with asyncio.timeout(30):
        while True:
            for event in rig.events:
                if event["type"] == "hook" and event["data"].get("name") == "team" and event["data"]["body"].get("tool") == tool:
                    return event
            await asyncio.sleep(0.05)


async def test_team_tools_through_the_real_team_mcp() -> None:
    async with Rig(ptyd_bin=Path(BINARY)) as rig:
        assert (rig.bin / "ptyd").resolve() == Path(BINARY).resolve()
        trust(rig)
        launch = await rig.register(hold_max_ms=20_000)
        # The adapter writes the daemon's path from the launch's environment, as here.
        mcp = {"mcpServers": {"daedalus_team": {"command": launch["env"]["DAEDALUS_PTYD_BIN"], "args": ["team-mcp"], "env": {"DAEDALUS_ASK_HOLD_MS": "20000"}}}}
        allow = ["mcp__daedalus_team__Report", "mcp__daedalus_team__AskOrchestrator"]
        term = await rig.spawn(
            ["claude", "--session-id", SESSION, "--settings", command_hooks(allow=allow), "--mcp-config", json.dumps(mcp),
             "--", "report:done:the task is finished; askorch:Which branch?|main|dev"],
            launch_id=launch["launch_id"],
        )
        report = await team_post(rig, "report")
        call_id = report["data"]["body"].pop("call_id")
        assert report["data"]["body"] == {"tool": "report", "kind": "done", "note": "the task is finished", "artifacts": []}
        assert call_id.startswith(f"{launch['launch_id']}:")
        assert report["data"]["reply_id"] and report["data"]["hold_ms"] == 15_000
        await rig.client.call("hooks.reply", {"reply_id": report["data"]["reply_id"], "body": {"text": "commit first", "error": True}})
        ask = await team_post(rig, "ask")
        ask["data"]["body"].pop("call_id")
        assert ask["data"]["body"] == {"tool": "ask", "question": "Which branch?", "options": ["main", "dev"]}
        assert ask["data"]["hold_ms"] == 20_000
        # The real server said the tools loaded, unheld, before any call.
        assert {h["body"]["stage"] for h in rig.hooks("team") if h["body"]["tool"] == "hello"} == {"initialize", "tools/list"}
        await rig.client.call("hooks.reply", {"reply_id": ask["data"]["reply_id"], "body": {"text": "dev"}})
        screen = await rig.screen_until(term, "AskOrchestrator: dev")
        assert "Report: commit first" in screen
        await rig.event("hook", where={"name": "Stop"})
        assert not logged(rig, "dialog_opened")


async def test_command_hooks_through_the_real_hook_command() -> None:
    async with Rig(ptyd_bin=Path(BINARY)) as rig:
        trust(rig)
        launch = await rig.register(hold_max_ms=10_000)
        await rig.spawn(["claude", "--session-id", SESSION, "--settings", command_hooks(hold_ms=10_000), "perm:npm install grammy"], launch_id=launch["launch_id"])
        start = await rig.event("hook", where={"name": "SessionStart"})
        assert start["data"]["body"]["hook_event_name"] == "SessionStart" and start["data"]["launch_id"] == launch["launch_id"]
        request = await rig.event("hook", where={"name": "PermissionRequest"})
        assert request["data"]["body"]["tool_input"]["command"] == "npm install grammy" and request["data"]["reply_id"]
        decision = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
        await rig.client.call("hooks.reply", {"reply_id": request["data"]["reply_id"], "body": decision})
        await rig.event("hook", where={"name": "Stop"})
        assert not [d for d in logged(rig, "dialog_opened") if d["kind"] == "permission"]

        # The launch ends under a running CLI: its hooks fail quietly (exit 0), never as a refusal.
        await rig.client.call("hooks.unregister_launch", {"launch_id": launch["launch_id"]})
        env = {**os.environ, "DAEDALUS_HOOK_URL": launch["hook_url"], "DAEDALUS_HOOK_TOKEN": launch["hook_token"]}
        code, out, err = await run([str(rig.bin / "ptyd"), "hook", "Stop"], env)
        assert code == 0 and out == b"" and b"410" in err
        code, _, _ = await run([str(rig.bin / "hook-post"), "Stop"], env)
        assert code == 2


async def run(argv: list[str], env: dict[str, str]) -> tuple[int, bytes, bytes]:
    """A command beside the CLI. Not ``subprocess.run``: the daemon's double listens on this event
    loop, and blocking it would make every post time out."""
    proc = await asyncio.create_subprocess_exec(*argv, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(b"{}"), 30)
    return proc.returncode or 0, out, err
