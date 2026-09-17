"""HTTP API for the Mini App (FastAPI, served in-process by uvicorn)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import re
import secrets
import shutil
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode

import httpx
import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from protocore.contracts.memory import MemoryScope
from protocore.contracts.types import ToolResultBlock, ToolUseBlock
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from daedalus.config import NO_MODEL_MESSAGE, PROVIDER_KINDS, HeartbeatConfig, ModelPresetConfig, ProviderConfig
from daedalus.doctor import DoctorContext, render_text, run_checks, summarize
from daedalus.extensions import commands as slash
from daedalus.extensions.heartbeat import TEMPLATE as HEARTBEAT_TEMPLATE
from daedalus.extensions.inbound import PAYLOAD_MAX_CHARS, flatten_payload, verify_signature
from daedalus.extensions.services import SHARE_COOKIE_PREFIX, SHARE_MODES, pid_alive
from daedalus.extensions.voice import tts_configured
from daedalus.host import capabilities
from daedalus.host.policy import sealed_root
from daedalus.host.prompts import DEFAULT_RULES
from daedalus.host.session_runner import TENANT, Attachment
from daedalus.host.transcript_view import message_view
from daedalus.providers.openai_compat import UsageRecord
from daedalus.security import redact
from daedalus.speech import catalog as speech_catalog
from daedalus.speech import models as speech_models
from daedalus.speech import service as speech_service
from daedalus.speech.engine import SAMPLE_RATE, SpeechError, clamp_rate
from daedalus.speech.service import recogniser_available, transcribe_recording
from daedalus.stores import pairing, passkeys
from daedalus.stores.projects import ProjectError, ProjectSettings, normalise_root
from daedalus.tools import websearch
from daedalus.transport.telegram.front import TelegramBusy, TelegramOutbox, TelegramRefused
from daedalus.transport.telegram.markdown import split_message
from daedalus.transport.telegram.voice import (
    TranscriptionError,
    asr_configured,
    effective_asr,
    voice_note_text,
)

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.config import RuntimeConfig

logger = logging.getLogger(__name__)

INIT_DATA_MAX_AGE = 24 * 3600
KEYPROXY_CACHE_SECONDS = 5.0
"""How long the key proxy's answer about its upstreams is reused. It is read once per app load and a
proxy that does not answer costs the whole timeout; a few seconds is well inside a first screen."""


LOGIN_WIDGET_MAX_AGE = 24 * 3600
SESSION_COOKIE = "daedalus_session"
SESSION_TTL = 30 * 24 * 3600
VOICE_AUDIO_MAX = 25 << 20
"""One spoken utterance, not a recording session: anything larger is a mistake, not speech."""
VOICE_SAY_MAX_CHARS = 4000
"""One utterance in words. Dictation runs long, a pasted document is not speech: past this it is refused."""

LISTEN_CHUNK_MAX = 2 << 20
"""Largest PCM chunk one POST may carry — about a minute of 16 kHz mono. A page sends a fifth of a
second at a time; anything near this is a client that stopped streaming and started uploading."""

LISTEN_IDLE_SECONDS = 120.0
"""A listening stream nobody has fed for this long is abandoned and its engine handle released."""

LISTEN_MAX_STREAMS = 4
"""More open streams than this means a page that never closes them; the oldest idle one goes."""

ENGINE_INSTALL_TIMEOUT = 600.0
"""How long the speech engine's install may take before it is killed. A resolution against a slow
index is minutes; anything past this is hung, and it is holding a worker while it hangs."""

VOICE_TTS_MAX_CHARS = 2000
"""One sentence to read aloud. The page only ever sends what ``split_sentences`` cut, and the speech
endpoint is usually metered by the character, so an unbounded body is somebody else's bill."""


def validate_login_widget(data: dict[str, Any], bot_token: str, *, max_age: int = LOGIN_WIDGET_MAX_AGE) -> dict[str, Any]:
    """Verify what Telegram's Login Widget handed the page (id, first_name, …, auth_date, hash) and return it."""
    fields = {k: v for k, v in data.items() if k != "hash" and v is not None}
    received = str(data.get("hash") or "")
    if not received or not fields.get("id") or not fields.get("auth_date"):
        raise ValueError("incomplete login data")
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError("bad signature")
    if time.time() - int(fields["auth_date"]) > max_age:
        raise ValueError("login data expired")
    return fields


def session_cookie_value(secret: bytes, user_id: int, *, ttl: int = SESSION_TTL) -> str:
    """A signed, expiring statement that the browser holding it is the owner."""
    payload = base64.urlsafe_b64encode(json.dumps({"uid": int(user_id), "exp": int(time.time()) + ttl}).encode()).decode().rstrip("=")
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_session_cookie(secret: bytes, value: str) -> int | None:
    """The user id a session cookie vouches for, or None when it is forged or expired."""
    payload, _, signature = (value or "").partition(".")
    if not payload or not signature:
        return None
    if not hmac.compare_digest(hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest(), signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    if "uid" not in data:
        return None
    # Zero is the owner of an installation with no Telegram account behind it, so it is an answer
    # like any other: only a forged or expired cookie has none.
    return int(data["uid"])


def validate_init_data(init_data: str, bot_token: str, *, max_age: int = INIT_DATA_MAX_AGE) -> dict[str, Any]:
    """Verify Telegram Mini App ``initData`` and return its fields."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise ValueError("missing hash")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError("bad signature")
    auth_date = int(pairs.get("auth_date", "0"))
    if time.time() - auth_date > max_age:
        raise ValueError("initData expired")
    if "user" in pairs:
        pairs["user"] = json.loads(pairs["user"])
    return pairs


class PasskeyRegisterBody(BaseModel):
    credential: dict[str, Any]
    """What ``navigator.credentials.create`` returned, serialised as the browser gives it."""
    name: str = Field(default="", max_length=80)
    ceremony: str = Field(default="", max_length=64)
    """The id the matching ``begin`` call answered with: it names the challenge this response signs."""


class PasskeyLoginBody(BaseModel):
    credential: dict[str, Any]
    """What ``navigator.credentials.get`` returned, serialised as the browser gives it."""
    ceremony: str = Field(default="", max_length=64)


class SendMessageBody(BaseModel):
    text: str
    steer: bool = False


class VoiceSayBody(BaseModel):
    text: str


class VoiceSpeakBody(BaseModel):
    text: str


class AnswerBody(BaseModel):
    answers: list[dict[str, Any]]


class SpaFiles(StaticFiles):
    """The built app with its screens in the URL: a path that is not a file is the app itself."""

    async def get_response(self, path: str, scope: Any) -> Any:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
                return await super().get_response("index.html", scope)
            raise


class ProjectBody(BaseModel):
    name: str
    root: str
    """The absolute path of the folder, as the operator gave it. It need not exist here: in a container
    it becomes reachable once the launcher mounts it, and until then the row is what the launcher reads."""
    snapshots: bool = False


class ProjectPatch(BaseModel):
    name: str | None = None
    root: str | None = None
    snapshots: bool | None = None


class NewSessionBody(BaseModel):
    title: str
    prompt: str | None = None
    project_id: str | None = None
    """The project to work in: its folder becomes the session's workspace and the limit of its reach."""
    workspace: str | None = None
    """A workspace directory name to work in (another session's id or a named workspace); empty = a directory of its own."""
    tools_off: list[str] = Field(default_factory=list)
    """Tools this session does not get (by name); everything else stays on."""
    preset: str | None = None
    """The model preset the session starts on; empty = the global default."""
    loop: LoopBody | None = None
    """Make it a loop agent: woken up for this instruction on an interval or when it says so."""


class ToolsOffBody(BaseModel):
    tools_off: list[str] = Field(default_factory=list)


class MemoryBody(BaseModel):
    text: str = Field(min_length=1)
    kind: str = "fact"
    scope: str = "global"
    scope_key: str = ""


class MemoryPatch(BaseModel):
    text: str | None = None
    kind: str | None = None


class MemoryDeleteBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class WorkspaceBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ShareBody(BaseModel):
    mode: str = Field(pattern="^(local|public|key)$")
    rotate_key: bool = False


class LoopBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    mode: str = "interval"
    interval_minutes: int | None = Field(default=None, ge=1)
    max_runs: int | None = Field(default=None, ge=1)
    start_now: bool = True


class LoopActionBody(BaseModel):
    action: str
    reason: str = ""


class DecisionBody(BaseModel):
    decision: str
    reason: str = ""


class ScheduleBody(BaseModel):
    name: str
    prompt: str
    cron: str | None = None
    run_at: str | None = None
    model: str | None = None
    kind: str = "agent"
    target_session: str | None = None
    run_in: str = "new"


class SchedulePatchBody(BaseModel):
    name: str | None = None
    prompt: str | None = None
    cron: str | None = None
    run_at: str | None = None
    enabled: bool | None = None
    model_config = {"extra": "forbid"}


class InboxReadBody(BaseModel):
    ids: list[int] | None = None
    """Omitted = mark everything read."""


class HeartbeatBody(BaseModel):
    text: str | None = Field(default=None, max_length=20_000)
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=5)
    active_hours: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    preset: str | None = None
    max_runs_per_day: int | None = Field(default=None, ge=1)


class RenameBody(BaseModel):
    title: str


class RevertBody(BaseModel):
    seq: int


class InboundBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    sender: str = Field(default="local", max_length=100)
    session: str | None = None
    """Session id or exact title; empty = the standing '[inbound]' session."""
    prompt: str = ""


class BoardTaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    acceptance: str = ""
    checklist: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    priority: int = Field(default=3, ge=1, le=5)
    notes: str = ""
    session_id: str | None = None
    """The agent whose board the task goes on; ``None`` posts it to every agent's board."""


class BoardUpdateBody(BaseModel):
    status: str | None = None
    note: str = ""
    check: list[int] | None = None
    uncheck: list[int] | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    title: str | None = None
    acceptance: str | None = None


class PeerBody(BaseModel):
    name: str
    session_id: str


class CommandBody(BaseModel):
    line: str = Field(min_length=2, max_length=4000)


class BriefBody(BaseModel):
    brief: str = Field(default="", max_length=12_000)


class SessionCapBody(BaseModel):
    usd_cap: float | None = None
    """None removes the session's own cap; the global limits still apply."""


class ModeBody(BaseModel):
    mode: str | None = None
    """A configured mode name; null or "" = default behaviour."""


class ForkBody(BaseModel):
    seq: int
    title: str | None = None


class CompactBody(BaseModel):
    instructions: str = ""


class SettingsBody(BaseModel):
    model: dict[str, Any] | None = None
    prompt: dict[str, Any] | None = None
    vision: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    mcp: dict[str, Any] | None = None
    self_change: dict[str, Any] | None = None
    limits: dict[str, Any] | None = None
    """max_iterations, tool_timeout_seconds and usd_per_run; the daily cap is the supervisor's."""
    balance: dict[str, Any] | None = None
    scheduler: dict[str, Any] | None = None
    telegram: dict[str, Any] | None = None
    asr: dict[str, Any] | None = None
    modes: dict[str, Any] | None = None
    webhooks: dict[str, Any] | None = None
    ops: dict[str, Any] | None = None
    compaction: dict[str, Any] | None = None
    answer_language: str | None = None


class SttSelectBody(BaseModel):
    """Choosing a local speech model. Every field is optional; what is sent is what changes."""

    model: str | None = None
    """A catalog id, or "" to stop using a local model."""
    language: str | None = None
    threads: int | None = Field(default=None, ge=1, le=16)


class SearchCheckBody(BaseModel):
    """The Mini App's "check" button for the WebSearch backends."""

    backend: str = ""
    """One backend to try on its own; empty = the configured backend and its fallbacks."""
    query: str = "searxng json api"


class ProviderPatch(BaseModel):
    """Partial edit of one configured provider endpoint (see ``apply_provider_patch``)."""

    kind: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    """Omitted = keep the stored key; "" or null = clear it; any other value = store it."""
    timeout_seconds: float | None = None
    pricing: dict[str, dict[str, Any]] | None = None
    """Per-model USD per 1M tokens; a subscription-backed endpoint sets zeros so its runs are metered, not unknown."""


