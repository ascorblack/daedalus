"""Doctor: one list of checks about the deployment, shown the same way everywhere.

Runs without a bot token where it can (config, state, git, disk) and adds live checks
when a running application is given (Telegram, providers, supervisor, queues). Every
check carries a fix hint; a few can fix themselves when asked.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from daedalus import supervisor_client
from daedalus.config import RuntimeConfig, Settings, keyproxy_base, keyproxy_configured, keyproxy_unresolved
from daedalus.host import capabilities
from daedalus.host.toolchain import status as toolchain_status
from daedalus.providers.pricing import pricing_table
from daedalus.providers.registry import _is_vendor_host
from daedalus.security.redact import redact as redact_text
from daedalus.tools.shell import bwrap_status, native_sandbox_note

try:  # the core's compiled token estimator, which an older core does not carry
    from protocore.runtime import token_counting
except ImportError:  # pragma: no cover — taken by a run against a core without the module
    token_counting = None  # type: ignore[assignment]

PROBE_TIMEOUT = 6.0
"""Default per-probe timeout; the configured value (``ops.doctor_probe_timeout_seconds``) wins."""


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    message: str
    severity: str = "warn"
    """``warn`` or ``fail`` when not ok; ``ok``/``info`` otherwise."""
    fix_hint: str = ""
    fixable: bool = False
    fixed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DoctorContext:
    settings: Settings
    config: RuntimeConfig
    db: Any = None
    manager: Any = None
    front: Any = None
    extensions: dict[str, Any] = field(default_factory=dict)
    guard: Any = None
    fix: bool = False


async def run_checks(ctx: DoctorContext) -> list[Check]:
    checks: list[Check] = []
    for probe in (_config, _telegram, _state, _selfdev, _git_probe, _supervisor, _native, _token_counter, _runtime, _keyproxy, _providers, _github_org):
        try:
            checks.extend(await probe(ctx))
        except Exception as exc:  # noqa: BLE001 — one broken probe must not hide the others
            checks.append(Check(probe.__name__.strip("_"), False, f"check crashed: {type(exc).__name__}: {exc}", "fail"))
    return checks


def _timeout(ctx: DoctorContext) -> float:
    return float(ctx.config.ops.doctor_probe_timeout_seconds)


def summarize(checks: list[Check]) -> dict[str, int]:
    return {
        "ok": sum(1 for c in checks if c.ok),
        "warn": sum(1 for c in checks if not c.ok and c.severity == "warn"),
        "fail": sum(1 for c in checks if not c.ok and c.severity == "fail"),
        "fixed": sum(1 for c in checks if c.fixed),
    }


def render_text(checks: list[Check]) -> str:
    icon = {True: "✅"}
    lines = []
    for c in checks:
        mark = icon.get(c.ok) or ("❌" if c.severity == "fail" else "⚠️")
        if c.fixed:
            mark = "🔧"
        line = f"{mark} {c.name}: {c.message}"
        if not c.ok and c.fix_hint:
            line += f"\n    → {c.fix_hint}"
        lines.append(line)
    s = summarize(checks)
    lines.append(f"\n{s['ok']} ok · {s['warn']} warnings · {s['fail']} failures" + (f" · {s['fixed']} fixed" if s["fixed"] else ""))
    return "\n".join(lines)


# -- probes -----------------------------------------------------------------------------


async def _config(ctx: DoctorContext) -> list[Check]:
    out: list[Check] = []
    cfg, st = ctx.config, ctx.settings
    found = cfg.default_preset()
    if found is None:
        out.append(Check("default model", False, "no model is configured; nothing can run yet", "fail", "open the app and add one: Settings → Models → Add a model"))
        pid, preset = "", None
    else:
        pid, preset = found
    if preset is not None:
        out.append(Check("default model", True, f"{pid} = {preset.display(pid)}", "ok"))
        provider = cfg.providers.get(preset.provider)
        if provider is None:
            out.append(Check("default provider", False, f"preset {pid} uses provider '{preset.provider}', which is not configured", "fail", "add the provider in Settings → Models, or pick another default preset"))
        else:
            env_key = {"deepseek": st.deepseek_api_key, "openrouter": st.openrouter_api_key, "vllm": st.vllm_api_key}.get(provider.kind, "")
            has_key = bool(provider.api_key or env_key)
            via_proxy = provider.kind in ("deepseek", "openrouter", "opencode") and not _is_vendor_host(provider.kind, provider.base_url)
            out.append(Check("default provider key", has_key or via_proxy or provider.kind in ("vllm", "openai_compat"), "configured" if has_key else ("held by the key proxy" if via_proxy else "no API key (fine for a keyless self-hosted endpoint)"), "ok" if has_key or via_proxy else "warn", "set the key in Settings → Models → provider"))
            try:
                table = pricing_table(provider.kind, provider.pricing)
                priced = any(preset.model.startswith(k) for k in table)
                out.append(Check("pricing for the default model", priced, "known" if priced else f"no price for {preset.model}: its calls are recorded as unmetered and cannot count toward caps", "ok" if priced else "warn", "add a pricing entry for the provider in config.toml"))
            except Exception as exc:  # noqa: BLE001 — a hand-edited price table is exactly what this check is for
                out.append(Check("pricing for the default model", False, f"the pricing table for {preset.provider} does not parse: {type(exc).__name__}: {exc}", "fail", "fix [providers.*.pricing] in config.toml"))
    vision = cfg.vision_preset()
    if cfg.has_model:
        out.append(Check("vision preset", vision is not None, f"{vision[0]}" if vision else "no image-capable preset: ImageView and photos in chat are unavailable", "ok" if vision else "warn", "mark a preset as accepting images"))
    chain_bad = [c for c in cfg.model.chain if c not in cfg.presets]
    out.append(Check("fallback chain", not chain_bad, ", ".join(cfg.model.chain) or "none" if not chain_bad else f"unknown presets: {', '.join(chain_bad)}", "ok" if not chain_bad else "warn", "fix the chain in Settings → Models"))
    sandbox = cfg.tools.exec.sandbox
    status = await asyncio.to_thread(bwrap_status)
    usable = status == "ok"
    sandbox_fix = "give the container cap_add SYS_ADMIN and security_opt seccomp=unconfined (see deploy/compose.yaml), then rebuild"
    if st.native:
        sandbox_fix = "set tools.exec.sandbox = off in Settings → Tools, or allow unprivileged user namespaces on this machine (sysctl kernel.apparmor_restrict_unprivileged_userns=0)"
    # An unavailable sandbox that was asked for is not a degraded one: Exec is refused outright, and
    # a row saying commands run unsandboxed would send the operator looking for the wrong problem.
    detail = f"{sandbox} (available: {status})" if sandbox == "off" else ("workspace" if usable else f"requested but unavailable — every Exec is refused until it is off or it works: {status}")
    out.append(Check("exec sandbox", sandbox == "off" or usable, detail, "ok" if sandbox == "off" or usable else "fail", sandbox_fix))
    # Informational, not warnings: an installation that never opens a page or runs npx is not a broken one.
    browser = await asyncio.to_thread(toolchain_status, "browser")
    browser_fix = "run the `:browser` tag of the agent image if the browser skills are wanted"
    node_fix = "the skills that shell out to npx cannot run here; nothing else needs it"
    if st.native:
        browser_fix = "the launcher installs it on demand: `daedalus-desktop` → the runtime extras, or `daedalus-desktop install browser`"
        node_fix = "the launcher installs it on demand: `daedalus-desktop install node`; nothing but those skills needs it"
    out.append(Check("browser tools", browser == "ok", "Playwright, a headless Chromium and Pillow are here" if browser == "ok" else browser, "ok" if browser == "ok" else "info", browser_fix))
    node = await asyncio.to_thread(toolchain_status, "node")
    out.append(Check("node", node == "ok", "available" if node == "ok" else node, "ok" if node == "ok" else "info", node_fix))
    out.append(Check("per-run spend cap", cfg.limits.usd_per_run > 0, f"${cfg.limits.usd_per_run:.2f} per run, ${st.usd_per_day:.2f} per day" if cfg.limits.usd_per_run > 0 else f"no per-run cap (daily cap ${st.usd_per_day:.2f})", "ok" if cfg.limits.usd_per_run > 0 else "warn", "set limits.usd_per_run in Settings"))
    return out


async def _telegram(ctx: DoctorContext) -> list[Check]:
    st = ctx.settings
    out = [await _sign_in(ctx)]
    if not st.telegram_bot_token:
        # Telegram is a front, not a requirement: without a token there is nothing here to be wrong.
        out.append(Check("telegram", True, "no bot token: the app is the only front", "ok"))
        return out
    out.append(Check("telegram credentials", bool(st.owner_user_id), "token and owner set" if st.owner_user_id else "TELEGRAM_BOT_TOKEN is set without OWNER_USER_ID", "fail", "set OWNER_USER_ID in the environment (.env)"))
    tg = ctx.config.telegram
    private = tg.session_mode() == "private"
    # Both shapes are complete: the private chat is a window onto one session at a time, a bound
    # forum gives each session a topic. Only "topics without a forum" is a configuration to fix.
    hub_ok = private or bool(tg.forum_chat_id)
    hub = "every session in the private chat (/sessions, /use)" if private else (f"forum {tg.forum_chat_id}" if tg.forum_chat_id else "telegram.mode is topics but no forum is bound")
    out.append(Check("session hub", hub_ok, hub, "ok" if hub_ok else "warn", "add the bot to a supergroup with topics and send /bind there, or set telegram.mode to private"))
    if ctx.front is not None:
        try:
            me = await asyncio.wait_for(ctx.front.bot.get_me(), timeout=_timeout(ctx))
            out.append(Check("bot api", True, f"@{me.username} via {st.telegram_api_base}{' (local server)' if st.telegram_local_mode else ''}", "ok"))
        except Exception as exc:  # noqa: BLE001
            out.append(Check("bot api", False, f"getMe failed: {type(exc).__name__}: {exc}", "fail", "check the local Bot API server / network"))
    return out


async def _sign_in(ctx: DoctorContext) -> Check:
    """Whether a browser can get in at all, and with what."""
    from daedalus.stores import pairing, passkeys  # Lazy: doctor runs in processes that never serve the API

    ways = []
    if ctx.settings.telegram_bot_token:
        ways.append("telegram login")
    enrolled = await passkeys.count(ctx.db) if ctx.db is not None else 0
    if enrolled:
        ways.append(f"{enrolled} passkey" + ("s" if enrolled != 1 else ""))
    if ctx.db is not None and await pairing.outstanding(ctx.db):
        ways.append(f"a pairing link ({ctx.settings.state_dir / pairing.URL_FILE})")
    return Check(
        "sign-in",
        bool(ways),
        ", ".join(ways) if ways else "no way into the app: no bot, no passkey, no live pairing link",
        "ok" if ways else "warn",
        "mint one with `python -m daedalus auth pair`",
    )


async def _state(ctx: DoctorContext) -> list[Check]:
    st = ctx.settings
    out: list[Check] = []
    for label, path in (("state dir", st.state_dir), ("workspaces dir", st.workspaces_dir)):
        writable = path.exists() and os.access(path, os.W_OK)
        out.append(Check(label, writable, str(path) if writable else f"{path} is missing or not writable", "ok" if writable else "fail", "create it and make it writable by the bot user"))
    try:
        usage = shutil.disk_usage(st.state_dir if st.state_dir.exists() else Path("/"))
        free_gb = usage.free / 1e9
        min_free = ctx.config.ops.doctor_min_free_gb
        out.append(Check("disk free", free_gb >= min_free, f"{free_gb:.1f} GB free", "ok" if free_gb >= min_free else "fail", "free space: /cleanup, prune Docker, remove old workspaces"))
    except OSError:
        pass
    if st.workspaces_dir.exists():
        total = await asyncio.to_thread(_dir_size, st.workspaces_dir)
        gb = total / 1e9
        warn_gb = ctx.config.ops.doctor_workspaces_warn_gb
        out.append(Check("workspace size", gb < warn_gb, f"{gb:.1f} GB in {st.workspaces_dir}", "ok" if gb < warn_gb else "warn", "close finished sessions with 'delete the agent + workspace', or /cleanup"))
    if ctx.manager is not None:
        ops = ctx.config.ops
        snapshots = await ctx.manager.checkpoint_retention.total_size() / 1e9
        within = not ops.checkpoint_total_max_gb or snapshots <= ops.checkpoint_total_max_gb
        bound = f"{ops.checkpoint_total_max_gb:.1f} GB" if ops.checkpoint_total_max_gb else "no size bound"
        out.append(
            Check(
                "checkpoint store",
                within,
                f"{snapshots:.2f} GB of workspace snapshots; bounds: {bound} and {ops.checkpoint_keep_days} days, last {ops.checkpoint_keep_last} per session always kept",
                "ok" if within else "warn",
                "the maintenance tick prunes it every ops.db_maintenance_minutes; `python -m daedalus db checkpoints-prune` does it now",
            )
        )
    private = not st.secrets_dir.exists() or (st.secrets_dir.stat().st_mode & 0o077) == 0
    out.append(Check("secrets dir", private, "private" if private else f"{st.secrets_dir} is readable by others", "ok" if private else "warn", f"chmod 700 {st.secrets_dir}"))
    budget = st.state_dir / "BUDGET_EXCEEDED"
    out.append(Check("daily budget", not budget.exists(), "within budget" if not budget.exists() else f"exceeded: {budget.read_text(encoding='utf-8').strip()[:120]}", "ok" if not budget.exists() else "warn", "runs resume tomorrow, or the supervisor's /budget reset"))
    if ctx.guard is not None:
        n = getattr(ctx.guard, "unclean_boots", 0)
        skipped = getattr(ctx.guard, "skip_recovery", False)
        out.append(Check("boot health", n == 0, "this boot was clean" if n == 0 else f"{n} unclean restart(s) counted at this boot" + (" — recovery was skipped" if skipped else ""), "ok" if n == 0 else "warn", "read the logs for the crash; a parked run resumes at the next clean restart, or send a message in its session to start a new run"))
    if ctx.db is not None:
        row = await ctx.db.fetchone("SELECT version FROM schema_version")
        out.append(Check("database", row is not None, f"schema version {row['version']}" if row else "schema version missing", "ok" if row else "fail"))
        parked = await ctx.db.fetchall("SELECT s.run_id, s.session_id, r.created_at FROM snapshots s JOIN runs r ON r.id = s.run_id WHERE r.status IN ('running', 'paused')")
        live = ctx.manager.running_run_ids() if ctx.manager is not None else set()
        if ctx.manager is None and (st.state_dir / "RUNNING").exists():
            # Another process owns these runs right now; nothing here may touch them.
            live = {r["run_id"] for r in parked}
        waiting = [r for r in parked if r["run_id"] not in live]
        if waiting:
            hint = "they resume at the next clean restart; to start over, send a message in the session" + (" — recovery was skipped at this boot" if getattr(ctx.guard, "skip_recovery", False) else "")
            out.append(Check("parked runs", not getattr(ctx.guard, "skip_recovery", False), f"{len(waiting)} unfinished run(s) with a snapshot, not active in this process", "ok" if not getattr(ctx.guard, "skip_recovery", False) else "warn", hint))
        cutoff = (datetime.now(UTC) - timedelta(hours=ctx.config.ops.doctor_stale_snapshot_hours)).isoformat()
        stale = [r for r in waiting if r["created_at"] < cutoff]
        if stale and ctx.fix:
            for row in stale:
                await ctx.db.execute("DELETE FROM snapshots WHERE run_id = ?", (row["run_id"],))
                await ctx.db.execute("UPDATE runs SET status = 'cancelled' WHERE id = ?", (row["run_id"],))
            out.append(Check("stale run snapshots", True, f"removed {len(stale)} snapshot(s) older than {ctx.config.ops.doctor_stale_snapshot_hours} h", "ok", fixable=True, fixed=True))
        else:
            out.append(Check("stale run snapshots", not stale, "none" if not stale else f"{len(stale)} parked run(s) older than {ctx.config.ops.doctor_stale_snapshot_hours} h would be resumed at the next restart", "ok" if not stale else "warn", "/doctor fix removes them (the sessions keep their history)", fixable=True))
        if ctx.manager is not None:
            orphans = await ctx.manager.orphan_workspaces()
            if ctx.fix and orphans:
                removed = await ctx.manager.sweep_orphan_workspaces()
                out.append(Check("orphan workspaces", True, f"removed {len(removed)}: {', '.join(removed[:10])}", "ok", fixable=True, fixed=True))
            else:
                out.append(Check("orphan workspaces", not orphans, "none" if not orphans else f"{len(orphans)} folder(s) belong to no session or schedule: {', '.join(o.name for o in orphans[:10])}", "ok" if not orphans else "warn", "/doctor fix deletes them", fixable=True))
    return out


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _git_cmd(repo: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def _selfdev_mode(ctx: DoctorContext) -> str:
    """The resolved mode: the running manager's answer, or the same resolution done again."""
    manager_caps = getattr(ctx.manager, "capabilities", None) if ctx.manager is not None else None
    if manager_caps is not None:
        return str(manager_caps.selfdev.mode)
    return capabilities.resolve_selfdev(ctx.settings, ctx.config).mode


