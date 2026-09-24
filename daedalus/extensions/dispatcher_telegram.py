"""The main orchestrator's window in Telegram: its questions, posted once, where it lives.

Telegram is optional; without a bot nothing here exists and the app shows everything. With one, the
main orchestrator lives in General (a forum) or in the private chat under its header, and the
requests shown in its chat — a project's question on a dispatch, the confirmation of a new project —
are posted there once, with buttons that answer them and a way to answer in words. A mirrored request
is posted here and nowhere else in Telegram: the project topic leaves it out (:func:`is_mirrored`).

The request row is the truth: every event that may mean a request appeared or was answered only says
"look again", so a post is made once and its buttons are taken away however it was answered — here,
in the app, or from a notification. A request that acts on the host is shown without buttons: the
operator answers those in the app alone.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from daedalus.host.events import AppEvent, EventFilter
from daedalus.stores.staff import Ask

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.extensions.dispatcher import Dispatcher
    from daedalus.transport.telegram.front import TelegramFront, TelegramOutbox

logger = logging.getLogger(__name__)

POSTS_KEY = "dispatcher_telegram.posts"
"""kv: ``"<chat>:<message>" -> ask id`` for the requests posted here, so a reply after a restart still
finds its request and a late answer can still take its buttons away."""
POSTS_KEPT = 400
OPTIONS_MAX = 8
TEXT_MAX = 3500
EVENTS = ("ask.pending", "permission.pending", "ask.answered", "permission.resolved", "notify", "dispatch.created", "dispatch.closed")


def _clip(text: str, limit: int = TEXT_MAX) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class DispatcherTelegram:
    def __init__(self, app: Application, front: TelegramFront, dispatcher: Dispatcher) -> None:
        self.app = app
        self.front = front
        self.dispatcher = dispatcher
        assert app.manager is not None
        self.manager = app.manager
        self._prompts: dict[int, str] = {}
        """A "type your answer" prompt's message id → the request it answers."""
        self._lock = asyncio.Lock()

    async def outbox(self) -> TelegramOutbox | None:
        session_id = await self.dispatcher.session_id()
        if not session_id:
            return None
        state = await self.manager.get_state(session_id)
        if state is None or state.metadata.get("telegram_detached"):
            return None
        return self.front._main_outbox(session_id)

    # -- posting ------------------------------------------------------------------------------------

    async def _posts(self) -> dict[str, str]:
        value = await self.manager.db.kv_get(POSTS_KEY, {})
        return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}

    async def _remember(self, chat_id: int, message_id: int, ask_id: str) -> None:
        posts = await self._posts()
        posts[f"{chat_id}:{message_id}"] = ask_id
        if len(posts) > POSTS_KEPT:
            posts = dict(list(posts.items())[-POSTS_KEPT:])
        await self.manager.db.kv_set(POSTS_KEY, posts)

    async def _forget(self, key: str) -> None:
        posts = await self._posts()
        if posts.pop(key, None) is not None:
            await self.manager.db.kv_set(POSTS_KEY, posts)

    async def sync(self) -> int:
        """Post every mirrored request not posted yet, and close the posts of the ones answered."""
        async with self._lock:
            await self._close_answered()
            outbox = await self.outbox()
            if outbox is None:
                return 0
            posted = set((await self._posts()).values())
            count = 0
            for ask in await self.dispatcher.mirrored(open_only=True):
                if ask.id in posted:
                    continue
                text, rows = await self.card(ask)
                message_id = await self.front.send_choice(outbox, text, rows) if rows else await outbox.send_text(text, markdown=False)
                await self._remember(outbox.chat_id, message_id, ask.id)
                count += 1
            return count

    async def card(self, ask: Ask) -> tuple[str, list[list[tuple[str, str]]]]:
        project = await self.manager.projects.get(ask.project_id) if ask.project_id else None
        name = project.name if project is not None else str(ask.detail.get("name") or "new project")
        if ask.origin == "dispatcher":
            head = f"❓ Confirm a new project [{ask.short_id}]"
        else:
            member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
            who = "its orchestrator" if ask.origin == "orchestrator" else (member.name if member is not None else "a staff member")
            what = {"question": "asks", "permission": "asks for permission", "folder": "asks for a folder"}.get(ask.kind, "asks")
            head = f"❓ {name} · {who} {what} [{ask.short_id}]"
        lines = [head, "", _clip(ask.text, 3000)]
        if ask.suggestion:
            lines += ["", f"The orchestrator suggests: {_clip(ask.suggestion, 300)}"]
        rows: list[list[tuple[str, str]]] = []
        if await self.dispatcher.host_level(ask):
            lines += ["", "This one acts on the host: answer it in the app."]
        elif ask.kind in ("question", "project"):
            options = [str(o) for o in (ask.detail.get("options") or []) if str(o).strip()][:OPTIONS_MAX]
            rows = [[(option[:60], f"mq:{ask.id}:{i}")] for i, option in enumerate(options)]
            if ask.kind == "question":
                rows.append([("✍️ Answer", f"mw:{ask.id}")])
        else:
            rows = [[("✅ Allow", f"mp:{ask.id}:allow"), ("✖ Deny", f"mp:{ask.id}:deny")]]
        return "\n".join(lines), rows

    async def _close_answered(self) -> None:
        for key, ask_id in list((await self._posts()).items()):
            ask = await self.manager.asks.get(ask_id)
            if ask is not None and ask.open and await self.dispatcher.is_mirrored(ask):
                continue
            chat_id, _, message_id = key.partition(":")
            said = answered(ask) if ask is not None else "withdrawn"
            text = _clip(f"{ask.text}\n\n— {said}") if ask is not None else "— withdrawn"
            await self.front.edit_post(int(chat_id), int(message_id), text)
            await self._forget(key)

    # -- the operator answers here ------------------------------------------------------------------

    async def _answer(self, ask_id: str, **answer: Any) -> str:
        team: Any = self.app.extensions.get("staff")
        if team is None:
            return "the team is not running here"
        try:
            await team.answer(ask_id, by="operator", via="telegram", **answer)
        except KeyError:
            return "that request no longer exists"
        except ValueError as exc:
            if type(exc).__name__ == "AlreadyAnswered":
                ask = await self.manager.asks.get(ask_id)
                return f"too late: {answered(ask) if ask is not None else exc}"
            return str(exc)[:180]
        await self.sync()
        return "answered"

    async def on_callback(self, query: Any, data: list[str]) -> None:
        """``mq:<ask>:<i>`` picks an option, ``mp:<ask>:allow|deny`` decides, ``mw:<ask>`` asks for words."""
        kind = data[0]
        ask = await self.manager.asks.get(data[1]) if len(data) > 1 and data[1] else None
        if ask is None:
            await query.answer("stale button")
            return
        if await self.dispatcher.host_level(ask):
            await query.answer("This one acts on the host: answer it in the app.", show_alert=True)
            return
        if kind == "mw":
            message = query.message
            chat_id = message.chat.id if message is not None else self.front.settings.owner_user_id
            thread_id = getattr(message, "message_thread_id", None) if message is not None else None
            prompt = await self.front.send_force_reply(chat_id, thread_id, f"Your answer to [{ask.short_id}]:")
            self._prompts[prompt] = ask.id
            await query.answer()
            return
        if kind == "mq":
            options = [str(o) for o in (ask.detail.get("options") or [])]
            try:
                choice = options[int(data[2])]
            except (IndexError, ValueError):
                await query.answer("stale button")
                return
            said = await self._answer(ask.id, selected=[choice])
        else:
            if len(data) != 3 or data[2] not in ("allow", "deny"):
                await query.answer("stale button")
                return
            said = await self._answer(ask.id, allow=data[2] == "allow")
        await query.answer(said[:190])
        if said.startswith("too late"):
            await self.sync()

    async def on_message(self, message: Any) -> bool:
        """A reply to a posted question (or to its "type your answer" prompt) answers it in the operator's words."""
        reply = getattr(message, "reply_to_message", None)
        if reply is None:
            return False
        ask_id = self._prompts.pop(reply.message_id, None) or (await self._posts()).get(f"{message.chat.id}:{reply.message_id}")
        if not ask_id:
            return False
        text = (message.text or message.caption or "").strip()
        ask = await self.manager.asks.get(ask_id)
        if ask is None or not ask.open or ask.kind != "question" or not text:
            # Anything else about it is words for the main orchestrator, through the ordinary routing.
            return False
        said = await self._answer(ask.id, text=text)
        await message.reply(said if said != "answered" else f"Answered [{ask.short_id}].")
        return True

    async def on_event(self, event: AppEvent) -> None:
        try:
            await self.sync()
        except Exception:  # noqa: BLE001 — Telegram is a window; a failed post never stops the bus
            logger.warning("main orchestrator: could not post for %s", event.type, exc_info=True)

    def attach(self) -> asyncio.Task[None]:
        self.front.callback_hooks.update({"mq": self.on_callback, "mp": self.on_callback, "mw": self.on_callback})
        self.front.message_interceptors.append(self.on_message)
        return self.manager.bus.on(EventFilter(types=EVENTS), self.on_event, name="dispatcher-telegram")


