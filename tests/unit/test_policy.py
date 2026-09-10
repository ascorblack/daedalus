"""The tool policy: shell parsing, built-in rules, operator rules that only tighten, grants, hooks, timing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from protocore.contracts.hooks import HookActionKind
from protocore.contracts.types import HookEvent

from daedalus.config import HooksConfig, PolicyRuleConfig, RuntimeConfig
from daedalus.host.hooks import DaedalusHookManager
from daedalus.host.policy import ASK, DENY, Policy, Rule, approval_key, host_allowed, hosts_in, shell_segments
from daedalus.security.redact import Redactor


def test_shell_segments_split_on_operators_strip_wrappers_and_open_bash_c() -> None:
    assert shell_segments("cd a && sudo rm -rf / ; ls | wc -l") == [["cd", "a"], ["rm", "-rf", "/"], ["ls"], ["wc", "-l"]]
    assert shell_segments("FOO=1 env BAR=2 nohup python x.py") == [["python", "x.py"]]
    assert shell_segments("bash -c 'git push --force origin main'") == [["git", "push", "--force", "origin", "main"]]
    assert shell_segments("echo 'unbalanced")[0][0] == "echo"


def test_hosts_are_found_in_network_commands_only() -> None:
    segs = shell_segments("curl -s https://api.example.com/v1 | jq . && ssh ubuntu@10.0.0.5 uptime && git clone git@github.com:o/r.git && echo example.org")
    assert hosts_in(segs) == ["api.example.com", "10.0.0.5", "github.com"]
    assert host_allowed("api.example.com", ["*.example.com"]) and not host_allowed("example.org", ["*.example.com"]) and host_allowed("example.com", ["*.example.com"])


def test_builtin_rules_deny_the_machine_and_the_operators_paths() -> None:
    policy = Policy(protected_paths=[Path("/opt/launcher"), Path("/srv/state/secrets")], workspace_roots=[Path("/srv/workspaces")])
    assert policy.evaluate("Exec", {"command": "ls -la && cat README.md"}).action == "allow"
    assert policy.evaluate("Exec", {"command": "cd /tmp && rm -rf /"}).action == DENY
    assert policy.evaluate("Exec", {"command": "rm -rf ./build"}).action == "allow"
    assert policy.evaluate("Exec", {"command": "rm -rf /srv/workspaces/abc"}).action == ASK
    assert policy.evaluate("Exec", {"command": "echo x > /opt/launcher/supervisor.py"}).action == DENY
    assert policy.evaluate("Exec", {"command": "cp key.pem /srv/state/secrets/k"}).action == DENY
    assert policy.evaluate("Exec", {"command": "sed -i s/a/b/ /opt/launcher/x.py"}).action == DENY
    assert policy.evaluate("Exec", {"command": "git push origin main", "cwd": "/srv/daedalus"}).action == DENY
    assert policy.evaluate("Exec", {"command": "cd /srv/protocore-exp && git push"}).action == DENY
    assert policy.evaluate("Exec", {"command": "git push --force origin feature"}).action == ASK
    assert policy.evaluate("Exec", {"command": "git push --force-with-lease origin feature"}).action == "allow"
    assert policy.evaluate("Exec", {"command": "mkfs.ext4 /dev/sda1"}).action == DENY
    assert policy.evaluate("Exec", {"command": "dd if=/dev/zero of=/dev/sda"}).action == DENY
    assert policy.evaluate("Exec", {"command": ":(){ :|:& };:"}).action == DENY
    assert policy.evaluate("Verify", {"command": "rm -rf ~"}).action == DENY
    assert policy.evaluate("Read", {"path": "/etc/passwd"}).action == "allow"


def test_egress_allowlist_asks_and_logs_hosts() -> None:
    policy = Policy(egress_allow=["github.com", "*.pypi.org"])
    assert policy.evaluate("WebFetch", {"url": "https://files.pypi.org/x"}).action == "allow"
    decision = policy.evaluate("WebFetch", {"url": "https://evil.example/x"})
    assert decision.action == ASK and decision.hosts == ["evil.example"] and len(decision.key) == 12
    decision = policy.evaluate("Exec", {"command": "curl https://github.com/a && wget http://mirror.example/b"})
    assert decision.action == ASK and decision.hosts == ["github.com", "mirror.example"]
    assert Policy().evaluate("WebFetch", {"url": "https://anything.example"}).action == "allow"


def test_operator_rules_can_tighten_and_lift_asks_but_never_builtin_denials() -> None:
    rules = [
        Rule(id="no-pip", tool="Exec", action=DENY, note="no global installs", pattern=r"\bpip install\b(?!.*--user)", source="config"),
        Rule(id="ask-docker", tool="*", action=ASK, note="docker needs a look", pattern=r"\bdocker\b", source="config"),
        Rule(id="free-rm-root", tool="Exec", action="allow", note="", pattern=r"rm -rf /", source="config"),
        Rule(id="free-force", tool="Exec", action="allow", note="", pattern=r"--force", source="config"),
    ]
    policy = Policy(rules=rules)
    assert policy.evaluate("Exec", {"command": "pip install requests"}).action == DENY
    assert policy.evaluate("Exec", {"command": "docker ps"}).action == ASK
    assert policy.evaluate("Exec", {"command": "rm -rf /"}).action == DENY  # an allow rule cannot lift a built-in denial
    assert policy.evaluate("Exec", {"command": "git push --force origin x"}).action == "allow"  # but it lifts an ask
    assert any(r["id"] == "no-pip" and r["source"] == "config" for r in policy.describe())


def test_a_grant_lets_the_same_call_through_once_and_not_another() -> None:
    policy = Policy(egress_allow=["github.com"])
    call = {"command": "curl https://other.example/x"}
    key = approval_key("Exec", call)
    assert policy.evaluate("Exec", call).action == ASK
    granted = policy.evaluate("Exec", call, grants=[key])
    assert granted.action == "allow" and granted.key == key
    assert policy.evaluate("Exec", {"command": "curl https://other.example/y"}, grants=[key]).action == ASK


def test_config_rules_are_validated() -> None:
    cfg = RuntimeConfig(policy={"rules": [PolicyRuleConfig(tool="Exec", pattern="x", action="deny")], "egress_allow": ["a.example"]})
    assert cfg.policy.rules[0].action == "deny" and cfg.policy.egress_allow == ["a.example"]
    with pytest.raises(ValueError):
        PolicyRuleConfig(action="maybe")


async def test_operator_hook_scripts_deny_rewrite_and_are_ignored_when_broken(tmp_path: Path) -> None:
    deny = tmp_path / "deny.sh"
    deny.write_text("#!/bin/bash\nread -r payload; echo \"no $(echo \"$payload\" | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"tool_name\"])')\"; exit 2\n")
    rewrite = tmp_path / "rewrite.sh"
    rewrite.write_text("#!/bin/bash\ncat >/dev/null; echo '{\"arguments\": {\"command\": \"echo replaced\"}}'\n")
    post = tmp_path / "post.sh"
    post.write_text("#!/bin/bash\ncat >/dev/null; echo '{\"tool_output\": \"rewritten output\"}'\n")
    for f in (deny, rewrite, post):
        f.chmod(0o755)
    cfg = HooksConfig(pre_tool=str(deny), post_tool=str(post), timeout_seconds=10)
    hooks = DaedalusHookManager(Redactor([]), hooks_config=lambda: cfg)
    result = await hooks.invoke(HookEvent.pre_tool_use, {"tool_name": "Exec", "arguments": {"command": "ls"}}, "t")
    assert result.action == HookActionKind.DENY and "no Exec" in result.reason
    cfg.pre_tool = str(rewrite)
    result = await hooks.invoke(HookEvent.pre_tool_use, {"tool_name": "Exec", "arguments": {"command": "ls"}}, "t")
    assert result.action == HookActionKind.MODIFY and result.modifications["arguments"] == {"command": "echo replaced"}
    result = await hooks.invoke(HookEvent.post_tool_use, {"tool_name": "Exec", "tool_output": "original"}, "t")
    assert result.action == HookActionKind.MODIFY and result.modifications["tool_output"] == "rewritten output"
    cfg.pre_tool = str(tmp_path / "missing.sh")
    assert (await hooks.invoke(HookEvent.pre_tool_use, {"tool_name": "Exec", "arguments": {}}, "t")).action == HookActionKind.ALLOW
    assert (await hooks.invoke(HookEvent.run_finalize, {"status": "completed"}, "t")).action == HookActionKind.ALLOW


async def test_grants_timing_and_subagent_spend_live_in_the_manager(settings, db) -> None:  # type: ignore[no-untyped-def]
    from protocore.runtime.events.envelope import TurnEvent
    from protocore.runtime.events.types import EventType

    from daedalus.host.session_runner import SessionManager

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("policy")
        sid = state.session.id
        with pytest.raises(ValueError):
            await manager.grant(sid, "not-a-key")
        assert await manager.grant(sid, "0123456789ab") == ["0123456789ab"]
        gate = manager.policy_gate(sid, "run-1")
        decision = gate.decide("Exec", {"command": "curl https://x.example/"})
        assert decision.action == "allow" and decision.hosts == ["x.example"]
        await asyncio.sleep(0.05)
        assert [e["host"] for e in await manager.egress(sid)] == ["x.example"]
        await manager._dispatch_event(state, TurnEvent(type=EventType.TOOL_USE_START, run_id="run-1", payload={"tool_call_id": "c1", "tool_name": "Read"}))
        await asyncio.sleep(0.02)
        await manager._dispatch_event(state, TurnEvent(type=EventType.TOOL_RESULT, run_id="run-1", payload={"tool_call_id": "c1", "content": "x", "is_error": False}))
        await asyncio.sleep(0.05)
        timing = await manager.tool_timing(sid)
        assert timing and timing[0]["name"] == "Read" and timing[0]["calls"] == 1 and timing[0]["errors"] == 0
        child = await manager.create_session("[sub] x", metadata={"subagent_of": sid, "subagent_name": "x"})
        await db.execute("INSERT INTO usage_events(at, provider_id, model, purpose, run_id, session_id, input_tokens, output_tokens, cost_usd, raw) VALUES ('2026-09-10', 'p', 'm', 'stream', 'r', ?, 1, 1, 0.5, '{}')", (child.session.id,))
        await db.execute("INSERT INTO usage_events(at, provider_id, model, purpose, run_id, session_id, input_tokens, output_tokens, cost_usd, raw) VALUES ('2026-09-10', 'p', 'm', 'stream', 'r2', ?, 1, 1, 0.25, '{}')", (sid,))
        assert await manager.spend_with_subagents(sid) == 0.75
    finally:
        await manager.close()


def test_the_policy_adapter_shapes_a_refusal_the_model_can_act_on() -> None:
    from daedalus.host.engine_factory import PolicyAdapter
    from daedalus.host.policy import Decision

    class T:
        name = "Exec"

    adapter = PolicyAdapter(lambda tool, args: Decision(ASK, "outside the allowlist", "egress.allowlist", key="abcdef012345"))
    decision = adapter.evaluate(T(), {"command": "curl x"}, None)
    assert decision.denied and "Approval key: abcdef012345" in decision.reason and "AskUser" in decision.reason
    adapter = PolicyAdapter(lambda tool, args: Decision(DENY, "a fork bomb", "shell.forkbomb"))
    assert "refused by policy" in adapter.evaluate(T(), {}, None).reason
    adapter = PolicyAdapter(lambda tool, args: Decision("allow"))
    assert adapter.evaluate(T(), {}, None).allowed and json.dumps({}) == "{}"
