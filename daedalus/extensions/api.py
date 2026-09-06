"""HTTP API for the Mini App (FastAPI, served in-process by uvicorn)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

import httpx
import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from pydantic import BaseModel

from daedalus.config import PROVIDER_KINDS, ProviderConfig

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.config import RuntimeConfig

logger = logging.getLogger(__name__)

INIT_DATA_MAX_AGE = 24 * 3600


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


class SendMessageBody(BaseModel):
    text: str
    steer: bool = False


class AnswerBody(BaseModel):
    answers: list[dict[str, Any]]


class NewSessionBody(BaseModel):
    title: str
    prompt: str | None = None


class DecisionBody(BaseModel):
    decision: str
    reason: str = ""


class ScheduleBody(BaseModel):
    name: str
    prompt: str
    cron: str | None = None
    run_at: str | None = None
    model: str | None = None


class RenameBody(BaseModel):
    title: str


class CompactBody(BaseModel):
    instructions: str = ""


class SettingsBody(BaseModel):
    model: dict[str, Any] | None = None
    prompt: dict[str, Any] | None = None
    vision: dict[str, Any] | None = None
    mcp: dict[str, Any] | None = None
    self_change: dict[str, Any] | None = None
    limits: dict[str, Any] | None = None
    """Only max_iterations and tool_timeout_seconds; the spend cap is the supervisor's."""
    balance: dict[str, Any] | None = None
    scheduler: dict[str, Any] | None = None
    telegram: dict[str, Any] | None = None
    answer_language: str | None = None


class ProviderPatch(BaseModel):
    """Partial edit of one configured provider endpoint (see ``apply_provider_patch``)."""

    kind: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    api_key: str | None = None
    """Omitted = keep the stored key; "" or null = clear it; any other value = store it."""
    supports_images: bool | None = None
    supports_thinking: bool | None = None
    timeout_seconds: float | None = None


class ModelsLookupBody(BaseModel):
    base_url: str
    api_key: str | None = None