async def _selfdev(ctx: DoctorContext) -> list[Check]:
    """Which self-development mode this installation runs in, and whether it can honour it."""
    manager_caps = getattr(ctx.manager, "capabilities", None) if ctx.manager is not None else None
    selfdev = manager_caps.selfdev if manager_caps is not None else capabilities.resolve_selfdev(ctx.settings, ctx.config)
    how = "resolved from what is installed" if selfdev.configured == "auto" else f"set to {selfdev.configured!r} in the configuration"
    out = [Check("self-development", True, f"{selfdev.mode} ({how})", "ok")]
    if selfdev.missing:
        out.append(
            Check(
                "self-development prerequisites",
                False,
                f"mode {selfdev.mode} is set but {', '.join(selfdev.missing)} is missing",
                "warn",
                f"provide what is missing, or set [self_change] mode to \"auto\" and let the resolution pick the mode this installation can honour ({'; '.join(selfdev.reasons[1:])})",
            )
        )
    return out


async def _github_org(ctx: DoctorContext) -> list[Check]:
    """Whether the agent's own organisation is reachable and its token may create repositories there."""
    org = ctx.settings.daedalus_github_org.strip()
    token = ctx.settings.github_daedalus_token.strip()
    if not org and not token:
        return []
    if not org or not token:
        return [Check("github org", False, "DAEDALUS_GITHUB_ORG and GITHUB_DAEDALUS_TOKEN go together; one is missing", "warn", "set both in .env")]
    try:
        async with httpx.AsyncClient(timeout=ctx.config.ops.doctor_probe_timeout_seconds) as client:
            headers = {"authorization": f"Bearer {token}", "accept": "application/vnd.github+json"}
            # An empty name is refused with 422 once the token is allowed to create; without the right it is 403 first.
            response = await client.post(f"https://api.github.com/orgs/{org}/repos", headers=headers, json={"name": ""})
    except httpx.HTTPError as exc:
        return [Check("github org", False, f"{org}: {type(exc).__name__}", "warn", "network or GitHub down")]
    if response.status_code == 422:
        return [Check("github org", True, f"{org}: the token may create repositories", "ok")]
    needed = response.headers.get("x-accepted-github-permissions", "")
    return [Check("github org", False, f"{org}: HTTP {response.status_code}; needs {needed or 'administration=write on the organisation'}", "warn", f"issue the token with resource owner {org} and Administration: write")]


