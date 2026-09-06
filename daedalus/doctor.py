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

from daedalus.config import RuntimeConfig, Settings
from daedalus.providers.pricing import pricing_table

MIN_FREE_GB = 1.0
WORKSPACES_WARN_GB = 20.0
STALE_SNAPSHOT_HOURS = 24
PROBE_TIMEOUT = 6.0


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
    for probe in (_config, _telegram, _state, _git_probe, _supervisor, _runtime, _providers):
        try:
            checks.extend(await probe(ctx))
        except Exception as exc:  # noqa: BLE001 — one broken probe must not hide the others
            checks.append(Check(probe.__name__.strip("_"), False, f"check crashed: {type(exc).__name__}: {exc}", "fail"))
    return checks


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
    try:
        pid, preset = cfg.preset()
        out.append(Check("default model", True, f"{pid} = {preset.display(pid)}", "ok"))
        provider = cfg.providers.get(preset.provider)
        if provider is None:
            out.append(Check("default provider", False, f"preset {pid} uses provider '{preset.provider}', which is not configured", "fail", "add the provider in Settings → Models, or pick another default preset"))
        else:
            env_key = {"deepseek": st.deepseek_api_key, "openrouter": st.openrouter_api_key, "vllm": st.vllm_api_key}.get(provider.kind, "")
            has_key = bool(provider.api_key or env_key)
            out.append(Check("default provider key", has_key or provider.kind in ("vllm", "openai_compat"), "configured" if has_key else "no API key (fine for a keyless self-hosted endpoint)", "ok" if has_key else "warn", "set the key in Settings → Models → provider"))
            table = pricing_table(provider.kind, provider.pricing)
            priced = any(preset.model.startswith(k) for k in table)
            out.append(Check("pricing for the default model", priced, "known" if priced else f"no price for {preset.model}: its calls are recorded as unmetered and cannot count toward caps", "ok" if priced else "warn", "add a pricing entry for the provider in config.toml"))
    except RuntimeError as exc:
        out.append(Check("default model", False, str(exc), "fail", "create a preset in Settings → Models"))
    vision = cfg.vision_preset()
    out.append(Check("vision preset", vision is not None, f"{vision[0]}" if vision else "no image-capable preset: ImageView and photos in chat are unavailable", "ok" if vision else "warn", "mark a preset as accepting images"))
    chain_bad = [c for c in cfg.model.chain if c not in cfg.presets]
    out.append(Check("fallback chain", not chain_bad, ", ".join(cfg.model.chain) or "none" if not chain_bad else f"unknown presets: {', '.join(chain_bad)}", "ok" if not chain_bad else "warn", "fix the chain in Settings → Models"))
    out.append(Check("per-run spend cap", cfg.limits.usd_per_run > 0, f"${cfg.limits.usd_per_run:.2f} per run, ${st.usd_per_day:.2f} per day" if cfg.limits.usd_per_run > 0 else f"no per-run cap (daily cap ${st.usd_per_day:.2f})", "ok" if cfg.limits.usd_per_run > 0 else "warn", "set limits.usd_per_run in Settings"))
    return out


async def _telegram(ctx: DoctorContext) -> list[Check]:
    st = ctx.settings
    out = [Check("telegram credentials", bool(st.telegram_bot_token and st.owner_user_id), "token and owner set" if st.telegram_bot_token and st.owner_user_id else "TELEGRAM_BOT_TOKEN or OWNER_USER_ID missing", "fail", "set them in the environment (.env)")]
    out.append(Check("session hub", bool(ctx.config.telegram.forum_chat_id), f"forum {ctx.config.telegram.forum_chat_id}" if ctx.config.telegram.forum_chat_id else "no forum bound: only the private chat works, one session", "ok" if ctx.config.telegram.forum_chat_id else "warn", "add the bot to a supergroup with topics and send /bind there"))
    if ctx.front is not None:
        try:
            me = await asyncio.wait_for(ctx.front.bot.get_me(), timeout=PROBE_TIMEOUT)
            out.append(Check("bot api", True, f"@{me.username} via {st.telegram_api_base}{' (local server)' if st.telegram_local_mode else ''}", "ok"))
        except Exception as exc:  # noqa: BLE001
            out.append(Check("bot api", False, f"getMe failed: {type(exc).__name__}: {exc}", "fail", "check the local Bot API server / network"))
    return out


