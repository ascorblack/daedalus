"""The voice page: a concierge the operator talks to, and the agents it hands the work to.

The operator speaks; a small fast model answers in a second, out loud. That model is not the agent
that does the work — it is the manager who stays on the line while the engineers work. It answers
what it can itself and, the moment something needs doing, starts an agent session for it and says so
at once, so the conversation never stalls on a tool call that takes four minutes.

The concierge is an ordinary Daedalus session: its transcript is persisted, it is visible in the app,
compaction applies to it and its calls appear in Usage. What makes it a voice session is one metadata
flag, which narrows its tools to the four below and swaps its system prompt for a spoken-word brief.

A run of a delegated agent that ends, or that stops to ask the operator something, becomes a line in
the concierge's conversation — but only while somebody is listening. With no client connected the
lines are held and delivered together when one connects, so the concierge does not talk to an empty
room and does not pay for a turn nobody hears.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx
from protocore.contracts.types import MessageRole, TextBlock
from protocore.runtime.events.types import EventType

from daedalus.config import TtsConfig
from daedalus.host.prompts import split_headline, without_turn_context
from daedalus.transport.telegram.voice import TranscriptionError, asr_configured, effective_asr

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

SESSION_KEY = "voice_session"
"""Where the id of the operator's standing voice session is remembered between restarts."""

TITLE = "Voice"
ANSWER_CLIP = 4000
"""How much of an agent's answer AgentResult returns; a spoken summary needs no more."""
LIST_CLIP = 300
REPORT_CLIP = 500
MAX_AGENTS = 20
MAX_PENDING = 8
"""Reports held for a client that is not connected; the oldest go first, the newest are what matters."""

SENTENCE_END = re.compile(r"(?<=[.!?…。！？])[\s\n]+|(?<=[.!?…])$|\n\n+")
"""Where a spoken chunk may be cut. Sentence-final punctuation followed by space, or a paragraph break."""

MIN_SAY_CHARS = 12
"""A fragment shorter than this waits for the next one: "Yes." alone is a worse utterance than "Yes. Here is why."."""


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split a growing draft into whole sentences plus the tail that is not finished yet.

    Speech starts on the first sentence, not on the last token: the answer is read aloud while the
    model is still writing it, which is most of the difference between a conversation and a wait.
    A sentence too short to be worth its own request ("Yes.") waits and is spoken with the next one.
    """
    out: list[str] = []
    chunk, rest = "", buffer
    while True:
        match = SENTENCE_END.search(rest)
        if match is None or match.end() == 0:
            break
        head, rest = rest[: match.end()].strip(), rest[match.end() :]
        chunk = (chunk + " " + head).strip() if chunk else head
        if len(chunk) >= MIN_SAY_CHARS:
            out.append(chunk)
            chunk = ""
    return out, (chunk + " " + rest if chunk else rest)


def speakable(text: str) -> str:
    """The answer as it is read out: without the retrieval headline, the turn context, or markdown scaffolding."""
    body = split_headline(without_turn_context(text))[0]
    body = re.sub(r"```.*?```", " ", body, flags=re.S)
    body = re.sub(r"[*_`#>]+", "", body)
    return re.sub(r"[ \t]+", " ", body).strip()


def effective_tts(config: TtsConfig, manager: Any) -> TtsConfig:
    """The endpoint to call: a configured provider's base URL and key when ``provider`` is set, else ``url``/``api_key``."""
    if not config.provider:
        return config
    registry = getattr(manager, "providers", None)
    try:
        endpoint = registry.get(config.provider).endpoint if registry is not None else None
    except KeyError:
        endpoint = None
    if endpoint is None or not endpoint.base_url:
        raise RuntimeError(f"text-to-speech provider {config.provider!r} is not configured")
    return config.model_copy(update={"url": endpoint.base_url, "api_key": endpoint.api_key})


def tts_configured(config: TtsConfig) -> bool:
    return bool(config.provider or config.url)


AUDIO_TYPES = {"mp3": "audio/mpeg", "opus": "audio/ogg", "pcm": "audio/pcm"}