async def _git_probe(ctx: DoctorContext) -> list[Check]:
    out: list[Check] = []
    mode = _selfdev_mode(ctx)
    if mode == "off":
        return []  # nothing here changes its own code: the state of the checkouts is not a health question
    if shutil.which("git") is None:
        return [Check("git", False, "git is not installed in this environment", "warn", "install git; the supervisor rebuild and the self-development tools need it")]
    for label, repo in (("bot repo", ctx.settings.bot_repo_dir), ("core repo", ctx.settings.core_repo_dir)):
        if not (repo / ".git").exists():
            out.append(Check(label, False, f"{repo} is not a git checkout", "warn", "the supervisor rebuild needs a git checkout"))
            continue
        code, branch = await asyncio.to_thread(_git_cmd, repo, "rev-parse", "--abbrev-ref", "HEAD")
        _, dirty = await asyncio.to_thread(_git_cmd, repo, "status", "--porcelain")
        _, head = await asyncio.to_thread(_git_cmd, repo, "rev-parse", "--short", "HEAD")
        msg = f"{head} on {branch}" + (f", {len(dirty.splitlines())} uncommitted change(s)" if dirty else ", clean")
        ok = code == 0 and not dirty
        out.append(Check(label, ok, msg, "ok" if ok else "warn", "commit or discard local changes: a rebuild resets the checkout to origin/main"))
        if mode != "server":
            continue  # local mode has no remote to push to, so there is no push right to prove
        # Reading a public repository proves nothing about the token; only the server's answer to a push does.
        push_code, push_out = await asyncio.to_thread(_git_cmd, repo, "push", "--dry-run", "origin", "HEAD:refs/heads/doctor-permission-probe")
        can_push = push_code == 0 and "denied" not in push_out.lower()
        detail = "the token may push (dry run accepted)" if can_push else redact_text(push_out.strip().splitlines()[-1] if push_out.strip() else f"exit {push_code}")[:160]
        out.append(Check(f"{label} push access", can_push, detail, "ok" if can_push else "fail", "grant the GitHub token write access to this repository (fine-grained token → repository access + Contents: read and write); a recreated repository needs the token re-issued"))
    return out


