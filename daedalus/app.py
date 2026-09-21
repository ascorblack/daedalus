"""Application composition: stores → sessions → API (+ Telegram, scheduler, monitors).

Telegram is one front among the ways in, not the way in: with no bot token the same
installation runs on its API and its app alone. Everything that used to speak to the chat
goes through :meth:`Application.notify` and :meth:`Application.create_session`, which fall
back to the inbox and to a plain session when there is no front to speak to.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.boot_guard import BootGuard
from daedalus.host.component_install import Installer
from daedalus.host.config_validation import ConfigConflict, config_revision
from daedalus.host.session_runner import SessionManager, SessionState
from daedalus.providers.llamacpp import describe_discovery, discover_llamacpp
from daedalus.search.service import ConversationSearch
from daedalus.speech.service import LocalSpeech
from daedalus.speech.tts_service import LocalTts
from daedalus.stores.database import Database
from daedalus.transport.telegram.front import TelegramFront

logger = logging.getLogger(__name__)


class Application:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config = RuntimeConfig.load(settings.config_path)
        self.db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
        self.manager: SessionManager | None = None
        self.front: TelegramFront | None = None
        self.background: list[asyncio.Task[None]] = []
        self.extensions: dict[str, object] = {}
        self.extension_failures: dict[str, str] = {}
        """Subsystems that raised while installing, by name. Empty on a healthy start; see
        :data:`daedalus.extensions.FATAL` for why a failure here is survivable."""
        self.stopping = asyncio.Event()
        self._shut_down = False
        self._config_lock = asyncio.Lock()
        # The local speech models: a directory listing and a configuration read, no engine and no
        # model until something actually asks for words.
        self.search = ConversationSearch(self.db, settings.state_dir)
        self.speech = LocalSpeech(settings.state_dir, self.config)
        # And the voices it speaks with, on the same terms: a directory listing and a configuration
        # read, no engine and no voice until something actually asks to be heard.
        self.tts = LocalTts(settings.state_dir, self.config)
        # What the installation is missing and how a missing piece arrives. It is held here rather
        # than built per request because an install outlives the request that asked for it: the
        # headless browser takes minutes, and the page that started it may be reloaded meanwhile.
        self.components = Installer(settings)
        self.guard = BootGuard(settings.state_dir, window_minutes=self.config.ops.boot_loop_window_minutes, threshold=self.config.ops.boot_loop_threshold)

    async def save_config(self, config: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        async with self._config_lock:
            old = self.config
            if expected_revision is not None and config_revision(old) != expected_revision:
                raise ConfigConflict(config_revision(old))
            config.save(self.settings.config_path)
            try:
                if self.manager is not None:
                    self.manager.reload_config(config)
            except BaseException:
                old.save(self.settings.config_path)
                if self.manager is not None:
                    self.manager.reload_config(old)
                raise
            self.config = config
            self.speech.config = config
            self.tts.config = config
            was = old.stt
            if (was.local_model, was.local_language, was.local_threads) != (config.stt.local_model, config.stt.local_language, config.stt.local_threads):
                # A different model, language or thread count is a different recogniser; the loaded one
                # is now the wrong one and holds most of a gigabyte while being it.
                self.speech.forget()
            spoke, now = old.voice.tts, config.voice.tts
            if (spoke.local_voice, spoke.local_threads) != (now.local_voice, now.local_threads):
                # A different voice or thread count is a different synthesiser. The speaker inside a
                # multi-voice model is not, and neither is the speed: both are arguments to every call,
                # so dropping a loaded model for either is a second of silence bought for nothing.
                self.tts.forget()

    async def start(self) -> None:
        self.guard.on_boot()
        await self.db.open()
        self.manager = SessionManager(self.settings, self.config, db=self.db)
        await self.manager.start()
        self.search.manager = self.manager
        self.search.task = asyncio.create_task(self.search.run(), name="conversation-index")
        await self._log_llamacpp_startup()
        if self.settings.telegram_bot_token and not self.settings.owner_user_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is set without OWNER_USER_ID: the bot would not know whose messages to answer")
        if self.settings.telegram_bot_token:
            self.front = TelegramFront(self.settings, self.config, self.manager, save_config=self.save_config, speech=self.speech)
        await self._install_extensions()
        # The voice the operator chose is built now rather than by the first answer they ask for.
        # It costs a second or two of a start that is already doing several, and it is the difference
        # between an answer that is read out as it is written and one that is read out after it.
        self.tts.warm()
        await self._report_startup()
        if self.guard.skip_recovery:
            note = (
                f"⚠️ {self.guard.unclean_boots} unclean restarts in a row: boot recovery (resuming runs, re-sending "
                "answers) is skipped this once so the bot stays up. Unfinished runs stay parked; /doctor shows them."
            )
            if self.front is not None:
                await self.front.notify(note, markdown=False)  # the chat is the alarm; the entry below is the record
            inbox = self.extensions.get("inbox")
            if inbox is not None:
                await inbox.post("boot_guard", "Boot recovery skipped after repeated crashes", note, severity="error")  # type: ignore[attr-defined]
            return
        resumed = await self.manager.resume_unfinished()
        if resumed:
            await self.notify(f"Resumed {len(resumed)} run(s) after restart.", markdown=False, kind="startup")
        if stale := self.manager.stale_runs:
            # Natively the agent is a process under the launcher, so closing the window is a stop.
            # A run that was working when it happened is worth a line: the operator's question the
            # next morning is "what happened to it", and the answer belongs where they will look.
            why = (
                " Closing the app stops the agent with it — leave the window open while a run is working."
                if self.settings.native
                else ""
            )
            await self.notify(f"{len(stale)} run(s) were interrupted by the last stop and could not be resumed.{why}", markdown=False, kind="startup", severity="notice")
        if self.front is not None:
            resent = await self.front.redeliver_pending()
            if resent:
                await self.front.notify(f"Re-sent {resent} answer(s) the previous process had not confirmed as delivered.", markdown=False)

    async def _log_llamacpp_startup(self) -> None:
        """Record what each configured local server says before the first run needs it."""

        async def probe(provider_id: str, provider: Any) -> None:
            result = await discover_llamacpp(provider.base_url, provider.api_key)
            logger.info("llama.cpp provider %s: %s", provider.name or provider_id, describe_discovery(result))

        await asyncio.gather(
            *(probe(provider_id, provider) for provider_id, provider in self.config.providers.items() if provider.kind == "llamacpp")
        )

    async def notify(self, text: str, *, markdown: bool = True, kind: str = "notice", severity: str = "info") -> None:
        """Say something to the operator: the chat when Telegram is configured, the inbox when it is not.

        Without a front the inbox is the only channel the operator reads, so a message that would have
        been a chat line becomes an entry there rather than disappearing into the log.
        """
        if self.front is not None:
            await self.front.notify(text, markdown=markdown)
            return
        inbox = self.extensions.get("inbox")
        if inbox is None:  # before the extensions are installed there is nowhere to put it but the log
            logger.warning("%s", text)
            return
        headline, _, body = text.partition("\n")
        await inbox.post(kind, headline.strip().strip("*_ ") or kind, body.strip(), severity=severity)  # type: ignore[attr-defined]

    async def create_session(self, title: str, *, metadata: dict[str, Any] | None = None, workspace: Path | None = None, project_id: str | None = None, own_directory: bool = False) -> SessionState:
        """A session with its chat topic where Telegram is configured, a plain session where it is not."""
        assert self.manager is not None
        if workspace is not None and project_id is None:
            project_id = (await self.manager.projects.adopt_directory(title, workspace)).id
        if self.front is None:
            return await self.manager.create_session(title, workspace=workspace, metadata=metadata, project_id=project_id, own_directory=own_directory)
        state, _binding = await self.front.create_session_topic(title, metadata=metadata, project_id=project_id, own_directory=own_directory)
        return state

    async def _report_startup(self) -> None:
        """Tell the operator about a failed rebuild or an exhausted budget."""
        failed = self.settings.state_dir / "good" / "FAILED"
        if failed.exists():
            text = failed.read_text(encoding="utf-8")
            await self.notify("❌ The last rebuild failed preflight and was rolled back:\n\n" + text[-3000:], markdown=False, kind="rebuild", severity="error")
            failed.rename(failed.with_suffix(".reported"))
        last = self.settings.state_dir / "good" / "LAST_REBUILD"
        if last.exists():
            await self.notify("🔄 " + last.read_text(encoding="utf-8").strip()[-1500:], markdown=False, kind="rebuild", severity="notice")
            last.rename(last.with_suffix(".reported"))
        if self.extension_failures:
            # The inbox already holds one entry per subsystem; this is the line in the chat, because a
            # bot that came up without its scheduler looks entirely healthy until something does not happen.
            broken = ", ".join(f"{name} ({reason.split(':')[0]})" for name, reason in self.extension_failures.items())
            await self.notify(f"⚠️ The bot started without {len(self.extension_failures)} subsystem(s): {broken}. The inbox has the error for each.", markdown=False, kind="extension", severity="error")
        exceeded = self.manager.budget_exceeded() if self.manager else None
        if exceeded:
            await self.notify(f"💸 Daily budget exceeded ({exceeded}). New runs are refused until tomorrow or /budget reset.", markdown=False, kind="budget", severity="warning")

    async def _install_extensions(self) -> None:
        """Scheduler, balance monitor, self-development, API — each attaches here."""
        from daedalus.extensions import (
            install_all,  # Lazy: extensions import the Application type; a top-level import would be a cycle
        )

        assert self.manager is not None
        self.background.extend(await install_all(self))

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)

    async def run(self) -> None:
        if self.front is None:
            # Nothing to poll: the API, the scheduler and the loops carry the installation on their own.
            await self.stopping.wait()
            await self.shutdown()
            return
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
            await self.search.close()
            await self.speech.downloads.close()
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
