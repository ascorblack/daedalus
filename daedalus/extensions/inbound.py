"""Inbound events: a loopback inbox, authenticated webhooks, and standing intents.

Anything on this box can hand the agent a message (``POST /api/inbound``); GitHub, CI or
any service with a shared secret can post events (``POST /webhooks/<provider>``); and the
agent can register standing intents — "when an inbound event mentions X, do Y" — that
fire as runs with a cooldown, a fire budget and an expiry. Every inbound event is echoed
in the chat and recorded in the inbox, so no channel into the agent is invisible.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

PAYLOAD_MAX_CHARS = 2000
DELIVERIES_KEEP_DAYS = 14


def flatten_payload(value: Any, prefix: str = "", *, limit: int = PAYLOAD_MAX_CHARS) -> str:
    """``key.path: value`` lines from a JSON payload, capped so a webhook cannot flood the prompt."""
    lines: list[str] = []

    def walk(v: Any, path: str) -> None:
        if len("\n".join(lines)) > limit:
            return
        if isinstance(v, dict):
            for k, item in v.items():
                walk(item, f"{path}.{k}" if path else str(k))
        elif isinstance(v, list):
            for i, item in enumerate(v[:20]):
                walk(item, f"{path}[{i}]")
        else:
            text = str(v).replace("\n", " ")
            if text.strip():
                lines.append(f"{path}: {text[:300]}")

    walk(value, prefix)
    out = "\n".join(lines)
    return out[:limit] + ("\n…" if len(out) > limit else "")


def verify_signature(scheme: str, secret: str, body: bytes, headers: dict[str, str]) -> bool:
    if not secret:
        return False
    if scheme == "github":
        signature = headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    auth = headers.get("authorization", "")
    return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], secret)


class Inbound:
    def __init__(self, app: Application) -> None:
        self.app = app

    # -- delivery ---------------------------------------------------------------------

    async def resolve_session(self, ref: str | None, *, default_title: str) -> Any:
        """A session by id or exact title; otherwise the standing session with ``default_title`` (created once)."""
        manager = self.app.manager
        assert manager is not None
        if ref:
            state = await manager.get_state(ref)
            if state is not None:
                return state
            for s in await manager.list_sessions(limit=500):
                if s["title"] == ref:
                    return await manager.get_state(s["id"])
        for s in await manager.list_sessions(limit=500):
            if s["title"] == default_title:
                return await manager.get_state(s["id"])
        front = self.app.front
        metadata = {"unattended": True, "inbound": True}
        if front is not None:
            state, _ = await front.create_session_topic(default_title, metadata=metadata)
            return state
        return await manager.create_session(default_title, metadata=metadata)

    async def deliver(self, *, source: str, text: str, session_ref: str | None, default_title: str, prompt: str = "") -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        state = await self.resolve_session(session_ref, default_title=default_title)
        front = self.app.front
        if front is not None:
            outbox = await front.outbox_for_session(state.session.id)
            if outbox is not None:
                try:
                    await outbox.send_text(f"📨 **inbound from {source}**\n\n{text[:1500]}", markdown=True)
                except Exception:  # noqa: BLE001
                    logger.warning("inbound echo failed", exc_info=True)
        body = (prompt.strip() + "\n\n" if prompt.strip() else "") + f"[inbound event from {source}]\n{text}"
        run_id = await manager.submit(state.session.id, body, as_answer=False, origin=f"inbound:{source}")
        inbox = self.app.extensions.get("inbox")
        if inbox is not None:
            await inbox.post("inbound", f"Inbound from {source}", text[:2000], session_id=state.session.id, run_id=run_id or None)
        await self.match_intents(source, text)
        return {"session_id": state.session.id, "run_id": run_id}

    async def record_delivery(self, provider: str, delivery_id: str) -> bool:
        """True when the delivery is new; an INSERT OR IGNORE is the atomic dedupe."""
        async with self.app.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO webhook_deliveries(provider, delivery_id, at) VALUES (?, ?, ?)",
                (provider, delivery_id, datetime.now(UTC).isoformat()),
            )
            fresh = bool(cursor.rowcount)
            cutoff = (datetime.now(UTC) - timedelta(days=DELIVERIES_KEEP_DAYS)).isoformat()
            await conn.execute("DELETE FROM webhook_deliveries WHERE at < ?", (cutoff,))
        return fresh

    # -- intents ------------------------------------------------------------------------

    async def create_intent(self, *, pattern: str, action: str, session_id: str | None, cooldown_minutes: int, max_fires: int, expires_in_hours: int | None, created_by: str | None) -> dict[str, Any]:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"pattern is not a valid regular expression: {exc}") from exc
        intent_id = uuid.uuid4().hex[:8]
        expires = (datetime.now(UTC) + timedelta(hours=expires_in_hours)).isoformat() if expires_in_hours else None
        await self.app.db.execute(
            "INSERT INTO intents(id, pattern, action, session_id, cooldown_minutes, max_fires, expires_at, created_by_session, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (intent_id, pattern[:500], action[:4000], session_id, max(1, cooldown_minutes), max(1, max_fires), expires, created_by, datetime.now(UTC).isoformat()),
        )
        return {"id": intent_id, "pattern": pattern, "expires_at": expires}

    async def list_intents(self) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.app.db.fetchall("SELECT * FROM intents ORDER BY created_at DESC")]

    async def delete_intent(self, intent_id: str) -> bool:
        row = await self.app.db.fetchone("SELECT id FROM intents WHERE id = ?", (intent_id,))
        if row is None:
            return False
        await self.app.db.execute("DELETE FROM intents WHERE id = ?", (intent_id,))
        return True

    async def match_intents(self, source: str, text: str) -> list[str]:
        """Fire every enabled intent whose pattern matches the event text, within cooldown, budget and expiry."""
        now = datetime.now(UTC)
        fired: list[str] = []
        manager = self.app.manager
        if manager is None:
            return fired
        for row in await self.app.db.fetchall("SELECT * FROM intents WHERE enabled = 1"):
            if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < now:
                await self.app.db.execute("UPDATE intents SET enabled = 0 WHERE id = ?", (row["id"],))
                continue
            if int(row["fired_count"]) >= int(row["max_fires"]):
                await self.app.db.execute("UPDATE intents SET enabled = 0 WHERE id = ?", (row["id"],))
                await self._post("intent_exhausted", f"Standing intent '{row['pattern']}' used its fire budget", row["action"][:500], severity="notice")
                continue
            if row["last_fired_at"] and now - datetime.fromisoformat(row["last_fired_at"]) < timedelta(minutes=int(row["cooldown_minutes"])):
                continue
            try:
                if not re.search(row["pattern"], text, re.IGNORECASE):
                    continue
            except re.error:
                continue
            session = row["session_id"] or row["created_by_session"]
            state = await manager.get_state(session) if session else None
            prompt = f"[standing intent {row['id']} matched an inbound event from {source}: pattern {row['pattern']!r}]\n\n{row['action']}\n\nThe event:\n{text[:PAYLOAD_MAX_CHARS]}"
            try:
                if state is not None and not state.running and state.pending is None:
                    await manager.submit(state.session.id, prompt, as_answer=False, origin="intent")
                else:
                    scheduler = self.app.extensions.get("scheduler")
                    if scheduler is None:
                        continue
                    await scheduler.run_task_session(f"[intent] {row['pattern'][:30]}", prompt, self.app.settings.workspaces_dir / f"intent-{row['id']}", {"intent_id": row["id"], "unattended": True}, origin="intent")  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                await self._post("intent_failed", f"Standing intent '{row['pattern']}' could not run", f"{type(exc).__name__}: {exc}", severity="warning")
                continue
            await self.app.db.execute("UPDATE intents SET fired_count = fired_count + 1, last_fired_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
            fired.append(row["id"])
            await self._post("intent_fired", f"Standing intent fired: {row['pattern']}", row["action"][:500], severity="notice", session_id=session)
        return fired

    async def _post(self, kind: str, title: str, body: str = "", **kw: Any) -> None:
        inbox = self.app.extensions.get("inbox")
        if inbox is not None:
            await inbox.post(kind, title, body, **kw)

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "create":
            return await self.create_intent(**kwargs)
        if op == "list":
            return await self.list_intents()
        if op == "delete":
            return await self.delete_intent(kwargs["intent_id"])
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    inbound = Inbound(app)
    app.extensions["inbound"] = inbound
    assert app.manager is not None
    app.manager.service_hooks["intents"] = inbound.service
    front = app.front
    if front is not None:

        async def cmd_intents(message, command) -> None:  # type: ignore[no-untyped-def]
            parts = (command.args or "").split()
            if len(parts) == 2 and parts[0] == "delete":
                await message.answer("deleted" if await inbound.delete_intent(parts[1]) else "no such intent")
                return
            items = await inbound.list_intents()
            if not items:
                await message.answer("No standing intents. The agent creates them with IntentCreate ('when an inbound event mentions X, do Y').")
                return
            lines = [f"{'✓' if i['enabled'] else '✗'} {i['id']} /{i['pattern']}/ → {i['action'][:60]} · fired {i['fired_count']}/{i['max_fires']}" for i in items]
            await message.answer("\n".join(lines) + "\n\n/intents delete <id>")

        front.command_hooks["intents"] = cmd_intents
    return []


__all__ = ["Inbound", "flatten_payload", "install", "verify_signature"]