async def _supervisor(ctx: DoctorContext) -> list[Check]:
    address = ctx.settings.supervisor_address
    if _selfdev_mode(ctx) == "off":
        return []  # without self-development there is nothing for the supervisor to rebuild or roll back
    if not supervisor_client.present(address):
        return [Check("supervisor", False, f"{address} not present (development mode: no rebuild/rollback)", "warn", "run under the supervisor for self-updates")]
    selfdev = ctx.extensions.get("selfdev")
    if selfdev is None:
        return [Check("supervisor", True, "socket present", "ok")]
    try:
        status = await asyncio.wait_for(selfdev.supervisor_status(), timeout=_timeout(ctx))
    except Exception as exc:  # noqa: BLE001
        hint = "restart the launcher; it is what keeps the supervisor running" if ctx.settings.native else "restart the container; the supervisor is PID 1"
        return [Check("supervisor", False, f"status call failed: {type(exc).__name__}: {exc}", "fail", hint)]
    if not status:
        return [Check("supervisor", False, "no answer", "fail")]
    out = [Check("supervisor", True, f"bot {str(status.get('bot'))[:10]} core {str(status.get('core'))[:10]}, child {'running' if status.get('child_running') else 'stopped'}", "ok")]
    if status.get("failed"):
        out.append(Check("last rebuild", False, str(status["failed"])[:200], "warn", "fix the failing preflight step and /rebuild again"))
    return out