class Voice:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._listeners: set[asyncio.Queue[tuple[str, dict[str, Any]]]] = set()
        self._pending: list[str] = []
        self._drafts: dict[str, str] = {}
        """Per run: the part of the concierge's answer that has not been handed to speech yet."""
        self._id = ""
        """The voice session's id, held in memory: every event of every session passes through the sink, and
        a database read per event would make the whole installation pay for the voice page being installed."""
        self._lock = asyncio.Lock()

    # -- the session ------------------------------------------------------------------

    async def session_id(self, *, create: bool = True) -> str:
        """The operator's standing voice session, remembered across restarts; a new one when it is gone."""
        manager = self.app.manager
        assert manager is not None
        async with self._lock:
            known = self._id or str(await self.app.db.kv_get(SESSION_KEY) or "")
            if known and await manager.get_state(known) is not None:
                self._id = known
                return known
            self._id = ""
            if not create:
                return ""
            state = await self.app.create_session(TITLE, metadata={"voice": True})
            preset = self.app.config.voice.preset
            if preset in self.app.config.presets:
                await manager.set_model(state.session.id, preset=preset)
            await self.app.db.kv_set(SESSION_KEY, state.session.id)
            self._id = state.session.id
            return state.session.id

    async def restore(self) -> str:
        """Pick the remembered session up after a restart, so the sink knows it before the page is opened."""
        self._id = str(await self.app.db.kv_get(SESSION_KEY) or "")
        return self._id

    async def new_session(self) -> str:
        """Start the conversation over: a fresh voice session, the old one left in place as a transcript."""
        await self.app.db.kv_set(SESSION_KEY, "")
        self._id = ""
        self._pending.clear()
        return await self.session_id()

    async def state(self) -> dict[str, Any]:
        config = self.app.config.voice
        tts = config.tts
        ready, reason = tts_configured(tts), ""
        if ready:
            try:
                effective_tts(tts, self.app.manager)
            except RuntimeError as exc:
                ready, reason = False, str(exc)
        preset = self.app.config.presets.get(config.preset)
        return {
            "enabled": config.enabled,
            "session_id": await self.session_id(create=False),
            "model": preset.display(config.preset) if preset else config.preset,
            "tts": {"configured": ready, "reason": reason, "voice": tts.voice, "model": tts.model, "format": tts.format},
            "stt": await self._asr_state(),
            "agents": await self.agents(),
            "listening": bool(self._listeners),
        }

    async def _asr_state(self) -> dict[str, Any]:
        asr = self.app.config.asr
        ready, reason = asr_configured(asr), ""
        if ready:
            try:
                effective_asr(asr, self.app.manager)
            except TranscriptionError as exc:
                ready, reason = False, str(exc)
        return {"configured": ready, "reason": reason, "model": asr.model, "max_seconds": asr.max_seconds}

    # -- what the operator says -------------------------------------------------------

    async def say(self, text: str) -> str:
        """One utterance, already in words: a turn of the voice session like any other message."""
        body = text.strip()
        if not body:
            raise ValueError("nothing was said")
        manager = self.app.manager
        assert manager is not None
        session_id = await self.session_id()
        await self.emit("status", {"state": "thinking"})
        return await manager.submit(session_id, body)

    async def interrupt(self) -> bool:
        """Barge-in: the operator started talking over the answer, so the answer stops."""
        manager = self.app.manager
        assert manager is not None
        session_id = await self.session_id(create=False)
        stopped = await manager.stop(session_id) if session_id else False
        self._drafts.clear()
        await self.emit("status", {"state": "idle"})
        return stopped

    # -- the concierge's tools --------------------------------------------------------

    async def delegate(self, *, title: str, task: str, session_id: str | None = None) -> dict[str, Any]:
        """Hand work to an agent: a new session for it, or another instruction to one already working."""
        manager = self.app.manager
        assert manager is not None
        body = task.strip()
        if not body:
            raise ValueError("an agent needs a task; write what is to be done")
        voice_id = await self.session_id()
        if session_id:
            state = await manager.get_state(session_id)
            if state is None or state.metadata.get("voice_parent") != voice_id:
                raise KeyError(session_id)
            await manager.submit(session_id, body, as_answer=False, origin="voice")
            await self.emit("status", {"state": "delegating", "title": state.session.title})
            await self.emit("agents", {"agents": await self.agents()})
            return {"session_id": session_id, "title": state.session.title, "steered": True}
        name = title.strip() or body[:40]
        state = await self.app.create_session(name, metadata={"voice_parent": voice_id, "brief": body})
        await manager.submit(state.session.id, body, as_answer=False, origin="voice")
        await self.emit("status", {"state": "delegating", "title": name})
        await self.emit("agents", {"agents": await self.agents()})
        return {"session_id": state.session.id, "title": name, "steered": False}

    async def agents(self) -> list[dict[str, Any]]:
        """What the concierge has running, newest first: title, status, when it last spoke, its last words."""
        manager = self.app.manager
        if manager is None:
            return []
        voice_id = self._id
        out: list[dict[str, Any]] = []
        for row in await manager.list_sessions(limit=200):
            if row.get("metadata", {}).get("voice_parent") != voice_id or not voice_id:
                continue
            answer = await self.last_answer(row["id"])
            out.append(
                {
                    "session_id": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "last_message_at": row["last_message_at"],
                    "answer": answer[:LIST_CLIP],
                }
            )
            if len(out) >= MAX_AGENTS:
                break
        return out

    async def result(self, session_id: str) -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        rows = await manager.list_sessions(limit=200)
        status = next((r["status"] for r in rows if r["id"] == session_id), "idle")
        return {"session_id": session_id, "title": state.session.title, "status": status, "answer": (await self.last_answer(session_id))[:ANSWER_CLIP]}

    async def stop_agent(self, session_id: str) -> bool:
        manager = self.app.manager
        assert manager is not None
        return await manager.stop(session_id)

    async def last_answer(self, session_id: str) -> str:
        manager = self.app.manager
        assert manager is not None
        rows = await manager.sessions.list_transcript(session_id)
        for message in reversed(rows):
            if message.role is MessageRole.assistant:
                text = "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock)).strip()
                if text:
                    return split_headline(text)[0]
        return ""

    # -- the stream to the page -------------------------------------------------------

    async def emit(self, name: str, payload: dict[str, Any]) -> None:
        for queue in list(self._listeners):
            queue.put_nowait((name, payload))

    @contextlib.asynccontextmanager
    async def listen(self) -> AsyncIterator[asyncio.Queue[tuple[str, dict[str, Any]]]]:
        """One connected page. The first one to arrive gets whatever the agents reported while nobody listened."""
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._listeners.add(queue)
        try:
            await self.flush_pending()
            yield queue
        finally:
            self._listeners.discard(queue)

    @property
    def held(self) -> list[str]:
        """Reports waiting for a page to connect, oldest first."""
        return list(self._pending)

    async def flush_pending(self) -> None:
        """Everything the agents said to an empty room, delivered as one turn so the concierge speaks it once."""
        if not self._pending:
            return
        held, self._pending = self._pending, []
        try:
            await self.say_to_concierge("\n\n".join(held))
        except Exception:  # noqa: BLE001 — a report that cannot be delivered must not close the stream
            logger.exception("could not deliver the held agent reports")

    async def say_to_concierge(self, text: str) -> None:
        manager = self.app.manager
        assert manager is not None
        session_id = await self.session_id()
        await manager.submit(session_id, text, as_answer=False, origin="agent")

    async def report(self, text: str) -> None:
        """A line for the concierge to speak — now if a page is listening, at the next connect if not."""
        if self._listeners:
            try:
                await self.say_to_concierge(text)
                return
            except Exception:  # noqa: BLE001
                logger.exception("could not deliver an agent report to the concierge")
        self._pending.append(text)
        del self._pending[:-MAX_PENDING]

    # -- events ------------------------------------------------------------------------

    async def on_event(self, session_id: str, event: Any) -> None:
        voice_id = self._id
        if not voice_id:
            return
        if session_id != voice_id:
            if event.type is EventType.TOOL_CALL_PENDING and event.payload.get("kind") == "ask_user":
                await self._agent_asks(session_id, event)
            elif event.type in (EventType.MESSAGE_START, EventType.STATE_CHANGED):
                await self.emit("agents", {"agents": await self.agents()})
            return
        if not self._listeners:
            return
        run_id = str(getattr(event, "run_id", "") or "")
        if event.type is EventType.MESSAGE_START:
            self._drafts[run_id] = ""
            await self.emit("status", {"state": "thinking"})
        elif event.type is EventType.CONTENT_BLOCK_DELTA:
            delta = event.payload.get("delta") or {}
            if delta.get("type") != "text_delta":
                return
            draft = self._drafts.get(run_id, "") + str(delta.get("text") or "")
            await self.emit("partial", {"text": draft})
            sentences, self._drafts[run_id] = split_sentences(draft)
            for sentence in sentences:
                spoken = speakable(sentence)
                if spoken:
                    await self.emit("say", {"text": spoken})
        elif event.type is EventType.MESSAGE_STOP:
            await self._flush_draft(run_id)
        elif event.type is EventType.TOOL_USE_STOP and str(event.payload.get("tool_name") or "") == "Delegate":
            title = str((event.payload.get("final_input") or {}).get("title") or "")
            await self.emit("status", {"state": "delegating", "title": title})
        elif event.type is EventType.ERROR:
            await self.emit("error", {"message": str(event.payload.get("message") or "the run failed")})

    async def _flush_draft(self, run_id: str) -> None:
        tail = speakable(self._drafts.pop(run_id, ""))
        if tail:
            await self.emit("say", {"text": tail})

    async def _agent_asks(self, session_id: str, event: Any) -> None:
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(session_id)
        if state is None or state.metadata.get("voice_parent") != self._id:
            return
        questions = (event.payload.get("ask_user_payload") or {}).get("questions") or []
        asked = str(questions[0].get("question") if questions else "").strip()
        await self.report(f'[agent "{state.session.title}" is waiting for the operator: {asked[:REPORT_CLIP]}]')
        await self.emit("agents", {"agents": await self.agents()})

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        voice_id = self._id
        if not voice_id:
            return
        if session_id == voice_id:
            await self._flush_draft(run_id)
            await self.emit("done", {"status": status})
            await self.emit("status", {"state": "idle"})
            await self.emit("agents", {"agents": await self.agents()})
            return
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(session_id)
        if state is None or state.metadata.get("voice_parent") != voice_id or status == "awaiting":
            return
        answer = await self.last_answer(session_id) if status == "completed" else ""
        body = answer[:REPORT_CLIP] if answer else f"no final answer ({status}); its session has the detail"
        await self.report(f'[agent "{state.session.title}" finished: {body}]')
        await self.emit("agents", {"agents": await self.agents()})

    # -- speech ------------------------------------------------------------------------

    async def speech(self, text: str) -> tuple[AsyncIterator[bytes], str]:
        """One sentence as audio from the configured endpoint, streamed on as it arrives."""
        config = effective_tts(self.app.config.voice.tts, self.app.manager)
        url = config.url.rstrip("/") + "/audio/speech"
        headers = {"authorization": f"Bearer {config.api_key}"} if config.api_key else {}
        body = {"model": config.model, "voice": config.voice, "input": text, "response_format": config.format}
        client = httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds, connect=15.0))
        request = client.build_request("POST", url, headers=headers, json=body)
        response = await client.send(request, stream=True)
        if response.status_code >= 400:
            await response.aclose()
            await client.aclose()
            raise RuntimeError(f"the speech endpoint answered HTTP {response.status_code}")

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return chunks(), AUDIO_TYPES.get(config.format, "application/octet-stream")

    # -- the tools' way in ---------------------------------------------------------------

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "delegate":
            return await self.delegate(**kwargs)
        if op == "agents":
            return await self.agents()
        if op == "result":
            return await self.result(kwargs["session_id"])
        if op == "stop":
            return await self.stop_agent(kwargs["session_id"])
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    voice = Voice(app)
    await voice.restore()
    app.extensions["voice"] = voice
    assert app.manager is not None
    app.manager.service_hooks["voice"] = voice.service
    app.manager.add_sink(voice.on_event)
    app.manager.on_finished(voice.on_run_finished)
    return []


__all__ = ["SESSION_KEY", "Voice", "effective_tts", "install", "speakable", "split_sentences", "tts_configured"]
