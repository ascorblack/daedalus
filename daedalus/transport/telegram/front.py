"""The Telegram side: topics as sessions, the operator channel, files, questions."""

from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import shutil
import time
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
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
    ReactionTypeEmoji,
    ReplyParameters,
)
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import Attachment, SessionManager, SessionState
from daedalus.transport.telegram.markdown import markdown_to_html, split_message, strip_tags
from daedalus.transport.telegram.render import Outbox, RunRenderer, RunView

logger = logging.getLogger(__name__)

HELP = """<b>Daedalus</b>
Each forum topic is one agent session with its own workspace. Write in a topic to talk to that session; files you send land in its workspace.

/bind — (in a supergroup with topics) make it the session hub
/new &lt;title&gt; — new session (new topic)
/stop — stop the current run · /close — close this topic (asks whether to delete the agent and its workspace)
/rename &lt;title&gt; — rename this session (and its topic) · /compact [focus] — replace the history with a summary
/delete &lt;id&gt; · /cleanup — delete a session; delete every session whose topic is already closed
/sessions · /status — what exists, what is running
/model [provider/]&lt;name&gt;|default · /thinking on|off|low|medium|high — model settings (default in General, per session in a topic)
/usage · /balance — spend and provider balances
/schedules · /schedule run|on|off|delete &lt;id&gt; — scheduled tasks
/inbox [all|clear] · /heartbeat [on|off|run] · /doctor — what happened while you were away, the periodic check, health
/approval manual|auto · /verbosity 0|1|2 — self-change approval, chat detail
/rebuild · /rollback [n] · /panic — supervisor operations
/prompt — show the editable working rules (edit them in the Mini App → Settings)
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


_FLOOD_UNTIL: dict[int, float] = {}
"""Per chat: the monotonic time until which Telegram asked us to stop. Shared by every
message in the chat, so one flood-controlled edit pauses all the cosmetic edits at once."""


def flooded(chat_id: int) -> bool:
    return time.monotonic() < _FLOOD_UNTIL.get(chat_id, 0.0)


def note_flood(chat_id: int, seconds: float) -> None:
    _FLOOD_UNTIL[chat_id] = max(_FLOOD_UNTIL.get(chat_id, 0.0), time.monotonic() + seconds)


async def tg_call(fn: Callable[..., Awaitable[Any]], *args: Any, attempts: int = 4, flood_chat: int | None = None, **kwargs: Any) -> Any:
    """Call a Bot API method, waiting out flood-control pauses instead of failing.

    ``flood_chat`` records the pause for that chat so optional edits skip it instead of queueing.
    """
    for attempt in range(attempts):
        try:
            result = await fn(*args, **kwargs)
            if attempt and flood_chat is not None:
                _FLOOD_UNTIL.pop(flood_chat, None)  # the pause was waited out; edits may resume
            return result
        except TelegramRetryAfter as exc:
            if flood_chat is not None:
                note_flood(flood_chat, float(exc.retry_after))
            if attempt == attempts - 1:
                raise
            logger.warning("telegram flood control: waiting %ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 0.5)
    raise RuntimeError("unreachable")


FREE_REACTIONS = frozenset(
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡".split()
)
"""The emoji a bot may react with (Telegram rejects anything else)."""

RUN_REACTIONS = {"received": "👀", "steered": "✍", "completed": "🔥", "failed": "💔", "cancelled": "🫡", "awaiting": "🤔", "interrupted": "😴", "busy": "🤝"}
TOPIC_STATUS_PREFIX = {"running": "🟢", "awaiting": "🔴", "completed": "🏁", "failed": "💥", "cancelled": "⏹", "interrupted": "⏸", "compacting": "🗜"}
"""Telegram silently drops some emoji from the start of a topic name (✅ ❓ ✔️ ☑️ were measured to
vanish, and a repeat rename then fails with TOPIC_NOT_MODIFIED); every prefix here was verified to survive."""
TOPIC_RENAME_DEBOUNCE_SECONDS = 2.0
TOPIC_RENAME_MAX_WAIT_SECONDS = 60.0
STALE_NOTICE_DELAY_SECONDS = 3.0
"""Stale messages delivered in a burst after a restart are answered with one notice per chat."""

assert set(RUN_REACTIONS.values()) <= FREE_REACTIONS, "a run reaction is not one Telegram lets bots use"


class TelegramOutbox(Outbox):
    """One chat (or topic) as seen by the renderer.

    Text goes out as a rich message (Telegram renders Markdown natively: tables, headings,
    code, quotes, collapsible blocks). When the server refuses rich content the same text
    falls back to HTML entities and finally to plain text.
    """

    def __init__(self, bot: Bot, chat_id: int, thread_id: int | None) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id or None

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        if markdown:
            try:
                msg = await tg_call(
                    self.bot.send_rich_message,
                    self.chat_id,
                    InputRichMessage(markdown=text),
                    message_thread_id=self.thread_id,
                    flood_chat=self.chat_id,
                )
                return msg.message_id
            except TelegramBadRequest as exc:
                logger.warning("rich markdown refused (%s); falling back to HTML", exc)
            try:
                msg = await tg_call(
                    self.bot.send_message,
                    self.chat_id,
                    markdown_to_html(text),
                    message_thread_id=self.thread_id,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    flood_chat=self.chat_id,
                )
                return msg.message_id
            except TelegramBadRequest:
                pass
        msg = await tg_call(self.bot.send_message, self.chat_id, text, message_thread_id=self.thread_id, parse_mode=None, flood_chat=self.chat_id)
        return msg.message_id

    async def send_html(self, html: str) -> int:
        try:
            msg = await tg_call(
                self.bot.send_rich_message, self.chat_id, InputRichMessage(html=html), message_thread_id=self.thread_id, flood_chat=self.chat_id
            )
            return msg.message_id
        except TelegramBadRequest as exc:
            logger.warning("rich html refused (%s); sending plain text", exc)
        msg = await tg_call(self.bot.send_message, self.chat_id, strip_tags(html), message_thread_id=self.thread_id, parse_mode=None, flood_chat=self.chat_id)
        return msg.message_id

    async def react(self, message_id: int, emoji: str | None) -> None:
        """Set (or clear, with ``None``) the bot's reaction on a message; never raises."""
        if emoji is not None and emoji not in FREE_REACTIONS:
            return
        try:
            await self.bot.set_message_reaction(
                self.chat_id, message_id, reaction=[ReactionTypeEmoji(emoji=emoji)] if emoji else []
            )
        except TelegramRetryAfter as exc:
            note_flood(self.chat_id, float(exc.retry_after))
        except Exception:  # noqa: BLE001 — a reaction is decoration
            logger.debug("reaction failed", exc_info=True)

    async def edit_text(self, message_id: int, text: str, *, html: bool = False) -> None:
        if flooded(self.chat_id):
            return  # Telegram asked for a pause; a status edit is skipped, the next one carries the newer state
        try:
            if html:
                try:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id, message_id=message_id, rich_message=InputRichMessage(html=text)
                    )
                    return
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc):
                        return
                    text = strip_tags(text)
            await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=message_id, parse_mode=None)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
        except TelegramRetryAfter as exc:
            note_flood(self.chat_id, float(exc.retry_after))

    async def send_document(self, path: Path, caption: str | None = None) -> int:
        msg = await tg_call(
            self.bot.send_document,
            self.chat_id,
            FSInputFile(path),
            caption=(caption or "")[:1000] or None,
            message_thread_id=self.thread_id,
            parse_mode=None,
        )
        return msg.message_id

    async def send_photo(self, path: Path, caption: str | None = None) -> int:
        msg = await tg_call(
            self.bot.send_photo,
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

    async def send_draft(self, draft_id: int, text: str) -> bool:
        """Live draft (Bot API 9.5+). Telegram allows it in private chats only."""
        if self.chat_id < 0:
            return False
        try:
            if text:
                try:
                    await self.bot.send_rich_message_draft(self.chat_id, draft_id, rich_message=InputRichMessage(markdown=text), message_thread_id=self.thread_id, can_stop=True)
                except TelegramBadRequest:
                    await self.bot.send_message_draft(self.chat_id, draft_id, message_thread_id=self.thread_id, text=text, parse_mode=None, can_stop=True)
            else:
                await self.bot.send_message_draft(self.chat_id, draft_id, message_thread_id=self.thread_id, text="", parse_mode=None)
            return True
        except TelegramRetryAfter:
            return True  # skip this frame; the next one carries more text
        except TelegramBadRequest as exc:
            logger.warning("drafts unavailable in chat %s: %s", self.chat_id, exc)
            return False


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
        self._compact_focus: dict[str, str] = {}
        self._last_operator_message: dict[str, tuple[int, int]] = {}
        """Per session: the chat and id of the operator's latest message, for the outcome reaction."""
        self._topic_status: dict[str, str] = {}
        self._topic_status_tasks: dict[str, asyncio.Task[None]] = {}
        self._stale_counts: dict[tuple[int, int], int] = {}
        self._stale_notices: dict[tuple[int, int], asyncio.Task[None]] = {}
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
        for task in list(self._topic_status_tasks.values()) + list(self._stale_notices.values()):
            task.cancel()
        for renderer in self._renderers.values():
            renderer.close()
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
        r.message.register(self.cmd_rename, Command("rename"))
        r.message.register(self.cmd_compact, Command("compact"))
        r.message.register(self.cmd_prompt, Command("prompt"))
        r.message.register(self.cmd_delete, Command("delete"))
        r.message.register(self.cmd_cleanup, Command("cleanup"))
        r.message.register(self.on_topic_closed, F.forum_topic_closed)
        r.message.register(self.cmd_sessions, Command("sessions"))
        r.message.register(self.cmd_model, Command("model"))
        r.message.register(self.cmd_thinking, Command("thinking"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_usage, Command("usage"))
        r.message.register(self.cmd_settings, Command("settings"))
        r.message.register(self.cmd_bind, Command("bind"))
        r.message.register(self.cmd_operator, Command("rebuild", "rollback", "panic", "schedules", "verbosity", "approval", "balance", "schedule", "inbox", "heartbeat", "doctor"))
        r.message.register(
            self.on_message,
            F.text | F.caption | F.document | F.photo | F.audio | F.video | F.voice | F.video_note | F.animation | F.sticker | F.location | F.contact | F.poll,
        )
        r.message.register(self.on_unsupported)
        r.callback_query.register(self.on_callback)
        r.stopped_message_generation.register(self.on_generation_stopped)

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
        await self._ask_close(binding, message.chat.id, message.message_thread_id)

    async def cmd_rename(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        title = (command.args or "").strip()
        state = await self._session_for_message(message)
        if state is None or (self._is_general(message) and message.chat.type != "private"):
            await message.answer("Use /rename inside a session topic (or the private chat).")
            return
        if not title:
            await message.answer(f"Current title: {state.session.title}\nusage: /rename <new title>")
            return
        await self.rename_session(state.session.id, title)
        await message.answer(f"Renamed to: {title}")

    async def rename_session(self, session_id: str, title: str) -> None:
        """Rename the session and, when it has a topic, the topic itself."""
        state = await self.manager.rename_session(session_id, title)
        binding = await self.binding_for_session(session_id)
        if binding is not None and binding.thread_id:
            try:
                await tg_call(self.bot.edit_forum_topic, binding.chat_id, binding.thread_id, name=self._topic_name(session_id, state.session.title), flood_chat=binding.chat_id)
            except TelegramBadRequest as exc:
                logger.warning("could not rename topic: %s", exc)

    # -- ambient status: reactions and topic names ----------------------------------------

    def _topic_name(self, session_id: str, title: str) -> str:
        prefix = TOPIC_STATUS_PREFIX.get(self._topic_status.get(session_id, "")) if self.config.telegram.topic_status_emoji else None
        name = f"{prefix} {title}" if prefix else title
        return name[:128]

    def set_topic_status(self, session_id: str, status: str) -> None:
        """Show the session's state in its topic name (debounced: one rename per burst of changes)."""
        if not self.config.telegram.topic_status_emoji or self._topic_status.get(session_id) == status:
            return
        self._topic_status[session_id] = status
        task = self._topic_status_tasks.get(session_id)
        if task is None or task.done():
            self._topic_status_tasks[session_id] = asyncio.create_task(self._apply_topic_status(session_id))

    async def _apply_topic_status(self, session_id: str) -> None:
        """Rename after a short debounce; a flood pause defers the rename rather than dropping it.

        The end of a run is exactly when the chat is busiest (final status edit, the answer,
        the reaction), so the completion rename is the one most likely to hit flood control.
        """
        try:
            await asyncio.sleep(TOPIC_RENAME_DEBOUNCE_SECONDS)
            binding = await self.binding_for_session(session_id)
            if binding is None or not binding.thread_id:
                return
            state = self.manager._states.get(session_id)
            title = state.session.title if state is not None else binding.title
            deadline = time.monotonic() + TOPIC_RENAME_MAX_WAIT_SECONDS
            for _ in range(3):
                while flooded(binding.chat_id):
                    if time.monotonic() > deadline:
                        return  # give up; the next state change carries the newer state anyway
                    await asyncio.sleep(1.0)
                name = self._topic_name(session_id, title)
                try:
                    await tg_call(self.bot.edit_forum_topic, binding.chat_id, binding.thread_id, name=name, attempts=1, flood_chat=binding.chat_id)
                    logger.debug("topic %s renamed to %r", binding.thread_id, name)
                    return
                except TelegramBadRequest as exc:
                    if "not modified" not in str(exc).lower():
                        logger.warning("could not mark topic status: %s", exc)
                    return
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(min(float(exc.retry_after) + 0.5, max(0.0, deadline - time.monotonic())))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a cosmetic rename must never become an unretrieved task exception
            logger.warning("topic status rename failed", exc_info=True)
        finally:
            if self._topic_status_tasks.get(session_id) is asyncio.current_task():
                self._topic_status_tasks.pop(session_id, None)

    async def react_to_last(self, session_id: str, kind: str) -> None:
        """Put the run's outcome on the operator's message that started it."""
        if not self.config.telegram.reactions:
            return
        ref = self._last_operator_message.get(session_id)
        emoji = RUN_REACTIONS.get(kind)
        if ref is None or emoji is None:
            return
        await TelegramOutbox(self.bot, ref[0], None).react(ref[1], emoji)

    async def cmd_compact(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        state = await self._session_for_message(message)
        if state is None or (self._is_general(message) and message.chat.type != "private"):
            await message.answer("Use /compact inside a session topic (or the private chat).")
            return
        count = len(await self.manager.sessions.list_messages(state.session.id, "daedalus", limit=10_000))
        if count == 0:
            await message.answer("Nothing to compact yet.")
            return
        focus = (command.args or "").strip()
        self._compact_focus[state.session.id] = focus
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🗜 Replace {count} messages with a summary", callback_data=f"cm:{state.session.id}:go")],
                [InlineKeyboardButton(text="Cancel", callback_data=f"cm:{state.session.id}:cancel")],
            ]
        )
        await message.answer(
            "Compact the history? The agent keeps only a model-written summary; the full transcript is saved "
            "in the workspace as .history-<time>.jsonl." + (f"\nFocus: {focus}" if focus else ""),
            reply_markup=keyboard,
        )

    async def _on_compact_decision(self, query: CallbackQuery, data: list[str]) -> None:
        if len(data) != 3:
            await query.answer("stale button")
            return
        _, session_id, action = data
        focus = self._compact_focus.pop(session_id, "")
        if action != "go":
            await query.answer("cancelled")
            if query.message is not None:
                await query.message.edit_text("Compact cancelled.", reply_markup=None)
            return
        await query.answer("compacting…")
        if query.message is not None:
            await query.message.edit_text("🗜 Compacting the history…", reply_markup=None)
        try:
            summary = await self.manager.compact(session_id, focus)
        except Exception as exc:  # noqa: BLE001
            if query.message is not None:
                await query.message.edit_text(f"⚠️ compact failed: {exc}")
            return
        outbox = await self.outbox_for_session(session_id)
        if query.message is not None:
            try:
                await query.message.delete()
            except TelegramBadRequest:
                pass
        if outbox is not None:
            await outbox.send_html(
                "<p><b>🗜 History compacted.</b> The session continues from this summary.</p>"
                f"<details><summary>Summary</summary>{markdown_to_html(summary)}</details>"
            )

    async def cmd_prompt(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        from daedalus.host.prompts import DEFAULT_RULES

        rules = self.config.prompt.rules.strip() or DEFAULT_RULES.strip()
        origin = "custom (config)" if self.config.prompt.rules.strip() else "built-in default"
        outbox = TelegramOutbox(self.bot, message.chat.id, message.message_thread_id if message.is_topic_message else None)
        await outbox.send_html(f"<p><b>Working rules</b> — {origin}. Edit in the Mini App → Settings.</p><pre>{html.escape(rules)}</pre>")

    async def _ask_close(self, binding: TopicBinding, chat_id: int, thread_id: int | None) -> None:
        state = await self.manager.get_state(binding.session_id)
        size = 0
        if state is not None and state.workspace.exists():
            size = sum(f.stat().st_size for f in state.workspace.rglob("*") if f.is_file())
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Close and delete the agent + workspace", callback_data=f"cl:{binding.session_id}:delete")],
                [InlineKeyboardButton(text="📦 Close the topic, keep the agent", callback_data=f"cl:{binding.session_id}:keep")],
                [InlineKeyboardButton(text="Cancel", callback_data=f"cl:{binding.session_id}:cancel")],
            ]
        )
        await tg_call(
            self.bot.send_message,
            chat_id,
            f"Close session '{binding.title}' ({binding.session_id})? Its workspace holds {size / 1_048_576:.1f} MB.",
            message_thread_id=thread_id,
            reply_markup=keyboard,
        )

    async def _close_topic(self, binding: TopicBinding) -> None:
        await self.manager.db.execute(
            "UPDATE topics SET closed_at = ? WHERE chat_id = ? AND thread_id = ?",
            (datetime.now(UTC).isoformat(), binding.chat_id, binding.thread_id),
        )
        if binding.thread_id:
            try:
                await self.bot.close_forum_topic(binding.chat_id, binding.thread_id)
            except TelegramBadRequest:
                pass

    async def _on_close_decision(self, query: CallbackQuery, data: list[str]) -> None:
        if len(data) != 3:
            await query.answer("stale button")
            return
        _, session_id, action = data
        binding = await self.binding_for_session(session_id)
        if action == "cancel":
            await query.answer("kept open")
            if query.message is not None:
                await query.message.edit_text("Close cancelled.", reply_markup=None)
            return
        if action == "keep":
            if binding is not None:
                await self.manager.stop(session_id)
                await self._close_topic(binding)
            await query.answer("closed")
            if query.message is not None:
                await query.message.edit_text(f"Topic closed; session {session_id} and its workspace are kept (/sessions, Mini App).", reply_markup=None)
            return
        if binding is not None:
            await self._close_topic(binding)
        removed = await self.manager.delete_session(session_id, delete_workspace=True)
        await query.answer("deleted" if removed else "already gone")
        if query.message is not None:
            await query.message.edit_text(f"Session {session_id} deleted with its workspace.", reply_markup=None)
        if binding is not None and binding.thread_id:
            try:
                await self.bot.delete_forum_topic(binding.chat_id, binding.thread_id)
            except TelegramBadRequest:
                pass

    async def on_topic_closed(self, message: Message) -> None:
        """The operator closed a topic by hand: ask in General what to do with its session."""
        binding = await self.binding_for_topic(message.chat.id, message.message_thread_id or 0)
        if binding is None:
            return
        await self.manager.stop(binding.session_id)
        await self.manager.db.execute(
            "UPDATE topics SET closed_at = ? WHERE chat_id = ? AND thread_id = ?",
            (datetime.now(UTC).isoformat(), binding.chat_id, binding.thread_id),
        )
        await self._ask_close(binding, message.chat.id, self.config.telegram.general_topic_id or None)

    async def cmd_delete(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        session_id = (command.args or "").strip()
        if not session_id:
            await message.answer("usage: /delete <session id>  (see /sessions)")
            return
        binding = await self.binding_for_session(session_id)
        if binding is None:
            removed = await self.manager.delete_session(session_id)
            await message.answer("deleted" if removed else "no such session")
            return
        await self._ask_close(binding, message.chat.id, message.message_thread_id if message.is_topic_message else None)

    async def cmd_cleanup(self, message: Message) -> None:
        """Delete every session whose topic is already closed."""
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        orphans = await self.manager.closed_topic_sessions()
        swept = await self.manager.sweep_orphan_workspaces()
        if not orphans:
            await message.answer("No sessions with closed topics." + (f" Removed {len(swept)} orphan workspace folder(s)." if swept else ""))
            return
        total = sum(o["bytes"] for o in orphans) / 1_048_576
        lines = [f"• {o['title']} ({o['session_id']}) {o['bytes'] / 1_048_576:.1f} MB" for o in orphans[:30]]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🗑 Delete all {len(orphans)} ({total:.1f} MB)", callback_data="cu:all")],
                [InlineKeyboardButton(text="Cancel", callback_data="cu:cancel")],
            ]
        )
        await message.answer("Sessions whose topics are closed:\n" + "\n".join(lines), reply_markup=keyboard)

    async def _on_cleanup_decision(self, query: CallbackQuery, data: list[str]) -> None:
        if data[-1] != "all":
            await query.answer("cancelled")
            if query.message is not None:
                await query.message.edit_text("Cleanup cancelled.", reply_markup=None)
            return
        removed = 0
        for orphan in await self.manager.closed_topic_sessions():
            if await self.manager.delete_session(orphan["session_id"]):
                removed += 1
                try:
                    if orphan["thread_id"]:
                        await self.bot.delete_forum_topic(orphan["chat_id"], orphan["thread_id"])
                except TelegramBadRequest:
                    pass
        await query.answer(f"deleted {removed}")
        if query.message is not None:
            await query.message.edit_text(f"Deleted {removed} session(s) with their workspaces.", reply_markup=None)

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
        default_id, default = self.config.preset()
        if not arg:
            lines = [f"  {pid} — {p.display(pid)}{'  (default)' if pid == default_id else ''}" for pid, p in self.config.presets.items()]
            await message.answer(
                f"default: {default.display(default_id)}\nmodels:\n" + "\n".join(lines)
                + "\nusage: /model <preset-id> (in General: sets the default; in a topic: this session) · /model default · /model provider/model-id"
            )
            return
        general = self._is_general(message) and message.chat.type != "private"
        if arg in self.config.presets:
            if general:
                self.config.model.preset = arg
                self.config.model.chain = [c for c in self.config.model.chain if c != arg]
                await self.save_config(self.config)
                await message.answer(f"Default model: {self.config.presets[arg].display(arg)}")
                return
            state = await self._session_for_message(message)
            if state is not None:
                await self.manager.set_model(state.session.id, preset=arg)
                await message.answer(f"Session model: {self.config.presets[arg].display(arg)} (from the next model call)")
            return
        if general:
            await message.answer("In General, /model takes a preset id (see /model). Create presets in the Mini App → Settings → Models.")
            return
        state = await self._session_for_message(message)
        if state is None:
            return
        if arg in ("default", "reset"):
            await self.manager.set_model(state.session.id, clear=True)
            await message.answer("Session model: back to the global default")
            return
        provider, _, name = arg.partition("/")
        if not name or provider not in self.manager.providers.available():
            await message.answer("usage: /model <preset-id> | provider/model-id | default")
            return
        await self.manager.set_model(state.session.id, model_name=name, provider=provider)
        await message.answer(f"Session model: {provider}/{name} (from the next model call)")

    async def cmd_thinking(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        arg = (command.args or "").strip().lower()
        thinking: bool | None = None
        effort: str | None = None
        default_id, default = self.config.preset()
        if arg in ("on", "off"):
            thinking = arg == "on"
        elif arg in ("low", "medium", "high"):
            thinking, effort = True, arg
        else:
            await message.answer(f"{default.display(default_id)}: thinking={default.thinking} effort={default.reasoning_effort}\nusage: /thinking on|off|low|medium|high")
            return
        if self._is_general(message) and message.chat.type != "private":
            default.thinking = bool(thinking)
            if effort:
                default.reasoning_effort = effort  # type: ignore[assignment]
            await self.save_config(self.config)
            await message.answer(f"{default.display(default_id)}: thinking={default.thinking} effort={default.reasoning_effort}")
            return
        state = await self._session_for_message(message)
        if state is None:
            return
        await self.manager.set_model(state.session.id, thinking=thinking, reasoning_effort=effort)
        await message.answer(f"Session thinking={thinking} effort={effort or default.reasoning_effort}")

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
            "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) ch, sum(cost_usd) usd,"
            " sum(cost_usd IS NULL) unmetered FROM usage_events WHERE at >= ?",
            (today,),
        )
        text = f"today: {row['c'] or 0} calls · in {row['i'] or 0:,} · out {row['o'] or 0:,} · cached {row['ch'] or 0:,}"
        text += _cost_words(row["usd"], int(row["unmetered"] or 0))
        state = await self._session_for_message(message) if not self._is_general(message) else None
        if state is not None:
            srow = await self.manager.db.fetchone(
                "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cost_usd) usd, sum(cost_usd IS NULL) unmetered"
                " FROM usage_events WHERE session_id = ?",
                (state.session.id,),
            )
            text += f"\nthis session: {srow['c'] or 0} calls · in {srow['i'] or 0:,} · out {srow['o'] or 0:,}"
            text += _cost_words(srow["usd"], int(srow["unmetered"] or 0))
        if self.config.limits.usd_per_run > 0:
            text += f"\nper-run cap: ${self.config.limits.usd_per_run:.2f} (limits.usd_per_run)"
        await message.answer(text)

    async def cmd_settings(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        c = self.config
        await message.answer(
            f"model: {c.preset()[1].display(c.preset()[0])} thinking={c.preset()[1].thinking} effort={c.preset()[1].reasoning_effort}\n"
            f"fallback: {', '.join(c.model.chain) or 'none'}\n"
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
        if message.text and message.text.startswith("/") and not self.config.telegram.forward_unknown_commands:
            return
        for interceptor in self.message_interceptors:
            if await interceptor(message):
                return
        if self._is_stale(message):
            self._note_stale(message)
            return
        state = await self._session_for_message(message)
        if state is None:
            return
        if await self._maybe_custom_answer(message, state):
            if self.config.telegram.reactions:
                await TelegramOutbox(self.bot, message.chat.id, None).react(message.message_id, RUN_REACTIONS["received"])
            return
        oversize = self._oversize(message)
        if oversize:
            await message.reply(f"⚠️ {oversize}")
            return
        key = (message.chat.id, message.message_thread_id or 0)
        text = self._text_of(message)
        self._last_operator_message[state.session.id] = (message.chat.id, message.message_id)
        if self.config.telegram.reactions:
            await TelegramOutbox(self.bot, message.chat.id, None).react(message.message_id, RUN_REACTIONS["received"])
        attachment = await self._download(message, state)  # may take a while for big files
        buffer = self._buffers.setdefault(key, InboundBuffer())
        if text:
            buffer.text.append(text)
        if attachment is not None:
            buffer.attachments.append(attachment)
        if buffer.task is not None:
            buffer.task.cancel()
        wait = self.config.telegram.inbound_merge_window_seconds
        if message.photo is not None and not any(buffer.text):
            # A bare photo is usually followed by the words about it (often a voice note).
            wait = max(wait, self.config.telegram.photo_caption_wait_seconds)
        buffer.task = asyncio.create_task(self._flush_inbound(key, state, wait))

    def _is_stale(self, message: Message) -> bool:
        limit = self.config.telegram.stale_after_seconds
        if limit <= 0:
            return False
        return (datetime.now(UTC) - message.date).total_seconds() > limit

    def _note_stale(self, message: Message) -> None:
        """Count stale messages per chat and answer the burst with one notice."""
        key = (message.chat.id, message.message_thread_id or 0)
        self._stale_counts[key] = self._stale_counts.get(key, 0) + 1
        task = self._stale_notices.get(key)
        if task is None or task.done():
            self._stale_notices[key] = asyncio.create_task(self._send_stale_notice(key, message))

    async def _send_stale_notice(self, key: tuple[int, int], sample: Message) -> None:
        await asyncio.sleep(STALE_NOTICE_DELAY_SECONDS)
        count = self._stale_counts.pop(key, 0)
        self._stale_notices.pop(key, None)
        if not count:
            return
        age = int((datetime.now(UTC) - sample.date).total_seconds() // 60)
        what = "this message" if count == 1 else f"{count} messages"
        try:
            await sample.reply(f"⏳ Ignored: {what} arrived {age}+ min late (sent while the bot was down). Send it again if it still applies.")
        except TelegramBadRequest:
            pass

    def _oversize(self, message: Message) -> str | None:
        """Refuse a file by its declared size before spending the download."""
        media = message.document or message.video or message.audio or message.voice or message.video_note or message.animation
        size = getattr(media, "file_size", None) if media is not None else None
        limit = self.config.telegram.max_inbound_file_mb
        if size and size > limit * 1_048_576:
            return f"file refused: {size / 1_048_576:.0f} MB is over the {limit} MB limit (telegram.max_inbound_file_mb)"
        return None

    def _text_of(self, message: Message) -> str:
        """The operator's words plus, for content Telegram cannot hand over as a file, a faithful description."""
        text = message.text or message.caption or ""
        if message.sticker is not None:
            text = f"[sticker {message.sticker.emoji or ''} from set {message.sticker.set_name or '?'}]"
        elif message.location is not None:
            text = f"[location: {message.location.latitude}, {message.location.longitude}]" + (f"\n{text}" if text else "")
        elif message.contact is not None:
            c = message.contact
            text = f"[contact: {c.first_name} {c.last_name or ''} {c.phone_number}]".replace("  ", " ")
        elif message.poll is not None:
            text = f"[poll: {message.poll.question}: " + "; ".join(o.text for o in message.poll.options) + "]"
        reply = message.reply_to_message
        if reply is not None and reply.forum_topic_created is None:
            quoted = (reply.text or reply.caption or "").strip()
            if quoted:
                who = "the agent" if (reply.from_user and reply.from_user.is_bot) else "the operator"
                text = f'[replying to {who}: "{quoted[:500]}"]\n\n{text}'
        return text

    async def on_unsupported(self, message: Message) -> None:
        """Anything the bot has no path for gets a named answer instead of silence."""
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        fields = [name for name in ("game", "invoice", "story", "giveaway", "checklist", "paid_media", "dice", "venue") if getattr(message, name, None) is not None]
        if not fields:
            return  # a service message (topic reopened, chat title changed, …): nothing to read, nothing to say
        try:
            await message.reply(f"⚠️ I cannot read {', '.join(fields)}; send text, a file, a photo, a voice note or a location.")
        except TelegramBadRequest:
            pass

    async def _flush_inbound(self, key: tuple[int, int], state: SessionState, wait: float) -> None:
        await asyncio.sleep(wait)
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
            await self.react_to_last(state.session.id, "failed")
            return
        if was_running:
            await self.react_to_last(state.session.id, "steered")
            renderer = self._renderers.get(state.session.id)
            if renderer is not None:
                renderer.view.narration.append(f"↪ steer (applies before the next model call): {text[:160]}")
                renderer._mark()
        else:
            self.set_topic_status(state.session.id, "running")

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
        elif message.video_note:
            file_id, name, mime = message.video_note.file_id, f"video-note-{message.video_note.file_unique_id}.mp4", "video/mp4"
        elif message.animation:
            file_id, name, mime = message.animation.file_id, message.animation.file_name or f"animation-{message.animation.file_unique_id}.mp4", message.animation.mime_type or "video/mp4"
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
        msg = await tg_call(
            self.bot.send_message, outbox.chat_id, text, message_thread_id=outbox.thread_id, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
        state["message_ids"].append(msg.message_id)

    async def on_generation_stopped(self, event: Any) -> None:
        """The operator pressed Stop on a streaming draft: cancel that session's run."""
        chat_id = event.chat.id
        thread_id = getattr(event, "message_thread_id", None) or 0
        binding = await self.binding_for_topic(chat_id, thread_id)
        if binding is not None:
            await self.manager.stop(binding.session_id)

    async def on_callback(self, query: CallbackQuery) -> None:
        if not self._is_owner(query.from_user.id):
            await query.answer()
            return
        try:
            await self._dispatch_callback(query)
        except Exception:  # noqa: BLE001 — a failed button must not kill polling or lose the answer
            logger.exception("callback handling failed")
            try:
                await query.answer("Something went wrong; try again.")
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch_callback(self, query: CallbackQuery) -> None:
        data = (query.data or "").split(":")
        if not data:
            return
        if data[0] == "aq":
            await self._on_answer(query, data)
            return
        if data[0] == "cl":
            await self._on_close_decision(query, data)
            return
        if data[0] == "cu":
            await self._on_cleanup_decision(query, data)
            return
        if data[0] == "cm":
            await self._on_compact_decision(query, data)
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
                except Exception:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001 — cosmetic edit; flood limits must never block the answer
                logger.warning("could not freeze the question message", exc_info=True)
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
        qs["answers"][index]["custom"] = self._text_of(message)
        qs["index"] = index
        await self._advance_question(state.session.id, None)
        return True

    # -- events from runs -----------------------------------------------------------

    async def _renderer_for(self, session_id: str, run_id: str) -> RunRenderer | None:
        renderer = self._renderers.get(session_id)
        if renderer is not None and renderer.view.run_id == run_id:
            return renderer
        if renderer is not None:
            renderer.close()  # a new run replaces it; its timers must not keep editing a dead status
        outbox = await self.outbox_for_session(session_id)
        if outbox is None:
            return None
        state = await self.manager.get_state(session_id)
        model = state.engine.effective_model_name if state and state.engine else self.config.preset()[1].model

        async def cost_lookup(rid: str) -> float | None:
            row = await self.manager.db.fetchone("SELECT sum(cost_usd) c, count(*) n FROM usage_events WHERE run_id = ?", (rid,))
            return float(row["c"]) if row and row["c"] is not None else None

        renderer = RunRenderer(
            outbox,
            RunView(run_id=run_id, model=model, verbosity=self.config.telegram.verbosity),
            edit_interval=self.config.telegram.status_edit_interval_seconds,
            edit_tiers=self.config.telegram.status_edit_tiers,
            slow_tool_seconds=self.config.telegram.slow_tool_seconds,
            cost_lookup=cost_lookup,
            streaming=self.config.telegram.streaming and outbox.chat_id > 0,
            draft_interval=self.config.telegram.draft_interval_seconds,
        )
        self._renderers[session_id] = renderer
        return renderer

    async def _on_event(self, session_id: str, event: TurnEvent) -> None:
        renderer = await self._renderer_for(session_id, event.run_id)
        if renderer is None:
            return
        if event.type is EventType.STATE_CHANGED and event.payload.get("to") in ("running", "compacting"):
            self.set_topic_status(session_id, str(event.payload["to"]))
        await renderer.handle(event)
        if event.type is EventType.COMPACTION_COMPLETED:
            p = event.payload
            outbox = await self.outbox_for_session(session_id)
            if outbox is not None:
                try:
                    await outbox.send_html(
                        f"<p>🗜 <b>Context compacted</b> ({p.get('reason', 'routine')}): "
                        f"{int(p.get('tokens_before') or 0):,} → {int(p.get('tokens_after') or 0):,} tokens; "
                        f"{int(p.get('tier2_summarised') or 0)} turn(s) summarised. Older detail is now a summary in the transcript.</p>"
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("could not post the compaction note", exc_info=True)
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
        self.set_topic_status(session_id, status)
        await self.react_to_last(session_id, status)
        renderer = self._renderers.get(session_id)
        if renderer is None:
            return
        if renderer.view.run_id != run_id:
            renderer.close()
            self._renderers.pop(session_id, None)
            return
        state = await self.manager.get_state(session_id)
        if status == "awaiting":
            renderer.view.state = "awaiting"
            await renderer.flush()
            return
        services = state.services if state is not None else None
        quiet = services is not None and services.extra.get("silent_run") == run_id
        await renderer.finish(status, workspace=state.workspace if state else Path("/tmp"), quiet=quiet)
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


def _cost_words(usd: float | None, unmetered: int) -> str:
    """Spend for a summary line: a priced total, and an honest count of calls with no known price."""
    if usd is None:
        return " · cost unknown (no price for these calls)" if unmetered else ""
    text = f" · ${usd:.4f}"
    if unmetered:
        text += f" (+{unmetered} unmetered call{'s' if unmetered != 1 else ''})"
    return text


def _unused(_: ReplyParameters | None = None) -> None:
    return None


__all__ = ["TelegramFront", "TelegramOutbox", "TopicBinding"]