async def _state(ctx: DoctorContext) -> list[Check]:
    st = ctx.settings
    out: list[Check] = []
    for label, path in (("state dir", st.state_dir), ("workspaces dir", st.workspaces_dir)):
        writable = path.exists() and os.access(path, os.W_OK)
        out.append(Check(label, writable, str(path) if writable else f"{path} is missing or not writable", "fail", "create it and make it writable by the bot user"))
    try:
        usage = shutil.disk_usage(st.state_dir if st.state_dir.exists() else Path("/"))
        free_gb = usage.free / 1e9
        out.append(Check("disk free", free_gb >= MIN_FREE_GB, f"{free_gb:.1f} GB free", "ok" if free_gb >= MIN_FREE_GB else "fail", "free space: /cleanup, prune Docker, remove old workspaces"))
    except OSError:
        pass
    if st.workspaces_dir.exists():
        total = await asyncio.to_thread(_dir_size, st.workspaces_dir)
        gb = total / 1e9
        out.append(Check("workspace size", gb < WORKSPACES_WARN_GB, f"{gb:.1f} GB in {st.workspaces_dir}", "ok" if gb < WORKSPACES_WARN_GB else "warn", "close finished sessions with 'delete the agent + workspace', or /cleanup"))
    out.append(Check("secrets dir", not st.secrets_dir.exists() or (st.secrets_dir.stat().st_mode & 0o077) == 0, "private" if not st.secrets_dir.exists() or (st.secrets_dir.stat().st_mode & 0o077) == 0 else f"{st.secrets_dir} is readable by others", "warn", f"chmod 700 {st.secrets_dir}"))
    budget = st.state_dir / "BUDGET_EXCEEDED"
    out.append(Check("daily budget", not budget.exists(), "within budget" if not budget.exists() else f"exceeded: {budget.read_text(encoding='utf-8').strip()[:120]}", "ok" if not budget.exists() else "warn", "runs resume tomorrow, or the supervisor's /budget reset"))
    if ctx.guard is not None:
        n = getattr(ctx.guard, "unclean_boots", 0)
        out.append(Check("boot health", n == 0, "clean boot" if n == 0 else f"{n} unclean restart(s) in the last 10 min" + (" — recovery was skipped" if getattr(ctx.guard, "skip_recovery", False) else ""), "ok" if n == 0 else "warn", "read the logs for the crash; a parked run can be resumed by sending a message to its session"))
    if ctx.db is not None:
        row = await ctx.db.fetchone("SELECT version FROM schema_version")
        out.append(Check("database", row is not None, f"schema version {row['version']}" if row else "schema version missing", "ok" if row else "fail"))
        cutoff = (datetime.now(UTC) - timedelta(hours=STALE_SNAPSHOT_HOURS)).isoformat()
        stale = await ctx.db.fetchall("SELECT s.run_id, s.session_id FROM snapshots s JOIN runs r ON r.id = s.run_id WHERE r.status IN ('running', 'paused') AND r.created_at < ?", (cutoff,))
        if stale and ctx.fix:
            for row in stale:
                await ctx.db.execute("DELETE FROM snapshots WHERE run_id = ?", (row["run_id"],))
                await ctx.db.execute("UPDATE runs SET status = 'cancelled' WHERE id = ?", (row["run_id"],))
            out.append(Check("stale run snapshots", True, f"removed {len(stale)} snapshot(s) older than a day", "ok", fixable=True, fixed=True))
        else:
            out.append(Check("stale run snapshots", not stale, "none" if not stale else f"{len(stale)} unfinished run(s) older than a day would be resumed at the next restart", "ok" if not stale else "warn", "/doctor fix removes them (the sessions keep their history)", fixable=True))
        if ctx.manager is not None:
            orphans = await ctx.manager.sweep_orphan_workspaces() if ctx.fix else await _orphan_workspaces(ctx)
            if ctx.fix:
                out.append(Check("orphan workspaces", True, f"removed {len(orphans)}" if orphans else "none", "ok", fixable=True, fixed=bool(orphans)))
            else:
                out.append(Check("orphan workspaces", not orphans, "none" if not orphans else f"{len(orphans)} folder(s) belong to no session or schedule", "ok" if not orphans else "warn", "/doctor fix deletes them", fixable=True))
    return out


