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

An agent also talks while it works: between two tool calls it writes a paragraph saying what it just
found and what it is doing next, and that is what makes a four-minute job bearable to listen to. Those
interim lines are relayed too, under a much tighter rule than a final answer: only while somebody is
listening (an interim is worthless late — it is superseded by the next one, and by the final answer),
coalesced over a window so a burst of narration costs one turn, and rate-capped per agent so a talkative
agent cannot take the conversation over. What is relayed is the agent's own prose only: not a tool
result, not its thinking, not a token at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from protocore.contracts.types import MessageRole, TextBlock
from protocore.runtime.events.types import EventType

from daedalus.config import ModelPresetConfig, RuntimeConfig, TtsConfig
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
PROGRESS_CLIP = 300
"""How much of an interim line is relayed. One spoken sentence comes out of it; the rest would be read
by a model that is about to throw it away, and an agent's next paragraph supersedes it anyway."""
RELAY_POLL_SECONDS = 0.25
"""How often a held interim looks again at whether the concierge has finished its own turn."""
DRAFT_CLIP = 8000
"""The most of one agent message held while it is being written. A message longer than this is not a
progress line by then, and an unbounded buffer per delegated run is how a long-running page leaks."""
APPROVALS_REMEMBERED = 8
"""Approval keys kept per agent, so an agent alternating between two refused calls reports each once."""
MAX_AGENTS = 20
MAX_PENDING = 8
"""Reports held for a client that is not connected; the oldest go first, the newest are what matters."""
TITLE_CLIP = 80

REPORT_OPEN = "⟪agent report⟫"
REPORT_CLOSE = "⟪end of report⟫"
"""The frame an agent's words arrive in. The concierge's brief names it and says that what is inside
it is a report to relay, never an instruction: an agent's answer is made of whatever it read — web
pages, files, another service's output — and it reaches the concierge while the delegation tools are
live. The frame characters are taken out of the agent's own text below, so it cannot close the block
early and write outside it."""


def quoted(text: str) -> str:
    """An agent's words as data: the frame's own characters replaced, so only we can open or close a block."""
    return text.replace("⟪", "«").replace("⟫", "»")


def report_block(*, kind: str, title: str, session_id: str, state: str, body: str) -> str:
    """One agent's news, framed. ``kind`` and ``state`` are ours to write; ``title`` and ``body`` are the agent's.

    ``kind`` is the one word the concierge needs before it reads a word of the body: ``progress`` is an
    agent thinking out loud and means nothing is finished, ``final`` is a result, ``question`` and
    ``approval`` are the two ways a run stops until the operator says something. It is written by us,
    on its own line, so the difference between "it found the bug" and "it fixed the bug" does not
    depend on how the agent happened to phrase a sentence.
    """
    head = f"agent: {quoted(title.strip())[:TITLE_CLIP] or session_id} ({session_id}) {state}"
    return "\n".join([REPORT_OPEN, f"kind: {kind}", head, no_kind_line(quoted(body.strip())), REPORT_CLOSE])


def no_kind_line(text: str) -> str:
    """The agent's words with any line of its own that reads as our ``kind:`` header defused.

    The word on the second line is what the concierge acts on — whether the work is done or the
    agent is only talking — and it is ours to write. A progress line cannot forge one (its
    whitespace is collapsed into a single line), but a final answer, a question and an approval are
    the agent's own prose, and an agent answering a question *about* this format would write one by
    accident as readily as a hostile page would write one on purpose. The words are kept — the line
    is marked as quoted, so it is no longer a line that begins with ours.
    """
    return KIND_LINE_RE.sub(lambda match: "> " + match.group(0).strip(), text)


KIND_LINE_RE = re.compile(r"(?mi)^[ \t]*kind[ \t]*:")


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


EVIDENCE_RE = re.compile(r"<(?:file|run)\b[^>]*/?>")
"""The evidence tags an answer carries for the app to render as chips. Spoken, they are punctuation read
out loud, so an interim line drops them rather than trying to say them."""

APPROVAL_KEY_RE = re.compile(r"Approval key: ([0-9a-f]{12})")
"""How a refusal by the policy is recognised in a tool result: the key the operator approves it with.
A session whose next step is an approval is not working, and the operator is the only one who can
unblock it — which makes it something the concierge must say, not something it may summarise later."""