def answered(ask: Ask) -> str:
    """How a request was answered, in one line, for the post whose buttons go away."""
    r = ask.resolution or {}
    if ask.resolved_by == "system":
        return f"withdrawn: {r.get('closed') or 'what it was for is over'}"
    via = str(r.get("via") or "")
    where = {
        "telegram": "in Telegram",
        "app": "in the app",
        "main": "in the main chat",
        "project": "in the project's chat",
        "notification": "from a notification",
        "push": "from a notification",
        "orchestrator": "by the orchestrator",
        "dispatcher": "through the main orchestrator",
    }.get(via, "in the app")
    if ask.kind in ("permission", "folder") and r.get("allow") is not None:
        answer = "allowed" if r.get("allow") else "denied"
    else:
        answer = ", ".join(str(s) for s in r.get("selected") or []) or str(r.get("text") or "") or "answered"
    return f"answered {where}: {answer}"


async def is_mirrored(app: Application, ask: Ask) -> bool:
    """Whether a request is posted by the main orchestrator's window, so no other Telegram window posts it."""
    dispatcher: Any = app.extensions.get("dispatcher")
    return bool(dispatcher is not None and app.extensions.get("dispatcher_telegram") is not None and await dispatcher.is_mirrored(ask))


__all__ = ["DispatcherTelegram", "answered", "is_mirrored"]