# What a container would have carried, said once and plainly rather than as three rows named after
# checks that exist nowhere. Docker mode emits no probe called "container image" either, so listing
# these as skipped checks invented the questions it claimed to be answering.
CONTAINER_ONLY = "a container would carry the environment in an image, rebuild it through the rebuilder sidecar and publish a session's service ports to the host; natively the runtime folder, the launcher and the loopback interface do those three things"


async def _native(ctx: DoctorContext) -> list[Check]:
    """What native mode is, and what the container-only checks have to say where there is no container."""
    if not ctx.settings.native:
        return []
    out = [
        Check("isolation", True, await asyncio.to_thread(native_sandbox_note), "info", "the policy rules, the approval gates, the protected paths, the egress allowlist and the spend caps all still apply"),
        Check("services address", True, f"{ctx.settings.services_public_host or '127.0.0.1'}:{ctx.settings.services_port_range} — bound on this machine, not published from anywhere", "ok"),
    ]
    out.append(Check("container-only checks", True, CONTAINER_ONLY, "info"))
    git = shutil.which("git")
    out.append(Check("portable runtime", bool(git), f"git {git}" if git else "git is not on PATH; the checkouts and self-development need it", "ok" if git else "fail", "install git, or start the launcher again — it puts the runtime's own on PATH"))
    return out


