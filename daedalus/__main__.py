"""Command-line entry point.

``daedalus serve``  — run the bot (Telegram + API + scheduler).
``daedalus check``  — open the state, register tools and providers, exit.
``daedalus run``    — run one agent session from the terminal (no Telegram).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
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
    from daedalus.host.session_runner import SessionManager
    from daedalus.stores.database import Database

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
        print(
            f"model: {config.model.provider}/{config.model.name}"
            f" thinking={config.model.thinking} effort={config.model.reasoning_effort}"
        )
        print(f"tools ({len(tools)}): {', '.join(tools)}")
        skills = await manager.skills.list("daedalus")
        print(f"skills ({len(skills)}): {', '.join(s.name for s in skills) or '(none)'}")
        await manager.close()
    finally:
        await db.close()
    return 0


async def cmd_run(args: argparse.Namespace) -> int:
    from daedalus.host.cli_session import run_terminal_session

    settings = _settings(args)
    config = RuntimeConfig.load(settings.config_path)
    return await run_terminal_session(settings, config, prompt=args.prompt, title=args.title)


async def cmd_serve(args: argparse.Namespace) -> int:
    from daedalus.app import serve

    settings = _settings(args)
    return await serve(settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daedalus")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--workspaces-dir", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate configuration and registration, then exit")
    run = sub.add_parser("run", help="run one session from the terminal")
    run.add_argument("--prompt", "-p", default=None, help="first message; omit for interactive input")
    run.add_argument("--title", default="terminal")
    sub.add_parser("serve", help="run the bot")
    return parser


class _DiagFilter(logging.Filter):
    """The core logs its DIAG traces at WARNING; keep them out of production logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("DIAG ")


def _install_task_dump() -> None:
    """SIGUSR1 prints every asyncio task's stack — the way to see what a stuck bot awaits."""
    import signal
    import traceback

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
    for handler_ in logging.getLogger().handlers:
        handler_.addFilter(_DiagFilter())
    _install_task_dump()
    handler = {"check": cmd_check, "run": cmd_run, "serve": cmd_serve}[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