async def _orphan_workspaces(ctx: DoctorContext) -> list[str]:
    rows = await ctx.db.fetchall("SELECT id FROM sessions")
    known = {r["id"] for r in rows}
    sched = await ctx.db.fetchall("SELECT workspace FROM schedules")
    known_paths = {Path(r["workspace"]).resolve() for r in sched}
    out = []
    for entry in ctx.settings.workspaces_dir.iterdir() if ctx.settings.workspaces_dir.exists() else []:
        if entry.is_dir() and entry.name not in known and entry.resolve() not in known_paths and entry.name != "heartbeat" and not entry.name.startswith("lazy-"):
            out.append(entry.name)
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


async def _git_probe(ctx: DoctorContext) -> list[Check]:
    out: list[Check] = []
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
    return out


async def _supervisor(ctx: DoctorContext) -> list[Check]:
    sock = ctx.settings.supervisor_socket
    if not sock.exists():
        return [Check("supervisor", False, f"socket {sock} not present (development mode: no rebuild/rollback)", "warn", "run under the supervisor for self-updates")]
    selfdev = ctx.extensions.get("selfdev")
    if selfdev is None:
        return [Check("supervisor", True, "socket present", "ok")]
    try:
        status = await asyncio.wait_for(selfdev.supervisor_status(), timeout=PROBE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return [Check("supervisor", False, f"status call failed: {type(exc).__name__}: {exc}", "fail", "restart the container; the supervisor is PID 1")]
    if not status:
        return [Check("supervisor", False, "no answer", "fail")]
    out = [Check("supervisor", True, f"bot {str(status.get('bot'))[:10]} core {str(status.get('core'))[:10]}, child {'running' if status.get('child_running') else 'stopped'}", "ok")]
    if status.get("failed"):
        out.append(Check("last rebuild", False, str(status["failed"])[:200], "warn", "fix the failing preflight step and /rebuild again"))
    return out


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
        out.append(Check("heartbeat", True, "armed" if hb["armed"] else ("on, file empty" if hb["enabled"] else "off"), "ok" if hb["armed"] or not hb["enabled"] else "info", "write HEARTBEAT.md in Settings → Heartbeat"))
    inbox = ctx.extensions.get("inbox")
    if inbox is not None:
        unread = await inbox.unread_count()
        out.append(Check("inbox", True, f"{unread} unread", "ok" if unread == 0 else "info", "/inbox"))
    if ctx.db is not None:
        row = await ctx.db.fetchone("SELECT count(*) c FROM schedules WHERE enabled = 0 AND failure_count > 0")
        if row and row["c"]:
            out.append(Check("schedules", False, f"{row['c']} task(s) switched off after repeated failures", "warn", "see /schedules and the inbox; /schedule on <id> re-enables"))
        row = await ctx.db.fetchone("SELECT count(*) c FROM deliveries WHERE status = 'failed'")
        if row and row["c"]:
            out.append(Check("deliveries", False, f"{row['c']} answer(s) could not be delivered to Telegram", "warn", "they are in the Mini App transcript and answer.md in the workspace"))
    return out


async def _providers(ctx: DoctorContext) -> list[Check]:
    out: list[Check] = []
    st = ctx.settings
    for pid, provider in ctx.config.providers.items():
        base = provider.base_url or (st.vllm_base_url if provider.kind == "vllm" else "")
        if not base:
            continue
        key = provider.api_key or {"deepseek": st.deepseek_api_key, "openrouter": st.openrouter_api_key, "vllm": st.vllm_api_key}.get(provider.kind, "")
        url = base.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
                response = await client.get(url, headers={"authorization": f"Bearer {key}"} if key else {})
            ok = response.status_code < 500 and response.status_code != 401
            detail = f"HTTP {response.status_code}"
            if response.status_code == 401:
                detail += " (key rejected)"
            out.append(Check(f"provider {pid}", ok, f"{base} → {detail}", "ok" if ok else "fail", "check the key and base_url in Settings → Models"))
        except httpx.HTTPError as exc:
            out.append(Check(f"provider {pid}", False, f"{base} unreachable: {type(exc).__name__}", "fail", "network, proxy or a wrong base_url"))
    return out


async def run_and_render(ctx: DoctorContext, *, as_json: bool = False) -> str:
    checks = await run_checks(ctx)
    if as_json:
        return json.dumps({"checks": [c.as_dict() for c in checks], "summary": summarize(checks)}, indent=2)
    return render_text(checks)


__all__ = ["Check", "DoctorContext", "render_text", "run_and_render", "run_checks", "summarize"]
