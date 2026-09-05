"""The Telegram side: topics as sessions, the operator channel, files, questions."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import Attachment, SessionManager, SessionState
from daedalus.transport.telegram.markdown import markdown_to_html, split_message
from daedalus.transport.telegram.render import Outbox, RunRenderer, RunView

logger = logging.getLogger(__name__)

HELP = """<b>Daedalus</b>
Each forum topic is one agent session with its own workspace. Write in a topic to talk to that session; files you send land in its workspace.

/bind — (in a supergroup with topics) make it the session hub
/new &lt;title&gt; — new session (new topic)
/stop — stop the current run · /close — close this session's topic
/sessions · /status — what exists, what is running
/model [provider/]&lt;name&gt; · /thinking on|off|low|medium|high — model settings (default in General, per session in a topic)
/usage · /balance — spend and provider balances
/schedules · /schedule run|on|off|delete &lt;id&gt; — scheduled tasks
/approval manual|auto · /verbosity 0|1|2 — self-change approval, chat detail
/rebuild · /rollback [n] · /panic — supervisor operations
/settings · /app — configuration, Mini App link
"""


@dataclass(slots=True)
class TopicBinding:
    chat_id: int
    thread_id: int
    session_id: str
    title: str


@dataclass(slots=True)
class InboundBuffer:
    """Merges the fragments Telegram makes of one long message or one media album."""

    text: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class TelegramOutbox(Outbox):
    def __init__(self, bot: Bot, chat_id: int, thread_id: int | None) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id or None

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        if markdown:
            try:
                msg = await self.bot.send_message(
                    self.chat_id,
                    markdown_to_html(text),
                    message_thread_id=self.thread_id,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return msg.message_id
            except TelegramBadRequest:
                pass
        msg = await self.bot.send_message(
            self.chat_id, text, message_thread_id=self.thread_id, parse_mode=None
        )
        return msg.message_id

    async def edit_text(self, message_id: int, text: str) -> None:
        try:
            await self.bot.edit_message_text(
                text, chat_id=self.chat_id, message_id=message_id, parse_mode=None
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

    async def send_document(self, path: Path, caption: str | None = None) -> int:
        msg = await self.bot.send_document(
            self.chat_id,
            FSInputFile(path),
            caption=(caption or "")[:1000] or None,
            message_thread_id=self.thread_id,
            parse_mode=None,
        )
        return msg.message_id

    async def send_photo(self, path: Path, caption: str | None = None) -> int:
        msg = await self.bot.send_photo(
            self.chat_id,
            FSInputFile(path),
            caption=(caption or "")[:1000] or None,
            message_thread_id=self.thread_id,
            parse_mode=None,
        )
        return msg.message_id

    async def delete(self, message_id: int) -> None:
        try:
            await self.bot.delete_message(self.chat_id, message_id)
        except TelegramBadRequest:
            pass


class TelegramFront:
    """Wires a :class:`SessionManager` to Telegram."""

    def __init__(
        self,
        settings: Settings,
        config: RuntimeConfig,
        manager: SessionManager,
        *,
        save_config: Callable[[RuntimeConfig], Awaitable[None]],
    ) -> None:
        self.settings = settings
        self.config = config
        self.manager = manager
        self.save_config = save_config
        api = TelegramAPIServer.from_base(settings.telegram_api_base, is_local=settings.telegram_local_mode)
        self.bot = Bot(
            settings.telegram_bot_token,
            session=AiohttpSession(api=api),
            default=DefaultBotProperties(parse_mode=None),
        )
        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)
        self._renderers: dict[str, RunRenderer] = {}
        self._buffers: dict[tuple[int, int], InboundBuffer] = {}
        self._question_state: dict[str, dict[str, Any]] = {}
        self.operator_hooks: dict[str, Callable[..., Awaitable[str]]] = {}
        """rebuild / rollback / panic, installed by the application."""
        self.command_hooks: dict[str, Callable[[Message, CommandObject], Awaitable[None]]] = {}
        self.callback_hooks: dict[str, Callable[[CallbackQuery, list[str]], Awaitable[None]]] = {}
        self.message_interceptors: list[Callable[[Message], Awaitable[bool]]] = []
        """Return True to consume a message before it reaches a session (e.g. a rejection reason)."""
        self._register_handlers()
        manager.add_sink(self._on_event)
        manager.on_finished(self._on_finished)
        manager.on_pending_restored(self._on_pending_restored)
        manager.service_hooks.update(
            {
                "send_file": self._service_send_file,
                "spawn_session": self._service_spawn,
                "progress": self._service_progress,
            }
        )

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        me = await self.bot.get_me()
        logger.warning("telegram: polling as @%s", me.username)
        await self.bot.delete_webhook(drop_pending_updates=False)
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def stop(self) -> None:
        await self.dp.stop_polling()
        await self.bot.session.close()

    # -- helpers --------------------------------------------------------------------

    def _is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id == self.settings.owner_user_id

    def _general_outbox(self) -> TelegramOutbox | None:
        chat_id = self.config.telegram.forum_chat_id or self.settings.owner_user_id
        if not chat_id:
            return None
        return TelegramOutbox(self.bot, chat_id, self.config.telegram.general_topic_id or None)

    async def notify(self, text: str, *, markdown: bool = True) -> None:
        """Post to the operator channel (General topic or the private chat)."""
        outbox = self._general_outbox()
        if outbox is None:
            return
        for chunk in split_message(text):
            await outbox.send_text(chunk, markdown=markdown)

    async def binding_for_session(self, session_id: str) -> TopicBinding | None:
        row = await self.manager.db.fetchone("SELECT * FROM topics WHERE session_id = ?", (session_id,))
        return TopicBinding(row["chat_id"], row["thread_id"], row["session_id"], row["title"]) if row else None

    async def binding_for_topic(self, chat_id: int, thread_id: int) -> TopicBinding | None:
        row = await self.manager.db.fetchone(
            "SELECT * FROM topics WHERE chat_id = ? AND thread_id = ? AND closed_at IS NULL", (chat_id, thread_id)
        )
        return TopicBinding(row["chat_id"], row["thread_id"], row["session_id"], row["title"]) if row else None

    async def _bind(self, chat_id: int, thread_id: int, session_id: str, title: str) -> TopicBinding:
        await self.manager.db.execute(
            "INSERT OR REPLACE INTO topics(chat_id, thread_id, session_id, title, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, thread_id, session_id, title, datetime.now(UTC).isoformat()),
        )
        return TopicBinding(chat_id, thread_id, session_id, title)

    async def outbox_for_session(self, session_id: str) -> TelegramOutbox | None:
        binding = await self.binding_for_session(session_id)
        if binding is None:
            return self._general_outbox()
        return TelegramOutbox(self.bot, binding.chat_id, binding.thread_id)

    async def create_session_topic(
        self, title: str, *, metadata: dict[str, Any] | None = None, chat_id: int | None = None, topic: bool = True
    ) -> tuple[SessionState, TopicBinding]:
        """Create a session and, when a forum is bound, its topic."""
        state = await self.manager.create_session(title, metadata=metadata)
        forum = (chat_id or self.config.telegram.forum_chat_id) if topic else 0
        if forum:
            topic = await self.bot.create_forum_topic(forum, title[:128])
            binding = await self._bind(forum, topic.message_thread_id, state.session.id, title)
            await self.bot.send_message(
                forum,
                f"Session {state.session.id} — {title}\nworkspace: {state.workspace}",
                message_thread_id=topic.message_thread_id,
            )
        else:
            binding = await self._bind(self.settings.owner_user_id, 0, state.session.id, title)
        return state, binding

    async def _session_for_message(self, message: Message) -> SessionState | None:
        chat_id = message.chat.id
        thread_id = message.message_thread_id or 0
        if message.chat.type == "private":
            thread_id = 0
        binding = await self.binding_for_topic(chat_id, thread_id)
        if binding is not None:
            return await self.manager.get_state(binding.session_id)
        if message.chat.type == "private":
            state, _ = await self.create_session_topic("direct", topic=False)
            return state
        if message.is_topic_message and thread_id:
            # A topic the operator created by hand: adopt it as a new session.
            title = None
            if message.reply_to_message and message.reply_to_message.forum_topic_created:
                title = message.reply_to_message.forum_topic_created.name
            title = title or f"topic {thread_id}"
            state = await self.manager.create_session(title)
            await self._bind(chat_id, thread_id, state.session.id, title)
            return state
        return None

    def _is_general(self, message: Message) -> bool:
        if message.chat.type == "private":
            return True
        return not message.is_topic_message or (message.message_thread_id or 0) == 0

    # -- handlers -------------------------------------------------------------------

    def _register_handlers(self) -> None:
        r = self.router
        r.message.register(self.cmd_start, Command("start", "help"))
        r.message.register(self.cmd_new, Command("new"))
        r.message.register(self.cmd_stop, Command("stop"))
        r.message.register(self.cmd_close, Command("close"))
        r.message.register(self.cmd_sessions, Command("sessions"))
        r.message.register(self.cmd_model, Command("model"))
        r.message.register(self.cmd_thinking, Command("thinking"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_usage, Command("usage"))
        r.message.register(self.cmd_settings, Command("settings"))
        r.message.register(self.cmd_bind, Command("bind"))
        r.message.register(self.cmd_operator, Command("rebuild", "rollback", "panic", "schedules", "verbosity", "approval", "balance", "schedule"))
        r.message.register(self.on_message, F.text | F.caption | F.document | F.photo | F.audio | F.video | F.voice)
        r.callback_query.register(self.on_callback)

    async def cmd_start(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        await message.answer(HELP, parse_mode=ParseMode.HTML)

    async def cmd_bind(self, message: Message) -> None:
        """Bind this forum supergroup as the session hub."""
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        if message.chat.type not in ("supergroup", "group") or not message.chat.is_forum:
            await message.answer("Run /bind inside a supergroup with topics enabled.")
            return
        self.config.telegram.forum_chat_id = message.chat.id
        self.config.telegram.general_topic_id = 0
        await self.save_config(self.config)
        await message.answer("Bound. Create sessions with /new <title>; each topic is a session.")

    async def cmd_new(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        title = (command.args or "").strip() or datetime.now(UTC).strftime("session %m-%d %H:%M")
        if message.chat.type == "private" and not self.config.telegram.forum_chat_id:
            await message.answer(
                "Topics need a supergroup with topics enabled: add me there as an admin "
                "(with 'manage topics') and send /bind in it. In this private chat there is "
                "one session; just write to it."
            )
            return
        forum = self.config.telegram.forum_chat_id if message.chat.type == "private" else message.chat.id
        state, binding = await self.create_session_topic(title, chat_id=forum)
        if message.chat.type == "private":
            await message.answer(f"Created topic '{title}' (session {state.session.id}).")

    async def cmd_stop(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        state = await self._session_for_message(message)
        if state is None or not await self.manager.stop(state.session.id):
            await message.answer("Nothing is running here.")
            return
        await message.answer("Stopping…")

    async def cmd_close(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        if self._is_general(message):
            await message.answer("Use /close inside a session topic.")
            return
        binding = await self.binding_for_topic(message.chat.id, message.message_thread_id or 0)
        if binding is None:
            return
        await self.manager.stop(binding.session_id)
        await self.manager.db.execute(
            "UPDATE topics SET closed_at = ? WHERE chat_id = ? AND thread_id = ?",
            (datetime.now(UTC).isoformat(), binding.chat_id, binding.thread_id),
        )
        try:
            await self.bot.close_forum_topic(binding.chat_id, binding.thread_id)
        except TelegramBadRequest:
            pass

    async def cmd_sessions(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        sessions = await self.manager.list_sessions(limit=30)
        if not sessions:
            await message.answer("No sessions yet.")
            return
        lines = [f"{'▶' if s['status']=='running' else '❓' if s['status']=='waiting' else '·'} {s['title']} — {s['id']} ({s['status']})" for s in sessions]
        await message.answer("\n".join(lines))

    async def cmd_model(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        arg = (command.args or "").strip()
        if not arg:
            await message.answer(
                f"default: {self.config.model.provider}/{self.config.model.name}\n"
                f"providers: {', '.join(self.manager.providers.available())}\n"
                "usage: /model [provider/]model-name"
            )
            return
        provider, _, name = arg.rpartition("/") if "/" in arg and arg.split("/")[0] in self.manager.providers.available() else ("", "", arg)
        if self._is_general(message):
            if provider:
                self.config.model.provider = provider
            self.config.model.name = name
            await self.save_config(self.config)
            self.manager.reload_config(self.config)
            await message.answer(f"Default model: {self.config.model.provider}/{self.config.model.name}")
            return
        state = await self._session_for_message(message)
        if state is None:
            return
        await self.manager.set_model(state.session.id, model_name=name)
        await message.answer(f"Session model: {name}")

    async def cmd_thinking(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        arg = (command.args or "").strip().lower()
        thinking: bool | None = None
        effort: str | None = None
        if arg in ("on", "off"):
            thinking = arg == "on"
        elif arg in ("low", "medium", "high"):
            thinking, effort = True, arg
        else:
            await message.answer(f"thinking={self.config.model.thinking} effort={self.config.model.reasoning_effort}\nusage: /thinking on|off|low|medium|high")
            return
        if self._is_general(message):
            self.config.model.thinking = bool(thinking)
            if effort:
                self.config.model.reasoning_effort = effort  # type: ignore[assignment]
            await self.save_config(self.config)
            await message.answer(f"Default thinking={self.config.model.thinking} effort={self.config.model.reasoning_effort}")
            return
        state = await self._session_for_message(message)
        if state is None:
            return
        await self.manager.set_model(state.session.id, thinking=thinking, reasoning_effort=effort)
        await message.answer(f"Session thinking={thinking} effort={effort or self.config.model.reasoning_effort}")

    async def cmd_status(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        sessions = await self.manager.list_sessions(limit=50)
        active = [s for s in sessions if s["status"] in ("running", "waiting")]
        if not active:
            await message.answer("Idle. No active runs.")
            return
        await message.answer("\n".join(f"{s['status']}: {s['title']} ({s['id']})" for s in active))

    async def cmd_usage(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        row = await self.manager.db.fetchone(
            "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) ch, sum(cost_usd) usd"
            " FROM usage_events WHERE at >= ?",
            (today,),
        )
        text = f"today: {row['c'] or 0} calls · in {row['i'] or 0:,} · out {row['o'] or 0:,} · cached {row['ch'] or 0:,}"
        text += f" · ${row['usd']:.4f}" if row["usd"] is not None else " · cost unknown (no pricing configured)"
        state = await self._session_for_message(message) if not self._is_general(message) else None
        if state is not None:
            srow = await self.manager.db.fetchone(
                "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cost_usd) usd FROM usage_events WHERE session_id = ?",
                (state.session.id,),
            )
            text += f"\nthis session: {srow['c'] or 0} calls · in {srow['i'] or 0:,} · out {srow['o'] or 0:,}"
            text += f" · ${srow['usd']:.4f}" if srow["usd"] is not None else ""
        await message.answer(text)

    async def cmd_settings(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        c = self.config
        await message.answer(
            f"model: {c.model.provider}/{c.model.name} thinking={c.model.thinking} effort={c.model.reasoning_effort}\n"
            f"chain: {', '.join(c.model.chain)}\n"
            f"self-change approval: {c.self_change.approval}, auto_rebuild={c.self_change.auto_rebuild}\n"
            f"limits: ${self.settings.usd_per_day}/day (env), {c.limits.max_iterations} iterations, tool timeout {c.limits.tool_timeout_seconds:.0f}s\n"
            f"balance thresholds: {c.balance.thresholds_usd} (every {c.balance.poll_seconds}s)\n"
            f"verbosity: {c.telegram.verbosity}\nforum: {c.telegram.forum_chat_id or 'not bound'}"
        )

    async def cmd_operator(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        name = command.command
        if name == "verbosity":
            try:
                self.config.telegram.verbosity = max(0, min(2, int((command.args or "").strip())))
            except ValueError:
                await message.answer("usage: /verbosity 0|1|2")
                return
            await self.save_config(self.config)
            await message.answer(f"verbosity={self.config.telegram.verbosity}")
            return
        if name == "approval":
            arg = (command.args or "").strip()
            if arg not in ("manual", "auto"):
                await message.answer(f"approval={self.config.self_change.approval}\nusage: /approval manual|auto")
                return
            self.config.self_change.approval = arg  # type: ignore[assignment]
            await self.save_config(self.config)
            await message.answer(f"approval={arg}")
            return
        hook = self.command_hooks.get(name) or None
        if hook is not None:
            await hook(message, command)
            return
        op = self.operator_hooks.get(name)
        if op is None:
            await message.answer(f"/{name} is not available yet.")
            return
        await message.answer(await op(command.args or ""))

    async def on_message(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        if message.text and message.text.startswith("/"):
            return
        for interceptor in self.message_interceptors:
            if await interceptor(message):
                return
        state = await self._session_for_message(message)
        if state is None:
            return
        if await self._maybe_custom_answer(message, state):
            return
        key = (message.chat.id, message.message_thread_id or 0)
        text = message.text or message.caption or ""
        attachment = await self._download(message, state)  # may take a while for big files
        buffer = self._buffers.setdefault(key, InboundBuffer())
        if text:
            buffer.text.append(text)
        if attachment is not None:
            buffer.attachments.append(attachment)
        if buffer.task is not None:
            buffer.task.cancel()
        buffer.task = asyncio.create_task(self._flush_inbound(key, state))

    async def _flush_inbound(self, key: tuple[int, int], state: SessionState) -> None:
        await asyncio.sleep(self.config.telegram.inbound_merge_window_seconds)
        buffer = self._buffers.pop(key, None)
        if buffer is None:
            return
        text = "\n".join(buffer.text).strip()
        if not text and not buffer.attachments:
            return
        if not text:
            text = "Files attached." if len(buffer.attachments) > 1 else "File attached."
        was_running = state.running and state.pending is None
        try:
            await self.manager.submit(state.session.id, text, buffer.attachments)
        except Exception as exc:  # noqa: BLE001
            logger.exception("submit failed")
            outbox = TelegramOutbox(self.bot, key[0], key[1] or None)
            await outbox.send_text(f"⚠️ could not start: {exc}", markdown=False)
            return
        if was_running:
            renderer = self._renderers.get(state.session.id)
            if renderer is not None:
                renderer.view.narration.append("↪ follow-up queued for the next step")
                renderer._mark()

    async def _download(self, message: Message, state: SessionState) -> Attachment | None:
        file_id: str | None = None
        name: str | None = None
        mime = "application/octet-stream"
        if message.document:
            file_id, name, mime = message.document.file_id, message.document.file_name, message.document.mime_type or mime
        elif message.photo:
            photo = message.photo[-1]
            file_id, name, mime = photo.file_id, f"photo-{photo.file_unique_id}.jpg", "image/jpeg"
        elif message.video:
            file_id, name, mime = message.video.file_id, message.video.file_name or f"video-{message.video.file_unique_id}.mp4", message.video.mime_type or "video/mp4"
        elif message.audio:
            file_id, name, mime = message.audio.file_id, message.audio.file_name or f"audio-{message.audio.file_unique_id}.mp3", message.audio.mime_type or "audio/mpeg"
        elif message.voice:
            file_id, name, mime = message.voice.file_id, f"voice-{message.voice.file_unique_id}.ogg", "audio/ogg"
        if file_id is None:
            return None
        inbox = state.workspace / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / (Path(name).name if name else file_id)
        try:
            tg_file = await self.bot.get_file(file_id)
            if self.settings.telegram_local_mode and tg_file.file_path and Path(tg_file.file_path).is_absolute():
                shutil.copy2(tg_file.file_path, target)
            else:
                await self.bot.download(tg_file, destination=target)
        except Exception as exc:  # noqa: BLE001
            logger.exception("download failed")
            await message.reply(f"⚠️ could not download {name}: {exc}")
            return None
        return Attachment(path=target, mime_type=mime or mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    # -- questions ------------------------------------------------------------------

    async def _ask(self, session_id: str, payload: dict[str, Any]) -> None:
        outbox = await self.outbox_for_session(session_id)
        if outbox is None:
            return
        questions = payload.get("questions", [])
        state = {"answers": [{"question": q.get("question"), "selected": [], "custom": None} for q in questions], "index": 0, "questions": questions, "message_ids": []}
        self._question_state[session_id] = state
        await self._send_question(session_id, outbox)

    async def _send_question(self, session_id: str, outbox: TelegramOutbox) -> None:
        state = self._question_state[session_id]
        index = state["index"]
        q = state["questions"][index]
        options = q.get("options") or []
        rows = []
        selected = set(state["answers"][index]["selected"])
        for oi, option in enumerate(options):
            label = option.get("label", "")
            mark = "☑ " if label in selected else ""
            rows.append([InlineKeyboardButton(text=f"{mark}{label}"[:60], callback_data=f"aq:{session_id}:{index}:{oi}")])
        if q.get("multiSelect"):
            rows.append([InlineKeyboardButton(text="✅ Done", callback_data=f"aq:{session_id}:{index}:done")])
        if q.get("allow_custom") or not options:
            rows.append([InlineKeyboardButton(text="✍️ Type an answer", callback_data=f"aq:{session_id}:{index}:custom")])
        text = f"❓ {q.get('question')}"
        details = [f"• {o.get('label')}: {o.get('description')}" for o in options if o.get("description")]
        if details:
            text += "\n" + "\n".join(details)
        if len(state["questions"]) > 1:
            text += f"\n({index + 1}/{len(state['questions'])})"
        msg = await self.bot.send_message(outbox.chat_id, text, message_thread_id=outbox.thread_id, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        state["message_ids"].append(msg.message_id)

    async def on_callback(self, query: CallbackQuery) -> None:
        if not self._is_owner(query.from_user.id):
            await query.answer()
            return
        data = (query.data or "").split(":")
        if not data:
            return
        if data[0] == "aq":
            await self._on_answer(query, data)
            return
        hook = self.callback_hooks.get(data[0])
        if hook is not None:
            await hook(query, data)
            return
        await query.answer()

    async def _on_answer(self, query: CallbackQuery, data: list[str]) -> None:
        if len(data) != 4 or not data[2].isdigit() or not (data[3].isdigit() or data[3] in ("done", "custom")):
            await query.answer("stale button")
            return
        _, session_id, index_s, choice = data
        state = self._question_state.get(session_id)
        if state is None:
            await query.answer("This question is no longer open.")
            return
        index = int(index_s)
        q = state["questions"][index]
        answer = state["answers"][index]
        if choice == "custom":
            await query.answer()
            if query.message is not None:
                await self.bot.send_message(query.message.chat.id, "Type your answer:", message_thread_id=query.message.message_thread_id if query.message.is_topic_message else None, reply_markup=ForceReply(selective=True))
            state["awaiting_custom"] = index
            return
        if choice == "done":
            await query.answer()
            await self._advance_question(session_id, query)
            return
        label = (q.get("options") or [])[int(choice)].get("label")
        if q.get("multiSelect"):
            if label in answer["selected"]:
                answer["selected"].remove(label)
            else:
                answer["selected"].append(label)
            await query.answer(", ".join(answer["selected"]) or "nothing selected")
            outbox = await self.outbox_for_session(session_id)
            if query.message is not None and outbox is not None:
                state["index"] = index
                rows = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
                new_rows = []
                for row in rows:
                    new_row = []
                    for btn in row:
                        cd = btn.callback_data or ""
                        base = btn.text[2:] if btn.text.startswith("☑ ") else btn.text
                        if cd.endswith(":done") or cd.endswith(":custom"):
                            new_row.append(btn)
                        else:
                            new_row.append(InlineKeyboardButton(text=("☑ " if base in answer["selected"] else "") + base, callback_data=cd))
                    new_rows.append(new_row)
                try:
                    await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=new_rows))
                except TelegramBadRequest:
                    pass
            return
        answer["selected"] = [label]
        await query.answer(label)
        await self._advance_question(session_id, query)

    async def _advance_question(self, session_id: str, query: CallbackQuery | None) -> None:
        state = self._question_state.get(session_id)
        if state is None:
            return
        if query is not None and query.message is not None:
            try:
                answered = state["answers"][state["index"]]
                summary = ", ".join(answered["selected"]) or (answered["custom"] or "")
                await query.message.edit_text(f"{query.message.text}\n→ {summary}", reply_markup=None)
            except TelegramBadRequest:
                pass
        state["index"] += 1
        if state["index"] < len(state["questions"]):
            outbox = await self.outbox_for_session(session_id)
            if outbox is not None:
                await self._send_question(session_id, outbox)
            return
        try:
            await self.manager.answer(session_id, state["answers"])
        except RuntimeError as exc:
            outbox = await self.outbox_for_session(session_id)
            if outbox is not None:
                await outbox.send_text(f"⚠️ could not deliver the answer: {exc}. Answer again in a moment.", markdown=False)
            state["index"] = max(0, len(state["questions"]) - 1)
            return
        self._question_state.pop(session_id, None)

    async def _maybe_custom_answer(self, message: Message, state: SessionState) -> bool:
        qs = self._question_state.get(state.session.id)
        if qs is None or "awaiting_custom" not in qs:
            return False
        index = qs.pop("awaiting_custom")
        qs["answers"][index]["custom"] = message.text or message.caption or ""
        qs["index"] = index
        await self._advance_question(state.session.id, None)
        return True

    # -- events from runs -----------------------------------------------------------

    async def _renderer_for(self, session_id: str, run_id: str) -> RunRenderer | None:
        renderer = self._renderers.get(session_id)
        if renderer is not None and renderer.view.run_id == run_id:
            return renderer
        outbox = await self.outbox_for_session(session_id)
        if outbox is None:
            return None
        state = await self.manager.get_state(session_id)
        model = state.engine.effective_model_name if state and state.engine else self.config.model.name

        async def cost_lookup(rid: str) -> float | None:
            row = await self.manager.db.fetchone("SELECT sum(cost_usd) c, count(*) n FROM usage_events WHERE run_id = ?", (rid,))
            return float(row["c"]) if row and row["c"] is not None else None

        renderer = RunRenderer(
            outbox,
            RunView(run_id=run_id, model=model, verbosity=self.config.telegram.verbosity),
            edit_interval=self.config.telegram.status_edit_interval_seconds,
            cost_lookup=cost_lookup,
        )
        self._renderers[session_id] = renderer
        return renderer

    async def _on_event(self, session_id: str, event: TurnEvent) -> None:
        renderer = await self._renderer_for(session_id, event.run_id)
        if renderer is None:
            return
        await renderer.handle(event)
        if event.type is EventType.TOOL_CALL_PENDING and event.payload.get("kind") == "ask_user":
            await renderer.flush()
            await self._ask(session_id, dict(event.payload.get("ask_user_payload") or {}))

    async def _on_pending_restored(self, session_id: str, pending: Any) -> None:
        """After a restart, post the open question again with a fresh keyboard."""
        outbox = await self.outbox_for_session(session_id)
        if outbox is not None:
            await outbox.send_text("↩️ Restarted while waiting for your answer; here is the question again.", markdown=False)
        await self._ask(session_id, dict(pending.payload))

    async def _on_finished(self, session_id: str, run_id: str, status: str) -> None:
        renderer = self._renderers.get(session_id)
        if renderer is None or renderer.view.run_id != run_id:
            return
        state = await self.manager.get_state(session_id)
        if status == "awaiting":
            renderer.view.state = "awaiting"
            await renderer.flush()
            return
        await renderer.finish(status, workspace=state.workspace if state else Path("/tmp"))
        self._renderers.pop(session_id, None)

    # -- services for tools ---------------------------------------------------------

    async def _service_send_file(self, session_id: str, path: Path, caption: str | None) -> str:
        outbox = await self.outbox_for_session(session_id)
        if outbox is None:
            return "no chat bound"
        mime = mimetypes.guess_type(path.name)[0] or ""
        if mime.startswith("image/") and path.stat().st_size < 10_000_000:
            await outbox.send_photo(path, caption)
        else:
            await outbox.send_document(path, caption)
        return "delivered"

    async def _service_spawn(self, session_id: str, title: str, prompt: str, files: list[str]) -> str:
        state, _ = await self.create_session_topic(title)
        attachments = [Attachment(path=Path(f), mime_type=mimetypes.guess_type(f)[0] or "application/octet-stream") for f in files if Path(f).is_file()]
        await self.manager.submit(state.session.id, prompt, attachments)
        return state.session.id

    async def _service_progress(self, session_id: str, line: str) -> None:
        renderer = self._renderers.get(session_id)
        if renderer is not None:
            await renderer.progress(line)


def _unused(_: ReplyParameters | None = None) -> None:
    return None


__all__ = ["TelegramFront", "TelegramOutbox", "TopicBinding"]