class ModelsLookupBody(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    provider: str | None = None
    """A configured client id: probe its stored base_url with its stored key (keys stay server-side)."""


class PresetPatch(BaseModel):
    label: str | None = None
    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None
    reasoning_effort: str | None = None
    images: bool | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None


PRESET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NO_MODEL_LABEL = "no model"
"""Where a model name would go in a list or a chip and there is none yet."""


def resolve_model_patch(current: dict[str, Any], patch: dict[str, Any]) -> None:
    """Merge a ``model`` settings patch (default preset id and/or fallback chain) in place."""
    presets = current.get("presets") or {}
    merged = dict(current.get("model", {}))
    if "preset" in patch:
        preset_id = str(patch.get("preset") or "").strip()
        if preset_id not in presets:
            raise HTTPException(400, f"no such model preset {preset_id!r}")
        merged["preset"] = preset_id
    if "chain" in patch:
        chain = [str(c) for c in (patch.get("chain") or []) if str(c) in presets and str(c) != merged.get("preset")]
        merged["chain"] = chain
    current["model"] = merged


def per_million(pricing: Any) -> dict[str, float]:
    """A price per token (what OpenRouter publishes) as a price per million (what everything here uses)."""
    if not isinstance(pricing, dict):
        return {}
    out: dict[str, float] = {}
    for name, key in (("input", "prompt"), ("output", "completion"), ("cache_hit", "input_cache_read")):
        try:
            value = float(pricing[key])
        except (KeyError, TypeError, ValueError):
            continue
        if value >= 0:
            out[name] = round(value * 1_000_000, 6)
    return out


def model_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """One model as the Add-a-model list shows it: whatever the endpoint chose to say about it.

    Only ``id`` is ever there — a plain OpenAI-compatible ``/models`` says nothing else, and the
    operator fills the rest in by hand. OpenRouter carries the context window, the modalities,
    whether the model reasons and what a million tokens cost; those prefill the form instead.
    """
    entry: dict[str, Any] = {"id": str(raw.get("id") or "").strip()}
    name = raw.get("name")
    if isinstance(name, str) and name.strip():
        entry["name"] = name.strip()
    top = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
    context = raw.get("context_length") or top.get("context_length")
    if isinstance(context, int | float) and context > 0:
        entry["context_length"] = int(context)
    max_output = top.get("max_completion_tokens")
    if isinstance(max_output, int | float) and max_output > 0:
        entry["max_output_tokens"] = int(max_output)
    architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    modalities = architecture.get("input_modalities")
    if isinstance(modalities, list):
        entry["input_modalities"] = [str(m) for m in modalities]
        entry["images"] = "image" in entry["input_modalities"]
    parameters = raw.get("supported_parameters")
    if isinstance(parameters, list):
        entry["reasoning"] = "reasoning" in parameters or "reasoning_effort" in parameters
    pricing = per_million(raw.get("pricing"))
    if pricing:
        entry["pricing"] = pricing
    return entry


async def lookup_openai_models(
    base_url: str,
    api_key: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """List the model ids served at an OpenAI-compatible ``/models`` endpoint.

    Tries ``{base}/models`` first, then ``{base}/v1/models``, so a base_url typed
    without the ``/v1`` prefix (``http://host:9000``) still resolves; the returned
    ``base_url`` is the exact root the list was found at. ``models`` is the bare list of
    ids; ``entries`` is the same list with whatever else the endpoint said about each one
    (see :func:`model_entry`). Tests inject an
    ``httpx.AsyncClient`` with a mock transport; production uses its own short-timeout
    client. Raises :class:`ValueError` when nothing answers with a model list.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base or not re.match(r"^https?://", base, re.IGNORECASE):
        raise ValueError("base_url must be an http(s) URL")
    candidates = [f"{base}/models"]
    if not base.endswith("/v1"):
        candidates.append(f"{base}/v1/models")
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    errors: list[str] = []
    owns_client = client is None
    if client is None:
        # No redirects: the base URL is the operator's and the request carries their key. httpx
        # drops the header across origins, so this is defence in depth — and a models endpoint that
        # answers with a redirect is a misconfiguration worth seeing rather than following.
        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)
    try:
        for url in candidates:
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                errors.append(f"{url}: {exc.__class__.__name__}")
                continue
            if response.status_code != 200:
                errors.append(f"{url}: HTTP {response.status_code}")
                continue
            try:
                data = response.json()
            except ValueError:
                errors.append(f"{url}: not JSON")
                continue
            listed = data.get("data") if isinstance(data, dict) else None
            rows = [m for m in listed if isinstance(m, dict) and str(m.get("id") or "").strip()] if isinstance(listed, list) else []
            if not rows:
                errors.append(f"{url}: no model list in the response")
                continue
            entries = [model_entry(m) for m in rows]
            return {"base_url": url[: -len("/models")], "models": [e["id"] for e in entries], "entries": entries}
    finally:
        if owns_client:
            await client.aclose()
    raise ValueError("; ".join(errors[:3]) or "no response")


def apply_provider_patch(providers: dict[str, Any], provider_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial patch into a config-style ``providers`` dict (in place; returns it).

    ``patch`` is the ``exclude_unset`` dump of :class:`ProviderPatch` — only the keys the
    client actually sent. Unknown or null fields are skipped; ``api_key`` is special-cased
    so an absent key never overwrites a stored one. Unknown ids create a new endpoint
    (defaulting to the generic OpenAI-compatible kind).
    """
    entry = dict(providers.get(provider_id) or {"kind": "openai_compat"})
    for key, value in patch.items():
        if key == "api_key":
            entry[key] = value or ""
        elif value is not None and key in ProviderConfig.model_fields:
            entry[key] = value
    providers[provider_id] = entry
    return providers


def mask_provider_keys(settings_view: dict[str, Any]) -> dict[str, Any]:
    """Never echo stored credentials back to the Mini App.

    Provider keys become an ``api_key_set`` flag; MCP header and env values (the same
    values the redactor masks everywhere else) are shown as the mask and, when sent back
    unchanged, keep their stored value (see :func:`restore_masked_mcp`).
    """
    for entry in (settings_view.get("providers") or {}).values():
        entry["api_key_set"] = bool(entry.get("api_key"))
        entry["api_key"] = ""
    for server in ((settings_view.get("mcp") or {}).get("servers") or {}).values():
        for section in ("headers", "env"):
            values = server.get(section)
            if isinstance(values, dict):
                server[section] = {k: (redact.MASK if v else v) for k, v in values.items()}
    asr = settings_view.get("asr") or {}
    if isinstance(asr, dict):
        asr["api_key_set"] = bool(asr.get("api_key"))
        asr["api_key"] = ""
    for hook in (settings_view.get("webhooks") or {}).values():
        if isinstance(hook, dict):
            hook["secret_set"] = bool(hook.get("secret"))
            hook["secret"] = ""
    settings_view["provider_kinds"] = list(PROVIDER_KINDS)
    return settings_view


def restore_masked_secrets(current: dict[str, Any], dumped: dict[str, Any]) -> None:
    """An empty or masked secret sent back by the Mini App means "keep what is stored"."""
    asr = dumped.get("asr")
    if isinstance(asr, dict) and not asr.get("api_key") and "api_key" in asr:
        asr["api_key"] = (current.get("asr") or {}).get("api_key", "")
    hooks = dumped.get("webhooks")
    if isinstance(hooks, dict):
        for name, hook in hooks.items():
            if isinstance(hook, dict) and not hook.get("secret") and "secret" in hook:
                hook["secret"] = ((current.get("webhooks") or {}).get(name) or {}).get("secret", "")


def restore_masked_mcp(current: dict[str, Any], patch: dict[str, Any]) -> None:
    """A masked MCP header/env value sent back by the Mini App means "keep what is stored"."""
    stored = ((current.get("mcp") or {}).get("servers") or {})
    for name, server in ((patch.get("servers") or {}).items()):
        if not isinstance(server, dict):
            continue
        for section in ("headers", "env"):
            values = server.get(section)
            if not isinstance(values, dict):
                continue
            kept = (stored.get(name) or {}).get(section) or {}
            for key, value in list(values.items()):
                if value == redact.MASK:
                    if key in kept:
                        values[key] = kept[key]
                    else:
                        del values[key]


def _deep_merge(base: Any, patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        out[key] = _deep_merge(out.get(key, {}), value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


WEBHOOK_MAX_BYTES = 2 * 1024 * 1024

GZIP_MIN_BYTES = 1024
"""Responses smaller than this go out as they are: compressing them costs more than it saves."""

MAX_TRANSCRIPT_PAGE = 2000
"""Turns one request may ask for. Beyond this a client is asking for a session, not a page."""


_TOOL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Self-development", ("Self", "LearningReport")),
    ("Agents & peers", ("SubAgent", "SpawnAgent", "AskPeer", "PeerList")),
    ("Scheduling & board", ("Schedule", "Board", "Intent", "StaySilent", "Loop")),
    ("Services", ("Service",)),
    ("MCP", ("Mcp",)),
    ("Memory & history", ("Remember", "Recall", "Forget", "History", "Skill")),
    ("Web", ("Web",)),
    ("Files & shell", ("Exec", "Job", "Read", "Write", "Edit", "MultiEdit", "Find", "Search", "SendFile", "ImageView", "Verify")),
)


def _tool_group(name: str) -> str:
    for group, prefixes in _TOOL_GROUPS:
        if any(name.startswith(p) for p in prefixes):
            return group
    return "Other"


def build_app(app: Application, api_token: str) -> FastAPI:
    api = FastAPI(title="Daedalus", docs_url=None, redoc_url=None)
    # A session page is JSON and compresses about fivefold; over a phone connection that is the
    # difference the operator feels. The event stream is excluded by content type, so a token
    # still leaves the process the moment it arrives.
    api.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)
    manager = app.manager
    assert manager is not None
    settings = app.settings
    # The manager resolved this on the way up; resolving it again here costs nothing and keeps the
    # routes buildable around a stand-in manager (the auth tests build the app without one).
    caps = getattr(manager, "capabilities", None) or capabilities.resolve(settings, app.config)
    secret_cache: dict[str, bytes] = {}
    secret_lock = asyncio.Lock()

    async def session_secret() -> bytes:
        """What signs the browser's session cookie.

        The API token, so that rotating it invalidates every cookie, plus a secret minted once for
        this installation, so that the token alone is not enough to forge one. Not the bot token:
        an installation without Telegram has none, and its cookies must still mean something.
        """
        async with secret_lock:  # two first requests at once must not mint two secrets
            if "value" not in secret_cache:
                stored = await app.db.kv_get("session_secret")
                if not stored:
                    stored = secrets.token_urlsafe(32)
                    await app.db.kv_set("session_secret", stored)
                secret_cache["value"] = hashlib.sha256(f"session:{api_token}:{stored}".encode()).digest()
            return secret_cache["value"]

    async def revoke_sessions() -> None:
        """Every browser session ends at once: a new secret signs the cookies from here on."""
        async with secret_lock:
            await app.db.kv_set("session_secret", secrets.token_urlsafe(32))
            secret_cache.clear()

    def over_https(request: Request) -> bool:
        """Whether the browser reached us over TLS.

        A cookie marked secure is never sent back over plain http, which is exactly how the app is
        opened on the machine itself; marking it unconditionally locks that case out. TLS is either
        on this connection or on the proxy the public address names; a forwarded-proto header is not
        consulted, because any client can send one.
        """
        return request.url.scheme == "https" or settings.miniapp_public_url.lower().startswith("https://")

    async def sign_in(response: Response, request: Request) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            session_cookie_value(await session_secret(), settings.owner_user_id),
            max_age=SESSION_TTL,
            httponly=True,
            secure=over_https(request),
            samesite="lax",
            path="/",
        )

    async def auth(request: Request) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        if header.startswith("tma "):
            if not settings.telegram_bot_token:
                raise HTTPException(401, "this installation has no Telegram bot")
            try:
                data = validate_init_data(header[4:], settings.telegram_bot_token)
            except ValueError as exc:
                raise HTTPException(401, f"invalid initData: {exc}") from exc
            user = data.get("user") or {}
            if int(user.get("id", 0)) != settings.owner_user_id:
                raise HTTPException(403, "not the owner")
            return {"user_id": settings.owner_user_id}
        token = request.headers.get("x-daedalus-token")
        if not token and request.url.path.endswith("/download"):
            token = request.query_params.get("token")  # browser navigation cannot set headers
        if token and secrets.compare_digest(token, api_token):
            return {"user_id": settings.owner_user_id}
        # A browser that paired, signed in with a passkey or used Telegram's widget holds a signed cookie.
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie and verify_session_cookie(await session_secret(), cookie) == settings.owner_user_id:
            return {"user_id": settings.owner_user_id, "via": "cookie"}
        raise HTTPException(401, "authentication required")

    # -- auth: the site outside Telegram ---------------------------------------------

    bot_username: dict[str, str] = {}

    async def _bot_username() -> str | None:
        """The bot whose Login Widget vouches for the operator; None when there is none to ask."""
        if bot_username.get("name"):
            return bot_username["name"]
        front = app.front
        if front is None or getattr(front, "bot", None) is None:
            return None
        try:
            me = await front.bot.get_me()
        except Exception:  # noqa: BLE001 — an unreachable bot is one way in missing, not a broken login page
            logger.warning("could not read the bot's username", exc_info=True)
            return None
        bot_username["name"] = str(me.username or "")
        return bot_username["name"] or None

    def _relying_party() -> passkeys.RelyingParty:
        return passkeys.relying_party(settings.miniapp_public_url, settings.api_port)

    # Both ceremonies are a pair of calls, and the challenge of the first has to survive until the
    # second. Each begin names its ceremony, so two browsers (or a stranger polling the public login
    # endpoint) cannot spend each other's challenge; the table stays small by dropping what expired.
    CEREMONY_TTL, CEREMONY_MAX = 300.0, 64
    challenges: dict[str, tuple[str, bytes, float]] = {}

    def _hold_challenge(kind: str, options: dict[str, Any]) -> str:
        now = time.time()
        for key in [k for k, (_, _, until) in challenges.items() if until < now]:
            challenges.pop(key, None)
        while len(challenges) >= CEREMONY_MAX:
            challenges.pop(next(iter(challenges)))  # the oldest goes; a flood cannot grow the table
        ceremony = secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64decode(options["challenge"] + "=" * (-len(options["challenge"]) % 4))
        challenges[ceremony] = (kind, challenge, now + CEREMONY_TTL)
        return ceremony

    def _take_challenge(kind: str, ceremony: str) -> bytes:
        held = challenges.pop(ceremony, None) if ceremony else None
        if held is None or held[0] != kind or held[2] < time.time():
            raise HTTPException(400, "the request expired; start again")
        return held[1]

    @api.get("/api/auth/config")
    async def auth_config() -> dict[str, Any]:
        """What the login page needs: which ways in this installation actually has."""
        username = await _bot_username()
        return {
            "telegram": {"bot_username": username} if username else None,
            "passkeys": await passkeys.count(app.db),
            "pairing": await pairing.outstanding(app.db) > 0,
        }

    @api.post("/api/auth/telegram")
    async def auth_telegram(body: dict[str, Any], request: Request, response: JSONResponse) -> dict[str, Any]:
        """Telegram's Login Widget result: verified with the bot token, accepted only for the owner, answered with a session cookie."""
        if not settings.telegram_bot_token:
            raise HTTPException(503, "this installation has no Telegram bot")
        try:
            fields = validate_login_widget(body, settings.telegram_bot_token)
        except ValueError as exc:
            raise HTTPException(401, f"login refused: {exc}") from exc
        if int(fields.get("id", 0)) != settings.owner_user_id:
            raise HTTPException(403, "not the owner")
        await sign_in(response, request)
        return {"ok": True, "user_id": settings.owner_user_id, "name": fields.get("first_name")}

    @api.get("/api/auth/pair")
    async def auth_pair(code: str, request: Request) -> RedirectResponse:
        """A pairing link: spend the code, hand the browser a session cookie and open the app."""
        if not await pairing.redeem(app.db, code, state_dir=settings.state_dir):
            # A spent link still lands on the app: a browser that paired with it earlier is signed in
            # already, and one that is not sees the login page say why.
            return RedirectResponse("/app/?pairing=spent", status_code=303)
        response = RedirectResponse("/app/", status_code=303)
        await sign_in(response, request)
        return response

    @api.post("/api/auth/passkeys/register/begin")
    async def passkey_register_begin(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        options = await passkeys.registration_options(app.db, _relying_party())
        return {**options, "ceremony": _hold_challenge("register", options)}

    @api.post("/api/auth/passkeys/register/finish")
    async def passkey_register_finish(body: PasskeyRegisterBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            verified = passkeys.verify_registration(body.credential, challenge=_take_challenge("register", body.ceremony), rp=_relying_party())
        except Exception as exc:  # noqa: BLE001 — every failure here is the same answer: this key is not accepted
            raise HTTPException(400, f"the passkey was not accepted: {exc}") from exc
        if passkeys.declined_resident_key(body.credential):
            # Login offers no list of credentials (no username to type), so a key the authenticator
            # did not make discoverable could be enrolled and then never offered at sign-in.
            raise HTTPException(400, "this authenticator did not make the passkey discoverable, so it could not sign you in later; use a device or key that stores passkeys")
        transports = list(body.credential.get("response", {}).get("transports") or [])
        await passkeys.store(
            app.db,
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=int(verified.sign_count),
            transports=[str(x) for x in transports],
            name=body.name.strip() or "This device",
        )
        return {"ok": True, "passkeys": await passkeys.listing(app.db)}

    @api.post("/api/auth/passkeys/login/begin")
    async def passkey_login_begin() -> dict[str, Any]:
        if await passkeys.count(app.db) == 0:
            raise HTTPException(404, "no passkey is enrolled")
        options = await passkeys.authentication_options(app.db, _relying_party())
        return {**options, "ceremony": _hold_challenge("login", options)}

    @api.post("/api/auth/passkeys/login/finish")
    async def passkey_login_finish(body: PasskeyLoginBody, request: Request, response: JSONResponse) -> dict[str, Any]:
        stored = await passkeys.find(app.db, str(body.credential.get("id") or ""))
        if stored is None:
            raise HTTPException(403, "this passkey is not enrolled here")
        try:
            verified = passkeys.verify_authentication(
                body.credential,
                challenge=_take_challenge("login", body.ceremony),
                rp=_relying_party(),
                public_key=base64.urlsafe_b64decode(stored["public_key"] + "=" * (-len(stored["public_key"]) % 4)),
                sign_count=int(stored["sign_count"]),
            )
        except Exception as exc:  # noqa: BLE001 — a refused signature is a refused login, whatever went wrong
            raise HTTPException(403, f"the passkey was refused: {exc}") from exc
        await passkeys.used(app.db, stored["credential_id"], int(verified.new_sign_count))
        await sign_in(response, request)
        return {"ok": True, "user_id": settings.owner_user_id}

    @api.get("/api/auth/passkeys")
    async def passkey_list(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await passkeys.listing(app.db)

    @api.delete("/api/auth/passkeys/{passkey_id}")
    async def passkey_delete(passkey_id: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not await passkeys.remove(app.db, passkey_id):
            raise HTTPException(404, "no such passkey")
        return {"ok": True, "passkeys": await passkeys.listing(app.db)}

    @api.post("/api/auth/sessions/revoke")
    async def auth_revoke(request: Request, response: JSONResponse, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Sign out everywhere: every cookie issued so far stops working, this browser gets a fresh one."""
        await revoke_sessions()
        if who.get("via") == "cookie":
            await sign_in(response, request)
        return {"ok": True}

    @api.get("/api/auth/me")
    async def auth_me(who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"user_id": who["user_id"], "via": who.get("via", "token")}

    @api.post("/api/auth/logout")
    async def auth_logout(response: JSONResponse) -> dict[str, Any]:
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    # -- projects: the folders the operator adds, and the sessions that work inside them -------

    @api.get("/api/projects")
    async def list_projects(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """Every project, with who works in it and whether this process can reach its folder.

        ``reachable`` is the Docker seam: the row exists as soon as the operator adds the folder, but
        in a container the folder is only there once it is bind-mounted, so the app can say "restart
        to mount this" instead of showing a project whose files are mysteriously absent.
        """
        busy = manager.busy_sessions()
        out = []
        for project in await manager.projects.list():
            # Which of them are working right now, so the app can name them before it asks the
            # operator to confirm something that would move the folder under them.
            sessions = [{**s, "running": s["id"] in busy} for s in await manager.projects.sessions_of(project.id)]
            out.append({**project.view(), "sessions": sessions})
        return out

    @api.post("/api/projects")
    async def create_project(body: ProjectBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            project = await manager.projects.create(body.name, body.root, settings=ProjectSettings(snapshots=body.snapshots))
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {**project.view(), "sessions": []}

    async def _busy_in(project_id: str) -> list[dict[str, str]]:
        """The agents of this project with a turn in flight.

        Moving a root or forgetting a project re-points the workspace of every loaded session of it.
        Doing that mid-turn means the agent's next tool call resolves into a different directory from
        the one its earlier reads and its snapshot refer to, so it is refused while a run is up — the
        same rule ``revert`` and ``fork`` already keep.
        """
        busy = manager.busy_sessions()
        return [s for s in await manager.projects.sessions_of(project_id) if s["id"] in busy]

    def _refuse_busy(project: Any, busy: list[dict[str, str]], what: str) -> None:
        if not busy:
            return
        names = ", ".join(s["title"] or s["id"] for s in busy)
        one = len(busy) == 1
        raise HTTPException(409, f"{len(busy)} agent{'' if one else 's'} {'is' if one else 'are'} working in {project.name} right now ({names}); {what} would move the folder under {'it' if one else 'them'} mid-turn — stop {'it' if one else 'them'} first")

    @api.patch("/api/projects/{project_id}")
    async def patch_project(project_id: str, body: ProjectPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        current = await manager.projects.get(project_id)
        if current is None:
            raise HTTPException(404, "no such project")
        settings_patch = None if body.snapshots is None else ProjectSettings(snapshots=body.snapshots)
        try:
            moving = body.root is not None and normalise_root(body.root) != current.root
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        if moving:
            _refuse_busy(current, await _busy_in(project_id), "moving it")
        try:
            project = await manager.projects.update(project_id, name=body.name, root=body.root, settings=settings_patch)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        # The sessions this process already holds keep their own copy of the project: without this, a
        # folder moved in the app would reach only the sessions opened after the change.
        await manager.reload_project(project, project_id)
        return {**project.view(), "sessions": await manager.projects.sessions_of(project.id)}

    @api.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, detach: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget a project. Not one file of it is deleted — the folder is the operator's.

        A project with sessions in it is refused unless ``detach=1`` says what should happen to them:
        they keep their history and go back to a directory of their own, which is empty. Saying that
        out loud is the point of the refusal.
        """
        project = await manager.projects.get(project_id)
        if project is None:
            raise HTTPException(404, "no such project")
        sessions = await manager.projects.sessions_of(project_id)
        _refuse_busy(project, await _busy_in(project_id), "removing it")
        if sessions and not detach:
            one = len(sessions) == 1
            raise HTTPException(409, f"{len(sessions)} agent{'' if one else 's'} {'works' if one else 'work'} in {project.name}; removing it leaves them without its files (pass detach=1 to do it anyway)")
        await manager.projects.delete(project_id)
        await manager.reload_project(None, project_id)
        return {"ok": True, "detached": [s["id"] for s in sessions]}

    # -- sessions -------------------------------------------------------------------

    @api.get("/api/sessions")
    async def list_sessions(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        rows = await manager.list_sessions(limit=200)
        names = {p.id: p.name for p in await manager.projects.list()}
        default = app.config.default_preset()
        default_label = default[1].display(default[0]) if default else NO_MODEL_LABEL
        overrides_by_id = await manager.live.load_models([row["id"] for row in rows])
        for row in rows:
            # The directory the session works in, so the list can group sessions by workspace.
            workspace = Path(str(row["metadata"].get("workspace"))) if row["metadata"].get("workspace") else manager.workspace_for(row["id"])
            row["workspace"] = workspace.name
            row["workspace_own"] = workspace == manager.workspace_for(row["id"])
            row["project"] = names.get(row.get("project_id") or "")
            overrides = overrides_by_id.get(row["id"], {})
            if overrides.get("preset") and overrides["preset"] in app.config.presets:
                row["model"] = app.config.presets[overrides["preset"]].display(overrides["preset"])
            elif overrides.get("provider") and overrides.get("model_name"):
                row["model"] = f"{overrides['provider']}/{overrides['model_name']}"
            else:
                row["model"] = default_label
        return rows

    @api.post("/api/sessions")
    async def new_session(body: NewSessionBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        metadata: dict[str, Any] = {"tools_off": sorted(set(body.tools_off))} if body.tools_off else {}
        if body.project_id and body.workspace:
            raise HTTPException(400, "a session works in a project or in a workspace directory, not both")
        if body.project_id:
            project = await manager.projects.get(body.project_id)
            if project is None:
                raise HTTPException(404, "no such project")
            if not project.reachable:
                raise HTTPException(409, f"the folder of {project.name} ({project.root}) is not reachable from here yet; mount it and restart before starting an agent in it")
        if body.workspace:
            directory = _workspace_dir(body.workspace)
            if not directory.is_dir():
                raise HTTPException(404, f"no workspace named {body.workspace!r}")
            metadata["workspace"] = str(directory)
        try:
            state = await app.create_session(body.title, metadata=metadata or None, project_id=body.project_id or None)
        except TelegramBusy as exc:
            raise HTTPException(429, f"Telegram asks to wait {exc.retry_after}s before creating another topic (session {exc.session_id} exists without a topic)") from exc
        except TelegramRefused as exc:
            raise HTTPException(502, f"Telegram refused to create the topic: {exc}") from exc
        if body.preset:
            # Before the first run, so the session's opening task already goes to the chosen model.
            try:
                await manager.set_model(state.session.id, preset=body.preset)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if body.prompt:
            await manager.submit(state.session.id, body.prompt)
        if body.loop is not None:
            loops = app.extensions.get("loops")
            if loops is None:
                raise HTTPException(503, "loops are not installed")
            try:
                await loops.create(state.session.id, instruction=body.loop.instruction, mode=body.loop.mode, interval_seconds=(body.loop.interval_minutes or 0) * 60 or None, max_runs=body.loop.max_runs, start_now=body.loop.start_now)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        return {"id": state.session.id, "title": body.title, "model": await session_model_label(state)}

    async def session_model_label(state: Any) -> str:
        overrides = await manager.live.load(state.session.id)
        if overrides.get("preset") and overrides["preset"] in app.config.presets:
            return app.config.presets[overrides["preset"]].display(overrides["preset"])
        if overrides.get("provider") and overrides.get("model_name"):
            return f"{overrides['provider']}/{overrides['model_name']}"
        default = app.config.default_preset()
        return default[1].display(default[0]) if default else NO_MODEL_LABEL

    async def session_provider(state: Any) -> str:
        """The provider id the session's next call goes to (for the usage card beside the chat)."""
        overrides = await manager.live.load(state.session.id)
        if overrides.get("preset") and overrides["preset"] in app.config.presets:
            return app.config.presets[overrides["preset"]].provider
        if overrides.get("provider"):
            return str(overrides["provider"])
        default = app.config.default_preset()
        return default[1].provider if default else ""

    @api.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, tail: int = 600, before: int = 0, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One page of a session. ``tail`` is the newest N turns; ``before=<seq>`` the page older than one already shown.

        A page is all that is ever read: a session whose transcript is twenty thousand turns
        costs the same to open as one with fifty.
        """
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        tail = max(1, min(tail, MAX_TRANSCRIPT_PAGE))
        messages = await manager.transcript_page(session_id, tail=tail, before=before)
        oldest, _newest = await manager.sessions.transcript_bounds(session_id)
        first_seq = next((v["seq"] for v in messages if isinstance(v.get("seq"), int)), 0)
        # Compacting first: a compaction between runs happens on a task of its own, and a session
        # reported as running while it summarises is a run the app draws that nobody started.
        status = "compacting" if state.compacting else "running" if state.running else "waiting" if state.pending else "idle"
        usage = await app.db.fetchone(
            "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) ch, sum(cost_usd) usd FROM usage_events WHERE session_id = ?",
            (session_id,),
        )
        context = await manager.context_status(state)
        leader = await manager.get_state(str(state.metadata["subagent_of"])) if state.metadata.get("subagent_of") else None
        subagents = []
        for child in await app.extensions["subagents"].children(session_id) if "subagents" in app.extensions else []:
            child_state = await manager.get_state(child["session_id"])
            subagents.append({**child, "model": await session_model_label(child_state) if child_state else ""})
        return {
            "context": context,
            "id": session_id,
            "title": state.session.title,
            "status": status,
            "compacting": state.compacting,
            "run_id": state.run_id,
            "workspace": str(state.workspace),
            "workspace_name": state.workspace.name,
            "workspace_own": state.workspace == manager.workspace_for(session_id),
            "project": state.project.view() if state.project is not None else None,
            # Subagents share their leader's workspace by design; they are listed under Subagents (and the leader under
            # "leader:"), so the workspace list shows only the sessions that were attached to it.
            "workspace_sessions": [u for u in await manager.workspace_users(state.workspace) if u["id"] != session_id and u["id"] not in {c["session_id"] for c in subagents} and u["id"] != state.metadata.get("subagent_of")],
            "pending": state.pending.payload if state.pending else None,
            "model": await session_model_label(state),
            "provider": await session_provider(state),
            "mode": state.metadata.get("mode") or "",
            "usd_cap": state.metadata.get("usd_cap"),
            "brief": state.metadata.get("brief") or "",
            "spawned_by": state.metadata.get("spawned_by"),
            "tools_off": sorted(manager.tools_off(state)),
            "loop": state.metadata.get("loop"),
            "services": await app.extensions["services"].list(session_id) if "services" in app.extensions else [],
            "subagent_of": state.metadata.get("subagent_of"),
            "subagent_name": state.metadata.get("subagent_name"),
            "leader_title": leader.session.title if leader is not None else None,
            "subagents": subagents,
            "verifications": dict(await app.db.fetchone("SELECT count(*) total, sum(passed) passed FROM verifications WHERE session_id = ?", (session_id,)) or {}),
            "messages": messages,
            "first_seq": first_seq,
            "has_older": bool(first_seq and oldest and first_seq > oldest),
            "usage": dict(usage) if usage else {},
        }

    @api.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, after: int = 0, limit: int = 500, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        state = await manager.get_state(session_id)
        if state is None or state.run_id is None:
            return {"run_id": None, "events": [], "last_seq": after}
        rows = await manager.events.list_events(state.run_id, after_seq=after, limit=limit)
        return {
            "run_id": state.run_id,
            "events": [{"seq": seq, "type": e.name, "payload": e.payload} for seq, e in rows],
            "last_seq": rows[-1][0] if rows else after,
        }

    @api.get("/api/sessions/{session_id}/stream")
    async def session_stream(session_id: str, request: Request, _: dict[str, Any] = Depends(auth)) -> StreamingResponse:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")

        async def gen():  # type: ignore[no-untyped-def]
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

            async def sink(sid: str, event: Any) -> None:
                if sid == session_id:
                    queue.put_nowait((event.type.value, event.payload))

            manager.add_sink(sink)
            try:
                yield "event: hello\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        name, payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"
            finally:
                manager._sinks.remove(sink)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @api.get("/api/sessions/{session_id}/tool-results/{call_id}")
    async def tool_result(session_id: str, call_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The whole text of one tool result, for the listing's preview to expand."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        for message in await manager.sessions.messages_for_call(session_id, call_id):
            for block in message.content_blocks:
                if isinstance(block, ToolResultBlock) and block.tool_call_id == call_id:
                    return {"id": call_id, "content": redact.redact(block.content), "is_error": block.is_error, "length": len(block.content)}
        raise HTTPException(404, "no such tool result")

    @api.get("/api/sessions/{session_id}/sent/{call_id}/download")
    async def sent_file(session_id: str, call_id: str, _: dict[str, Any] = Depends(auth)) -> FileResponse:
        """The file a SendFile call handed over, by that call: the app attaches it under the answer, wherever the file lives."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        for message in await manager.sessions.messages_for_call(session_id, call_id):
            for block in message.content_blocks:
                if isinstance(block, ToolUseBlock) and block.tool_call_id == call_id:
                    if block.name != "SendFile":
                        raise HTTPException(404, "that call did not send a file")
                    try:
                        raw = json.loads(block.arguments_json or "{}").get("path")
                    except ValueError:
                        raw = None
                    if not isinstance(raw, str) or not raw.strip():
                        raise HTTPException(404, "the call named no file")
                    candidate = Path(raw).expanduser()
                    target = candidate if candidate.is_absolute() else state.workspace / candidate
                    # The path is read back out of the transcript, so it is checked again rather than
                    # trusted: a project whose folder has moved since the call must not serve the old one.
                    if state.services is not None and not state.services.contains(target):
                        raise HTTPException(403, "that file is outside this project")
                    if not target.is_file():
                        raise HTTPException(404, "the file is gone")
                    return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", filename=target.name, headers={"Access-Control-Allow-Origin": "https://web.telegram.org"})
        raise HTTPException(404, "no such call")

    @api.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: SendMessageBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            run_id = await manager.submit(session_id, body.text, steer=body.steer)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id}

    @api.post("/api/sessions/{session_id}/upload")
    async def upload(
        session_id: str,
        text: str = Form(""),
        files: list[UploadFile] = File(default=[]),
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        """Send a message with attachments (or attachments alone) from the Mini App."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        inbox = state.workspace / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        attachments: list[Attachment] = []
        for upload_file in files:
            name = Path(upload_file.filename or "file").name
            target = inbox / name
            counter = 1
            while target.exists():
                target = inbox / f"{Path(name).stem}-{counter}{Path(name).suffix}"
                counter += 1
            with target.open("wb") as fh:
                while chunk := await upload_file.read(1 << 20):
                    fh.write(chunk)
            attachments.append(Attachment(path=target, mime_type=upload_file.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"))
        if not text.strip() and not attachments:
            raise HTTPException(400, "nothing to send")
        body = text.strip() or ("Files attached." if len(attachments) > 1 else "File attached.")
        try:
            run_id = await manager.submit(session_id, body, attachments)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id, "files": [a.path.name for a in attachments]}

    @api.get("/api/asr")
    async def asr_status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Whether the site may offer a microphone: speech-to-text is configured and its endpoint resolves."""
        asr = app.config.asr
        ready = asr_configured(asr)
        reason = ""
        if ready:
            try:
                effective_asr(asr, manager)
            except TranscriptionError as exc:
                ready, reason = False, str(exc)
        local = app.speech.state()
        if local["active"]:
            # A local model answers before the endpoint is consulted, so "configured" must be true
            # even on an installation that has no endpoint at all — otherwise the site hides the
            # microphone from the one setup that needs nothing.
            ready, reason = True, ""
        return {
            "configured": ready,
            "reason": reason,
            "provider": asr.provider,
            "model": asr.model,
            "max_seconds": asr.max_seconds,
            "autosend": asr.autosend,
            "local": local,
        }

    @api.post("/api/sessions/{session_id}/transcribe")
    async def transcribe_audio(session_id: str, audio: UploadFile = File(...), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A recording from the site's microphone → its words, marked as a transcript, for the composer."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        if not recogniser_available(app.speech, app.config):
            raise HTTPException(409, "speech-to-text is not set up (Settings → Voice → Speech recognition)")
        suffix = Path(audio.filename or "").suffix or mimetypes.guess_extension((audio.content_type or "").split(";")[0]) or ".webm"
        inbox = state.workspace / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / f".recording-{secrets.token_hex(4)}{suffix}"
        size = 0
        with target.open("wb") as fh:
            while chunk := await audio.read(1 << 20):
                size += len(chunk)
                if size > 50 << 20:
                    fh.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(413, "recording is over 50 MB")
                fh.write(chunk)
        try:
            transcript = await transcribe_recording(app.speech, app.config, manager, target)
        except TranscriptionError as exc:
            raise HTTPException(502, str(exc)) from exc
        finally:
            target.unlink(missing_ok=True)
        return {"transcript": transcript, "text": voice_note_text(transcript), "autosend": app.config.asr.autosend}

    # -- voice: the concierge page ---------------------------------------------------

    def voice() -> Any:
        """The voice extension, or a plain refusal: the page is optional and can be switched off."""
        extension = app.extensions.get("voice")
        if extension is None:
            raise HTTPException(503, "the voice page is not installed")
        if not app.config.voice.enabled:
            raise HTTPException(503, "the voice page is switched off in the configuration")
        return extension

    @api.get("/api/voice")
    async def voice_status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What the page needs to decide how to listen and how to speak: the model, the endpoints, the agents."""
        extension = app.extensions.get("voice")
        if extension is None or not app.config.voice.enabled:
            return {"enabled": False, "session_id": "", "model": "", "tts": {"configured": False}, "stt": {"configured": False}, "agents": [], "listening": False}
        state = await extension.state()
        # Which recogniser the page should use is decided here rather than in the extension: the
        # extension knows about endpoints, and a local model is not one.
        local = app.speech.state()
        stt = dict(state.get("stt") or {})
        stt["local"] = local
        if local["active"]:
            stt["configured"] = True
            stt["reason"] = ""
            stt["model"] = local["label"]
        state["stt"] = stt
        return state

    @api.get("/api/voice/stream")
    async def voice_stream(request: Request, _: dict[str, Any] = Depends(auth)) -> StreamingResponse:
        """The concierge's half of the conversation: a status chip, the answer as it is written, a sentence at a time to speak."""
        extension = voice()

        async def gen():  # type: ignore[no-untyped-def]
            async with extension.listen() as queue:
                yield "event: hello\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        name, payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @api.post("/api/voice/say")
    async def voice_say(body: VoiceSayBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One utterance the browser already turned into words."""
        if len(body.text) > VOICE_SAY_MAX_CHARS:
            raise HTTPException(413, f"an utterance may be up to {VOICE_SAY_MAX_CHARS} characters")
        try:
            run_id = await voice().say(body.text)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id}

    @api.post("/api/voice/audio")
    async def voice_audio(audio: UploadFile = File(...), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One utterance as a recording, for a browser with no speech recognition of its own."""
        extension = voice()
        if not recogniser_available(app.speech, app.config):
            raise HTTPException(409, "speech-to-text is not set up (Settings → Voice → Speech recognition)")
        suffix = Path(audio.filename or "").suffix or mimetypes.guess_extension((audio.content_type or "").split(";")[0]) or ".webm"
        target = settings.state_dir / "tmp" / f"utterance-{secrets.token_hex(4)}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = 0
            with target.open("wb") as fh:
                while chunk := await audio.read(1 << 20):
                    size += len(chunk)
                    if size > VOICE_AUDIO_MAX:
                        raise HTTPException(413, f"an utterance may be up to {VOICE_AUDIO_MAX >> 20} MB")
                    fh.write(chunk)
            transcript = await transcribe_recording(app.speech, app.config, manager, target)
        except TranscriptionError as exc:
            raise HTTPException(502, str(exc)) from exc
        finally:
            # A client that aborts mid-upload leaves a part file behind unless the write is in here too.
            target.unlink(missing_ok=True)
        try:
            run_id = await extension.say(transcript)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"transcript": transcript, "run_id": run_id}

    @api.post("/api/voice/interrupt")
    async def voice_interrupt(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Barge-in: the operator talked over the answer, so the answer stops."""
        return {"stopped": await voice().interrupt()}

    @api.post("/api/voice/new")
    async def voice_new(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Start the conversation over; the previous one stays as a session of its own."""
        return {"session_id": await voice().new_session()}

    @api.post("/api/voice/tts")
    async def voice_tts(body: VoiceSpeakBody, _: dict[str, Any] = Depends(auth)) -> StreamingResponse:
        """One sentence read aloud by the configured endpoint; 404 tells the page to use the browser's own voice."""
        extension = voice()
        if len(body.text) > VOICE_TTS_MAX_CHARS:
            raise HTTPException(413, f"a sentence may be up to {VOICE_TTS_MAX_CHARS} characters")
        if not tts_configured(app.config.voice.tts):
            raise HTTPException(404, "no speech endpoint is configured; the browser speaks this one itself")
        if not body.text.strip():
            raise HTTPException(400, "nothing to say")
        try:
            chunks, media_type = await extension.speech(body.text.strip())
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"the speech endpoint could not be reached: {type(exc).__name__}") from exc
        return StreamingResponse(chunks, media_type=media_type)

    # -- local speech recognition ------------------------------------------------------------
    #
    # A model is chosen from the catalog, downloaded into the state directory and used in front of
    # everything else. The endpoints below are the whole of that: the list with what is installed,
    # a download that reports itself over SSE, a selection that is one line of configuration, and a
    # stream the voice page talks into so that a browser without its own recognition still sees words
    # appear while the sentence is being said.

    listening: dict[str, dict[str, Any]] = {}
    """Open streams by id. Small, short-lived, and pruned on every call that touches one."""

    def _prune_streams() -> None:
        """Let go of what nobody is feeding. Called on every listen request, not only on a new one.

        Reaping only when a new stream arrives is not a timeout: a page that opens four streams and
        is then closed leaves four decoders resident until somebody else starts listening, which on a
        single-operator installation may be never.
        """
        now = time.monotonic()
        stale = [sid for sid, entry in listening.items() if now - entry["seen"] > LISTEN_IDLE_SECONDS]
        while len(listening) - len(stale) > LISTEN_MAX_STREAMS:
            oldest = min((sid for sid in listening if sid not in stale), key=lambda sid: listening[sid]["seen"])
            stale.append(oldest)
        for sid in stale:
            listening.pop(sid, None)

    @api.get("/api/stt")
    async def stt_models(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The picker: every model, what is installed, what is downloading, and what it all costs."""
        view = speech_models.view(app.speech.downloads, selected=app.config.stt.local_model)
        view["language"] = app.config.stt.local_language
        view["threads"] = app.config.stt.local_threads
        view["engine_installed"] = speech_service.engine_present()
        view["decoders"] = speech_service.decoders()
        view["recommended"] = {code: model.id for code in ("en", "ru") if (model := speech_catalog.recommended(code))}
        return view

    engine_install = asyncio.Lock()
    """One install of the speech engine at a time; the second caller waits rather than racing."""

    @api.post("/api/stt/engine")
    async def stt_install_engine(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Install the engine that runs a downloaded model.

        In a container it is already there — the image carries it — and this answers so. Natively it
        is an optional extra the installation has not paid for yet, and the first download is when it
        starts being worth paying for, so the wheels are fetched into the installation's own
        environment. ``--inexact`` is what keeps that from removing whatever else was installed into
        it (the browser extra, typically).
        """
        if speech_service.engine_present():
            return {"installed": True, "message": "the speech engine is already installed"}
        if not settings.native:
            raise HTTPException(409, "the runtime image carries the speech engine; this one was built without it")
        uv = shutil.which("uv")
        if uv is None:
            raise HTTPException(503, "uv is not on the PATH, so the speech engine cannot be installed from here")
        # The picker calls this by itself before the first download, so two tabs or one impatient
        # double-click is the ordinary case rather than the adversarial one — and two `uv sync` runs
        # into the same virtualenv at once is how it ends up with a half-written site-packages.
        async with engine_install:
            if speech_service.engine_present():
                return {"installed": True, "message": "the speech engine is already installed"}
            process = await asyncio.create_subprocess_exec(
                uv, "sync", "--frozen", "--inexact", "--extra", "speech",
                cwd=str(settings.bot_repo_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _unused = await asyncio.wait_for(process.communicate(), timeout=ENGINE_INSTALL_TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise HTTPException(504, "installing the speech engine took too long and was stopped") from None
            if process.returncode:
                tail = out.decode(errors="replace").strip()[-400:]
                # The engine ships wheels and no sdist, so a platform without a prebuilt wheel fails
                # at resolution rather than building. That is not a broken installation and the
                # operator should not go looking for one.
                raise HTTPException(502, (
                    f"the speech engine could not be installed: {tail}\n\n"
                    "If this says no matching distribution, this platform has no prebuilt wheel for the "
                    "engine: local recognition is not available here, and a transcription endpoint is."
                ))
        speech_service.forget_engine()
        logger.warning("the local speech engine was installed on demand")
        return {"installed": True, "message": "the speech engine is installed; the model can be downloaded now"}

    @api.post("/api/stt/models/{model_id}/download")
    async def stt_download(model_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Fetch a model. Returns at once; the bar is fed by /api/stt/progress."""
        try:
            progress = app.speech.downloads.start(model_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"id": progress.id, "state": progress.state, "fraction": progress.fraction}

    @api.post("/api/stt/models/{model_id}/cancel")
    async def stt_cancel(model_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Stop a download. What has arrived is kept, so starting again continues from there."""
        try:
            return {"cancelled": app.speech.downloads.cancel(model_id)}
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.delete("/api/stt/models/{model_id}")
    async def stt_delete(model_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Remove an installed model, and unselect it if it was the one in use."""
        try:
            removed = await app.speech.downloads.delete(model_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if app.config.stt.local_model == model_id:
            await app.save_config(app.config.model_copy(update={"stt": app.config.stt.model_copy(update={"local_model": ""})}))
        app.speech.forget()
        return {"deleted": removed, **speech_models.view(app.speech.downloads, selected=app.config.stt.local_model)}

    @api.post("/api/stt/select")
    async def stt_select(body: SttSelectBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Use this model — or, with an empty id, go back to the endpoint and the browser."""
        patch: dict[str, Any] = {}
        if body.model is not None:
            if body.model and not app.speech.downloads.is_installed(body.model):
                raise HTTPException(409, f"{body.model} is not installed yet")
            patch["local_model"] = body.model
        if body.language is not None:
            patch["local_language"] = body.language.strip() or "auto"
        if body.threads is not None:
            patch["local_threads"] = body.threads
        if patch:
            await app.save_config(app.config.model_copy(update={"stt": app.config.stt.model_copy(update=patch)}))
        view = speech_models.view(app.speech.downloads, selected=app.config.stt.local_model)
        view["language"] = app.config.stt.local_language
        view["threads"] = app.config.stt.local_threads
        return view

    @api.get("/api/stt/progress")
    async def stt_progress(request: Request, _: dict[str, Any] = Depends(auth)) -> StreamingResponse:
        """Download progress as it happens, so the bar moves rather than being polled at."""

        async def gen():  # type: ignore[no-untyped-def]
            async with app.speech.downloads.watch() as queue:
                for current in app.speech.downloads.progress().values():
                    yield f"data: {json.dumps({'id': current.id, 'state': current.state, 'fraction': current.fraction, 'error': current.error})}\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        update = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    body = {"id": update.id, "state": update.state, "fraction": update.fraction, "error": update.error}
                    yield f"data: {json.dumps(body)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @api.post("/api/voice/listen/open")
    async def voice_listen_open(rate: int = SAMPLE_RATE, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Open a stream and get back the id every chunk of it must carry.

        The id is minted here rather than invented by the page. It is the only thing separating two
        listening sessions, and a page that picks its own with ``Math.random()`` can collide with
        another and interleave two people's audio into one transcript.

        The rate is the one the browser's own graph settled on, which is not always the one it was
        asked for: Safari and a number of Android webviews hand back the hardware's 44 100 or 48 000
        instead. It is declared once, here, because a stream that changed rate mid-sentence would have
        to be re-opened anyway — and because feeding 48 kHz samples to a model told they are 16 kHz
        makes it hear the sentence at three times its speed and answer with noise.
        """
        if not app.speech.available():
            raise HTTPException(409, "no local speech model is installed and selected (Settings → Voice)")
        _prune_streams()
        try:
            clamp_rate(rate)
            session = await app.speech.session()
        except SpeechError as exc:
            raise HTTPException(400 if "capture rate" in str(exc) else 503, str(exc)) from exc
        stream = secrets.token_urlsafe(16)
        listening[stream] = {"session": session, "seen": time.monotonic(), "rate": rate, "seq": 0}
        return {"stream": stream, "rate": rate}

    @api.post("/api/voice/listen")
    async def voice_listen(request: Request, stream: str, seq: int, final: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One chunk of mono PCM from the page, answered with the words heard so far.

        POSTed chunks rather than a socket: the app authenticates with a header that a browser cannot
        put on a WebSocket, and inside Telegram there is no cookie to fall back on either. A fifth of
        a second per request is well within what the loop and the model can keep up with — decoding
        runs tens of times faster than real time — and it makes the transport the same one every other
        call already uses.

        ``seq`` counts from one and must not skip. HTTP promises nothing about the order separate
        POSTs arrive in; the page serialises them, but a retry or a proxy that reorders would otherwise
        corrupt a transcript with nothing anywhere able to notice.

        A ``final`` answer is an utterance: the page hands it to the concierge exactly as it hands one
        the browser recognised itself.
        """
        if not app.speech.available():
            raise HTTPException(409, "no local speech model is installed and selected (Settings → Voice)")
        chunk = await request.body()
        if len(chunk) > LISTEN_CHUNK_MAX:
            raise HTTPException(413, f"a chunk may be up to {LISTEN_CHUNK_MAX >> 20} MB of samples")
        _prune_streams()
        entry = listening.get(stream)
        if entry is None:
            raise HTTPException(404, "this listening stream is not open; open one at /api/voice/listen/open")
        if seq != entry["seq"] + 1:
            listening.pop(stream, None)
            raise HTTPException(409, f"chunk {seq} arrived where {entry['seq'] + 1} was expected; the stream is closed")
        entry["seq"] = seq
        entry["seen"] = time.monotonic()
        try:
            partial = await entry["session"].feed(chunk, entry["rate"]) if chunk else None
            if final:
                partial = await entry["session"].finish()
                listening.pop(stream, None)
        except SpeechError as exc:
            listening.pop(stream, None)
            raise HTTPException(502, str(exc)) from exc
        if partial is None:
            return {"text": "", "final": False}
        return {"text": partial.text, "final": partial.final}

    @api.post("/api/voice/listen/close")
    async def voice_listen_close(stream: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The page stopped listening; let go of the stream without decoding what is left.

        For abandoning a stream, not for ending one: a page that wants the words it has not been given
        yet sends its last chunk with ``final=true``, which decodes the tail and pops the entry itself.
        """
        return {"closed": listening.pop(stream, None) is not None}

    @api.post("/api/sessions/{session_id}/answer")
    async def answer(session_id: str, body: AnswerBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            run_id = await manager.answer(session_id, body.answers)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id}

    @api.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, keep_workspace: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if app.front is not None:
            # Before the deletion: the topic to close and the private chat's pointer are both
            # read from rows that go with the session.
            await app.front.forget_session(session_id)
        return {"deleted": await manager.delete_session(session_id, delete_workspace=not keep_workspace)}

    @api.patch("/api/sessions/{session_id}")
    async def rename_session(session_id: str, body: RenameBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            if app.front is not None:
                await app.front.rename_session(session_id, body.title)
            else:
                await manager.rename_session(session_id, body.title)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": session_id, "title": body.title.strip()[:128]}

    @api.post("/api/sessions/{session_id}/revert")
    async def revert_session(session_id: str, body: RevertBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await manager.revert(session_id, body.seq)
        except KeyError:
            raise HTTPException(404, "no such session") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/sessions/{session_id}/fork")
    async def fork_session(session_id: str, body: ForkBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        source = await manager.get_state(session_id)
        if source is None:
            raise HTTPException(404, "no such session")
        title = (body.title or f"{re.sub(r'\s*\(fork @\d+\)$', '', source.session.title)} (fork @{body.seq})")[:128]
        try:
            # A fork of a project session stays in the project: the two sessions share the root, which
            # is what "several agents work in one project" already means, and the fork keeps the wall.
            target = await app.create_session(
                title,
                metadata={"forked_from": {"session_id": session_id, "seq": body.seq}},
                project_id=source.project.id if source.project is not None else None,
            )
        except TelegramBusy as exc:
            raise HTTPException(429, str(exc)) from exc
        except TelegramRefused as exc:
            raise HTTPException(502, str(exc)) from exc
        try:
            result = await manager.fork_into(session_id, body.seq, target)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": target.session.id, "title": title, **result}

    @api.post("/api/sessions/{session_id}/mode")
    async def set_mode(session_id: str, body: ModeBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"mode": await manager.set_mode(session_id, body.mode)}
        except KeyError:
            raise HTTPException(404, "no such session") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/sessions/{session_id}/cap")
    async def set_session_cap(session_id: str, body: SessionCapBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            cap = await manager.set_session_cap(session_id, body.usd_cap)
        except KeyError:
            raise HTTPException(404, "no such session") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        spent, _ = await manager.spend(session_id=session_id)
        return {"usd_cap": cap, "spent_usd": round(spent, 4)}

    @api.get("/api/tools")
    async def list_tools(_: dict[str, Any] = Depends(auth)) -> list[dict[str, str]]:
        """Every host tool with a one-line description and a group, for the tool checkboxes."""
        out = []
        for t in sorted(manager.tools.list_all(), key=lambda t: t.name):
            if t.name.startswith("Mcp_"):
                continue  # MCP tools come and go with their servers; they are switched per server
            desc = " ".join((t.definition.description or "").split())
            out.append({"name": t.name, "description": desc[:160], "group": _tool_group(t.name)})
        return out

    @api.get("/api/services")
    async def all_services(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """Every hosted service with the session it belongs to."""
        services = app.extensions.get("services")
        return await services.list_all() if services is not None else []

    @api.get("/api/sessions/{session_id}/services")
    async def session_services(session_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        services = app.extensions.get("services")
        return await services.list(session_id) if services is not None else []

    @api.post("/api/sessions/{session_id}/services/{name}/stop")
    async def session_service_stop(session_id: str, name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        services = app.extensions.get("services")
        if services is None:
            raise HTTPException(503, "services are not installed")
        try:
            return await services.stop(session_id, name, note="stopped by the operator")
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.delete("/api/sessions/{session_id}/services/{name}")
    async def session_service_remove(session_id: str, name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget a service: a running one is stopped first; its log file stays in the workspace."""
        services = app.extensions.get("services")
        if services is None:
            raise HTTPException(503, "services are not installed")
        if not await services.remove(session_id, name):
            raise HTTPException(404, f"no service {name!r} in this session")
        return {"removed": name}

    @api.get("/api/sessions/{session_id}/services/{name}/logs")
    async def session_service_logs(session_id: str, name: str, lines: int = 120, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        services = app.extensions.get("services")
        if services is None:
            raise HTTPException(503, "services are not installed")
        try:
            return {"text": await services.logs(session_id, name, lines)}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/api/sessions/{session_id}/services/{name}/share")
    async def session_service_share(session_id: str, name: str, body: ShareBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """local: LAN only · public: anyone through the site · key: whoever opens the link that carries the key."""
        services = app.extensions.get("services")
        if services is None:
            raise HTTPException(503, "services are not installed")
        try:
            return await services.share(session_id, name, body.mode, rotate_key=body.rotate_key)  # type: ignore[attr-defined]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # -- shared services: the site proxies /s/<slug>/… to the service's port --------------
    # The reverse proxy in front of the API already makes the site public; a shared service rides on the
    # same address instead of a port of its own. No auth dependency here: public mode is open to anyone,
    # key mode to whoever presents the key (once in the query, then in a cookie scoped to the slug).

    hop_headers = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host"}

    @api.get("/s/{slug}/robots.txt")
    async def shared_robots(slug: str) -> Any:
        """Crawlers that reach a shared service anyway are told to leave (the proxied pages say the same in a header)."""
        return PlainTextResponse("User-agent: *\nDisallow: /\n", headers={"X-Robots-Tag": "noindex, nofollow"})

    @api.get("/s/{slug}")
    async def shared_root(slug: str, request: Request) -> RedirectResponse:
        return RedirectResponse(f"/s/{slug}/" + (f"?{request.url.query}" if request.url.query else ""))

    @api.api_route("/s/{slug}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def shared_proxy(slug: str, path: str, request: Request) -> Any:
        services = app.extensions.get("services")
        row = await services.by_slug(slug) if services is not None else None  # type: ignore[attr-defined]
        if row is None or (row.get("share_mode") or "local") not in SHARE_MODES[1:]:
            raise HTTPException(404, "nothing is shared here")
        cookie_name = SHARE_COOKIE_PREFIX + slug
        presented = request.query_params.get("key")
        if row["share_mode"] == "key" and presented is not None:
            if not services.share_allows(row, presented):  # type: ignore[attr-defined]
                raise HTTPException(403, "wrong key")
            # The key moves from the address into a cookie, so the link people copy afterwards does not carry it.
            rest = [(k, v) for k, v in request.query_params.multi_items() if k != "key"]
            response: Any = RedirectResponse(f"/s/{slug}/{path}" + (f"?{urlencode(rest)}" if rest else ""), status_code=303)
            response.set_cookie(cookie_name, presented, httponly=True, samesite="lax", path=f"/s/{slug}", max_age=30 * 86400, secure=request.url.scheme == "https")
            return response
        if not services.share_allows(row, request.cookies.get(cookie_name) or request.headers.get("x-share-key")):  # type: ignore[attr-defined]
            raise HTTPException(403, "this service needs its key: open the link that carries ?key=…")
        if row["status"] != "running" or not pid_alive(row.get("pid")) or not row.get("port"):
            raise HTTPException(503, "the service is not running")
        upstream = f"http://127.0.0.1:{row['port']}/{path}" + (f"?{request.url.query}" if request.url.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in hop_headers}
        headers["x-forwarded-prefix"] = f"/s/{slug}"
        headers["x-forwarded-host"] = request.headers.get("host", "")
        headers["x-forwarded-proto"] = request.url.scheme
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
        try:
            up = await client.send(client.build_request(request.method, upstream, headers=headers, content=await request.body()), stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(502, f"the service did not answer: {exc}") from exc
        # Nothing under /s/ is for search engines: the address is the only secret of a public share.
        out: dict[str, str] = {"X-Robots-Tag": "noindex, nofollow, noarchive"}
        for k, v in up.headers.multi_items():
            lk = k.lower()
            if lk in hop_headers or lk == "x-robots-tag":
                continue
            if lk == "location" and v.startswith("/") and not v.startswith(f"/s/{slug}/"):
                v = f"/s/{slug}{v}"  # a redirect to the service's root stays under the slug
            out[k] = v
        # A page must not outlive the share in a browser cache: switching a service back to LAN-only takes
        # effect on the next reload, not when the cached copy expires.
        if up.headers.get("content-type", "").startswith("text/html"):
            out["Cache-Control"] = "no-store"

        async def body() -> Any:
            try:
                async for chunk in up.aiter_raw():
                    yield chunk
            finally:
                await up.aclose()
                await client.aclose()

        return StreamingResponse(body(), status_code=up.status_code, headers=out)

    @api.post("/api/sessions/{session_id}/loop")
    async def set_session_loop(session_id: str, body: LoopBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Attach (or replace) the session's loop."""
        loops = app.extensions.get("loops")
        if loops is None:
            raise HTTPException(503, "loops are not installed")
        try:
            return await loops.create(session_id, instruction=body.instruction, mode=body.mode, interval_seconds=(body.interval_minutes or 0) * 60 or None, max_runs=body.max_runs, start_now=body.start_now)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/sessions/{session_id}/loop/action")
    async def session_loop_action(session_id: str, body: LoopActionBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """pause | resume | stop | remove | run (an iteration now)."""
        loops = app.extensions.get("loops")
        if loops is None:
            raise HTTPException(503, "loops are not installed")
        try:
            if body.action == "pause":
                return await loops.pause(session_id, body.reason or "paused by the operator")
            if body.action == "resume":
                return await loops.resume(session_id)
            if body.action == "stop":
                return await loops.stop(session_id, body.reason or "stopped by the operator")
            if body.action == "remove":
                return {"removed": await loops.remove(session_id)}
            if body.action == "run":
                await loops.schedule_next(session_id, 0, "run now")
                return {"fired": await loops._fire_if_due(session_id)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        raise HTTPException(422, "action is pause, resume, stop, remove or run")

    @api.post("/api/sessions/{session_id}/tools")
    async def set_session_tools(session_id: str, body: ToolsOffBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"tools_off": await manager.set_tools_off(session_id, body.tools_off)}
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc

    @api.post("/api/sessions/{session_id}/brief")
    async def set_brief(session_id: str, body: BriefBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"brief": await manager.set_brief(session_id, body.brief)}
        except KeyError:
            raise HTTPException(404, "no such session") from None

    @api.get("/api/commands")
    async def list_commands(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """Only what this installation can run: a palette entry that answers with a refusal is a lie."""
        return [{"name": c.name, "args": c.args, "description": c.description, "scope": c.scope, "confirm": c.confirm} for c in slash.available(app)]

    @api.post("/api/sessions/{session_id}/command")
    async def run_slash_command(session_id: str, body: CommandBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Run a slash command for this session and return the text the chat would have shown."""
        try:
            text = await slash.run_command(app, session_id, body.line)
        except KeyError as exc:
            raise HTTPException(404, f"unknown command or session: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except TelegramBusy as exc:
            raise HTTPException(429, str(exc)) from exc
        return {"text": redact.redact(text)}

    @api.get("/api/limits/spend")
    async def limits_spend(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Priced spend per provider and in total since ``limits.total_since``, next to the caps."""
        limits = app.config.limits
        since = limits.total_since or None
        rows = await app.db.fetchall(
            "SELECT provider_id, sum(cost_usd) usd, sum(cost_usd IS NULL) unmetered FROM usage_events" + (" WHERE at >= ?" if since else "") + " GROUP BY provider_id",
            (since,) if since else (),
        )
        per_provider = {r["provider_id"]: {"spent_usd": round(float(r["usd"] or 0.0), 4), "unmetered": int(r["unmetered"] or 0), "cap_usd": float(limits.usd_total_per_provider.get(r["provider_id"], 0) or 0)} for r in rows}
        for pid, cap in limits.usd_total_per_provider.items():
            per_provider.setdefault(pid, {"spent_usd": 0.0, "unmetered": 0, "cap_usd": float(cap or 0)})
        total, unmetered = await manager.spend(since=since)
        return {"since": limits.total_since, "total": {"spent_usd": round(total, 4), "unmetered": unmetered, "cap_usd": limits.usd_total}, "per_provider": per_provider}

    @api.post("/api/limits/reset-total")
    async def limits_reset_total(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Start the total counters afresh from now; past usage stays recorded, it just no longer counts."""
        raw = app.config.model_dump(mode="json")
        raw.setdefault("limits", {})["total_since"] = datetime.now(UTC).isoformat()
        await app.save_config(type(app.config).model_validate(raw))
        return {"total_since": app.config.limits.total_since}

    @api.get("/api/modes")
    async def modes(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {name: m.model_dump() for name, m in app.config.modes.items()}

    # -- board ---------------------------------------------------------------------------------

    def _board():  # type: ignore[no-untyped-def]
        board = app.extensions.get("board")
        if board is None:
            raise HTTPException(503, "the board is not installed")
        return board

    @api.get("/api/board")
    async def board_list(include_done: int = 0, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await _board().list(None, include_done=bool(include_done))

    @api.post("/api/board")
    async def board_add(body: BoardTaskBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            if body.session_id and await manager.get_state(body.session_id) is None:
                raise HTTPException(404, "no such session")
            return await _board().add(title=body.title, acceptance=body.acceptance, checklist=body.checklist, depends_on=body.depends_on, priority=body.priority, notes=body.notes, session_id=body.session_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.put("/api/board/{task_id}")
    async def board_update(task_id: str, body: BoardUpdateBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await _board().update(task_id, status=body.status, note=body.note, check=body.check, uncheck=body.uncheck, priority=body.priority, title=body.title, acceptance=body.acceptance)
        except KeyError:
            raise HTTPException(404, "no such task") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.delete("/api/board/{task_id}")
    async def board_delete(task_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"deleted": await _board().delete(task_id)}

    @api.get("/api/peers")
    async def peers_list(_: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        peers = app.extensions.get("peers")
        return await peers.registry() if peers is not None else {}  # type: ignore[attr-defined]

    @api.post("/api/peers")
    async def peers_register(body: PeerBody, _: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        peers = app.extensions.get("peers")
        if peers is None:
            raise HTTPException(503, "peers are not installed")
        if await manager.get_state(body.session_id) is None:
            raise HTTPException(404, "no such session")
        try:
            await peers.register(body.name, body.session_id)  # type: ignore[attr-defined]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return await peers.registry()  # type: ignore[attr-defined]

    @api.delete("/api/peers/{name}")
    async def peers_forget(name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        peers = app.extensions.get("peers")
        if peers is None:
            raise HTTPException(503, "peers are not installed")
        return {"forgotten": await peers.forget(name.lower())}  # type: ignore[attr-defined]

    # -- inbound events ------------------------------------------------------------------

    @api.post("/api/inbound")
    async def inbound_post(body: InboundBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        inbound = app.extensions.get("inbound")
        if inbound is None:
            raise HTTPException(503, "inbound events are not installed")
        try:
            return await inbound.deliver(source=body.sender, text=body.text, session_ref=body.session, default_title="[inbound]", prompt=body.prompt)  # type: ignore[attr-defined]
        except TelegramBusy as exc:
            raise HTTPException(429, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/webhooks/{provider}")
    async def webhook(provider: str, request: Request) -> dict[str, Any]:
        # Every pre-verification refusal looks the same from outside: an anonymous caller learns nothing about what is configured.
        inbound = app.extensions.get("inbound")
        conf = app.config.webhooks.get(provider)
        if inbound is None or conf is None or not conf.enabled or not conf.secret:
            logger.warning("webhook %s refused: %s", provider, "not installed" if inbound is None else "unknown or disabled" if conf is None or not conf.enabled else "no secret configured")
            raise HTTPException(401, "refused")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > WEBHOOK_MAX_BYTES:
            raise HTTPException(413, "payload too large")
        raw = await request.body()
        if len(raw) > WEBHOOK_MAX_BYTES:
            raise HTTPException(413, "payload too large")
        if not verify_signature(conf.scheme, conf.secret, raw, {k.lower(): v for k, v in request.headers.items()}):
            raise HTTPException(401, "refused")
        stamped = request.headers.get("x-github-delivery") or request.headers.get("x-delivery-id")
        # Without a delivery id, identical bodies within the same minute are one event; later repeats are new ones.
        delivery_id = stamped or hashlib.sha256(raw).hexdigest() + ":" + datetime.now(UTC).strftime("%Y%m%d%H%M")
        if not await inbound.record_delivery(provider, delivery_id):  # type: ignore[attr-defined]
            return {"status": "duplicate", "delivery_id": delivery_id}
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {"raw": raw.decode("utf-8", "replace")[:PAYLOAD_MAX_CHARS]}
        event = request.headers.get("x-github-event") or request.headers.get("x-event") or ""
        text = (f"event: {event}\n" if event else "") + flatten_payload(payload)
        try:
            result = await inbound.deliver(source=f"webhook:{provider}", text=text, session_ref=conf.session or None, default_title=f"[webhook {provider}]", prompt=conf.prompt)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — the sender must get a status, and the failure goes to the inbox
            logger.warning("webhook %s could not run", provider, exc_info=True)
            await inbound.forget_delivery(provider, delivery_id)  # type: ignore[attr-defined]
            inbox = app.extensions.get("inbox")
            if inbox is not None:
                await inbox.post("webhook_failed", f"Webhook {provider} could not start a run", f"{type(exc).__name__}: {exc}", severity="warning")  # type: ignore[attr-defined]
            raise HTTPException(503, "accepted but could not start a run; see the inbox") from exc
        return {"status": "accepted", "delivery_id": delivery_id, **result}

    @api.get("/api/sessions/{session_id}/verifications")
    async def session_verifications(session_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        rows = await app.db.fetchall("SELECT id, run_id, criterion, command, exit_code, passed, output_digest, output_head, duration_ms, at, sandboxed, dependencies, tree, tests_run, tests_skipped, file_digests FROM verifications WHERE session_id = ? ORDER BY id DESC LIMIT 100", (session_id,))
        r = redact.shared()
        return [{**dict(row), "criterion": r.redact(row["criterion"]), "command": r.redact(row["command"]), "output_head": r.redact(row["output_head"] or "")} for row in rows]

    @api.post("/api/sessions/{session_id}/compact")
    async def compact_session(session_id: str, body: CompactBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            summary = await manager.compact(session_id, body.instructions)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"summary": summary}

    @api.post("/api/sessions/{session_id}/clear")
    async def clear_session(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Start over with an empty working history; the workspace, the brief and the settings stay."""
        try:
            return await manager.clear_history(session_id)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/api/sessions/{session_id}/export/download")
    async def session_export(session_id: str, _: dict[str, Any] = Depends(auth)) -> Response:
        """The whole session as one Markdown file: every turn, tool call and result, with the spend at the end."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        lines = [f"# {state.session.title}", "", f"Session {session_id}, exported {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}.", ""]
        for m in await manager.transcript(session_id):
            view = message_view(m)
            if view.get("internal"):
                continue
            who = {"user": "Operator", "assistant": "Agent", "tool": "Tool", "system": "System"}.get(m.role.value, m.role.value)
            if view.get("text"):
                lines += [f"## {who}", "", redact.redact(view["text"]), ""]
            for call in view.get("tool_calls", []):
                lines += [f"### → {call['name']}", "", "```json", json.dumps(call["arguments"], ensure_ascii=False, indent=1)[:4000], "```", ""]
            for result in view.get("tool_results", []):
                lines += [f"### ← result{' (error)' if result.get('is_error') else ''}", "", "```", result["content"][:4000], "```", ""]
        usage = await app.db.fetchone("SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) ch, sum(cost_usd) usd FROM usage_events WHERE session_id = ?", (session_id,))
        if usage and usage["c"]:
            lines += ["## Spend", "", f"{usage['c']} model calls · {int(usage['i'] or 0):,} in ({int(usage['ch'] or 0):,} cached) · {int(usage['o'] or 0):,} out · ${float(usage['usd'] or 0):.4f}", ""]
        body = "\n".join(lines)
        return Response(content=body, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="session-{session_id}.md"'})

    @api.post("/api/sessions/{session_id}/policy/grant")
    async def policy_grant(session_id: str, body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Let one refused call through: ``{"key": "<approval key from the refusal>"}``."""
        try:
            grants = await manager.grant(session_id, str(body.get("key") or ""))
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"grants": grants}

    @api.get("/api/sessions/{session_id}/egress")
    async def session_egress(session_id: str, limit: int = 200, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"items": await manager.egress(session_id, limit=limit)}

    @api.get("/api/sessions/{session_id}/tools/timing")
    async def session_tool_timing(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"items": await manager.tool_timing(session_id)}

    @api.get("/api/policy")
    async def policy_rules(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        policy = manager.policy()
        return {"rules": policy.describe(), "egress_allow": policy.egress_allow}

    @api.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"stopped": await manager.stop(session_id)}

    @api.post("/api/sessions/{session_id}/model")
    async def set_model(session_id: str, body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """``{"clear": true}`` returns the session to the global default; otherwise any of
        ``provider`` (a configured client id), ``model``, ``thinking``, ``reasoning_effort``."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        if body.get("context_window"):
            # Per-session window (in memory): smaller than the model's for cheap runs or tests.
            state.context_window = max(8_000, int(body["context_window"]))
        try:
            await manager.set_model(
                session_id,
                model_name=body.get("model"),
                provider=body.get("provider"),
                preset=body.get("preset") or None,
                thinking=body.get("thinking"),
                reasoning_effort=body.get("reasoning_effort"),
                clear=bool(body.get("clear")),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "model": await session_model_label(state)}

    # -- MCP per session --------------------------------------------------------------

    @api.get("/api/sessions/{session_id}/mcp")
    async def session_mcp(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        return {"enabled": manager.mcp_enabled(state), "servers": manager.mcp.status()}

    @api.put("/api/sessions/{session_id}/mcp")
    async def set_session_mcp(session_id: str, body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            enabled = await manager.set_mcp(session_id, str(body["server"]), bool(body.get("enabled", True)))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"enabled": enabled, "servers": manager.mcp.status()}

    # -- workspace files ------------------------------------------------------------

    def _safe_path(root: Path, rel: str) -> Path:
        target = (root / rel).resolve()
        if root.resolve() not in target.parents and target != root.resolve():
            raise HTTPException(400, "path escapes the workspace")
        if sealed_root(str(target), [str(p) for p in manager.protected_paths()]) is not None:
            # The containment above is what normally keeps this pane inside the operator's own work.
            # This is the same answer the tools get, asked again here: a root that ever comes to sit
            # over part of the installation must not open it through a browser either.
            raise HTTPException(403, "that path is part of the installation, not of its work")
        return target

    def _read_path(root: Path, path: str) -> dict[str, Any]:
        """A directory listing or a file's text at ``path`` under ``root`` (the sessions' and the workspaces' file panes share it)."""
        target = _safe_path(root, path)
        if target.is_file():
            if target.stat().st_size > 512_000:
                return {"path": path, "kind": "file", "truncated": True, "content": target.read_text(errors="replace")[:512_000]}
            try:
                return {"path": path, "kind": "file", "content": target.read_text(encoding="utf-8")}
            except UnicodeDecodeError:
                return {"path": path, "kind": "binary", "size": target.stat().st_size}
        if not target.is_dir():
            raise HTTPException(404, "no such path")
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name in {".git", "__pycache__", "node_modules", ".venv"}:
                continue
            stat = child.stat()
            entries.append({"name": child.name, "dir": child.is_dir(), "size": stat.st_size, "mtime": stat.st_mtime})
        return {"path": path, "kind": "dir", "entries": entries}

    def _file_response(root: Path, path: str) -> FileResponse:
        target = _safe_path(root, path)
        if not target.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(
            target,
            media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            filename=target.name,
            headers={"Access-Control-Allow-Origin": "https://web.telegram.org"},
        )

    async def _store_uploads(root: Path, files: list[UploadFile], sub: str = "") -> list[str]:
        """Write uploads under ``root/sub`` without clobbering what is there; returns the names used."""
        folder = _safe_path(root, sub) if sub else root
        folder.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        for upload_file in files:
            name = Path(upload_file.filename or "file").name
            target = folder / name
            counter = 1
            while target.exists():
                target = folder / f"{Path(name).stem}-{counter}{Path(name).suffix}"
                counter += 1
            with target.open("wb") as fh:
                while chunk := await upload_file.read(1 << 20):
                    fh.write(chunk)
            names.append(target.name)
        return names

    # -- workspaces: directories sessions work in; several sessions may share one ----------

    def _workspace_dir(name: str) -> Path:
        root = settings.workspaces_dir.resolve()
        target = (root / name).resolve()
        if target.parent != root or not name or name.startswith("."):
            raise HTTPException(400, "a workspace is a directory right under the workspaces root")
        return target

    def _dir_stats(path: Path) -> tuple[int, int, float]:
        files = size = 0
        newest = 0.0
        try:
            for f in path.rglob("*"):
                if f.is_file():
                    stat = f.stat()
                    files += 1
                    size += stat.st_size
                    newest = max(newest, stat.st_mtime)
        except OSError:
            pass
        return files, size, newest

    @api.get("/api/workspaces")
    async def list_workspaces(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """Every workspace directory with the sessions that work in it; a directory nobody uses is a spare."""
        root = settings.workspaces_dir
        users: dict[str, list[dict[str, str]]] = {}
        for row in await app.db.fetchall("SELECT id, title, metadata FROM sessions"):
            try:
                named = json.loads(row["metadata"] or "{}").get("workspace")
            except (ValueError, AttributeError):
                named = None
            path = Path(str(named)) if named else manager.workspace_for(row["id"])
            users.setdefault(str(path.resolve()), []).append({"id": row["id"], "title": row["title"]})
        scheduled = {str(Path(r["workspace"]).resolve()): r["name"] for r in await app.db.fetchall("SELECT name, workspace FROM schedules") if r["workspace"]}
        out = []
        if root.is_dir():
            for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                files, size, newest = await asyncio.to_thread(_dir_stats, entry)
                sessions = users.get(str(entry.resolve()), [])
                schedule = scheduled.get(str(entry.resolve()))
                kind = "schedule" if schedule else "heartbeat" if entry.name == "heartbeat" else "session" if any(u["id"] == entry.name for u in sessions) else "named"
                out.append({"name": entry.name, "path": str(entry), "sessions": sessions, "files": files, "size": size, "mtime": newest or entry.stat().st_mtime, "own_session": kind == "session", "kind": kind, "schedule": schedule})
        out.sort(key=lambda w: (-len(w["sessions"]), -(w["mtime"] or 0)))
        return out

    @api.post("/api/workspaces")
    async def create_workspace(body: WorkspaceBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        directory = _workspace_dir(body.name)
        if directory.exists():
            raise HTTPException(409, f"a workspace named {body.name!r} exists")
        (directory / "inbox").mkdir(parents=True)
        return {"name": directory.name, "path": str(directory)}

    @api.delete("/api/workspaces/{name}")
    async def delete_workspace(name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        directory = _workspace_dir(name)
        if not directory.is_dir():
            raise HTTPException(404, "no such workspace")
        users = await manager.workspace_users(directory)
        if users:
            raise HTTPException(409, f"{len(users)} session(s) work in it: {', '.join(u['title'] for u in users)[:200]}")
        for row in await app.db.fetchall("SELECT name, workspace FROM schedules"):
            if row["workspace"] and Path(row["workspace"]).resolve() == directory:
                raise HTTPException(409, f"the scheduled task {row['name']!r} runs in it")
        if directory.name == "heartbeat":
            raise HTTPException(409, "the heartbeat runs in it")
        await asyncio.to_thread(shutil.rmtree, directory, True)
        return {"deleted": True}

    @api.get("/api/workspaces/{name}/files")
    async def workspace_files(name: str, path: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        directory = _workspace_dir(name)
        if not directory.is_dir():
            raise HTTPException(404, "no such workspace")
        return _read_path(directory, path)

    @api.get("/api/workspaces/{name}/download")
    async def workspace_download(name: str, path: str, _: dict[str, Any] = Depends(auth)) -> FileResponse:
        directory = _workspace_dir(name)
        if not directory.is_dir():
            raise HTTPException(404, "no such workspace")
        return _file_response(directory, path)

    @api.post("/api/workspaces/{name}/upload")
    async def workspace_upload(name: str, path: str = Form(""), files: list[UploadFile] = File(default=[]), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Put files into a workspace (under ``path`` when given) without sending anything to an agent."""
        directory = _workspace_dir(name)
        if not directory.is_dir():
            raise HTTPException(404, "no such workspace")
        if not files:
            raise HTTPException(400, "no files")
        return {"files": await _store_uploads(directory, files, path)}

    @api.post("/api/sessions/{session_id}/files/upload")
    async def session_files_upload(session_id: str, path: str = Form(""), files: list[UploadFile] = File(default=[]), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Put files into the session's workspace without a message to the agent."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        if not files:
            raise HTTPException(400, "no files")
        return {"files": await _store_uploads(state.workspace, files, path)}

    @api.get("/api/sessions/{session_id}/files")
    async def list_files(session_id: str, path: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        return _read_path(state.workspace, path)

    @api.get("/api/sessions/{session_id}/download")
    async def download(session_id: str, path: str, _: dict[str, Any] = Depends(auth)) -> FileResponse:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        return _file_response(state.workspace, path)

    # -- memory: what the agent remembered, per session and globally ----------------------

    def _memory_view(record: Any) -> dict[str, Any]:
        return {
            "id": record.id,
            "scope": record.scope.value,
            "scope_key": record.scope_key,
            "kind": record.kind,
            "text": record.text,
            "salience": record.salience,
            "version": record.version,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "last_accessed_at": record.last_accessed_at.isoformat() if getattr(record, "last_accessed_at", None) else None,
        }

    @api.get("/api/memory")
    async def memory_list(scope: str = "", scope_key: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every memory record (or one scope's), with the titles of the sessions the session-scoped ones belong to."""
        try:
            wanted = MemoryScope(scope) if scope else None
        except ValueError as exc:
            raise HTTPException(422, f"unknown scope {scope!r}") from exc
        records = manager.memory.records(TENANT, scope=wanted, scope_key=scope_key or None)
        titles = {row["id"]: row["title"] for row in await app.db.fetchall("SELECT id, title FROM sessions")}
        buckets: dict[str, dict[str, Any]] = {}
        for rec in manager.memory.records(TENANT):
            key = f"{rec.scope.value}:{rec.scope_key}"
            b = buckets.setdefault(key, {"scope": rec.scope.value, "scope_key": rec.scope_key, "title": titles.get(rec.scope_key) if rec.scope is MemoryScope.session else None, "count": 0})
            b["count"] += 1
        return {"records": [_memory_view(r) for r in records], "buckets": sorted(buckets.values(), key=lambda b: (b["scope"] != "global", -b["count"])), "sessions": titles}

    @api.post("/api/memory")
    async def memory_add(body: MemoryBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            scope = MemoryScope(body.scope)
        except ValueError as exc:
            raise HTTPException(422, f"unknown scope {body.scope!r}") from exc
        if scope is not MemoryScope.global_ and not body.scope_key:
            raise HTTPException(422, "scope_key is required for a non-global scope")
        try:
            result = await manager.memory.write(TENANT, scope, body.scope_key, body.text.strip(), kind=body.kind.strip() or "fact", metadata={"source": "operator"})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"decision": result.decision.value if hasattr(result.decision, "value") else str(result.decision), "record": _memory_view(result.record)}

    @api.patch("/api/memory/{memory_id}")
    async def memory_edit(memory_id: str, body: MemoryPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return _memory_view(await manager.memory.update(TENANT, memory_id, text=body.text, kind=body.kind))
        except KeyError as exc:
            raise HTTPException(404, "no such memory") from exc

    @api.delete("/api/memory/{memory_id}")
    async def memory_delete(memory_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"deleted": await manager.memory.delete(TENANT, memory_id)}

    @api.post("/api/memory/delete")
    async def memory_delete_many(body: MemoryDeleteBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        deleted = 0
        for memory_id in body.ids:
            deleted += int(await manager.memory.delete(TENANT, memory_id))
        return {"deleted": deleted}

    # -- usage / balance / status ---------------------------------------------------

    @api.post("/api/usage/direct")
    async def record_direct_usage(body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A model call that bypassed the bot (curl against the key proxy) is booked like any other, at the provider's prices."""
        provider_id = str(body.get("provider_id") or "")
        model = str(body.get("model") or "")
        if not provider_id or not model:
            raise HTTPException(422, "provider_id and model are required")
        tokens = {k: int(body.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens")}
        cost: float | None = None
        try:
            pricing = manager.providers.get(provider_id).endpoint.pricing_for(model)
            cost = pricing.cost(tokens) if pricing is not None else None
        except KeyError:
            cost = None
        await manager.usage.record(
            UsageRecord(provider_id=provider_id, model=model, purpose="direct", raw=dict(body), normalized={**tokens, "cost_usd": cost}, cost_usd=cost, duration_ms=int(body.get("duration_ms") or 0), run_id=None, session_id=None)
        )
        return {"recorded": True, "cost_usd": cost}

    @api.get("/api/usage")
    async def usage(days: int = 7, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = await app.db.fetchall(
            "SELECT substr(at, 1, 10) day, provider_id, model, count(*) calls, sum(input_tokens) input_tokens,"
            " sum(output_tokens) output_tokens, sum(cache_read_tokens) cache_read_tokens,"
            " sum(reasoning_tokens) reasoning_tokens, sum(cost_usd) cost_usd, sum(cost_usd IS NULL) unmetered"
            " FROM usage_events WHERE at >= ? GROUP BY day, provider_id, model ORDER BY day DESC",
            (since,),
        )
        recent = await app.db.fetchall(
            "SELECT u.at, u.provider_id, u.model, u.purpose, u.session_id, u.run_id, u.input_tokens, u.output_tokens,"
            " u.cache_read_tokens, u.reasoning_tokens, u.cost_usd, u.duration_ms, u.raw, s.title session_title"
            " FROM usage_events u LEFT JOIN sessions s ON s.id = u.session_id ORDER BY u.seq DESC LIMIT 200"
        )
        by_session = await app.db.fetchall(
            "SELECT u.session_id, s.title, count(*) calls, sum(u.input_tokens) input_tokens, sum(u.output_tokens) output_tokens,"
            " sum(u.cost_usd) cost_usd, sum(u.cost_usd IS NULL) unmetered FROM usage_events u LEFT JOIN sessions s ON s.id = u.session_id"
            " WHERE u.at >= ? GROUP BY u.session_id ORDER BY cost_usd DESC NULLS LAST, calls DESC LIMIT 20",
            (since,),
        )
        return {
            "daily": [dict(r) for r in rows],
            "recent": [{**dict(r), "raw": json.loads(r["raw"])} for r in recent],
            "sessions": [dict(r) for r in by_session],
            "subscriptions": await subscription_usage(),
        }

    @api.get("/api/usage/provider/{provider_id}")
    async def usage_provider(provider_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One provider's day so far, its subscription windows when it is a login, its balance when it is metered."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        row = await app.db.fetchone(
            "SELECT count(*) calls, sum(input_tokens) input_tokens, sum(output_tokens) output_tokens, sum(cache_read_tokens) cache_read_tokens,"
            " sum(cost_usd) cost_usd, sum(cost_usd IS NULL) unmetered FROM usage_events WHERE provider_id = ? AND at >= ?",
            (provider_id, today),
        )
        monitor = app.extensions.get("balance")
        balances = await monitor.current() if monitor is not None else {}  # type: ignore[attr-defined]
        subscriptions = await subscription_usage()
        return {"provider": provider_id, "today": dict(row) if row else {}, "subscription": subscriptions.get(provider_id), "balance": balances.get(provider_id)}

    async def subscription_usage() -> dict[str, Any]:
        """Quota windows of the Codex, Grok and Claude subscriptions, read from the key proxy that holds their logins."""
        origin = _keyproxy_origin()
        if not origin:
            return {}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(origin + "/subscriptions/usage")
            return response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {}

    @api.get("/api/balance")
    async def balance(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        monitor = app.extensions.get("balance")
        current = await monitor.current() if monitor is not None else {}  # type: ignore[attr-defined]
        return {"balances": current, "thresholds": app.config.balance.thresholds_usd}

    @api.get("/api/status")
    async def status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        selfdev = app.extensions.get("selfdev")
        supervisor = await selfdev.supervisor_status() if selfdev is not None else None  # type: ignore[attr-defined]
        inbox = app.extensions.get("inbox")
        heartbeat = app.extensions.get("heartbeat")
        return {
            "model": app.config.model.model_dump(),
            "providers": list(manager.providers.available()),
            "supervisor": supervisor,
            "budget_exceeded": manager.budget_exceeded(),
            "sessions": await manager.list_sessions(limit=50),
            "inbox_unread": await inbox.unread_count() if inbox is not None else 0,  # type: ignore[attr-defined]
            "heartbeat": heartbeat.status() if heartbeat is not None else None,  # type: ignore[attr-defined]
        }

    def selfdev_on() -> None:
        """The proposals API answers only where changes are proposed at all.

        With the mode off the answer is 404, not an empty list: an empty list reads like "no changes
        yet" and the app would keep a navigation entry for a screen that can never fill.
        """
        if caps.selfdev.mode == "off":
            raise HTTPException(404, "self-development is off in this installation")

    @api.get("/api/capabilities")
    async def capability_report(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What this installation can do and why — the app hides what is not there instead of offering it.

        The mode was resolved once at startup; whether a change is waiting for a restart was not, and it is
        read here on every poll. It rides on this answer rather than on a route of its own because the app
        already asks this question and a banner that needs a second poll is a banner that arrives late.
        """
        selfdev = app.extensions.get("selfdev")
        pending = selfdev.pending_change() if selfdev is not None else None  # type: ignore[attr-defined]
        last = selfdev.last_change() if selfdev is not None else None  # type: ignore[attr-defined]
        return replace(caps, restart_required=pending, last_change=last).as_dict()

    @api.post("/api/self/restart")
    async def self_restart(_: dict[str, Any] = Depends(auth), __: None = Depends(selfdev_on)) -> dict[str, Any]:
        """Apply the change the agent committed: the supervisor checks it and restarts onto it.

        The answer comes back before the restart does — this very process is what goes away — so it says
        what was started, and the app learns how it ended from the capabilities it polls afterwards.
        """
        selfdev = app.extensions.get("selfdev")
        if selfdev is None:
            raise HTTPException(404, "self-development is off in this installation")
        return {"result": await selfdev.restart_to_apply("the operator asked the app to apply the change")}  # type: ignore[attr-defined]

    # -- doctor -------------------------------------------------------------------------

    def _doctor_context(fix: bool) -> DoctorContext:
        return DoctorContext(settings=settings, config=app.config, db=app.db, manager=manager, front=app.front, extensions=dict(app.extensions), guard=app.guard, fix=fix)

    @api.get("/api/doctor")
    async def doctor(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        checks = await run_checks(_doctor_context(False))
        return {"checks": [c.as_dict() for c in checks], "summary": summarize(checks)}

    @api.post("/api/doctor/fix")
    async def doctor_fix(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        checks = await run_checks(_doctor_context(True))
        return {"checks": [c.as_dict() for c in checks], "summary": summarize(checks)}

    # -- inbox --------------------------------------------------------------------------

    @api.get("/api/inbox")
    async def inbox_list(unread: int = 0, limit: int = 100, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        inbox = app.extensions.get("inbox")
        if inbox is None:
            return {"entries": [], "unread": 0}
        return {"entries": await inbox.list(limit=limit, unread_only=bool(unread)), "unread": await inbox.unread_count()}  # type: ignore[attr-defined]

    @api.get("/api/inbox/unread")
    async def inbox_unread(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        inbox = app.extensions.get("inbox")
        return {"unread": await inbox.unread_count() if inbox is not None else 0}  # type: ignore[attr-defined]

    @api.post("/api/inbox/read")
    async def inbox_read(body: InboxReadBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        inbox = app.extensions.get("inbox")
        if inbox is None:
            raise HTTPException(503, "inbox is not installed")
        return {"marked": await inbox.mark_read(body.ids), "unread": await inbox.unread_count()}  # type: ignore[attr-defined]

    @api.delete("/api/inbox/{entry_id}")
    async def inbox_delete(entry_id: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        inbox = app.extensions.get("inbox")
        if inbox is None:
            raise HTTPException(503, "inbox is not installed")
        await inbox.delete(entry_id)  # type: ignore[attr-defined]
        return {"deleted": entry_id}

    # -- heartbeat ------------------------------------------------------------------------

    @api.get("/api/heartbeat")
    async def heartbeat_get(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        heartbeat = app.extensions.get("heartbeat")
        if heartbeat is None:
            raise HTTPException(503, "heartbeat is not installed")
        return {**heartbeat.status(), "template": HEARTBEAT_TEMPLATE}  # type: ignore[attr-defined]

    @api.put("/api/heartbeat")
    async def heartbeat_put(body: HeartbeatBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        heartbeat = app.extensions.get("heartbeat")
        if heartbeat is None:
            raise HTTPException(503, "heartbeat is not installed")
        if body.text is not None:
            heartbeat.write(body.text)  # type: ignore[attr-defined]
        patch = {k: v for k, v in body.model_dump().items() if k != "text" and v is not None}
        if patch:
            if patch.get("preset") and patch["preset"] not in app.config.presets:
                raise HTTPException(400, f"no such model preset {patch['preset']!r}")
            try:
                heartbeat_config = HeartbeatConfig.model_validate({**app.config.heartbeat.model_dump(), **patch})
            except ValueError as exc:
                raise HTTPException(400, "; ".join(str(e.get("msg")) for e in getattr(exc, "errors", lambda: [])()) or "invalid heartbeat settings") from exc
            new_config = app.config.model_copy(update={"heartbeat": heartbeat_config})
            await app.save_config(new_config)
            if app.front is not None:
                app.front.config = new_config
        return heartbeat.status()  # type: ignore[attr-defined]

    @api.post("/api/heartbeat/run")
    async def heartbeat_run(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        heartbeat = app.extensions.get("heartbeat")
        if heartbeat is None:
            raise HTTPException(503, "heartbeat is not installed")
        try:
            return {"session_id": await heartbeat.fire(manual=True)}  # type: ignore[attr-defined]
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    # -- proposals ------------------------------------------------------------------

    @api.get("/api/proposals")
    async def proposals(_: dict[str, Any] = Depends(auth), __: None = Depends(selfdev_on)) -> list[dict[str, Any]]:
        rows = await app.db.fetchall("SELECT * FROM change_proposals ORDER BY created_at DESC LIMIT 100")
        return [dict(r) for r in rows]

    @api.get("/api/proposals/{proposal_id}/diff")
    async def proposal_diff(proposal_id: str, _: dict[str, Any] = Depends(auth), __: None = Depends(selfdev_on)) -> dict[str, Any]:
        row = await app.db.fetchone("SELECT * FROM change_proposals WHERE id = ?", (proposal_id,))
        if row is None:
            raise HTTPException(404, "no such proposal")
        selfdev = app.extensions.get("selfdev")
        if selfdev is None:
            raise HTTPException(503, "self-development is not installed")
        spec = selfdev.repo(row["repo"])  # type: ignore[attr-defined]
        try:
            diff = await selfdev.gh("pr", "diff", str(row["pr_number"]), cwd=spec.checkout)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not fetch the diff: {exc}") from exc
        return {"id": proposal_id, "diff": diff[:400_000]}

    @api.post("/api/proposals/{proposal_id}/decide")
    async def decide(proposal_id: str, body: DecisionBody, _: dict[str, Any] = Depends(auth), __: None = Depends(selfdev_on)) -> dict[str, Any]:
        selfdev = app.extensions.get("selfdev")
        if selfdev is None:
            raise HTTPException(503, "self-development is not installed")
        if body.decision not in ("approve", "reject"):
            raise HTTPException(400, "decision must be approve or reject")
        return {"result": await selfdev.decide(proposal_id, body.decision, reason=body.reason)}  # type: ignore[attr-defined]

    # -- schedules ------------------------------------------------------------------

    @api.get("/api/schedules")
    async def schedules(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        scheduler = app.extensions.get("scheduler")
        return await scheduler.list() if scheduler is not None else []  # type: ignore[attr-defined]

    @api.post("/api/schedules")
    async def create_schedule(body: ScheduleBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        scheduler = app.extensions.get("scheduler")
        if scheduler is None:
            raise HTTPException(503, "scheduler is not installed")
        try:
            return await scheduler.create(name=body.name, prompt=body.prompt, cron=body.cron, run_at=body.run_at, model=body.model, kind=body.kind, target_session=body.target_session, run_in=body.run_in)  # type: ignore[attr-defined]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.patch("/api/schedules/{schedule_id}")
    async def patch_schedule(schedule_id: str, body: SchedulePatchBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        scheduler = app.extensions.get("scheduler")
        if scheduler is None:
            raise HTTPException(503, "scheduler is not installed")
        fields = body.model_dump(exclude_unset=True)
        try:
            return await scheduler.update(schedule_id, **fields)  # type: ignore[attr-defined]
        except KeyError as exc:
            raise HTTPException(404, "no such schedule") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        scheduler = app.extensions.get("scheduler")
        if scheduler is None:
            raise HTTPException(503, "scheduler is not installed")
        return {"deleted": await scheduler.delete(schedule_id)}  # type: ignore[attr-defined]

    @api.post("/api/schedules/{schedule_id}/run")
    async def run_schedule(schedule_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        scheduler = app.extensions.get("scheduler")
        row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if scheduler is None or row is None:
            raise HTTPException(404, "no such schedule")
        return {"session_id": await scheduler.fire(dict(row))}  # type: ignore[attr-defined]

    # -- settings -------------------------------------------------------------------

    def _settings_view() -> dict[str, Any]:
        data = app.config.model_dump(mode="json")
        data["providers_available"] = list(manager.providers.available())
        data["usd_per_day"] = settings.usd_per_day
        data["prompt"]["default_rules"] = DEFAULT_RULES.strip()
        data["search_backends"] = websearch.catalogue()
        return mask_provider_keys(data)

    def _keyproxy_origin() -> str:
        for provider in app.config.providers.values():
            if "keyproxy" in provider.base_url:
                parts = provider.base_url.split("/", 3)
                return parts[0] + "//" + parts[2]
        return ""

    upstreams_cache: dict[str, Any] = {"at": 0.0, "value": None, "client": None}

    def keyproxy_client() -> httpx.AsyncClient:
        """One client for the health probe, kept for the life of the app rather than built per call."""
        client = upstreams_cache["client"]
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=5.0)
            upstreams_cache["client"] = client
        return client

    async def keyproxy_upstreams() -> list[str] | None:
        """Upstream names the key proxy holds a key for; None when it cannot be asked.

        Cached for a few seconds and asked through one client: this is on the path of the first
        screen, and a key proxy that hangs made every app load wait out the timeout again.
        """
        origin = _keyproxy_origin()
        if not origin:
            return None
        if upstreams_cache["at"] and time.monotonic() - float(upstreams_cache["at"]) < KEYPROXY_CACHE_SECONDS:
            return upstreams_cache["value"]  # type: ignore[return-value]
        try:
            response = await keyproxy_client().get(origin + "/healthz")
            names = response.json().get("upstreams") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
        value = [str(n) for n in names] if isinstance(names, list) else None
        upstreams_cache.update(at=time.monotonic(), value=value)
        return value

    @api.get("/api/onboarding")
    async def onboarding(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What this installation still needs before it can run anything.

        A fresh install has provider endpoints but no model — a provider is an address, not a
        choice of model — so the app opens Add a model instead of a chat that cannot answer.
        """
        usable = set(manager.providers.available())
        held = await keyproxy_upstreams()
        providers: list[dict[str, Any]] = []
        for pid, pc in app.config.providers.items():
            via_proxy = "keyproxy" in pc.base_url
            key_held = None if not via_proxy or held is None else pid in held
            providers.append(
                {
                    "id": pid,
                    "kind": pc.kind,
                    "base_url": pc.base_url,
                    "via_proxy": via_proxy,
                    "key_held": key_held,
                    "ready": pid in usable and key_held is not False,
                }
            )
        needs = [n for n, missing in (("provider_key", not any(p["ready"] for p in providers)), ("model", not app.config.has_model)) if missing]
        default = app.config.default_preset()
        return {
            "has_model": app.config.has_model,
            "presets": len(app.config.presets),
            "default_preset": default[0] if default else "",
            "providers": providers,
            "needs": needs,
            "message": "" if app.config.has_model else NO_MODEL_MESSAGE,
        }

    @api.get("/api/settings")
    async def get_settings(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        view = _settings_view()
        keyed = await keyproxy_upstreams()
        for entry in view["search_backends"]:
            entry["available"] = True if not entry["needs_key"] else (None if keyed is None else entry["id"] in keyed)
        return view

    @api.post("/api/settings/search-check")
    async def search_check(body: SearchCheckBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Run one query through a search backend and report what came back; the Mini App's "check" button."""
        if body.backend and body.backend not in websearch.BACKENDS:
            raise HTTPException(400, f"unknown backend {body.backend!r}")
        query = websearch.SearchQuery(text=body.query.strip() or "searxng json api", limit=5)
        outcome = await websearch.search(query, app.config.tools.web, chain=[body.backend] if body.backend else None)
        return {
            "backend": outcome.backend,
            "count": len(outcome.hits),
            "attempts": [{"backend": a.backend, "hits": a.hits, "error": a.error, "ms": a.ms} for a in outcome.attempts],
            "hits": [{"title": h.title, "url": h.url, "source": h.source, "published": h.published} for h in outcome.hits],
        }

    @api.put("/api/settings")
    async def put_settings(body: SettingsBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        current = app.config.model_dump(mode="json")
        dumped = body.model_dump(exclude_none=True)
        model_patch = dumped.pop("model", None)
        if isinstance(model_patch, dict):
            resolve_model_patch(current, model_patch)
        if isinstance(dumped.get("mcp"), dict):
            restore_masked_mcp(current, dumped["mcp"])
        restore_masked_secrets(current, dumped)
        for section in ("modes", "webhooks"):
            if isinstance(dumped.get(section), dict):
                current[section] = dumped.pop(section)  # whole-dict sections replace, so an entry can be removed
        for section, value in dumped.items():
            if isinstance(value, dict):
                current[section] = _deep_merge(current.get(section, {}), value)
            else:
                current[section] = value
        try:
            new_config = type(app.config).model_validate(current)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        switching_to_topics = app.front is not None and app.front.private_mode() and new_config.telegram.session_mode() == "topics"
        await app.save_config(new_config)
        await manager.providers.close_retired()
        if app.front is not None:
            app.front.config = new_config
            if switching_to_topics:
                # Sessions opened in the private chat have no topic; without one they would all
                # speak in General at once, with nothing saying which is which.
                await app.front.adopt_sessions_into_topics()
        return _settings_view()

    # -- provider endpoints ----------------------------------------------------------------

    async def _save_provider_config(new_config: RuntimeConfig) -> dict[str, Any]:
        await app.save_config(new_config)
        await manager.providers.close_retired()
        if app.front is not None:
            app.front.config = new_config
        return _settings_view()

    def _reference_check(config: RuntimeConfig, provider_id: str) -> str | None:
        users = [pid for pid, preset in config.presets.items() if preset.provider == provider_id]
        if users:
            return f"model preset(s) {', '.join(users)} use it"
        return None

    @api.put("/api/providers/{provider_id}")
    async def put_provider(provider_id: str, body: ProviderPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        patch = body.model_dump(exclude_unset=True)
        if not patch:
            return _settings_view()
        raw = app.config.model_dump(mode="json")
        apply_provider_patch(raw.setdefault("providers", {}), provider_id, patch)
        try:
            new_config = type(app.config).model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        if not new_config.providers[provider_id].base_url:
            raise HTTPException(400, "base_url is required for a provider endpoint")
        return await _save_provider_config(new_config)

    @api.delete("/api/providers/{provider_id}")
    async def delete_provider(provider_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if provider_id not in app.config.providers:
            raise HTTPException(404, "no such provider")
        reason = _reference_check(app.config, provider_id)
        if reason:
            raise HTTPException(400, f"cannot delete {provider_id!r}: {reason}. Point the model settings elsewhere first.")
        raw = app.config.model_dump(mode="json")
        del raw["providers"][provider_id]
        new_config = type(app.config).model_validate(raw)
        return await _save_provider_config(new_config)

    @api.post("/api/providers/lookup-models")
    async def lookup_provider_models(body: ModelsLookupBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """List the models served at an OpenAI-compatible endpoint (``{base}/models``).

        Probes from the bot (a Mini App in a browser or Telegram cannot reach LAN
        addresses), with an optional bearer key. ``base_url`` in the reply is the exact
        root the list was found at (``/v1`` appended when the caller omitted it), ``models``
        the ids, and ``entries`` the ids with the context window, modalities, reasoning
        support and prices the endpoint reported — what Add a model fills the form from.
        """
        base_url, api_key = body.base_url, body.api_key
        if body.provider:
            provider_config = app.config.providers.get(body.provider)
            if provider_config is None:
                raise HTTPException(404, f"no such provider {body.provider!r}")
            base_url = provider_config.base_url or (settings.vllm_base_url if provider_config.kind == "vllm" else "")
            api_key = provider_config.api_key or {
                "deepseek": settings.deepseek_api_key,
                "openrouter": settings.openrouter_api_key,
                "vllm": settings.vllm_api_key,
            }.get(provider_config.kind, "") or None
        if not base_url:
            raise HTTPException(400, "base_url is required (or a provider with one configured)")
        try:
            return await lookup_openai_models(base_url, api_key)
        except ValueError as exc:
            message = str(exc)
            status = 400 if message.startswith("base_url") else 502
            raise HTTPException(status, message) from exc

    # -- model presets -------------------------------------------------------------------

    @api.put("/api/presets/{preset_id}")
    async def put_preset(preset_id: str, body: PresetPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Create or edit a named model: client, model id, label, thinking, effort, images, window, output cap."""
        if not PRESET_ID_RE.fullmatch(preset_id):
            raise HTTPException(400, "preset id: letters, digits, . _ - (max 64)")
        raw = app.config.model_dump(mode="json")
        entry = dict(raw.setdefault("presets", {}).get(preset_id) or ModelPresetConfig().model_dump(mode="json"))
        for key, value in body.model_dump(exclude_unset=True).items():
            if value is not None:
                entry[key] = value.strip() if isinstance(value, str) else value
        if entry["provider"] not in raw.get("providers", {}):
            raise HTTPException(400, f"provider {entry['provider']!r} is not a configured client")
        if not entry["model"]:
            raise HTTPException(400, "a preset needs a model id")
        raw["presets"][preset_id] = entry
        model = raw.setdefault("model", {})
        if str(model.get("preset") or "") not in raw["presets"]:
            # The first model added is the one that runs: an install is not finished until one is.
            model["preset"] = preset_id
        vision = raw.setdefault("vision", {})
        if entry.get("images") and str(vision.get("preset") or "") not in raw["presets"]:
            vision["preset"] = preset_id
        try:
            new_config = type(app.config).model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        return await _save_provider_config(new_config)

    @api.delete("/api/presets/{preset_id}")
    async def delete_preset(preset_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if preset_id not in app.config.presets:
            raise HTTPException(404, "no such preset")
        last = len(app.config.presets) == 1
        if app.config.model.preset == preset_id and not last:
            raise HTTPException(400, "this preset is the global default; pick another default first")
        if app.config.vision.preset == preset_id and not last:
            raise HTTPException(400, "this preset is the ImageView model; pick another in Settings → Tools first")
        raw = app.config.model_dump(mode="json")
        del raw["presets"][preset_id]
        raw["model"]["chain"] = [c for c in raw["model"].get("chain", []) if c != preset_id]
        # Removing the last model is allowed: an installation with none is a state the app knows —
        # it asks for one — and refusing would leave a wrong entry no one can take out.
        for section in ("model", "vision", "voice"):
            if raw[section].get("preset") == preset_id:
                raw[section]["preset"] = ""
        return await _save_provider_config(type(app.config).model_validate(raw))

    # -- static mini app ------------------------------------------------------------

    dist = settings.bot_repo_dir / "miniapp" / "dist"
    if dist.is_dir():
        api.mount("/app", SpaFiles(directory=str(dist), html=True), name="miniapp")

        @api.get("/")
        async def root() -> RedirectResponse:
            return RedirectResponse("/app/")
    else:

        @api.get("/")
        async def root_missing() -> JSONResponse:
            return JSONResponse({"detail": "Mini App is not built; run `npm run build` in miniapp/"}, status_code=503)

    return api


async def install(app: Application) -> list[asyncio.Task[None]]:
    token = await app.db.kv_get("api_token")
    if not token:
        token = secrets.token_urlsafe(24)
        await app.db.kv_set("api_token", token)
    api = build_app(app, token)
    config = uvicorn.Config(api, host=app.settings.api_host, port=app.settings.api_port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    app.extensions["api_token"] = token
    base = app.settings.miniapp_public_url or f"http://127.0.0.1:{app.settings.api_port}"
    # An installation with no other way in gets one at start: a link in a file only the operator can
    # read (never in the log, which is copied around and which the agent's own tools can read).
    # Anything else — a bot, an enrolled passkey — is a way in already; a fresh link is one
    # `daedalus auth pair` away.
    if not app.settings.telegram_bot_token and await passkeys.count(app.db) == 0:
        await pairing.announce(app.db, app.settings.state_dir, base)
        logger.warning("pairing link written to %s (opens once; `daedalus auth pair` makes another)", app.settings.state_dir / pairing.URL_FILE)
    else:
        # A link a previous start left behind is at best expired and at worst a way in nobody asked for.
        (app.settings.state_dir / pairing.URL_FILE).unlink(missing_ok=True)
    if app.front is not None:

        async def cmd_app(message, command) -> None:  # type: ignore[no-untyped-def]
            await message.answer(f"Mini App: {base}/app/\nAPI token (for scripts): {token}")

        app.front.command_hooks["app"] = cmd_app

        async def cmd_doctor(message, command) -> None:  # type: ignore[no-untyped-def]
            fix = (command.args or "").strip().lower() == "fix"
            ctx = DoctorContext(settings=app.settings, config=app.config, db=app.db, manager=app.manager, front=app.front, extensions=dict(app.extensions), guard=app.guard, fix=fix)
            checks = await run_checks(ctx)
            outbox = TelegramOutbox(app.front.bot, message.chat.id, message.message_thread_id if message.is_topic_message else None)  # type: ignore[union-attr]
            for chunk in split_message(redact.redact(render_text(checks))):
                await outbox.send_text(chunk, markdown=False)

        app.front.command_hooks["doctor"] = cmd_doctor
    return [asyncio.create_task(server.serve(), name="api-server")]


__all__ = ["build_app", "install", "message_view", "validate_init_data"]