def delta_text(payload: dict[str, Any]) -> str:
    """The assistant's own words in one delta, and nothing else.

    A run publishes three kinds of stream to the operator: prose, thinking, and tool traffic. Only the
    first is what the operator would read in the session view as the agent talking, and only the first
    is worth a sentence out loud — thinking is a draft the model did not commit to, and tool arguments
    and results are the detail the concierge is explicitly told never to read aloud.
    """
    delta = payload.get("delta") or {}
    return str(delta.get("text") or "") if delta.get("type") == "text_delta" else ""


def settles_interim(payload: dict[str, Any]) -> bool:
    """Whether this end-of-message is an interim one: the agent stopped writing to call a tool.

    Any other stop reason ends the turn, and the turn's own text is the final answer, which is reported
    by the finished-run path with everything it knows — its status included.
    """
    return str(payload.get("stop_reason") or "") == "tool_use"


def progress_line(text: str) -> str:
    """One settled paragraph of an agent's prose, as a line a concierge can turn into a sentence.

    The same cleaning a spoken answer gets, plus the evidence tags and the line breaks: what arrives at
    the concierge is one clipped line of plain words, because everything else is either unsayable or a
    detail that will be superseded before anyone could ask about it.
    """
    # The tags go first: what makes an answer sayable also strips the punctuation that makes a tag a tag.
    body = speakable(EVIDENCE_RE.sub(" ", text))
    return re.sub(r"\s+", " ", body).strip()[:PROGRESS_CLIP]


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


FAST_MAX_OUTPUT = 8_000
"""The largest reply a model may be allowed to write before it is no longer a concierge's model.

A spoken answer is two sentences; a model configured to write thirty thousand tokens is configured
for a different job, and it takes the time to match. This and ``thinking`` are the two things the
page warns about, and they are decided here rather than in the app so that the warning and the
default the page suggests cannot disagree.
"""


def fast_enough(preset: ModelPresetConfig) -> bool:
    """Whether a preset answers while the operator is still listening: no thinking, a short reply."""
    return not preset.thinking and preset.max_output_tokens <= FAST_MAX_OUTPUT