async def _token_counter(ctx: DoctorContext) -> list[Check]:
    """Whether the core estimates tokens with its compiled extension or the pure-Python fallback.

    The fallback (``estimate_tokens_python``) runs at a few MB/s and is re-run on every tool
    surface each round; the compiled one is roughly thirty times faster on the same input. This
    just reports which one is wired up — see the core's ``protocore.runtime.token_counting``
    module for the estimator itself. A core without the module answers "unknown" rather than
    taking the whole API down with it at import time: the host runs under either core.
    """
    if token_counting is None:
        return [Check("token counter", True, "unknown: this core has no token_counting module to ask", "info")]
    active = token_counting.NATIVE_ACTIVE
    return [
        Check(
            "token counter",
            True,
            "native extension active" if active else "pure-Python fallback (no native extension installed for this platform)",
            "ok" if active else "info",
        ),
    ]


async def _runtime(ctx: DoctorContext) -> list[Check]:
    out: list[Check] = []
    if ctx.manager is not None:
        sessions = await ctx.manager.list_sessions(limit=200)
        running = [s for s in sessions if s["status"] == "running"]
        waiting = [s for s in sessions if s["status"] == "waiting"]
        out.append(Check("sessions", True, f"{len(sessions)} total, {len(running)} running, {len(waiting)} waiting for an answer", "ok"))
        if waiting:
            out.append(Check("open questions", False, ", ".join(s["title"] for s in waiting[:5]), "warn", "answer them in the topic or the Mini App; unattended runs time out on their own"))
        names = sorted(t.name for t in ctx.manager.tools.list_all())
        out.append(Check("tools", len(names) > 10, f"{len(names)} registered", "ok" if len(names) > 10 else "fail"))
        mcp = ctx.manager.mcp.status()
        broken = [m["name"] for m in mcp if m.get("error")]
        if mcp:
            out.append(Check("mcp servers", not broken, f"{len(mcp)} configured" + (f", errors: {', '.join(broken)}" if broken else ""), "ok" if not broken else "warn", "McpOAuthBegin / fix the server command in Settings"))
    heartbeat = ctx.extensions.get("heartbeat")
    if heartbeat is not None:
        hb = heartbeat.status()
        out.append(Check("heartbeat", True, "armed" if hb["armed"] else ("on, file empty" if hb["enabled"] else "off"), "ok" if hb["armed"] or not hb["enabled"] else "info"))
    inbox = ctx.extensions.get("inbox")
    if inbox is not None:
        unread = await inbox.unread_count()
        out.append(Check("inbox", True, f"{unread} unread", "ok" if unread == 0 else "info"))
    if ctx.db is not None:
        row = await ctx.db.fetchone("SELECT count(*) c FROM schedules WHERE enabled = 0 AND failure_count > 0")
        if row and row["c"]:
            out.append(Check("schedules", False, f"{row['c']} task(s) switched off after repeated failures", "warn", "see /schedules and the inbox; /schedule on <id> re-enables"))
        row = await ctx.db.fetchone("SELECT count(*) c FROM deliveries WHERE status = 'failed'")
        if row and row["c"]:
            out.append(Check("deliveries", False, f"{row['c']} answer(s) could not be delivered to Telegram", "warn", "they are in the Mini App transcript and answer.md in the workspace"))
    return out