async def lookup_openai_models(
    base_url: str,
    api_key: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """List the model ids served at an OpenAI-compatible ``/models`` endpoint.

    Tries ``{base}/models`` first, then ``{base}/v1/models``, so a base_url typed
    without the ``/v1`` prefix (``http://host:9000``) still resolves; the returned
    ``base_url`` is the exact root the list was found at. Tests inject an
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
        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=True)
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
            entries = data.get("data") if isinstance(data, dict) else None
            models = [str(m["id"]) for m in entries] if isinstance(entries, list) else None
            if not models:
                errors.append(f"{url}: no model list in the response")
                continue
            return {"base_url": url[: -len("/models")], "models": models}
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
    """Never echo stored keys back to the Mini App: replace with a ``api_key_set`` flag."""
    for entry in (settings_view.get("providers") or {}).values():
        entry["api_key_set"] = bool(entry.get("api_key"))
        entry["api_key"] = ""
    settings_view["provider_kinds"] = list(PROVIDER_KINDS)
    return settings_view


_SUMMARY_WRAP_RE = re.compile(r"</?compacted-turn[^>]*>")


def message_view(message: Message) -> dict[str, Any]:
    text: list[str] = []
    thinking: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for block in message.content_blocks:
        if isinstance(block, TextBlock):
            text.append(block.text)
        elif isinstance(block, ThinkingBlock):
            thinking.append(block.text)
        elif isinstance(block, ToolUseBlock):
            try:
                args = json.loads(block.arguments_json or "{}")
            except json.JSONDecodeError:
                args = {"raw": block.arguments_json}
            tool_calls.append({"id": block.tool_call_id, "name": block.name, "arguments": args})
        elif isinstance(block, ToolResultBlock):
            tool_results.append({"id": block.tool_call_id, "content": block.content[:4000], "is_error": block.is_error})
    compaction = message.metadata.get("daedalus.compaction") if isinstance(message.metadata, dict) else None
    is_summary = bool(message.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)) if isinstance(message.metadata, dict) else False
    body = "".join(text)
    if is_summary:
        body = _SUMMARY_WRAP_RE.sub("", body).strip()
        compaction = compaction or {"reason": "auto"}
    internal = message.role is MessageRole.user and body.lstrip().startswith("[internal control")
    return {
        "role": message.role.value,
        "summary": is_summary,
        "internal": internal,
        "compaction": compaction,
        "text": body,
        "thinking": "".join(thinking) or (message.reasoning_content or ""),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "created_at": message.created_at.isoformat(),
    }


def build_app(app: Application, api_token: str) -> FastAPI:
    api = FastAPI(title="Daedalus", docs_url=None, redoc_url=None)
    manager = app.manager
    assert manager is not None
    settings = app.settings

    async def auth(request: Request) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        if header.startswith("tma "):
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
        raise HTTPException(401, "authentication required")

    # -- sessions -------------------------------------------------------------------

    @api.get("/api/sessions")
    async def list_sessions(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await manager.list_sessions(limit=200)

    @api.post("/api/sessions")
    async def new_session(body: NewSessionBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        front = app.front
        if front is not None:
            state, _binding = await front.create_session_topic(body.title)
        else:
            state = await manager.create_session(body.title)
        if body.prompt:
            await manager.submit(state.session.id, body.prompt)
        return {"id": state.session.id, "title": body.title}

    async def session_model_label(state: Any) -> str:
        overrides = await manager.live.load(state.session.id)
        if overrides.get("provider"):
            provider = app.config.providers.get(overrides["provider"])
            model = overrides.get("model_name") or (provider.default_model if provider else "")
            return f"{overrides['provider']}/{model}" if model else overrides["provider"]
        if overrides.get("model_name"):
            return str(overrides["model_name"])
        if state.engine is not None and state.running:
            return str(state.engine.effective_model_name)
        return app.config.model.name

    @api.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, tail: int = 600, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        source = await manager.transcript(session_id, tail=tail)
        status = "running" if state.running else "waiting" if state.pending else "idle"
        usage = await app.db.fetchone(
            "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) ch, sum(cost_usd) usd FROM usage_events WHERE session_id = ?",
            (session_id,),
        )
        return {
            "id": session_id,
            "title": state.session.title,
            "status": status,
            "run_id": state.run_id,
            "workspace": str(state.workspace),
            "pending": state.pending.payload if state.pending else None,
            "model": await session_model_label(state),
            "messages": [message_view(m) for m in source],
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
        from daedalus.host.session_runner import Attachment

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

    @api.post("/api/sessions/{session_id}/answer")
    async def answer(session_id: str, body: AnswerBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            run_id = await manager.answer(session_id, body.answers)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id}

    @api.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, keep_workspace: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        binding = await app.front.binding_for_session(session_id) if app.front is not None else None
        if binding is not None and binding.thread_id and app.front is not None:
            try:
                await app.front.bot.delete_forum_topic(binding.chat_id, binding.thread_id)
            except Exception:  # noqa: BLE001
                pass
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

    @api.post("/api/sessions/{session_id}/compact")
    async def compact_session(session_id: str, body: CompactBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            summary = await manager.compact(session_id, body.instructions)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"summary": summary}

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
        try:
            await manager.set_model(
                session_id,
                model_name=body.get("model"),
                provider=body.get("provider"),
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
        return target

    @api.get("/api/sessions/{session_id}/files")
    async def list_files(session_id: str, path: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        target = _safe_path(state.workspace, path)
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

    @api.get("/api/sessions/{session_id}/download")
    async def download(session_id: str, path: str, _: dict[str, Any] = Depends(auth)) -> FileResponse:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        target = _safe_path(state.workspace, path)
        if not target.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(
            target,
            media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            filename=target.name,
            headers={"Access-Control-Allow-Origin": "https://web.telegram.org"},
        )

    # -- usage / balance / status ---------------------------------------------------

    @api.get("/api/usage")
    async def usage(days: int = 7, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = await app.db.fetchall(
            "SELECT substr(at, 1, 10) day, provider_id, model, count(*) calls, sum(input_tokens) input_tokens,"
            " sum(output_tokens) output_tokens, sum(cache_read_tokens) cache_read_tokens,"
            " sum(reasoning_tokens) reasoning_tokens, sum(cost_usd) cost_usd"
            " FROM usage_events WHERE at >= ? GROUP BY day, provider_id, model ORDER BY day DESC",
            (since,),
        )
        recent = await app.db.fetchall(
            "SELECT at, provider_id, model, purpose, session_id, run_id, input_tokens, output_tokens, cache_read_tokens,"
            " reasoning_tokens, cost_usd, duration_ms, raw FROM usage_events ORDER BY seq DESC LIMIT 100"
        )
        return {
            "daily": [dict(r) for r in rows],
            "recent": [{**dict(r), "raw": json.loads(r["raw"])} for r in recent],
        }

    @api.get("/api/balance")
    async def balance(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        monitor = app.extensions.get("balance")
        current = await monitor.current() if monitor is not None else {}  # type: ignore[attr-defined]
        return {"balances": current, "thresholds": app.config.balance.thresholds_usd}

    @api.get("/api/status")
    async def status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        selfdev = app.extensions.get("selfdev")
        supervisor = await selfdev.supervisor_status() if selfdev is not None else None  # type: ignore[attr-defined]
        return {
            "model": app.config.model.model_dump(),
            "providers": list(manager.providers.available()),
            "supervisor": supervisor,
            "budget_exceeded": manager.budget_exceeded(),
            "sessions": await manager.list_sessions(limit=50),
        }

    # -- proposals ------------------------------------------------------------------

    @api.get("/api/proposals")
    async def proposals(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        rows = await app.db.fetchall("SELECT * FROM change_proposals ORDER BY created_at DESC LIMIT 100")
        return [dict(r) for r in rows]

    @api.get("/api/proposals/{proposal_id}/diff")
    async def proposal_diff(proposal_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
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
    async def decide(proposal_id: str, body: DecisionBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
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
            return await scheduler.create(name=body.name, prompt=body.prompt, cron=body.cron, run_at=body.run_at, model=body.model)  # type: ignore[attr-defined]
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
        from daedalus.host.prompts import DEFAULT_RULES

        data = app.config.model_dump(mode="json")
        data["providers_available"] = list(manager.providers.available())
        data["usd_per_day"] = settings.usd_per_day
        data["prompt"]["default_rules"] = DEFAULT_RULES.strip()
        return mask_provider_keys(data)

    @api.get("/api/settings")
    async def get_settings(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return _settings_view()

    @api.put("/api/settings")
    async def put_settings(body: SettingsBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        current = app.config.model_dump(mode="json")
        for section, value in body.model_dump(exclude_none=True).items():
            if isinstance(value, dict):
                current[section] = {**current.get(section, {}), **value}
            else:
                current[section] = value
        try:
            new_config = type(app.config).model_validate(current)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        await app.save_config(new_config)
        await manager.providers.close_retired()
        if app.front is not None:
            app.front.config = new_config
        return _settings_view()

    # -- provider endpoints ----------------------------------------------------------------

    async def _save_provider_config(new_config: RuntimeConfig) -> dict[str, Any]:
        await app.save_config(new_config)
        await manager.providers.close_retired()
        if app.front is not None:
            app.front.config = new_config
        return _settings_view()

    def _reference_check(config: RuntimeConfig, provider_id: str) -> str | None:
        if config.model.provider == provider_id:
            return "it is the active model provider"
        if provider_id in config.model.chain:
            return "it is in the model fallback chain"
        if config.vision.provider == provider_id:
            return "it is the vision provider"
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
        """List model ids served at an OpenAI-compatible endpoint (``{base}/models``).

        Probes from the bot (a Mini App in a browser or Telegram cannot reach LAN
        addresses), with an optional bearer key. ``base_url`` in the reply is the exact
        root the list was found at (``/v1`` appended when the caller omitted it).
        """
        try:
            return await lookup_openai_models(body.base_url, body.api_key)
        except ValueError as exc:
            message = str(exc)
            status = 400 if message.startswith("base_url") else 502
            raise HTTPException(status, message) from exc

    # -- static mini app ------------------------------------------------------------

    dist = settings.bot_repo_dir / "miniapp" / "dist"
    if dist.is_dir():
        api.mount("/app", StaticFiles(directory=str(dist), html=True), name="miniapp")

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
    if app.front is not None:

        async def cmd_app(message, command) -> None:  # type: ignore[no-untyped-def]
            url = app.settings.miniapp_public_url or f"http://127.0.0.1:{app.settings.api_port}"
            await message.answer(f"Mini App: {url}/app/\nAPI token (for scripts): {token}")

        app.front.command_hooks["app"] = cmd_app
    return [asyncio.create_task(server.serve(), name="api-server")]


__all__ = ["build_app", "install", "message_view", "validate_init_data"]
