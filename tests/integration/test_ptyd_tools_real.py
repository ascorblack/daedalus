"""The browser's tool set through the real ``ptyd tools-mcp``: a fake Claude Code calls BrowserOpen,
BrowserSnapshot and BrowserAct from its per-launch MCP entry, each call arrives as a held ``tools``
post, and the host answers it with the same browser operations a Daedalus session's tools run —
here against the in-process browser daemon. The purchase is held until the operator's grant.

The terminal daemon around the commands is the test double (``LivePtyd``); the command is the built
binary. Needs ``DAEDALUS_INTEGRATION=1 DAEDALUS_PTYD_BIN=<path to ptyd>``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from daedalus.browser.agent import BrowserAgent, Caller, SensitiveAsk
from daedalus.browser.cli import SERVER, tools_file
from daedalus.browser.model import Owner
from daedalus.browser.service import Browsers
from daedalus.config import BrowserConfig
from daedalus.stores.database import Database
from tests.support.fake_browserd import FakeBrowserd
from tests.support.fake_cli.tui import read_log
from tests.support.harness_ports import Rig
from tests.unit.test_browser_tools import shop

BINARY = os.environ.get("DAEDALUS_PTYD_BIN", "")

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_PTYD_BIN to a built ptyd"),
    pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux"),
]

SESSION = "0b5c8a4e-3f7e-4d38-9a57-6f1f0b0e8a12"


class Everyone:
    async def exists(self, owner: Owner) -> bool:
        return True

    async def label(self, owner: Owner) -> str:
        return "Ada"


class NoFiles:
    async def read(self, path: str) -> tuple[str, bytes]:
        raise AssertionError("nothing is uploaded here")

    async def save(self, name: str, data: bytes, to: str | None) -> str:
        raise AssertionError("nothing is downloaded here")


async def test_a_cli_browses_through_the_real_tools_mcp_and_waits_for_the_grant(db: Database) -> None:
    base = Path(tempfile.mkdtemp(prefix="bd-"))
    daemon = await FakeBrowserd(base / "b").start()
    shop(daemon)
    service = Browsers(db, run_dirs={"container": base / "b", "host": None}, config=lambda: BrowserConfig(control_wait_seconds=0.2), owners=Everyone())  # type: ignore[arg-type]
    await service.start()
    agent = BrowserAgent(service)
    asked: list[SensitiveAsk] = []
    granted = asyncio.Event()

    async def gate(ask: SensitiveAsk) -> tuple[bool, str]:
        asked.append(ask)
        await granted.wait()
        return True, ""

    caller = Caller(owner=Owner("staff", "m1", project_id="p1", staff_id="m1"), actor="staff:Ada", gate=gate, files=NoFiles())
    try:
        assert await service.wait_available("container")
        async with Rig(ptyd_bin=Path(BINARY)) as rig:
            (rig.home / ".claude.json").write_text(json.dumps({"projects": {str(rig.work): {"hasTrustDialogAccepted": True}}}))
            launch = await rig.register(files={"tools/browser.json": tools_file(20_000)}, hold_max_ms=30_000)
            mcp = {"mcpServers": {SERVER: {"command": launch["env"]["DAEDALUS_PTYD_BIN"], "args": ["tools-mcp", "--set", "browser"], "env": {"DAEDALUS_TOOLS_HOLD_MS": "20000"}}}}
            allow = [f"mcp__{SERVER}__{name}" for name in ("BrowserOpen", "BrowserSnapshot", "BrowserAct")]
            script = "; ".join(
                (
                    'mcp:daedalus_browser:BrowserOpen:{"url": "https://shop.test/cart"}',
                    "mcp:daedalus_browser:BrowserSnapshot:{}",
                    'mcp:daedalus_browser:BrowserAct:{"action": "click", "ref": "e20", "element": "the Buy now button"}',
                )
            )
            await rig.spawn(["claude", "--session-id", SESSION, "--settings", json.dumps({"permissions": {"allow": allow}}), "--mcp-config", json.dumps(mcp), "--", script], launch_id=launch["launch_id"])

            answered: set[str] = set()

            async def host() -> None:
                """What the runtime does with each held ``tools`` post: run it, and reply on the post."""
                while True:
                    for event in list(rig.events):
                        data = event["data"]
                        if event["type"] != "hook" or data.get("name") != "tools" or not data.get("reply_id") or data["reply_id"] in answered:
                            continue
                        answered.add(data["reply_id"])
                        body = data["body"]
                        assert body["set"] == "browser" and body["call_id"].startswith(f"{launch['launch_id']}:")

                        async def answer(reply_id: str = data["reply_id"], tool: str = body["tool"], arguments: dict[str, Any] = body["arguments"]) -> None:
                            text, failed = await agent.run(tool, arguments, caller)
                            await rig.client.call("hooks.reply", {"reply_id": reply_id, "body": {"text": text, "error": failed}})

                        asyncio.ensure_future(answer())
                    await asyncio.sleep(0.05)

            serving = asyncio.ensure_future(host())
            try:
                async with asyncio.timeout(60):
                    while not asked:
                        await asyncio.sleep(0.05)
                # The purchase waits for the operator: the CLI's call is still held.
                await asyncio.sleep(0.5)
                assert not [e for e in read_log(rig.log) if e["event"] == "mcp_result" and e["tool"] == "BrowserAct"]
                assert "purchase" in asked[0].kinds and asked[0].decision.rule == "browser.sensitive"
                granted.set()
                async with asyncio.timeout(60):
                    while not [e for e in read_log(rig.log) if e["event"] == "mcp_result" and e["tool"] == "BrowserAct"]:
                        await asyncio.sleep(0.05)
            finally:
                serving.cancel()
            results = {e["tool"]: e["result"] for e in read_log(rig.log) if e["event"] == "mcp_result"}
            assert "Browser opened" in results["BrowserOpen"]
            assert "[page content from https://shop.test;" in results["BrowserSnapshot"] and "Buy now" in results["BrowserSnapshot"]
            assert "Done: click" in results["BrowserAct"]
            hellos = [h["body"] for h in rig.hooks("tools") if h["body"].get("tool") == "hello"]
            assert hellos and all(h["set"] == "browser" for h in hellos)
    finally:
        await service.close()
        await daemon.stop()
        shutil.rmtree(base, ignore_errors=True)