async def _keyproxy(ctx: DoctorContext) -> list[Check]:
    """Whether this process knows where its key proxy is, and says so when it does not.

    The bot sends the proxy its own API token, so it is asked at one address only: the one the
    launcher passes in ``KEYPROXY_BASE_URL``. A bot started without it — a unit file, a bare
    ``python -m daedalus``, a shell that did not come from the launcher — cannot recognise its own
    proxy on a loopback port, so it asks nobody and reports those endpoints as unknown instead of
    ready. That is a silent difference on the screen and a loud one here.
    """
    unresolved = sorted(pid for pid, provider in ctx.config.providers.items() if keyproxy_unresolved(provider.base_url))
    if keyproxy_configured():
        return [Check("key proxy", True, f"asked at {keyproxy_base()}", "ok")]
    if not unresolved:
        return []
    return [
        Check(
            "key proxy",
            False,
            f"KEYPROXY_BASE_URL is not set, so {', '.join(unresolved)} cannot be told apart from any other local endpoint and are not asked for keys",
            "warn",
            "start the bot through the launcher, or set KEYPROXY_BASE_URL to the address the proxy listens on",
        )
    ]


async def _providers(ctx: DoctorContext) -> list[Check]:
    st = ctx.settings
    timeout = _timeout(ctx)

    async def probe(pid: str, provider: Any) -> Check | None:
        base = provider.base_url or (st.vllm_base_url if provider.kind == "vllm" else "")
        if not base:
            return None
        key = provider.api_key or {"deepseek": st.deepseek_api_key, "openrouter": st.openrouter_api_key, "vllm": st.vllm_api_key}.get(provider.kind, "")
        shown = redact_text(base)  # a base_url with inline credentials must not reach the chat
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(base.rstrip("/") + "/models", headers={"authorization": f"Bearer {key}"} if key else {})
            ok = response.status_code < 500 and response.status_code != 401
            detail = f"HTTP {response.status_code}" + (" (key rejected)" if response.status_code == 401 else "")
            return Check(f"provider {pid}", ok, f"{shown} → {detail}", "ok" if ok else "fail", "check the key and base_url in Settings → Models")
        except Exception as exc:  # noqa: BLE001 — a typo in base_url raises InvalidURL, not HTTPError
            return Check(f"provider {pid}", False, f"{shown} unreachable: {type(exc).__name__}", "fail", "network, proxy or a wrong base_url")

    results = await asyncio.gather(*(probe(pid, p) for pid, p in ctx.config.providers.items()), return_exceptions=True)
    out: list[Check] = []
    for pid, result in zip(ctx.config.providers, results, strict=False):
        if isinstance(result, Check):
            out.append(result)
        elif isinstance(result, BaseException):
            out.append(Check(f"provider {pid}", False, f"probe crashed: {type(result).__name__}", "fail"))
    return out


async def run_and_render(ctx: DoctorContext, *, as_json: bool = False) -> str:
    checks = await run_checks(ctx)
    if as_json:
        return json.dumps({"checks": [c.as_dict() for c in checks], "summary": summarize(checks)}, indent=2)
    return render_text(checks)


__all__ = ["Check", "DoctorContext", "render_text", "run_and_render", "run_checks", "summarize"]