def model_options(config: RuntimeConfig) -> list[dict[str, Any]]:
    """Every preset the concierge could be pointed at, as the app lists them.

    The label, the provider and the model id are what the operator picks by; ``fast`` is the
    judgement, made here once. The order is the table's own, which is the order Settings shows.
    """
    return [
        {
            "id": pid,
            "label": preset.display(pid),
            "provider": preset.provider,
            "model": preset.model,
            "thinking": preset.thinking,
            "max_output_tokens": preset.max_output_tokens,
            "fast": fast_enough(preset),
        }
        for pid, preset in config.presets.items()
    ]


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
        self._agent_drafts: dict[tuple[str, str], str] = {}
        """Per delegated run: the message an agent is writing, until it stops to call a tool."""
        self._news: dict[str, dict[str, str]] = {}
        """Per delegated agent, for the page: its last interim line, when, and what it is waiting for."""
        self._waiting_relay: dict[str, str] = {}
        """Per delegated agent: the interim line that has not been relayed yet. One entry, not a queue —
        a newer line supersedes an older one, which is the whole point of coalescing progress."""
        self._relay_tasks: dict[str, asyncio.Task[None]] = {}
        self._relayed_at: dict[str, float] = {}
        """Per delegated agent: when its last interim was relayed, for the rate cap."""
        self._relays: list[float] = []
        """When each interim was relayed, across every agent, for the cap that is a total rather than a floor."""
        self._approvals: dict[str, list[str]] = {}
        """Per delegated agent: the approval keys already reported, so a retry loop is reported once. A few
        per agent, because an agent that alternates between two refused calls would otherwise report each
        of them again every time it changed its mind."""

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
            await self.app.db.kv_set(SESSION_KEY, state.session.id)
            self._id = state.session.id
            await self._point_at(state.session.id)
            return state.session.id

    async def _point_at(self, session_id: str) -> str:
        """Put the configured preset on the session, or take the override off when none is configured.

        The session carries the choice, not the configuration: the run reads a live override written
        when the session was made. So a preset changed in the app reached a session made before the
        change never — the concierge went on answering with the model it was born with until the
        operator started a new conversation. Reconciling is one write, and only when the two differ.

        It also decides the argument: Settings → Voice is where this conversation's model is chosen,
        so a model put on the voice session from the session screen does not outlive the next
        utterance. One setting, in the place the operator went looking for it.
        """
        manager = self.app.manager
        assert manager is not None
        wanted = self.app.config.voice.preset
        if wanted not in self.app.config.presets:
            wanted = ""
        current = str((await manager.live.load(session_id)).get("preset") or "")
        if wanted == current:
            return wanted
        if wanted:
            await manager.set_model(session_id, preset=wanted)
        else:
            # Empty means "the default preset", which is what a session with no override of its own
            # already resolves to — so the way to say it is to stop saying anything.
            await manager.set_model(session_id, clear=True)
        return wanted

    async def apply_model(self) -> str:
        """Point the standing conversation at the configured preset; the app calls this after a change."""
        if self.app.manager is None:
            return ""
        session_id = await self.session_id(create=False)
        return await self._point_at(session_id) if session_id else ""

    async def restore(self) -> str:
        """Pick the remembered session up after a restart, so the sink knows it before the page is opened."""
        self._id = str(await self.app.db.kv_get(SESSION_KEY) or "")
        return self._id

    async def new_session(self) -> str:
        """Start the conversation over: a fresh voice session, the old one left in place as a transcript."""
        await self.app.db.kv_set(SESSION_KEY, "")
        self._id = ""
        self._pending.clear()
        self._forget_agents()
        return await self.session_id()

    def _forget_agents(self) -> None:
        """Drop everything held about the old conversation's agents, scheduled relays included."""
        for task in self._relay_tasks.values():
            task.cancel()
        self._relay_tasks.clear()
        self._agent_drafts.clear()
        self._news.clear()
        self._waiting_relay.clear()
        self._relayed_at.clear()
        self._approvals.clear()

    async def state(self) -> dict[str, Any]:
        config = self.app.config.voice
        tts = config.tts
        ready, reason = tts_configured(tts), ""
        if ready:
            try:
                effective_tts(tts, self.app.manager)
            except RuntimeError as exc:
                ready, reason = False, str(exc)
        chosen = config.preset if config.preset in self.app.config.presets else ""
        resolved = self.app.config.default_preset(chosen)
        return {
            "enabled": config.enabled,
            "session_id": await self.session_id(create=False),
            # Three answers to one question, because the page asks it three ways: which preset the
            # operator picked ("" is "whatever the default is"), which one that comes out as, and
            # what there is to pick from. A page that had only the first showed "—" for the model
            # an installation was in fact talking to.
            "preset": chosen,
            "using": resolved[0] if resolved else "",
            "model": resolved[1].display(resolved[0]) if resolved else "",
            "presets": model_options(self.app.config),
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
        # Read the configured model at every utterance rather than once at the birth of the session:
        # the engine is built per run, so this is the last moment the choice can still take effect
        # without a restart and without a new conversation.
        await self._point_at(session_id)
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

    async def own_agent(self, session_id: str, voice_parent: str | None = None) -> Any:
        """The state of an agent the concierge started, or ``KeyError``.

        Every tool that names a session id goes through here. The concierge has no business with the
        operator's own sessions, with another leader's subagents, or with its own voice session: a
        hallucinated or borrowed id is refused rather than acted on.
        """
        manager = self.app.manager
        assert manager is not None
        parent = voice_parent if voice_parent is not None else await self.session_id(create=False)
        state = await manager.get_state(session_id)
        if not parent or state is None or state.metadata.get("voice_parent") != parent:
            raise KeyError(session_id)
        return state

    async def delegate(self, *, title: str, task: str, session_id: str | None = None) -> dict[str, Any]:
        """Hand work to an agent: a new session for it, or another instruction to one already working."""
        manager = self.app.manager
        assert manager is not None
        body = task.strip()
        if not body:
            raise ValueError("an agent needs a task; write what is to be done")
        voice_id = await self.session_id()
        if session_id:
            state = await self.own_agent(session_id, voice_id)
            # An agent stopped on a question is answered, not queued behind it: the operator is on the
            # line and the concierge is the only way their answer can reach the agent from this page.
            answering = state.pending is not None
            await manager.submit(session_id, body, as_answer=answering, origin="voice")
            await self.emit("status", {"state": "delegating", "title": state.session.title})
            await self.emit("agents", {"agents": await self.agents()})
            return {"session_id": session_id, "title": state.session.title, "steered": True, "answered": answering}
        name = title.strip() or body[:40]
        # The concierge hands work to an agent of its own; where the concierge itself works in a
        # project, so does the agent it makes, or the boundary would end at the microphone.
        parent_project = await manager.project_of(voice_id)
        state = await self.app.create_session(
            name,
            metadata={"voice_parent": voice_id, "brief": body},
            project_id=parent_project.id if parent_project is not None else None,
        )
        await manager.submit(state.session.id, body, as_answer=False, origin="voice")
        await self.emit("status", {"state": "delegating", "title": name})
        await self.emit("agents", {"agents": await self.agents()})
        return {"session_id": state.session.id, "title": name, "steered": False, "answered": False}

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
            news = self._news.get(row["id"], {})
            out.append(
                {
                    "session_id": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "last_message_at": row["last_message_at"],
                    "answer": answer[:LIST_CLIP],
                    "progress": news.get("line", ""),
                    "progress_at": news.get("at", ""),
                    "waiting": news.get("waiting", ""),
                }
            )
            if len(out) >= MAX_AGENTS:
                break
        return out

    async def result(self, session_id: str) -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        state = await self.own_agent(session_id)
        rows = await manager.list_sessions(limit=200)
        status = next((r["status"] for r in rows if r["id"] == session_id), "idle")
        return {"session_id": session_id, "title": state.session.title, "status": status, "answer": (await self.last_answer(session_id))[:ANSWER_CLIP]}

    async def stop_agent(self, session_id: str) -> bool:
        manager = self.app.manager
        assert manager is not None
        await self.own_agent(session_id)
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
        if manager is None:
            raise RuntimeError("there is no session manager to say anything to")
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
            # Every event of every session arrives here, so nothing expensive may happen before the
            # cheap check on what the process already holds: is this one of the concierge's agents?
            if not self._is_delegated(session_id):
                return
            payload = event.payload
            run_id = str(getattr(event, "run_id", "") or "")
            if event.type is EventType.TOOL_CALL_PENDING and payload.get("kind") == "ask_user":
                await self._agent_asks(session_id, event)
            elif event.type is EventType.CONTENT_BLOCK_DELTA:
                self._collect(session_id, run_id, delta_text(payload))
            elif event.type is EventType.MESSAGE_START:
                self._agent_drafts.pop((session_id, run_id), None)
                if self._listeners:
                    await self.emit("agents", {"agents": await self.agents()})
            elif event.type is EventType.MESSAGE_STOP:
                await self._agent_paused(session_id, run_id, payload)
            elif event.type is EventType.TOOL_RESULT and payload.get("is_error"):
                await self._agent_blocked(session_id, payload)
            elif self._listeners and event.type is EventType.STATE_CHANGED:
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

    def _collect(self, session_id: str, run_id: str, text: str) -> None:
        """Hold what an agent is writing, up to the cap; a delta that is not prose adds nothing."""
        if not text or not self.app.config.voice.progress:
            return
        key = (session_id, run_id)
        self._agent_drafts[key] = (self._agent_drafts.get(key, "") + text)[:DRAFT_CLIP]

    async def _agent_paused(self, session_id: str, run_id: str, payload: dict[str, Any]) -> None:
        """An agent stopped writing. If it stopped to call a tool, what it wrote is progress."""
        draft = self._agent_drafts.pop((session_id, run_id), "")
        if not draft or not settles_interim(payload):
            return
        await self._note_progress(session_id, progress_line(draft))

    async def _note_progress(self, session_id: str, line: str) -> None:
        """Record an interim for the page, and schedule the one the concierge will hear.

        The page gets it immediately — it is a line of text in a panel, it costs nothing and it is what
        the operator looks at when they want the detail. The concierge gets it through the window below,
        because for the concierge an interim is a spoken turn, and spoken turns are expensive in the one
        currency that matters here: the operator's attention.
        """
        if not line:
            return
        self._news.pop(session_id, None)  # re-inserted, so the trim below drops the quietest agent, not this one
        self._news[session_id] = {"line": line, "at": datetime.now(UTC).isoformat(), "waiting": ""}
        for stale in list(self._news)[: -2 * MAX_AGENTS]:
            # A delegated session that never finishes leaves its news behind; the panel shows at most
            # MAX_AGENTS of them anyway, so the oldest are dropped rather than kept for a restart.
            del self._news[stale]
        if self._listeners:
            await self.emit("agents", {"agents": await self.agents()})
        config = self.app.config.voice
        if not config.progress or not self._listeners:
            # Nobody is listening: an interim is not held for later. By the time a page connects the
            # agent has either moved on or finished, and the final report says more than this would.
            return
        self._waiting_relay[session_id] = line
        task = self._relay_tasks.get(session_id)
        if task is None or task.done():
            self._relay_tasks[session_id] = asyncio.create_task(self._relay_progress(session_id), name=f"voice-progress:{session_id}")

    async def _relay_progress(self, session_id: str) -> None:
        """Wait out the window and the rate cap, then relay whichever interim is the newest by then."""
        config = self.app.config.voice
        try:
            await asyncio.sleep(config.progress_window_seconds)
            gap = config.progress_min_gap_seconds - (time.monotonic() - self._relayed_at.get(session_id, -config.progress_min_gap_seconds))
            if gap > 0:
                await asyncio.sleep(gap)
            line = self._waiting_relay.pop(session_id, "")
            manager = self.app.manager
            if not line or not self._listeners or manager is None:
                return
            if not self._fleet_has_room():
                return
            if not await self._concierge_free(config.progress_window_seconds):
                # The concierge is in the middle of a turn of its own. Submitting here would steer that
                # turn, and the operator would hear their own answer interrupted by an agent's aside.
                # A progress line is not worth that, and the agent's next paragraph replaces it anyway.
                return
            state = await manager.get_state(session_id)
            if state is None or state.metadata.get("voice_parent") != self._id:
                return
            self._relayed_at[session_id] = time.monotonic()
            self._relays.append(self._relayed_at[session_id])
            await self.say_to_concierge(
                report_block(
                    kind="progress",
                    title=state.session.title,
                    session_id=session_id,
                    state="is still working; this is what it said on the way, not a result",
                    body=line,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a progress line is the least important thing here
            logger.exception("could not relay an agent's progress")
        finally:
            self._relay_tasks.pop(session_id, None)
            if self._waiting_relay.get(session_id) and self._listeners:
                # A line that arrived while this task was awaiting: it found the task not done, so
                # nothing scheduled another, and this one has already taken its own line away.
                self._relay_tasks[session_id] = asyncio.create_task(self._relay_progress(session_id), name=f"voice-progress:{session_id}")

    def _fleet_has_room(self) -> bool:
        """Whether another interim may be spoken at all this minute, counting every agent together.

        The per-agent floor bounds one narrator; twenty agents is what ``MAX_AGENTS`` allows, and
        twenty of them each obeying their own floor is a conversation nobody can hold. Over the cap
        the oldest relays are what age out of the window, and the line that arrives is dropped.
        """
        cap = int(self.app.config.voice.progress_max_per_minute)
        now = time.monotonic()
        self._relays = [at for at in self._relays if now - at < 60.0]
        return len(self._relays) < cap

    async def _concierge_free(self, hold_seconds: float) -> bool:
        """Whether the concierge can be told something now, after waiting up to ``hold_seconds`` for it.

        The operator's own turn comes first, always: the voice session answers what was said to it, and an
        interim that arrives mid-answer waits its turn or is dropped. Barge-in keeps working as before —
        the operator talking over the answer still goes through ``interrupt``.
        """
        manager = self.app.manager
        if manager is None or not self._id:
            return False
        deadline = time.monotonic() + max(hold_seconds, 0.0)
        while True:
            state = manager.live_state(self._id)
            if state is None or not state.running:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(RELAY_POLL_SECONDS, max(deadline - time.monotonic(), 0.0)))

    def _stop_relay(self, session_id: str) -> None:
        """Nothing more is relayed for this agent: what it was about to say is behind the news it just made."""
        self._waiting_relay.pop(session_id, None)
        task = self._relay_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def drain_progress(self) -> None:
        """Wait for the scheduled interims to be relayed or dropped. The tests' way to see what the window decided."""
        while True:
            tasks = [t for t in self._relay_tasks.values() if not t.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _agent_blocked(self, session_id: str, payload: dict[str, Any]) -> None:
        """A tool call the policy refused: the agent is stopped until the operator approves it.

        This is relayed like a question, not like progress — it is held for a page that is not connected
        and it does not wait out the window — because it is the one interim state that goes nowhere on its
        own. An agent narrating gets on with its job; an agent waiting for an approval does not.
        """
        match = APPROVAL_KEY_RE.search(str(payload.get("content") or ""))
        manager = self.app.manager
        if match is None or manager is None or not self.app.config.voice.progress:
            return
        key = match.group(1)
        reported = self._approvals.setdefault(session_id, [])
        if key in reported:
            return  # the agent retries the refused call; the operator is told about it once
        state = await manager.get_state(session_id)
        if state is None or state.metadata.get("voice_parent") != self._id:
            return
        reported.append(key)
        del reported[:-APPROVALS_REMEMBERED]
        pending = (state.metadata.get("policy_pending") or {}).get(key) or {}
        what = f"{pending.get('tool')}: {pending.get('text')}" if pending else "a call it tried to make"
        self._news[session_id] = {"line": f"waiting for approval — {what}"[:PROGRESS_CLIP], "at": datetime.now(UTC).isoformat(), "waiting": "approval"}
        self._stop_relay(session_id)
        await self.report(
            report_block(
                kind="approval",
                title=state.session.title,
                session_id=session_id,
                state="is stopped: the policy will not run one of its calls until the operator approves it in that session",
                body=what[:REPORT_CLIP],
            )
        )
        await self.emit("agents", {"agents": await self.agents()})

    def _is_delegated(self, session_id: str) -> bool:
        """Whether the concierge started this session, read from what the process already holds."""
        manager = self.app.manager
        state = manager.live_state(session_id) if manager is not None else None
        return state is not None and state.metadata.get("voice_parent") == self._id

    async def _flush_draft(self, run_id: str) -> None:
        tail = speakable(self._drafts.pop(run_id, ""))
        if tail:
            await self.emit("say", {"text": tail})

    async def _agent_asks(self, session_id: str, event: Any) -> None:
        manager = self.app.manager
        if manager is None:
            return
        state = await manager.get_state(session_id)
        if state is None or state.metadata.get("voice_parent") != self._id:
            return
        questions = (event.payload.get("ask_user_payload") or {}).get("questions") or []
        asked = str(questions[0].get("question") if questions else "").strip()
        self._news[session_id] = {"line": asked[:PROGRESS_CLIP], "at": datetime.now(UTC).isoformat(), "waiting": "operator"}
        self._stop_relay(session_id)
        await self.report(
            report_block(
                kind="question",
                title=state.session.title,
                session_id=session_id,
                state="is waiting for the operator and does nothing until it is answered; the answer goes back with Delegate(title, task, session_id) naming that id",
                body=asked[:REPORT_CLIP] or "it did not say what it is asking; its session has the question",
            )
        )
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
        # Whatever it was about to say on the way is behind us now: the answer says more, and a progress
        # line spoken after "it finished" would tell the operator the job is still running.
        self._stop_relay(session_id)
        self._news.pop(session_id, None)
        self._approvals.pop(session_id, None)
        self._agent_drafts = {k: v for k, v in self._agent_drafts.items() if k[0] != session_id}
        await self.report(report_block(kind="final", title=state.session.title, session_id=session_id, state="finished", body=body))
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


__all__ = [
    "REPORT_CLOSE",
    "REPORT_OPEN",
    "SESSION_KEY",
    "Voice",
    "delta_text",
    "effective_tts",
    "install",
    "progress_line",
    "quoted",
    "report_block",
    "settles_interim",
    "speakable",
    "split_sentences",
    "tts_configured",
]
