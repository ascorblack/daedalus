"""Application composition: stores → sessions → Telegram (+ API, scheduler, monitors)."""

from __future__ import annotations

import asyncio
import logging
import signal

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.boot_guard import BootGuard
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.transport.telegram.front import TelegramFront

logger = logging.getLogger(__name__)


class Application:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config = RuntimeConfig.load(settings.config_path)
        self.db = Database(settings.db_path)
        self.manager: SessionManager | None = None
        self.front: TelegramFront | None = None
        self.background: list[asyncio.Task[None]] = []
        self.extensions: dict[str, object] = {}
        self.stopping = asyncio.Event()
        self._shut_down = False
        self.guard = BootGuard(settings.state_dir, window_minutes=self.config.ops.boot_loop_window_minutes, threshold=self.config.ops.boot_loop_threshold)

    async def save_config(self, config: RuntimeConfig) -> None:
        self.config = config
        config.save(self.settings.config_path)
        if self.manager is not None:
            self.manager.reload_config(config)

    async def start(self) -> None:
        self.guard.on_boot()
        await self.db.open()
        self.manager = SessionManager(self.settings, self.config, db=self.db)
        await self.manager.start()
        if not self.settings.telegram_bot_token or not self.settings.owner_user_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and OWNER_USER_ID must be set")
        self.front = TelegramFront(self.settings, self.config, self.manager, save_config=self.save_config)
        await self._install_extensions()
        await self._report_startup()
        if self.guard.skip_recovery:
            note = (
                f"⚠️ {self.guard.unclean_boots} unclean restarts in a row: boot recovery (resuming runs, re-sending "
                "answers) is skipped this once so the bot stays up. Unfinished runs stay parked; /doctor shows them."
            )
            await self.front.notify(note, markdown=False)
            inbox = self.extensions.get("inbox")
            if inbox is not None:
                await inbox.post("boot_guard", "Boot recovery skipped after repeated crashes", note, severity="error")  # type: ignore[attr-defined]
            return
        resumed = await self.manager.resume_unfinished()
        if resumed:
            await self.front.notify(f"Resumed {len(resumed)} run(s) after restart.", markdown=False)
        resent = await self.front.redeliver_pending()
        if resent:
            await self.front.notify(f"Re-sent {resent} answer(s) the previous process had not confirmed as delivered.", markdown=False)

    async def _report_startup(self) -> None:
        """Tell the operator about a failed rebuild or an exhausted budget."""
        assert self.front is not None
        failed = self.settings.state_dir / "good" / "FAILED"
        if failed.exists():
            text = failed.read_text(encoding="utf-8")
            await self.front.notify("❌ The last rebuild failed preflight and was rolled back:\n\n" + text[-3000:], markdown=False)
            failed.rename(failed.with_suffix(".reported"))
        last = self.settings.state_dir / "good" / "LAST_REBUILD"
        if last.exists():
            await self.front.notify("🔄 " + last.read_text(encoding="utf-8").strip()[-1500:], markdown=False)
            last.rename(last.with_suffix(".reported"))
        exceeded = self.manager.budget_exceeded() if self.manager else None
        if exceeded:
            await self.front.notify(f"💸 Daily budget exceeded ({exceeded}). New runs are refused until tomorrow or /budget reset.", markdown=False)

    async def _install_extensions(self) -> None:
        """Scheduler, balance monitor, self-development, API — each attaches here."""
        from daedalus.extensions import (
            install_all,  # Lazy: extensions import the Application type; a top-level import would be a cycle
        )

        assert self.manager is not None and self.front is not None
        self.background.extend(await install_all(self))

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)

    async def run(self) -> None:
        assert self.front is not None
        polling = asyncio.create_task(self.front.start(), name="telegram-polling")
        stop_waiter = asyncio.create_task(self.stopping.wait(), name="stop-waiter")
        done, _ = await asyncio.wait({polling, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if polling in done and polling.exception() is not None:
            raise polling.exception()  # type: ignore[misc]
        await self.shutdown()
        if not polling.done():
            await polling

    async def shutdown(self) -> None:
        """Stop everything; runs are drained before the transport closes so a finishing answer still reaches the chat."""
        if self._shut_down:
            return
        self._shut_down = True
        try:
            for task in self.background:
                task.cancel()
            if self.manager is not None:
                await self.manager.close()
            if self.front is not None:
                await self.front.stop()
            await self.db.close()
        finally:
            self.guard.on_clean_shutdown()  # a deliberate stop is clean even when a step above failed


async def serve(settings: Settings) -> int:
    app = Application(settings)
    app.install_signal_handlers()
    try:
        await app.start()
        await app.run()
    except Exception:
        logger.exception("fatal")
        await app.shutdown()
        return 1
    return 0


__all__ = ["Application", "serve"]
