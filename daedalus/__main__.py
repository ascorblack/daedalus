"""Command-line entry point.

``daedalus serve``  — run the bot (Telegram + API + scheduler).
``daedalus check``  — open the state, register tools and providers, exit.
``daedalus run``    — run one agent session from the terminal (no Telegram).
``daedalus doctor`` — check the deployment (config, state, git, providers); ``--fix`` applies safe repairs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from daedalus.config import RuntimeConfig, Settings


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings()
    if args.state_dir:
        settings.state_dir = Path(args.state_dir)
    if args.workspaces_dir:
        settings.workspaces_dir = Path(args.workspaces_dir)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    return settings


async def cmd_check(args: argparse.Namespace) -> int:
    from daedalus.host.session_runner import SessionManager  # Lazy: each subcommand imports only what it runs
    from daedalus.stores.database import Database  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    config = RuntimeConfig.load(settings.config_path)
    db = Database(settings.db_path)
    await db.open()
    try:
        manager = SessionManager(settings, config, db=db)
        await manager.start()
        tools = sorted(t.name for t in manager.tools.list_all())
        print(f"state: {settings.state_dir}")
        print(f"config: {settings.config_path}")
        print(f"repos: bot={settings.bot_repo_dir} core={settings.core_repo_dir}")
        print(f"providers: {', '.join(manager.providers.available()) or '(none configured)'}")
        pid, preset = config.preset()
        print(f"model: {pid} = {preset.provider}/{preset.model} thinking={preset.thinking} effort={preset.reasoning_effort}")
        print(f"presets: {', '.join(config.presets)}"),
        print(f"tools ({len(tools)}): {', '.join(tools)}")
        skills = await manager.skills.list("daedalus")
        print(f"skills ({len(skills)}): {', '.join(s.name for s in skills) or '(none)'}")
        await manager.close()
    finally:
        await db.close()
    return 0


async def cmd_doctor(args: argparse.Namespace) -> int:
    from daedalus.doctor import (  # Lazy: each subcommand imports only what it runs
        DoctorContext,
        render_text,
        run_checks,
        summarize,
    )
    from daedalus.host.session_runner import SessionManager  # Lazy: each subcommand imports only what it runs
    from daedalus.stores.database import Database  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    config = RuntimeConfig.load(settings.config_path)
    db = Database(settings.db_path)
    await db.open()
    try:
        manager = SessionManager(settings, config, db=db)
        await manager.start()
        try:
            checks = await run_checks(DoctorContext(settings=settings, config=config, db=db, manager=manager, fix=args.fix))
        finally:
            await manager.close()
    finally:
        await db.close()
    if args.json:
        import json  # Lazy: only the --json output needs it

        print(json.dumps({"checks": [c.as_dict() for c in checks], "summary": summarize(checks)}, indent=2))
    else:
        print(render_text(checks))
    return 1 if summarize(checks)["fail"] else 0


async def cmd_run(args: argparse.Namespace) -> int:
    from daedalus.host.cli_session import run_terminal_session  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    config = RuntimeConfig.load(settings.config_path)
    return await run_terminal_session(settings, config, prompt=args.prompt, title=args.title)


async def cmd_bench(args: argparse.Namespace) -> int:
    from daedalus.bench.manifest import Manifest  # Lazy: each subcommand imports only what it runs
    from daedalus.bench.runner import BenchRunner  # Lazy: each subcommand imports only what it runs

    if not args.state_dir:
        fallback = Settings().bench_state_dir
        if fallback is None:
            print("bench needs its own state directory: pass --state-dir (or set BENCH_STATE_DIR); the bot's database is never used for benchmarks", file=sys.stderr)
            return 2
        args.state_dir = str(fallback)
    settings = _settings(args)
    config = RuntimeConfig.load(settings.config_path)
    manifest = Manifest.load(Path(args.manifest))
    out_dir = Path(args.out) if args.out else settings.state_dir / "bench" / f"{manifest.name}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    only = [t.strip() for t in (args.only or "").split(",") if t.strip()] or None
    async with BenchRunner(settings, config, preset=args.preset, out_dir=out_dir) as runner:
        records = await runner.run(manifest, concurrency=args.concurrency, only=only)
    judged = [r for r in records if r.passed is not None]
    passed = sum(1 for r in judged if r.passed)
    cost = sum(r.cost_usd for r in records if r.cost_usd is not None)
    print(f"{manifest.name}: {passed}/{len(judged)} passed of {len(records)} tasks; cost ${cost:.4f}; records in {out_dir}")
    return 0


async def cmd_serve(args: argparse.Namespace) -> int:
    from daedalus.app import serve  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    return await serve(settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daedalus")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--workspaces-dir", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate configuration and registration, then exit")
    doctor = sub.add_parser("doctor", help="check the deployment: config, state, git, providers")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--fix", action="store_true", help="apply the safe fixes (stale snapshots, orphan workspaces)")
    run = sub.add_parser("run", help="run one session from the terminal")
    run.add_argument("--prompt", "-p", default=None, help="first message; omit for interactive input")
    run.add_argument("--title", default="terminal")
    sub.add_parser("serve", help="run the bot")
    bench = sub.add_parser("bench", help="run a task manifest headless and record pass/turns/tokens/cost per task")
    bench.add_argument("manifest", help="JSON manifest: {name, tasks: [{id, prompt, setup, check, files, timeout_minutes, tags}], tools_off}")
    bench.add_argument("--preset", default=None, help="model preset id for every task (default: the configured default)")
    bench.add_argument("--concurrency", type=int, default=1)
    bench.add_argument("--only", default=None, help="comma-separated task ids")
    bench.add_argument("--out", default=None, help="output directory (default: <state>/bench/<name>-<timestamp>)")
    return parser


class _DiagFilter(logging.Filter):
    """The core logs its DIAG traces at WARNING; keep them out of production logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("DIAG ")


def _install_task_dump() -> None:
    """SIGUSR1 prints every asyncio task's stack — the way to see what a stuck bot awaits."""
    import signal  # Lazy: only the dump handler needs it
    import traceback  # Lazy: only the dump handler needs it

    def dump(signum: int, frame: object) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        sys.stderr.write("=== asyncio tasks ===\n")
        for task in asyncio.all_tasks(loop):
            sys.stderr.write(f"--- {task.get_name()} done={task.done()}\n")
            for f in task.get_stack(limit=12):
                sys.stderr.write("".join(traceback.format_stack(f, limit=1)))
        sys.stderr.flush()

    signal.signal(signal.SIGUSR1, dump)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from daedalus.security.redact import (  # Lazy: logging is configured before the package is imported
        install_logging_filter,
        shared,
    )

    for handler_ in logging.getLogger().handlers:
        handler_.addFilter(_DiagFilter())
    install_logging_filter(shared())
    _install_task_dump()
    handler = {"check": cmd_check, "run": cmd_run, "serve": cmd_serve, "doctor": cmd_doctor, "bench": cmd_bench}[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
